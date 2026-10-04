import { createContext, useContext } from 'react'

/**
 * Marks the top-bar search button. A palette opened by the global Ctrl+K has no
 * trigger to hand focus back to on Esc, so it falls back to this element.
 */
export const COMMAND_PALETTE_TRIGGER_ATTR = 'data-command-palette-trigger'

export type CommandPaletteContextValue = {
  open: boolean
  setOpen: (next: boolean) => void
}

const NOOP_CONTEXT: CommandPaletteContextValue = {
  open: false,
  setOpen: () => {},
}

export const CommandPaletteContext = createContext<CommandPaletteContextValue>(NOOP_CONTEXT)

export function useCommandPalette(): CommandPaletteContextValue {
  return useContext(CommandPaletteContext)
}

/** The palette dialog's chunk (command-palette-dialog.tsx). */
export const loadCommandPalette = () => import('@/components/command-palette-dialog')

/**
 * Start fetching the palette chunk before it is needed — the top-bar trigger
 * calls this on hover and focus, so a click opens a dialog that is already
 * there. Repeated calls reuse the one module request.
 */
export function preloadCommandPalette(): void {
  void loadCommandPalette().catch(() => {
    /* the lazy component retries (and recovers a stale chunk) on open */
  })
}
