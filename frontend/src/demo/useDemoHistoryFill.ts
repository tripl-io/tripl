import { useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { projectQueryOptions } from '@/lib/queryKeys'
import { invalidateForEvent } from '@/realtime/invalidationMap'
import type { Project } from '@/types'

/** How often a demo filling in its history asks how far it is. */
export const FILL_POLL_MS = 5_000

/** The least time between two chart refreshes while the fill advances. */
export const FILL_REFRESH_MIN_MS = 30_000

export interface DemoHistoryFill {
  /** The fill is running: the demo holds its last two days and is adding older ones. */
  filling: boolean
  /** The fill gave up; the demo keeps the days it already holds. */
  failed: boolean
  /** 0-100 through the fill while it runs; null when the server has not said. */
  progress: number | null
}

/**
 * Where a ready demo's history fill (``demo_history_status``) stands, read
 * live rather than from the `project` handed in.
 *
 * The shell's project usually comes from the project LIST, which nothing
 * refetches while the fill runs, so a reader of it would sit on "filling ·
 * 0%" until the fill ended. This reads the single-project query instead —
 * the key the shell, the banner and every project page already share — so
 * every caller sees the same answer and TanStack holds one cache entry for it.
 *
 * One caller polls (`poll`, the banner: it is mounted on every demo surface);
 * the rest only read (`poll: false`, the in-page notices). Two polling
 * observers of one key each run their own interval timer, out of phase, and
 * TanStack dedupes only a fetch already in flight — so a second poller would
 * double the requests rather than share them. A reader's query is disabled,
 * which still subscribes it to the entry the poller fills.
 *
 * The poller also refreshes the charts as the fill goes: the backend stores
 * the newest week first, so the warehouse gains older weeks while the demo is
 * open. The drilldown series are kept fresh by a 60 s adaptive poll at best,
 * and not at all while the realtime stream is connected (no stream event
 * announces a backfill), so without this the charts kept their two-day line
 * until the end-of-fill refetch. A fill job stores what a metric collection
 * stores, so it invalidates what `metric_collection.updated` does
 * (`invalidateForEvent`): the drilldown's series and its tabs (By version,
 * adoption, breakdowns, distribution drift) and the Overview volume card among
 * them. The end-of-fill refetch of everything stays with the banner, in one
 * place.
 *
 * Throttled: `progress` moves by whole percent per replay chunk, several
 * times a week's worth, so refetching on every change would reload every
 * chart on most 5 s polls. The first step after mount refreshes at once (the
 * page opened on two days); later steps at most once per
 * {@link FILL_REFRESH_MIN_MS}. A step skipped inside that interval is picked
 * up by the next one past it, and the last ones by the end-of-fill refetch.
 * It lives in the poller rather than in each notice because the poller is
 * mounted exactly once per page and is the first to see a new `progress`:
 * one refresh per interval, whatever number of notices the page shows.
 */
export function useDemoHistoryFill(
  project: Project | undefined,
  { poll = true }: { poll?: boolean } = {},
): DemoHistoryFill {
  const queryClient = useQueryClient()
  const slug = project?.slug
  const startedFilling = project?.demo_history_status === 'filling'
  const fillQuery = useQuery({
    ...projectQueryOptions(slug),
    enabled: startedFilling && poll,
    refetchInterval: startedFilling && poll ? FILL_POLL_MS : false,
  })
  // The live row wins once there is one; until then the shell's answer.
  const current = (startedFilling ? fillQuery.data : undefined) ?? project
  const status = current?.demo_history_status
  const filling = status === 'filling'
  const failed = status === 'failed'
  const progress = filling ? (current?.demo_history_progress ?? null) : null

  // Seeded with the progress the page opened on: the series fetched on mount
  // already hold those weeks, so only a later step refetches them.
  const seenProgress = useRef(progress)
  // When the charts were last refreshed; none yet, so the first step goes now.
  const refreshedAt = useRef<number | null>(null)
  useEffect(() => {
    if (!poll || !filling || !slug || progress === seenProgress.current) return
    const now = Date.now()
    if (refreshedAt.current !== null && now - refreshedAt.current < FILL_REFRESH_MIN_MS) return
    seenProgress.current = progress
    refreshedAt.current = now
    invalidateForEvent(queryClient, 'metric_collection.updated', slug)
  }, [poll, filling, progress, slug, queryClient])

  return { filling, failed, progress }
}
