import { useEffect, useMemo, useState } from 'react'

/**
 * How far the upper bound of a live window is allowed to lag real time.
 *
 * The bound is rounded UP, so it is always ≥ now and the in-progress bucket is
 * inside the window from the moment it starts. The step therefore does not
 * decide whether a new bucket is visible — only how often the window (and with
 * it the React Query key) changes. Five minutes keeps cached pages from being
 * re-keyed constantly while still moving on its own.
 */
export const LIVE_WINDOW_STEP_MS = 5 * 60 * 1000

function ceilTo(ms: number, stepMs: number): number {
  return Math.ceil(ms / stepMs) * stepMs
}

/**
 * A window bound that follows the clock instead of freezing at mount.
 *
 * `const to = new Date()` inside a `useMemo` keyed on the range length pins the
 * upper bound to whenever the page happened to mount. The charts still refetch —
 * on the adaptive poll and on SSE invalidation — but they keep asking for that
 * same stale window, so buckets recorded after the page opened never appear on a
 * "live" chart no matter how long you leave it open. It was
 * three separate copies of the same code: the monitoring detail page, the events
 * tab metrics card, and the per-row sparklines — whose memo had NO dependencies
 * at all, freezing the window for the whole life of the Events page.
 *
 * The bound advances in discrete steps so the query key is stable in between: a
 * continuously-moving `to` would mint a fresh cache entry on every render and
 * refetch in a loop.
 */
export function useLiveWindowEnd(stepMs: number = LIVE_WINDOW_STEP_MS): number {
  const [end, setEnd] = useState(() => ceilTo(Date.now(), stepMs))
  useEffect(() => {
    const id = window.setInterval(() => setEnd(ceilTo(Date.now(), stepMs)), stepMs)
    return () => window.clearInterval(id)
  }, [stepMs])
  // Re-seeding with the same number is a React bail-out, so a tick that lands
  // inside the current step costs nothing.
  return end
}

/** ISO `{from, to}` covering the last `spanMs`, ending at the live bound. */
export function useLiveTimeRange(
  spanMs: number,
  stepMs: number = LIVE_WINDOW_STEP_MS,
): { from: string; to: string } {
  const end = useLiveWindowEnd(stepMs)
  return useMemo(
    () => ({
      from: new Date(end - spanMs).toISOString(),
      to: new Date(end).toISOString(),
    }),
    [end, spanMs],
  )
}
