/**
 * A public demo's newcomer gets their demo without being asked for it.
 *
 * The sign-in card promises "a demo workspace of your own", and the visitor
 * then landed on an empty workspace whose one thing to do was a button that
 * made it. Starting the create for them keeps the promise; the provisioning
 * dialog says what is happening and opens the demo when it is ready.
 *
 * Once per account in this browser: someone who cancels the create, or later
 * deletes their demo, finds the button where it was and is not handed a demo
 * they did not ask for.
 */

import { useEffect } from 'react'

/** localStorage: the newcomer's demo was started for them once. */
export const DEMO_AUTOSTART_PREFIX = 'tripl-demo-autostarted:'

function alreadyStarted(userId: string): boolean {
  try {
    return window.localStorage.getItem(`${DEMO_AUTOSTART_PREFIX}${userId}`) === '1'
  } catch {
    // Without storage the "once" cannot be kept, so the button is left to them.
    return true
  }
}

function markStarted(userId: string): void {
  try {
    window.localStorage.setItem(`${DEMO_AUTOSTART_PREFIX}${userId}`, '1')
  } catch {
    /* unreachable in practice: alreadyStarted() read the same storage */
  }
}

/**
 * Call `start` once, the first time `enabled` holds for `userId`.
 *
 * Deferred a tick: React's development double mount runs the effect, its
 * cleanup and the effect again, and a create started in the first run would
 * be aborted by the provisioning hook's own unmount cleanup in between.
 */
export function useAutoStartDemo(enabled: boolean, userId: string | undefined, start: () => void): void {
  useEffect(() => {
    if (!enabled || !userId || alreadyStarted(userId)) return
    const timer = window.setTimeout(() => {
      markStarted(userId)
      start()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [enabled, userId, start])
}
