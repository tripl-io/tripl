/**
 * The demo guide floats above dialogs, menus and popovers and coaches the one
 * that is open, so a press on it — Minimise, Hide hints — is not a press
 * outside that layer and must not close it. A menu that shut under the guide
 * took its ringed item with it, and the ring went back to the menu's trigger.
 */
export function isInDemoGuide(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest('[data-demo-guide]') !== null
}

/** A dismissable layer's `onInteractOutside`, for which the guide is not outside. */
export function keepOpenForDemoGuide<E extends Event>(
  handler?: (event: E) => void,
): (event: E) => void {
  return (event) => {
    if (isInDemoGuide(event.target)) {
      event.preventDefault()
      return
    }
    handler?.(event)
  }
}
