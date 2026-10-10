/**
 * When a bulk status / reviewed / owner change must be confirmed first.
 *
 * Bulk delete always asked; these three applied at once to up to 10k ids, with
 * no word about selected rows the table no longer shows. They now ask
 * when the change reaches rows off screen, when it is large, and before an
 * archive, which takes events out of the active plan.
 */

import { formatNumber } from '@/lib/format'
import { countOf } from '@/lib/plural'

/**
 * "Delete 3 selected events?", and when part of the selection is off screen,
 * how much of it the reader can see. `action` is the question's opening verb
 * phrase ("Delete", "Set status to Live for"). Bulk delete and the bulk
 * changes ask through this one sentence, so a single event never reads
 * "1 selected events".
 */
export function selectionQuestion(action: string, selectedCount: number, selectedVisibleCount: number): string {
  const question = `${action} ${countOf(selectedCount, 'selected event', 'selected events')}?`
  const offScreen = selectedCount - selectedVisibleCount
  if (offScreen <= 0) return question
  return `${question} Only ${formatNumber(selectedVisibleCount)} of them are on screen — ${formatNumber(offScreen)} are outside the current filter or page.`
}

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
  return {
    title: actionLabel,
    message: selectionQuestion(`${actionLabel} for`, selectedCount, selectedVisibleCount),
    confirmLabel: 'Apply',
    variant: archives ? 'danger' : 'primary',
  }
}
