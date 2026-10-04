import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ApiError, AUTH_SIGNED_OUT_EVENT, AUTH_UNAUTHORIZED_EVENT } from '@/api/client'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import type { MetricDefinitionDetailResponse } from '@/types'
import { metricCollectWatchKey } from '@/lib/queryKeys'

/** How often to re-check the watched metric's persisted collection status. */
const DEFAULT_POLL_INTERVAL_MS = 3000
/**
 * Stop watching after this long. The Celery task budget is far larger (30 min
 * soft limit), so a long run is not an error — the watcher bows out with an
 * informational toast instead of spinning indefinitely.
 */
const WATCH_TIMEOUT_MS = 5 * 60_000
/**
 * Consecutive failed polls (5xx, network) after which the watch gives up. A
 * failing poll used to error the query, which kept refetching on the interval
 * — a global error toast every 3 s and a collect spinner that never stopped.
 */
const MAX_POLL_FAILURES = 3

/** `MetricDefinition.last_collection_status` markers stamped by the backend. */
const STATUS_RUNNING = 'running'
const STATUS_ERROR = 'error'

/**
 * Toast how a watch ended and return the terminal run status, or `null` when
 * the watch was abandoned before the run reported one. Shared by the
 * component-scoped hook and the detached watch so both say the same thing.
 */
function reportOutcome(
  displayName: string,
  outcome: MetricDefinitionDetailResponse | WatchAbandoned,
): 'success' | 'error' | null {
  if (outcome === 'timeout') {
    // The run may legitimately still be going.
    toast.info(`"${displayName}" is still collecting — the chart will update when it finishes.`)
    return null
  }
  if (outcome === 'missing') {
    toast.error(`"${displayName}" no longer exists — it was deleted while collecting.`)
    return null
  }
  if (outcome === 'unreachable') {
    toast.info(
      `Lost track of "${displayName}" — the server stopped answering. Reload the page to see whether it finished.`,
    )
    return null
  }
  if (outcome.last_collection_status === STATUS_ERROR) {
    toast.error(
      outcome.last_collection_error
        ? `Collection failed: ${outcome.last_collection_error}`
        : 'Collection failed.',
    )
    return 'error'
  }
  toast.success(`"${displayName}" collected — the chart is up to date.`)
  return 'success'
}

export interface MetricWatchRequest<TContext> {
  /**
   * The project the collect was fired against. Captured with the watch rather
   * than read live from the route: otherwise navigating to another project
   * mid-watch repointed the poll at `project-B/metric-A`, a metric that does not
   * exist there.
   */
  slug: string
  metricId: string
  displayName: string
  /**
   * Caller-supplied ids/state captured at collect-start (e.g. the route's
   * scope/scopeId). Threaded back to `onSettled` on completion so the settle
   * always acts on the metric it was actually collecting — never whatever the
   * page has since navigated to mid-watch.
   */
  context?: TContext
}

interface WatchTarget<TContext> extends MetricWatchRequest<TContext> {
  startedAt: number
}

/** How a watch ended without the run reaching a terminal status. */
type WatchAbandoned = 'timeout' | 'missing' | 'unreachable'

export interface MetricCollectionWatcherOptions {
  /** Poll cadence override — tests only. */
  pollIntervalMs?: number
}

export interface MetricCollectionWatcher<TContext = void> {
  /**
   * Start watching a metric whose manual collect was just accepted (202). The
   * whole request — project slug included — is captured now and handed back
   * verbatim to `onSettled`.
   */
  watch: (request: MetricWatchRequest<TContext>) => void
  /** True while a watched collection is still running. */
  isWatching: boolean
  /**
   * The metric currently being watched, or `null`. Callers key their own
   * "collecting" UI to this so an in-flight watch on metric A does not render as
   * "collecting" after the page navigates to metric B.
   */
  watchingMetricId: string | null
}

