import { createContext, useContext, type RefObject } from 'react'

export type TweaksPanelContextValue = {
  open: boolean
  /**
   * Opens or closes the panel. `anchor` is the control the popover hangs from;
   * without one it hangs from whatever had focus, which is the
   * clicked button in the common case.
   */
  setOpen: (next: boolean, anchor?: HTMLElement | null) => void
  /** The element the open panel is anchored to, read by the lazy panel. */
  anchorRef: RefObject<HTMLElement | null>
}

const NOOP_CONTEXT: TweaksPanelContextValue = {
  open: false,
  setOpen: () => {},
  anchorRef: { current: null },
}

export const TweaksPanelContext = createContext<TweaksPanelContextValue>(NOOP_CONTEXT)

export function useTweaksPanel(): TweaksPanelContextValue {
  return useContext(TweaksPanelContext)
}
