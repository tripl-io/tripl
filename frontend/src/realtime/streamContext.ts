/**
 * React bindings for the adaptive polling policy.
 *
 * The project event stream is mounted once (in the app Layout) and publishes its
 * status through the shared {@link StreamStatusContext}. Any polling query reads
 * it via {@link useAdaptiveRefetchInterval} to decide its `refetchInterval`, so
 * polling is centrally governed instead of hard-coded per widget.
 *
 * Hooks + context only (no component) so this stays a fast-refresh-friendly,
 * widely-imported module; the provider component lives in
 * {@link ./ProjectEventStreamProvider}.
 */

import { createContext, useContext, useSyncExternalStore } from 'react'
import { resolveRefetchInterval, type StreamStatus } from './pollingPolicy'

// Default `'closed'` so components rendered without a provider (workspace pages,
// tests) still poll — the safe fallback.
export const StreamStatusContext = createContext<StreamStatus>('closed')

export function useProjectStreamStatus(): StreamStatus {
  return useContext(StreamStatusContext)
}

function subscribeVisibility(onChange: () => void): () => void {
  if (typeof document === 'undefined') return () => {}
  document.addEventListener('visibilitychange', onChange)
  return () => document.removeEventListener('visibilitychange', onChange)
}

function getVisibilitySnapshot(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden'
}

/** `true` while the tab is backgrounded (Page Visibility), reactively. */
export function useDocumentHidden(): boolean {
  return useSyncExternalStore(subscribeVisibility, getVisibilitySnapshot, () => false)
}

export interface AdaptiveRefetchOptions {
  /** Fast cadence used while the stream is unavailable and work is active. */
  activeMs: number
  /** Whether there is work worth fast-polling; `false` stops (terminal reached). */
  active?: boolean
  /** Interval when `active === false`. Defaults to `false` (stop). */
  idleMs?: number | false
}

/**
 * The `refetchInterval` a query should use under the adaptive policy: `false`
 * while the stream is live or the tab is hidden, otherwise the configured
 * cadence (subject to the terminal-state `active` flag).
 */
export function useAdaptiveRefetchInterval(options: AdaptiveRefetchOptions): number | false {
  const streamStatus = useProjectStreamStatus()
  const documentHidden = useDocumentHidden()
  return resolveRefetchInterval({ streamStatus, documentHidden, ...options })
}

export interface AdaptiveRefetchFnOptions<TData> {
  /** Fast cadence used while there is active work and the stream is unavailable. */
  activeMs: number
  /** Interval when the work is terminal (`isActive` returns `false`). Default `false`. */
  idleMs?: number | false
  /** Whether the query's latest data still has work worth fast-polling. */
  isActive: (data: TData | undefined) => boolean
}

/**
 * Function form of {@link useAdaptiveRefetchInterval} for queries with a
 * terminal-state predicate (e.g. scan jobs): react-query re-evaluates the
 * returned function with the query's latest data, so fast polling STOPS the
 * moment the watched job reaches a terminal state — while still honouring the
 * stream-down-only and hidden-tab rules.
 */
export function useAdaptiveRefetchIntervalFn<TData>(
  options: AdaptiveRefetchFnOptions<TData>,
): (query: { state: { data: TData | undefined } }) => number | false {
  const streamStatus = useProjectStreamStatus()
  const documentHidden = useDocumentHidden()
  return (query) =>
    resolveRefetchInterval({
      streamStatus,
      documentHidden,
      activeMs: options.activeMs,
      idleMs: options.idleMs,
      active: options.isActive(query.state.data),
    })
}
