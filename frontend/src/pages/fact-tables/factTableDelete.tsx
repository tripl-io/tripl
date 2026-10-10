import type { QueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { factTablesApi } from '@/api/factTables'
import { ConfirmImpactMessage } from '@/components/dependencies/ImpactNotice'
import type { ConfirmOptions } from '@/hooks/useConfirm'
import { factTablesKey, projectFactTableKey } from '@/lib/queryKeys'
import type { FactTable } from '@/types'

/**
 * The one "Delete fact table" confirmation, asked from a list row's menu and
 * from the editor's Delete button. The delete runs inside the dialog: the
 * server refuses a table that metrics still read with a 409 naming them, and
 * that refusal shows in the dialog, beside the impact list that predicted it.
 *
 * `confirm(...)` resolves `true` only once the table is gone, so a caller with
 * more to do (the editor closing itself) does it after that.
 */
export function deleteFactTableConfirmation(
  table: Pick<FactTable, 'id' | 'display_name'>,
  slug: string,
  qc: QueryClient,
): ConfirmOptions {
  return {
    title: 'Delete this fact table?',
    // The metrics that read it, listed before the attempt (#257). These are
    // the one dependents that DO block: the server refuses with a 409.
    message: (
      <ConfirmImpactMessage
        message={
          `"${table.display_name}" disappears from every fact metric's picker. A fact table ` +
          'that metrics still read cannot be deleted; the refusal names them.'
        }
        slug={slug}
        branchId={null}
        mode="blocks"
        changes={[{ kind: 'fact_table', id: table.id, change: 'delete' }]}
      />
    ),
    confirmLabel: 'Delete fact table',
    variant: 'danger',
    errorPrefix: 'Could not delete the fact table',
    pendingLabel: 'Deleting…',
    action: async () => {
      await factTablesApi.remove(slug, table.id)
      void qc.invalidateQueries({ queryKey: factTablesKey(slug) })
      void qc.invalidateQueries({ queryKey: projectFactTableKey(slug) })
      toast.success('Fact table deleted.')
    },
  }
}
