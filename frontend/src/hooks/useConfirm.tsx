import { useCallback, useRef, useState, type ReactNode } from 'react'
import ConfirmDialog from '../components/ConfirmDialog'

export interface ConfirmOptions {
  title: string
  /** Plain text, or rich content (emphasis, a list). */
  message: ReactNode
  variant?: 'danger' | 'primary'
  confirmLabel?: string
  /** The safe answer's label; defaults to "Cancel". */
  cancelLabel?: string
  /** Confirm arms only once exactly this text is typed. */
  requireText?: string
  /**
   * Run the confirmed action INSIDE the dialog: it stays open while this runs,
   * a rejection renders in place and keeps it open for another go, and the
   * returned promise resolves `true` only once it has succeeded.
   */
  action?: () => Promise<unknown>
  /** Lead-in for an `action` failure, e.g. "Could not delete the project". */
  errorPrefix?: string
  /** Confirm's label while `action` runs. */
  pendingLabel?: string
}

interface ConfirmState extends ConfirmOptions {
  resolve: (v: boolean) => void
  pending: boolean
  error: unknown
  key: number
}

export function useConfirm() {
  const [state, setState] = useState<ConfirmState | null>(null)
  const nextKey = useRef(0)
  // The open request's resolver, outside state so `confirm` can settle it
  // without depending on (and so re-creating itself for) every state change.
  const openResolve = useRef<((v: boolean) => void) | null>(null)

  // Stable, so a hook that wraps it (useUnsavedChangesGuard) can memoise on it.
  const confirm = useCallback((opts: ConfirmOptions) => {
    return new Promise<boolean>(resolve => {
      // A second request while one is open replaces it. The first caller is
      // answered "no" rather than left awaiting a promise that would never
      // settle.
      openResolve.current?.(false)
      const settle = (v: boolean) => {
        if (openResolve.current === settle) openResolve.current = null
        resolve(v)
      }
      openResolve.current = settle
      nextKey.current += 1
      setState({ ...opts, resolve: settle, pending: false, error: null, key: nextKey.current })
    })
  }, [])

  const runAction = async (current: ConfirmState, action: () => Promise<unknown>) => {
    setState(s => (s?.key === current.key ? { ...s, pending: true, error: null } : s))
    try {
      await action()
    } catch (error) {
      setState(s => (s?.key === current.key ? { ...s, pending: false, error } : s))
      return
    }
    current.resolve(true)
    setState(s => (s?.key === current.key ? null : s))
  }

  const dialog = state ? (
    <ConfirmDialog
      // A fresh dialog per request, so a typed confirmation never carries over.
      key={state.key}
      open
      title={state.title}
      message={state.message}
      variant={state.variant}
      confirmLabel={state.confirmLabel}
      cancelLabel={state.cancelLabel}
      requireText={state.requireText}
      stayOpen={state.action !== undefined}
      pending={state.pending}
      error={state.error}
      errorPrefix={state.errorPrefix}
      pendingLabel={state.pendingLabel}
      onConfirm={() => {
        if (state.action) {
          if (!state.pending) void runAction(state, state.action)
          return
        }
        state.resolve(true)
        setState(null)
      }}
      onCancel={() => {
        if (state.pending) return
        state.resolve(false)
        setState(null)
      }}
    />
  ) : null

  return { confirm, dialog }
}
