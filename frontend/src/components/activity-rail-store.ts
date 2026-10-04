import { useEffect, useSyncExternalStore } from 'react'

// ───────── Is the rail beside the page right now? ─────────
//
// Overview's own "Recent activity" panel repeated the rail item for item when
// the rail sat inline next to it. The page cannot see the shell's
// state, so each open INLINE panel counts itself here. Layout says which one
// is inline (`inline`), so the width threshold lives in Layout alone: below it
// the panel is a modal drawer that covers the page, and hiding the page's panel
// behind it would only reflow the page.
let inlineRails = 0
const railListeners = new Set<() => void>()

function subscribeRail(listener: () => void): () => void {
  railListeners.add(listener)
  return () => {
    railListeners.delete(listener)
  }
}

function railIsInline(): boolean {
  return inlineRails > 0
}

/** True while the activity rail is open inline beside the page content. */
export function useActivityRailInline(): boolean {
  return useSyncExternalStore(subscribeRail, railIsInline, () => false)
}

/** Counts an open inline rail while `active`; the panel calls this. */
export function useRegisterInlineRail(active: boolean): void {
  useEffect(() => {
    if (!active) return
    inlineRails += 1
    railListeners.forEach((listener) => listener())
    return () => {
      inlineRails -= 1
      railListeners.forEach((listener) => listener())
    }
  }, [active])
}
