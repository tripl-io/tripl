import { Loader2, RefreshCw, X, Zap } from 'lucide-react'
import { useRegisterInlineRail } from '@/components/activity-rail-store'
import { ActivityFeed } from '@/components/activity-feed'
import { useQuery } from '@tanstack/react-query'
import { activityApi } from '@/api/activity'
import { Dot } from '@/components/primitives/dot'
import { useAdaptiveRefetchInterval } from '@/realtime/streamContext'
import { countOf } from '@/lib/plural'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { activityKey, projectsQueryOptions } from '@/lib/queryKeys'

const ACTIVITY_LIMIT = 20

// The rail earns its full width only when it has something to show. An empty
// feed narrows to a slim strip so it stops reading as permanent empty chrome on
// brand-new / quiet projects.
const RAIL_WIDTH = 'w-[304px]'
const RAIL_WIDTH_QUIET = 'w-[220px]'

export function ActivityPanel({
  open,
  slug,
  inline = false,
  onClose,
}: {
  open: boolean
  slug?: string
  /** Rendered in the page's flow beside the content, not as the drawer. */
  inline?: boolean
  /**
   * Drawer mode: shows a Close button in the header. The drawer (every width
   * below 1600px) had only a refresh icon, and covers most of a phone.
   */
  onClose?: () => void
}) {
  useRegisterInlineRail(open && inline)

  // Adaptive fallback: the live stream refreshes the feed via the invalidation
  // map, so poll only while the stream is unavailable (and never on a hidden tab).
  const refetchInterval = useAdaptiveRefetchInterval({ activeMs: 60_000 })
  const activityQuery = useQuery({
    meta: SILENT_ERROR_META,
    queryKey: activityKey(slug),
    queryFn: () => activityApi.list({ slug, limit: ACTIVITY_LIMIT }),
    enabled: open,
    staleTime: 30_000,
    refetchInterval,
  })

  // The workspace feed mixes projects; name them as people do, from the list
  // the shell already holds (read-only: never fetched from here).
  const { data: projects } = useQuery({ ...projectsQueryOptions(), enabled: false })
  const projectName = (projectSlug: string) =>
    projects?.find((project) => project.slug === projectSlug)?.name ?? projectSlug

  if (!open) return null

  const items = activityQuery.data ?? []
  const isInitialLoading = activityQuery.isLoading && items.length === 0
  // A failed refresh keeps what was already loaded: one missed poll used to
  // replace a good feed with "Activity unavailable".
  const hasItems = items.length > 0
  // Quiet = loaded, healthy, and genuinely empty. Only then do we shrink the
  // rail and drop its footer so it stops dominating an empty project.
  const isQuiet = !isInitialLoading && !activityQuery.isError && items.length === 0

  return (
    <aside
      aria-label="Activity feed"
      className={`flex ${isQuiet ? RAIL_WIDTH_QUIET : RAIL_WIDTH} flex-shrink-0 flex-col border-l border-border bg-bg-sunken`}
    >
      <div
        className="flex h-11 items-center gap-2 border-b px-3.5 border-border"
      >
        <Dot tone={activityQuery.isError ? 'warning' : 'accent'} pulse={activityQuery.isFetching} size={7} />
        {/* "Activity", as the top-bar toggle says (#238). "Recent
            activity" is the Overview card's name. */}
        <span className="text-body-sm font-semibold">Activity</span>
        {!isQuiet && (
          <span className="text-caption text-fg-tertiary">
            {activityQuery.isError ? 'offline' : 'auto-refresh'}
          </span>
        )}
        <div className="flex-1" />
        {activityQuery.isFetching && (
          <Loader2 className="size-3.5 animate-spin text-fg-tertiary" />
        )}
        <button
          type="button"
          onClick={() => {
            void activityQuery.refetch()
          }}
          className="p-1 text-fg-tertiary"
          aria-label="Refresh activity"
        >
          <RefreshCw className="size-3.5" aria-hidden="true" />
        </button>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="flex size-8 items-center justify-center rounded-md transition-colors hover:bg-[var(--surface-hover)] text-fg-secondary"
            aria-label="Close activity"
          >
            <X className="size-4" aria-hidden="true" />
          </button>
        )}
      </div>
      <div className="flex-1 overflow-y-auto py-2">
        {isInitialLoading && <ActivitySkeleton />}
        {activityQuery.isError && hasItems && (
          <div
            role="status"
            className="mx-3.5 mb-2 flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-caption bg-surface border-border-subtle text-fg-tertiary"
          >
            <span className="flex-1">Could not refresh; showing the last loaded items.</span>
            <button
              type="button"
              onClick={() => {
                void activityQuery.refetch()
              }}
              className="inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 transition-colors hover:bg-[var(--surface-hover)] text-fg"
            >
              <RefreshCw className="h-3 w-3" aria-hidden="true" />
              Retry
            </button>
          </div>
        )}
        {activityQuery.isError && !isInitialLoading && !hasItems && (
          <div className="px-3.5 py-3">
            <div
              className="rounded-md border p-3 text-caption bg-surface border-border-subtle text-fg-tertiary"
            >
              <div className="font-medium text-fg">
                Activity unavailable
              </div>
              <div className="mt-1 leading-[1.35]">
                The feed could not be loaded from the backend.
              </div>
              <button
                type="button"
                onClick={() => {
                  void activityQuery.refetch()
                }}
                className="mt-2 inline-flex items-center gap-1.5 rounded-sm px-2 py-1 text-caption transition-colors hover:bg-[var(--surface-hover)] text-fg"
              >
                <RefreshCw className="h-3 w-3" />
                Retry
              </button>
            </div>
          </div>
        )}
        {isQuiet && (
          <div className="px-3.5 py-6 text-center text-caption text-fg-tertiary">
            No recent activity
          </div>
        )}
        {!isInitialLoading && (
          <ActivityFeed items={items} projectName={slug ? undefined : projectName} />
        )}
      </div>
      {!isQuiet && (
        <div
          className="flex items-center gap-2 border-t px-3 py-2.5 text-caption border-border text-fg-tertiary"
        >
          <Zap className="h-3 w-3" />
          <span>
            last 7 days · {countOf(items.length, 'item', 'items')}
          </span>
        </div>
      )}
    </aside>
  )
}

function ActivitySkeleton() {
  return (
    <div className="space-y-1 py-1">
      {[0, 1, 2, 3, 4].map((item) => (
        <div key={item} className="flex gap-2.5 px-3.5 py-[9px]">
          <div className="h-[22px] w-[22px] rounded-sm bg-[var(--surface)]" />
          <div className="min-w-0 flex-1 space-y-1.5">
            <div className="h-3 w-4/5 rounded-sm bg-[var(--surface)]" />
            <div className="h-2.5 w-3/5 rounded-sm bg-[var(--surface)]" />
            <div className="h-2 w-16 rounded-sm bg-[var(--surface)]" />
          </div>
        </div>
      ))}
    </div>
  )
}
