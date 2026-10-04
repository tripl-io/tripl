/**
 * Adaptive polling policy.
 *
 * Central rule that replaces the scattered, unconditional `refetchInterval`
 * values. A pure function so it is trivially unit-testable; the React binding
 * lives in {@link ./streamContext}.
 *
 * Policy:
 *  - Never poll a hidden tab (Page Visibility) — react-query also pauses
 *    background intervals by default, this makes the intent explicit + testable.
 *  - When the live stream is `'live'`, don't poll at all — the SSE stream
 *    delivers updates. (When Redis is off the stream reports `'degraded'`, so we
 *    keep polling — the fallback.)
 *  - Otherwise poll at `activeMs`, but stop (`idleMs`, default `false`) once the
 *    watched work reached a terminal state (`active === false`).
 */

/**
 * `connecting` — EventSource opened, awaiting the `hello` greeting.
 * `live` — realtime backend (Redis pub/sub) is delivering events.
 * `degraded` — stream connected but the backend can't deliver (Redis off) → poll.
 * `closed` — no stream / disconnected → poll.
 */
export type StreamStatus = 'connecting' | 'live' | 'degraded' | 'closed'

export interface AdaptivePollingInput {
  streamStatus: StreamStatus
  documentHidden: boolean
  /** Fast cadence used while there is active work and the stream is unavailable. */
  activeMs: number
  /**
   * Whether there is work worth fast-polling (e.g. a running scan job). Omit or
   * `true` for surfaces that always want the fallback cadence when the stream is
   * down; `false` stops fast polling (terminal-state reached).
   */
  active?: boolean
  /** Interval to use when `active === false`. Defaults to `false` (stop). */
  idleMs?: number | false
}

/** Resolve the effective `refetchInterval` for a query under the adaptive policy. */
export function resolveRefetchInterval(input: AdaptivePollingInput): number | false {
  if (input.documentHidden) return false
  if (input.streamStatus === 'live') return false
  if (input.active === false) return input.idleMs ?? false
  return input.activeMs
}
