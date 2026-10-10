/**
 * The demo's coaching words for the pointer in hand. Its tags and cues say
 * "Click here" and "Click Run now."; on a touch screen there is nothing to
 * click, so a coarse pointer reads "Tap".
 *
 * Only the verb changes: "click" in running text and "Click" opening a
 * sentence. A capitalised "Click" anywhere else is a name, such as the seeded
 * "Buy Button Click" event, and stays as it is.
 */

import { COARSE_POINTER_QUERY } from '@/hooks/useMediaQuery'

/** A touch screen is the primary pointer. False where there is no `matchMedia`. */
export function isCoarsePointer(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia(COARSE_POINTER_QUERY).matches === true
  )
}

export function gestureCopy(text: string, coarse: boolean = isCoarsePointer()): string {
  if (!coarse) return text
  return text.replace(/(^|[.!?]\s+)Click\b/g, '$1Tap').replace(/\bclick\b/g, 'tap')
}
