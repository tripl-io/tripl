import { useCallback, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { UNSAFE_DataRouterContext, type BlockerFunction } from 'react-router-dom'
import { NavigationBlocker } from '@/components/navigation-blocker'
import { UNSAVED_CONFIRM_COPY } from '@/components/settings/unsaved-changes'
import { useConfirm } from './useConfirm'

export const UNSAVED_CHANGES_MESSAGE =
  'Your changes have not been saved. Leaving now discards them.'

type GuardOptions = {
  /** What is at stake, phrased for the confirm dialog. */
  message?: string
}

type PageGuardOptions = GuardOptions & {
  /**
   * Also block a same-path navigation this returns true for. For a page whose
   * editing mode lives in the query string (`?edit=1`), a link to the same
   * path without it would otherwise unmount the editor with no prompt.
   */
  alsoBlock?: BlockerFunction
}

/**
 * The page guard that is mounted right now, for exits that are neither a
 * navigation nor inside the guarded page: the sidebar's branch switcher swaps
 * the data every page renders, which remounts a form without changing the URL,
 * so no router blocker sees it. One slot, for the same reason a router consults
 * one blocker: only one page guard is mounted at a time.
 */
let activePageLeave: ((action: () => void, onCancel?: () => void) => void) | null = null

/**
 * Run `action` once the mounted page guard (if any) lets the page go: at once
 * when there is no guard or its form is clean, after a confirm when dirty.
 * `onCancel` runs instead when the user keeps the draft.
 * For controls outside the page that replace what it shows (BranchSwitcher).
 */
export function requestPageLeave(action: () => void, onCancel?: () => void): void {
  if (activePageLeave) activePageLeave(action, onCancel)
  else action()
}

/**
 * Reload and tab-close are not React navigations, so only the browser's own
 * prompt can stop them, and only from a listener that exists while the draft
 * does. Registered off the dirty flag alone so a form nobody has typed into
 * never interrupts a reload.
 */
function useBeforeUnloadWhile(active: boolean) {
  useEffect(() => {
    if (!active) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      // Browsers show their own wording. returnValue is what makes the older
      // ones (Chrome/Edge < 119) prompt at all.
      event.returnValue = true
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [active])
}

/**
 * The confirm half, shared by both guards: resolves true when there is
 * nothing to lose, or once the user has accepted the loss.
 */
function useConfirmDiscard(isDirty: boolean, message: string) {
  const { confirm, dialog } = useConfirm()
  // Read at call time, not render time: an exit handler can run in the same
  // tick as the state change that made the form dirty or clean.
  const dirtyRef = useRef(isDirty)
  useLayoutEffect(() => {
    dirtyRef.current = isDirty
  }, [isDirty])

  const confirmDiscard = useCallback(async (): Promise<boolean> => {
    if (!dirtyRef.current) return true
    return confirm({
      // "Leave without saving?" / Keep editing / Discard changes (AU-42).
      ...UNSAVED_CONFIRM_COPY,
      message,
      variant: 'danger',
    })
  }, [confirm, message])

  // Synchronous when there is nothing to lose, so a clean close or tab switch
  // lands in the same event as the click, exactly as it did before the guard.
  const runIfDiscarded = useCallback(
    (action: () => void, onCancel?: () => void) => {
      if (!dirtyRef.current) {
        action()
        return
      }
      void confirmDiscard().then(discard => {
        if (discard) action()
        else onCancel?.()
      })
    },
    [confirmDiscard],
  )

  return { confirmDiscard, runIfDiscarded, dialog, dirtyRef }
}

/**
 * Guard a full-page authoring form against losing its draft.
 *
 * While `isDirty`, every in-app navigation that leaves the current path (links,
 * the sidebar, `navigate(-1)` from a Cancel button, browser Back and Forward)
 * stops and asks first, and reload/tab-close get the browser's own prompt.
 * Navigations within the same path (a search-param change) are not blocked.
 *
 * Render `dialog`. Call `release()` right before a navigation that follows a
 * successful save: the state that made the form dirty has not re-rendered yet,
 * and the draft is no longer at risk. `requestLeave(action)` is for exits that
 * are not navigations (a tab switch inside the page): it runs `action` at once
 * when clean, and after a confirm when dirty. Controls outside the page that
 * replace its content without navigating (the branch switcher) reach the same
 * confirm through {@link requestPageLeave}.
 *
 * A router consults only ONE blocker, the most recently registered. Do not use
 * this inside the settings takeover (SettingsLayout owns that blocker; register
 * with `useUnsavedChanges` instead), and do not mount two of these at once.
 */
export function useUnsavedChangesGuard(
  isDirty: boolean,
  { message = UNSAVED_CHANGES_MESSAGE, alsoBlock }: PageGuardOptions = {},
): { dialog: ReactNode; requestLeave: (action: () => void) => void; release: () => void } {
  const { confirmDiscard, runIfDiscarded, dialog: confirmDialog, dirtyRef } = useConfirmDiscard(isDirty, message)
  const releasedRef = useRef(false)
  // A draft that becomes dirty again after a save that did not navigate away
  // (Save and add another) is at risk again.
  useLayoutEffect(() => {
    releasedRef.current = false
  }, [isDirty])
  const release = useCallback(() => {
    releasedRef.current = true
  }, [])

  useBeforeUnloadWhile(isDirty)

  useEffect(() => {
    activePageLeave = runIfDiscarded
    return () => {
      if (activePageLeave === runIfDiscarded) activePageLeave = null
    }
  }, [runIfDiscarded])

  // Read through a ref so an inline `alsoBlock` does not re-register the blocker.
  const alsoBlockRef = useRef(alsoBlock)
  useLayoutEffect(() => {
    alsoBlockRef.current = alsoBlock
  }, [alsoBlock])
  const shouldBlock = useCallback<BlockerFunction>(
    args =>
      dirtyRef.current
      && !releasedRef.current
      && (args.currentLocation.pathname !== args.nextLocation.pathname || (alsoBlockRef.current?.(args) ?? false)),
    [dirtyRef],
  )
  const ask = useCallback(async () => {
    // The blocker already knows the form is dirty; ask unconditionally.
    const leave = await confirmDiscard()
    if (leave) releasedRef.current = true
    return leave
  }, [confirmDiscard])

  const hasDataRouter = useContext(UNSAFE_DataRouterContext) !== null
  const dialog = (
    <>
      {confirmDialog}
      {hasDataRouter && <NavigationBlocker shouldBlock={shouldBlock} ask={ask} />}
    </>
  )
  return { dialog, requestLeave: runIfDiscarded, release }
}

/**
 * Guard a dialog form: route its close requests (Escape, an outside click, the
 * X, Cancel) through `requestClose`, which asks first while `isDirty`. Also
 * arms the browser prompt on reload/tab-close. No router blocker: a modal
 * dialog covers every in-app link, and a dialog must not steal the one blocker
 * a page may hold.
 *
 *   <Dialog open={open} onOpenChange={o => { if (!o) guard.requestClose(close) }}>
 */
export function useUnsavedDialogGuard(
  isDirty: boolean,
  { message = UNSAVED_CHANGES_MESSAGE }: GuardOptions = {},
): { dialog: ReactNode; requestClose: (close: () => void) => void } {
  const { runIfDiscarded, dialog } = useConfirmDiscard(isDirty, message)
  useBeforeUnloadWhile(isDirty)
  return { dialog, requestClose: runIfDiscarded }
}

/**
 * Whether a dialog's draft has changed since the dialog opened.
 *
 * For a dialog whose form state lives outside it (the parent resets it on
 * every open): the baseline is the draft as it stood on the first render with
 * `open` true, and it is dropped again on close. `draft` must be
 * JSON-serialisable.
 */
export function useDirtySinceOpen(open: boolean, draft: unknown): boolean {
  const snapshot = JSON.stringify(draft)
  const [baseline, setBaseline] = useState<string | null>(open ? snapshot : null)
  // Adjust-during-render with an equality guard, this repo's idiom for state
  // that follows a prop: the baseline is in place on the render that opens.
  if (open && baseline === null) setBaseline(snapshot)
  if (!open && baseline !== null) setBaseline(null)
  return open && baseline !== null && snapshot !== baseline
}