/**
 * Watches a manually-triggered metric collection until it reaches a terminal
 * state and reports the outcome as a toast.
 *
 * `POST /metrics/{id}/collect` stamps `last_collection_status="running"` before
 * it queues the Celery task, and the worker stamps `success` / `error` (plus
 * `last_collection_error`) when the run settles — the definition itself is the
 * queryable run status, so no extra job model is needed. This hook polls the
 * definition until the status leaves `running`, toasts "collected" or the
 * persisted failure reason, and calls `onSettled` so the caller can refresh its
 * series / list queries.
 */
export function useMetricCollectionWatcher<TContext = void>(
  onSettled?: (
    metricId: string,
    status: 'success' | 'error',
    context: TContext | undefined,
  ) => void,
  options?: MetricCollectionWatcherOptions,
): MetricCollectionWatcher<TContext> {
  const [target, setTarget] = useState<WatchTarget<TContext> | null>(null)

  // Latest-callback ref so callers can pass inline closures without the poll
  // resubscribing on every render.
  const onSettledRef = useRef(onSettled)
  useEffect(() => {
    onSettledRef.current = onSettled
  }, [onSettled])

  // Guards double-reporting if a poll resolves right as the watch is torn down.
  const reportedRef = useRef<number | null>(null)

  // Consecutive failed polls for the current watch (reset by a good poll).
  const failuresRef = useRef(0)

  const settle = (
    watched: WatchTarget<TContext>,
    outcome: MetricDefinitionDetailResponse | WatchAbandoned,
  ): void => {
    if (reportedRef.current === watched.startedAt) return
    reportedRef.current = watched.startedAt
    setTarget(null)
    const status = reportOutcome(watched.displayName, outcome)
    if (status) onSettledRef.current?.(watched.metricId, status, watched.context)
  }

  useQuery({
    // `startedAt` keys each watch to a fresh cache entry so a previous run's
    // terminal status never short-circuits a new watch with stale data. The slug
    // comes from the target, not the route, so the poll follows the collect it
    // started rather than the page the user has since walked to.
    queryKey: metricCollectWatchKey(target?.slug, target?.metricId, target?.startedAt),
    enabled: Boolean(target),
    refetchInterval: options?.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS,
    // The one-off watch entry is useless once the run settles.
    gcTime: 0,
    retry: false,
    // Terminal-state detection lives in the poll itself (an async callback, not
    // an effect): each fetch inspects the persisted status and settles the watch
    // as soon as it leaves "running".
    //
    // The poll never throws: every failure is counted and settled here, so no
    // error reaches the query (and its global toast) on each interval.
    queryFn: async () => {
      if (!target) return null
      // Checked before fetching, so the watch ends on time even while every
      // poll is failing.
      if (Date.now() - target.startedAt >= WATCH_TIMEOUT_MS) {
        settle(target, 'timeout')
        return null
      }
      let definition: MetricDefinitionDetailResponse
      try {
        definition = await metricsCatalogApi.get(target.slug, target.metricId)
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          settle(target, 'missing')
        } else {
          failuresRef.current += 1
          if (failuresRef.current >= MAX_POLL_FAILURES) settle(target, 'unreachable')
        }
        return null
      }
      failuresRef.current = 0
      const status = definition.last_collection_status
      if (status === STATUS_RUNNING || status === null) return definition
      settle(target, definition)
      return definition
    },
  })

  return {
    watch: (request) => {
      failuresRef.current = 0
      setTarget({ ...request, startedAt: Date.now() })
    },
    isWatching: target !== null,
    watchingMetricId: target?.metricId ?? null,
  }
}

// ---------------------------------------------------------------------------
// Detached watches
// ---------------------------------------------------------------------------

/*
 * The hook above lives and dies with the component that called it. That is
 * right for a detail page whose spinner is the watch, and wrong for a list row:
 * the catalog unmounts a row on every search keystroke, filter change or page
 * leave, which silently dropped the "you will be notified" promise (MET-8).
 * A detached watch is owned by this module instead, so it outlives any
 * component; components only read whether one is running.
 */

interface DetachedWatch {
  stop: () => void
}

const detachedWatches = new Map<string, DetachedWatch>()
const detachedListeners = new Set<() => void>()

function detachedWatchKey(slug: string, metricId: string): string {
  return `${slug}/${metricId}`
}

