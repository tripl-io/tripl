import { createContext, useContext } from 'react'

/**
 * A draft a settings section holds in component state and would lose if the
 * rail navigated away from it.
 *
 * The Platform pages share one draft: it is plain state in
 * ServiceSettingsPage, so leaving the Platform group threw away a
 * hand-written system prompt with no warning. Switching *within*
 * the group keeps it — InstanceSection is a module-scope lazy() ref rendered
 * without a key, so AI → Email preserves the draft — which is why the guard is
 * a predicate over the target path rather than a blanket "is dirty" block.
 *
 * Registering also arms the shell's browser-level guards — Back and
 * reload/close — for as long as the draft is registered, so a
 * section that forgets to pass `null` after saving leaves them armed.
 */
export type UnsavedWork = {
  /** True for settings paths that keep this draft alive (no warning needed). */
  keptBy: (settingsPath: string) => boolean
  /** What is at stake, phrased for a confirm dialog. */
  message: string
  /**
   * Settings paths (e.g. `instance/security`) whose own edits are unsaved, so
   * the rail can mark them. Save on Platform is per section, so an edit left
   * in another section needs a pointer back to it.
   */
  dirtyPaths?: readonly string[]
}

export type UnsavedChangesValue = {
  /** Register the live draft, or `null` once it is saved, discarded or gone. */
  registerUnsaved: (work: UnsavedWork | null) => void
}

/**
 * Carried in the navigation's own `state` by an exit that has already asked,
 * or that has nothing left to lose (a deleted project). Scoped to that single
 * navigation, so unlike a ref or a piece of component state it cannot survive
 * to wave a later one through.
 */
export const LEAVE_CONFIRMED = { leaveConfirmed: true } as const

/**
 * The wording of every "you have unsaved work" confirm. The form's own
 * Cancel opened a dialog offering "Cancel" and "Discard", two meanings of
 * Cancel in two seconds; the safe answer is now named for what it does, and
 * is the one the dialog focuses (AlertDialog focuses its cancel button).
 * Shared by the page/dialog guards (useUnsavedChangesGuard) and the settings
 * takeover's rail guard, so the app asks the question one way.
 */
export const UNSAVED_CONFIRM_COPY = {
  title: 'Leave without saving?',
  confirmLabel: 'Discard changes',
  cancelLabel: 'Keep editing',
} as const

/** No-op by default so a section renders fine outside the takeover shell. */
const UnsavedChangesContext = createContext<UnsavedChangesValue>({
  registerUnsaved: () => {},
})

export const UnsavedChangesProvider = UnsavedChangesContext.Provider

export function useUnsavedChanges(): UnsavedChangesValue {
  return useContext(UnsavedChangesContext)
}
