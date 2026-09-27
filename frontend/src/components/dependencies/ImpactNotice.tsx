import type { ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'
import { LoadingState } from '@/components/primitives/loading-state'
import { dedupeEdges, summarizeEdges } from '@/lib/dependencies'
import { countOf } from '@/lib/plural'
import type { ImpactChange } from '@/types'
import { UsedByList } from './UsedByList'
import { useImpact } from './useDependencies'

/**
 * What happens to the dependents, so the sentence under "This affects …" is
 * true for the change being confirmed:
 * - `warn`: dependents are left in place and may stop working (the default);
 * - `blocks`: the server refuses the change while they exist (fact tables);
 * - `cascades`: deleting the entity deletes its events with it (event types).
 */
export type ImpactNoticeMode = 'warn' | 'blocks' | 'cascades'

const MODE_SENTENCE: Record<ImpactNoticeMode, string> = {
  warn: 'They are not changed and may stop working as expected.',
  blocks: 'While they read it, the delete is refused; the refusal names them.',
  cascades:
    'Events of this type are deleted with it, and what reads those events loses them.',
}

/**
 * What a delete / deprecate / rename would touch, for a confirm dialog (F04,
 * #257): "This affects 2 metrics and 1 alert rule", then the list.
 *
 * It never disables the confirm and never delays it: the dialog is answerable
 * while this is still loading, and a failed check says so. The client adds no
 * refusal; in `blocks` mode (fact tables) the server's 409 is the refusal, so
 * the notice does not claim the change goes through. Names are not links here:
 * following one would navigate away under an open dialog.
 *
 * Only the one-line summary is a live region: announcing the whole list would
 * read every dependent aloud each time it renders.
 */
export function ImpactNotice({
  slug,
  changes,
  branchId,
  mode = 'warn',
}: {
  slug: string
  changes: readonly ImpactChange[]
  /** Defaults to the branch on screen. */
  branchId?: string | null
  mode?: ImpactNoticeMode
}) {
  const query = useImpact(slug, changes, { branchId })
  if (changes.length === 0) return null

  if (query.isPending) {
    return <LoadingState className="mt-3" label="Checking what depends on this…" />
  }
  if (query.isError) {
    return (
      <p className="mt-3 mb-0 text-body-sm text-fg-tertiary" role="status">
        {mode === 'blocks'
          ? 'Could not check what depends on this. If metrics still read it, the delete is refused and the refusal names them.'
          : 'Could not check what depends on this. The change is not blocked; open the Used by section first if you are unsure.'}
      </p>
    )
  }

  const items = query.data.items
  const affected = dedupeEdges(items.flatMap((item) => item.affected))
  const truncatedNote = query.truncated
    ? ` Checked the first ${countOf(query.checkedCount, 'item', 'items')} of ${changes.length.toLocaleString()}.`
    : ''

  if (affected.length === 0) {
    return (
      <p className="mt-3 mb-0 text-body-sm text-fg-tertiary" role="status">
        Nothing else in the project depends on this.{truncatedNote}
      </p>
    )
  }

  // One change: the server's own sentence. Several: one sentence over the
  // union, so an alert rule on two of the deleted events is counted once.
  const only = items.length === 1 ? items[0] : undefined
  const summary = only?.summary || summarizeEdges(affected)

  return (
    <div
      className="mt-3 rounded-md border border-warning bg-warning-soft px-3 py-2.5"
      data-testid="impact-notice"
    >
      <p className="m-0 flex items-start gap-1.5 text-body-sm font-medium text-fg" role="status">
        <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden="true" />
        <span>
          This affects {summary}. {MODE_SENTENCE[mode]}
          {truncatedNote}
        </span>
      </p>
      <div className="mt-2 max-h-48 overflow-y-auto">
        <UsedByList slug={slug} edges={affected} linked={false} headingLevel={4} compact />
      </div>
    </div>
  )
}

/**
 * A confirm dialog's message with the impact under it. The message keeps its
 * own words; the notice only adds.
 */
export function ConfirmImpactMessage({
  message,
  slug,
  changes,
  branchId,
  mode,
}: {
  message: ReactNode
  slug: string
  changes: readonly ImpactChange[]
  branchId?: string | null
  mode?: ImpactNoticeMode
}) {
  return (
    <>
      {typeof message === 'string' ? <p className="m-0">{message}</p> : message}
      <ImpactNotice slug={slug} changes={changes} branchId={branchId} mode={mode} />
    </>
  )
}
