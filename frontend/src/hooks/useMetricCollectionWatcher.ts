import { useSyncExternalStore } from 'react'
import { toast } from 'sonner'
import { ApiError, AUTH_SIGNED_OUT_EVENT, AUTH_UNAUTHORIZED_EVENT } from '@/api/client'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import type { MetricDefinitionDetailResponse } from '@/types'

/** How often to re-check the watched metric's persisted collection status. */
const DEFAULT_POLL_INTERVAL_MS = 3000
/**
 * Stop watching after this long. The Celery task budget is far larger (30 min
 * soft limit), so a long run is not an error — the watcher bows out with an
 * informational toast instead of spinning indefinitely.
 */
const WATCH_TIMEOUT_MS = 5 * 60_000
/**
 * Consecutive failed polls (5xx, network) after which the watch gives up,
 * rather than polling, and showing the collect spinner, forever.
 */
const MAX_POLL_FAILURES = 3

/** `MetricDefinition.last_collection_status` markers stamped by the backend. */
const STATUS_RUNNING = 'running'
const STATUS_ERROR = 'error'

/**
 * Toast how a watch ended and return the terminal run status, or `null` when
 * the watch was abandoned before the run reported one.
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

export interface MetricWatchRequest {
  /**
   * The project the collect was fired against. Captured with the watch rather
   * than read live from the route: otherwise navigating to another project
   * mid-watch repointed the poll at `project-B/metric-A`, a metric that does not
   * exist there.
   */
  slug: string
  metricId: string
  displayName: string
}

/** How a watch ended without the run reaching a terminal status. */
type WatchAbandoned = 'timeout' | 'missing' | 'unreachable'

/*
 * A watch is owned by this module, not by the component that started it, so
 * it outlives any component: the catalog unmounts a row on every search
 * keystroke, filter change or page leave, and a watch that died with the row
 * silently dropped the "you will be notified" promise. Components only read
 * whether one is running.
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
 * Start watching a metric whose manual collect was just accepted (202),
 * independent of any component, until the run reaches a terminal state, and
 * report the outcome as a toast.
 *
 * `POST /metrics/{id}/collect` stamps `last_collection_status="running"` before
 * it queues the Celery task, and the worker stamps `success` / `error` (plus
 * `last_collection_error`) when the run settles — the definition itself is the
 * queryable run status, so no extra job model is needed. The watch polls the
 * definition until the status leaves `running`, toasts "collected" or the
 * persisted failure reason, and calls `onSettled`. It gives up with a toast on
 * timeout, on a deleted metric and after repeated failed polls, and ends
 * silently once the session ends or access is lost (a 401 or 403 poll, a
 * sign-out).
 *
 * A second watch of the same metric in the same project replaces the first.
 */
export function startMetricCollectionWatch(
  request: MetricWatchRequest,
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
