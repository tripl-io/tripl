import { createContext, useContext, useId, useLayoutEffect } from 'react'

/**
 * What the shared `<Dialog>` (./dialog) hands the components inside it, so a
 * part of the dialog can say it holds unsaved input and a way out that is not
 * a close request can ask first. Its own module: a component file that also
 * exported a context and hooks would break Fast Refresh.
 */
export type DialogGuard = {
  /** Record whether one part of the dialog (keyed by `id`) holds unsaved input. */
  report: (id: string, dirty: boolean) => void
  /** Run `action` at once when nothing is unsaved, after a confirm when something is. */
  requestLeave: (action: () => void) => void
}

/** Provided by the shared `<Dialog>`; null outside one. */
export const DialogGuardContext = createContext<DialogGuard | null>(null)

/**
 * Tell the enclosing `<Dialog>` whether this part of it holds unsaved input.
 * For a dialog body that owns its form state: it mounts only while the dialog
 * is open, so the component rendering the `<Dialog>` cannot pass `dirty` for
 * it. Unmounting clears the report. Outside a dialog it does nothing.
 */
export function useDialogDirty(isDirty: boolean): void {
  const report = useContext(DialogGuardContext)?.report
  const id = useId()
  useLayoutEffect(() => {
    report?.(id, isDirty)
  }, [report, id, isDirty])
  useLayoutEffect(() => () => report?.(id, false), [report, id])
}

const runNow = (action: () => void) => action()

/**
 * A way out of a dialog that is not a close request, such as a link to
 * another page: wrap what it does in the returned function, which runs it at
 * once when the dialog holds nothing unsaved and after a confirm when it does.
 * Call it from a component inside the `<Dialog>`.
 */
export function useDialogLeave(): (action: () => void) => void {
  return useContext(DialogGuardContext)?.requestLeave ?? runNow
}
