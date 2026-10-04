import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { variablesApi } from '@/api/variables'
import { CodeToken } from '@/components/primitives/code-token'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useConfirm } from '@/hooks/useConfirm'
import { formatDateTime } from '@/lib/datetime'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { eventNameLabel } from '@/lib/eventName'
import { countOf } from '@/lib/plural'
import { branchVariableValuesKey, variablesKey, variableValuesKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import type { Variable } from '@/types'

/**
 * What the scans observed for ONE variable, per event and source column, with
 * the reset that clears it.
 *
 * `variable` is the LIVE row from the variables list, not a copy: after a clear
 * the list refetches, and a snapshot kept offering to clear "12 contexts" of a
 * variable that had none left (PLAN-29). `scrollClassName` caps the table in
 * the dialog; the page lets it run.
 */
export function VariableObservedSection({
  slug,
  branchId,
  variable,
  canWrite,
  scrollClassName = 'max-h-72',
}: {
  slug: string
  branchId: string | null
  variable: Variable
  canWrite: boolean
  scrollClassName?: string
}) {
  const qc = useQueryClient()
  const { confirm, dialog } = useConfirm()

  // Per-event contexts are fetched for the ONE variable on screen, never for
  // the list — only the variable's own view needs the full breakdown.
  const { data: contexts = [] } = useQuery({
    queryKey: variableValuesKey(slug, branchId, variable.id),
    queryFn: () => variablesApi.values(slug, variable.id, branchId),
  })

  const clearValuesMut = useMutation({
    // Its error is rendered beside the button.
    meta: SILENT_ERROR_META,
    mutationFn: () => variablesApi.clearValues(slug, variable.id, branchId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: variablesKey(slug, branchId) })
      // The contexts query key is an inline literal and sits OUTSIDE the
      // variablesKey prefix, so the line above does not reach it.
      qc.invalidateQueries({ queryKey: branchVariableValuesKey(slug, branchId) })
    },
  })

  const handleClearValues = async () => {
    // Read off the live row, so a second click after a clear cannot quote the
    // count from before it (PLAN-29).
    const contextCount = variable.context_count ?? 0
    const ok = await confirm({
      title: 'Clear observed values',
      message:
        `Clear the ${countOf(contextCount, 'observed value context', 'observed value contexts')} `
        + `recorded for "${variable.name}"? The property keeps its description, documented values, `
        + 'bindings, per-event overrides and every drift verdict.\n\n'
        // Two things a person would otherwise discover the hard way. The first
        // is why this is not simply undone by re-scanning; the second is that
        // "keep the variable" is not a guarantee the sweep is bound by.
        + `A later scan re-records a context only where an event field still says \${${variable.name}}. `
        + 'And if nothing refers to this property any more, having no observed values makes it '
        + "retirable — the next scan's cleanup may then remove it.",
      confirmLabel: 'Clear values',
      variant: 'danger',
    })
    if (ok) clearValuesMut.mutate()
  }

  return (
    <div className="rounded-md border bg-muted/30 p-3">
      {dialog}
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-body-sm font-semibold uppercase tracking-wide text-fg-tertiary">
          Observed values
        </div>
        {/* Sits with the thing it clears. Deleting the variable was the only
            reset available, and it takes everything else on the row with it.
            A viewer is not offered it at all. */}
        {canWrite && (
          <Button
            type="button"
            variant="ghost"
            size="xs"
            disabled={(variable.context_count ?? 0) === 0 || clearValuesMut.isPending}
            onClick={() => { void handleClearValues() }}
          >
            Clear observed values
          </Button>
        )}
      </div>
      {clearValuesMut.isError && (
        <p role="alert" className="mb-2 text-body text-destructive">
          Could not clear the observed values: {getErrorMessage(clearValuesMut.error)}
        </p>
      )}
      {/* Four columns, all scan-derived. Variable, Type and Description used to
          repeat the definition on every row and pushed Event, Source and Values
          into a sideways scroll (PLAN-30). */}
      <div className={`${scrollClassName} overflow-auto rounded-sm border bg-background`}>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Event</TableHead>
              <TableHead>Source</TableHead>
              <TableHead>Possible values</TableHead>
              <TableHead>Last refreshed</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {contexts.length === 0 ? (
              <TableRow>
                <TableCell colSpan={4} className="text-body-sm text-fg-tertiary">
                  No values observed yet.
                </TableCell>
              </TableRow>
            ) : contexts.map((context) => (
              <TableRow key={context.id}>
                {/* `event_name` is a bare passthrough of `Event.name`, so the
                    blank-named catalog row reaches this cell as '' and the
                    Event column painted nothing. */}
                <TableCell className="text-body-sm">{eventNameLabel(context.event_name)}</TableCell>
                <TableCell className="font-mono text-body-sm">
                  {context.source_column
                    ? <span title={context.source_column}>{context.source_column}</span>
                    : <span className="text-fg-tertiary">—</span>}
                </TableCell>
                <TableCell className="text-body-sm">
                  {context.values.length > 0 ? (
                    <div className="flex flex-wrap gap-1">
                      {context.values.map((value) => (
                        <CodeToken key={value} className="max-w-40" title={value}>
                          {value}
                        </CodeToken>
                      ))}
                      {context.value_kind === 'high' && (
                        <span className="text-micro text-fg-tertiary">(examples)</span>
                      )}
                    </div>
                  ) : (
                    <span className="text-fg-tertiary">—</span>
                  )}
                </TableCell>
                <TableCell className="text-body-sm text-fg-tertiary">
                  {(context.updated_at && formatDateTime(context.updated_at)) || '—'}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