function notifyDetachedListeners(): void {
  for (const listener of detachedListeners) listener()
}

function subscribeDetached(listener: () => void): () => void {
  detachedListeners.add(listener)
  return () => {
    detachedListeners.delete(listener)
  }
}

export interface DetachedMetricWatchOptions {
  /**
   * Called once the run reaches a terminal status — success AND error, so the
   * caller can refresh whatever lists the run's status. Must not depend on a
   * mounted component: capture a QueryClient, not component state.
   */
  onSettled?: (metricId: string, status: 'success' | 'error') => void
  /** Poll cadence override — tests only. */
  pollIntervalMs?: number
}

/**
 * Start watching a metric whose manual collect was just accepted, independent
 * of any component. Same polling, timeout, failure budget and toasts as
 * {@link useMetricCollectionWatcher}. A second watch of the same metric in the
 * same project replaces the first.
 */
export function startMetricCollectionWatch(
  request: MetricWatchRequest<void>,
  options: DetachedMetricWatchOptions = {},
): void {
  const key = detachedWatchKey(request.slug, request.metricId)
  detachedWatches.get(key)?.stop()

  const startedAt = Date.now()
  const pollIntervalMs = options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS
  let failures = 0
  let timer: ReturnType<typeof setTimeout> | undefined
  let stopped = false

  const watch: DetachedWatch = {
    stop: () => {
      stopped = true
      if (timer !== undefined) clearTimeout(timer)
      if (detachedWatches.get(key) === watch) {
        detachedWatches.delete(key)
        notifyDetachedListeners()
      }
    },
  }

  const finish = (outcome: MetricDefinitionDetailResponse | WatchAbandoned) => {
    if (stopped) return
    watch.stop()
    const status = reportOutcome(request.displayName, outcome)
    if (status) options.onSettled?.(request.metricId, status)
  }

  const schedule = () => {
    if (stopped) return
    timer = setTimeout(() => {
      void poll()
    }, pollIntervalMs)
  }

  const poll = async (): Promise<void> => {
    if (stopped) return
    // Checked before fetching, so the watch ends on time even while every
    // poll is failing.
    if (Date.now() - startedAt >= WATCH_TIMEOUT_MS) {
      finish('timeout')
      return
    }
    let definition: MetricDefinitionDetailResponse
    let status: string | null
    try {
      definition = await metricsCatalogApi.get(request.slug, request.metricId)
      status = definition.last_collection_status
    } catch (error) {
      if (stopped) return
      if (error instanceof ApiError && error.status === 404) {
        finish('missing')
        return
      }
      // The session ended (sign-out, expiry) or the user lost access: this
      // watch belongs to a session that is gone. Stop without a toast, so the
      // login screen does not say "Lost track of …" and a later sign-in never
      // hears about the previous user's metric.
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
        watch.stop()
        return
      }
      failures += 1
      if (failures >= MAX_POLL_FAILURES) finish('unreachable')
      else schedule()
      return
    }
    if (stopped) return
    failures = 0
    if (status === STATUS_RUNNING || status === null) {
      schedule()
      return
    }
    finish(definition)
  }

  detachedWatches.set(key, watch)
  notifyDetachedListeners()
  void poll()
}

/** True while a detached watch of this metric is running. */
export function useIsMetricCollectionWatched(slug: string, metricId: string): boolean {
  const key = detachedWatchKey(slug, metricId)
  return useSyncExternalStore(subscribeDetached, () => detachedWatches.has(key))
}

/** Stop every detached watch without reporting — sign-out and tests. */
export function stopAllMetricCollectionWatches(): void {
  for (const watch of [...detachedWatches.values()]) watch.stop()
}

// Detached watches outlive every component, including the signed-in shell, so
// they end with the session: a sign-out, or any 401 anywhere in the app, stops
// them all.
if (typeof window !== 'undefined') {
  window.addEventListener(AUTH_UNAUTHORIZED_EVENT, stopAllMetricCollectionWatches)
  window.addEventListener(AUTH_SIGNED_OUT_EVENT, stopAllMetricCollectionWatches)
}
