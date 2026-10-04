/**
 * When a bulk status / reviewed / owner change must be confirmed first.
 *
 * Bulk delete always asked; these three applied at once to up to 10k ids, with
 * no word about selected rows the table no longer shows. They now ask
 * when the change reaches rows off screen, when it is large, and before an
 * archive, which takes events out of the active plan.
 */

/** A sweep this large is confirmed even when every row is on screen. */
export const BULK_CONFIRM_THRESHOLD = 50

export type BulkConfirmation = {
  title: string
  message: string
  confirmLabel: string
  variant?: 'danger' | 'primary'
}

export function bulkUpdateConfirmation({
  selectedCount,
  selectedVisibleCount,
  actionLabel,
  archives = false,
  deprecates = false,
}: {
  selectedCount: number
  selectedVisibleCount: number
  /** What the change does, as a verb phrase: "Set status to Live". */
  actionLabel: string
  /** The change archives the events. */
  archives?: boolean
  /** The change deprecates the events: always asked, so the dialog can list
   * the metrics and alert rules that depend on them (#257). */
  deprecates?: boolean
}): BulkConfirmation | null {
  const offScreen = selectedCount - selectedVisibleCount
  if (offScreen <= 0 && selectedCount <= BULK_CONFIRM_THRESHOLD && !archives && !deprecates) return null
  const noun = `${selectedCount.toLocaleString()} selected event${selectedCount === 1 ? '' : 's'}`
  const message =
    offScreen > 0
      ? `${actionLabel} for ${noun}? Only ${selectedVisibleCount.toLocaleString()} of them are on screen — ${offScreen.toLocaleString()} are outside the current filter or page.`
      : `${actionLabel} for ${noun}?`
  return {
    title: actionLabel,
    message,
    confirmLabel: 'Apply',
    variant: archives ? 'danger' : 'primary',
  }
}
