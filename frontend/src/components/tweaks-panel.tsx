import { Suspense, useCallback, useMemo, useRef, useState, type ReactNode } from 'react'
import { ErrorBoundary } from '@/components/error-boundary'
import { lazyWithReload } from '@/lib/lazyWithReload'
import {
  TweaksPanelContext,
  type TweaksPanelContextValue,
} from '@/components/tweaks-panel-context'

// The panel is opened from a menu, rarely, so it is not part of the first load.
const TweaksPanel = lazyWithReload(() =>
  import('./tweaks-panel-dialog').then(module => ({ default: module.TweaksPanel })),
)

export function TweaksPanelProvider({ children }: { children: ReactNode }) {
  const [open, setOpenState] = useState(false)
  // The control the panel hangs from. Closing hands focus straight
  // back to it.
  const anchorRef = useRef<HTMLElement | null>(null)
  const setOpen = useCallback((next: boolean, anchor?: HTMLElement | null) => {
    if (next) {
      const active = document.activeElement
      anchorRef.current =
        anchor ?? (active instanceof HTMLElement && active !== document.body ? active : null)
    }
    setOpenState(next)
  }, [])
  const close = useCallback(() => {
    setOpenState(false)
    const anchor = anchorRef.current
    if (anchor?.isConnected) anchor.focus()
  }, [])
  const value = useMemo<TweaksPanelContextValue>(
    () => ({ open, setOpen, anchorRef }),
    [open, setOpen],
  )
  return (
    <TweaksPanelContext.Provider value={value}>
      {children}
      {open && (
        <ErrorBoundary fallback={() => null}>
          <Suspense fallback={null}>
            <TweaksPanel anchorRef={anchorRef} onClose={close} />
          </Suspense>
        </ErrorBoundary>
      )}
    </TweaksPanelContext.Provider>
  )
}
