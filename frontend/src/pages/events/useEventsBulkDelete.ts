import { createElement, useCallback, type ReactNode } from 'react'

import { ConfirmImpactMessage } from '@/components/dependencies/ImpactNotice'
import { selectionQuestion } from './bulkConfirm'
import type { EventMutations } from './useEventMutations'

type ConfirmFn = (options: {
  title: string
  message: ReactNode
  variant?: 'danger' | 'primary'
  confirmLabel?: string
}) => Promise<boolean>

/**
 * Deletes the WHOLE selection, including ids that are no longer loaded — that
 * is what "Select all N matching" is for. The confirmation names both numbers
 * whenever they differ, so the operator is never asked to approve a count that
 * exceeds what the table can show them.
 *
 * Under the question, the dialog lists what depends on the selection — the
 * metrics and alert rules that would lose their events (#257). A warning only:
 * Delete stays armed while it loads and whatever it finds.
 */
export function useEventsBulkDelete({
  slug,
  branchId,
  selectedEventIds,
  selectedVisibleEventIds,
  bulkDeleteMut,
  confirm,
}: {
  slug: string | undefined
  branchId: string | null
  selectedEventIds: string[]
  selectedVisibleEventIds: string[]
  bulkDeleteMut: EventMutations['bulkDeleteMut']
  confirm: ConfirmFn
}) {
  return useCallback(async () => {
    if (!selectedEventIds.length) return
    const question = selectionQuestion('Delete', selectedEventIds.length, selectedVisibleEventIds.length)
    const ok = await confirm({
      title: 'Delete selected events',
      message: slug
        ? createElement(ConfirmImpactMessage, {
            message: question,
            slug,
            branchId,
            changes: selectedEventIds.map((id) => ({ kind: 'event' as const, id, change: 'delete' as const })),
          })
        : question,
      confirmLabel: 'Delete',
      variant: 'danger',
    })
    if (ok) bulkDeleteMut.mutate(selectedEventIds)
  }, [branchId, bulkDeleteMut, confirm, selectedEventIds, selectedVisibleEventIds, slug])
}
