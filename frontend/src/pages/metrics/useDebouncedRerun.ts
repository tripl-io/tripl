import { useEffect, useRef } from 'react'

/** How long an armed preview waits after the last edit before running again. */
export const AUTO_RERUN_MS = 800

/**
 * Re-run a dry run on its own once its inputs settle — but only once the
 * author has run it by hand. A preview reaches a warehouse, so the
 * first run is always a click; after that, an edit that makes the shown
 * result stale re-runs it `delayMs` after the last keystroke instead of
 * leaving the author to press Preview again.
 *
 * `stale` is the caller's "the shown result no longer describes the inputs,
 * and they are complete enough to run". Every change to `inputKey` restarts
 * the wait, so typing never fires a request per character.
 */
export function useDebouncedRerun({
  armed,
  stale,
  inputKey,
  run,
  delayMs = AUTO_RERUN_MS,
}: {
  armed: boolean
  stale: boolean
  inputKey: string
  run: () => void
  delayMs?: number
}): void {
  const runRef = useRef(run)
  useEffect(() => {
    runRef.current = run
  })
  useEffect(() => {
    if (!armed || !stale) return
    const timer = window.setTimeout(() => runRef.current(), delayMs)
    return () => window.clearTimeout(timer)
  }, [armed, stale, inputKey, delayMs])
}
