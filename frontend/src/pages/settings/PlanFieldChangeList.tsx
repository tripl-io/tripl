import type { ReactNode } from 'react'

import type { PlanDiffKind, PlanFieldChange, PlanValueChange } from '@/types'
import { DiffPair, DiffValue } from './DiffValue'
import { KIND_META } from './branches/branchMeta'

/**
 * Before → after for each field a diff entry changed, with a collection that
 * moved one member at a time shown as those members rather than two dumps of
 * the whole list.
 *
 * The same presentation the branch review gives a changed entry, read-only,
 * so a revision diff says what changed and not only which fields did.
 * Kinds wear the branch review's own labels and tones, and a pair
 * renders through the review's own `DiffPair`, so a revision diff gets the
 * same word diff and the same visually hidden "before:"/"after:".
 *
 * The branch review renders its changed entries through this list too, with a
 * per-field Revert in `renderAction`; the revision history passes none.
 */
export function PlanFieldChangeList({
  changes,
  renderAction,
  renderItemAction,
}: {
  changes: PlanFieldChange[]
  /** Optional control shown beside each field's name (e.g. the branch
   * review's per-field Revert). */
  renderAction?: (change: PlanFieldChange) => ReactNode
  /** Optional control at the end of one collection member's line (the branch
   * review's "As merged" link on a per-event override). */
  renderItemAction?: (change: PlanFieldChange, item: PlanValueChange) => ReactNode
}) {
  return (
    <div className="flex flex-col gap-2">
      {changes.map((change) => (
        <div
          key={change.field}
          className="rounded-md border px-2.5 py-2 border-border-subtle"
        >
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="mono text-caption font-medium text-fg">
              {change.field}
            </span>
            {renderAction?.(change)}
          </div>
          {change.items && change.items.length > 0 ? (
            // A collection changed one member at a time — show those members,
            // not two dumps of the whole list.
            <div className="flex flex-col gap-1">
              {change.items.map((item) => (
                <PlanValueChangeRow
                  key={item.key}
                  item={item}
                  action={renderItemAction?.(change, item)}
                />
              ))}
            </div>
          ) : (
            <div className="flex flex-wrap items-start gap-1.5">
              <DiffPair before={change.before} after={change.after} />
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

/** What a member's gutter symbol means, for the ear. */
const MEMBER_KIND_WORD: Record<PlanDiffKind, string> = {
  added: 'added',
  changed: 'changed',
  removed: 'removed',
}

/** One member of a changed collection: `~ currency  USD → EUR`. */
function PlanValueChangeRow({ item, action }: { item: PlanValueChange; action?: ReactNode }) {
  const meta = KIND_META[item.kind]
  return (
    <div className="flex flex-wrap items-baseline gap-1.5 text-caption">
      <span
        className="mono w-3 shrink-0 text-center font-medium"
        style={{ color: `var(--${meta.tone})` }}
        aria-hidden="true"
      >
        {meta.sym}
      </span>
      <span className="sr-only">{MEMBER_KIND_WORD[item.kind]}:</span>
      <span className="mono shrink-0 text-fg">
        {item.key}
      </span>
      {item.kind === 'changed' ? (
        <DiffPair before={item.before} after={item.after} />
      ) : (
        <DiffValue value={item.kind === 'added' ? item.after : item.before} tone={meta.tone} />
      )}
      {action ? <span className="ml-auto shrink-0">{action}</span> : null}
    </div>
  )
}
