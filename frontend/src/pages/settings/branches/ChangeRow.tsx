import { Fragment, useId, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle, ArrowUpRight, ChevronRight, Pencil, Undo2 } from 'lucide-react'

import { Chip } from '@/components/primitives/chip'
import { ScenarioCoachMark } from '@/demo/ScenarioCoachMark'
import { useDemoScenarioActions } from '@/demo/demoScenarioContext'
import { SCENARIO_SEEDED } from '@/demo/scenarioModel'
import { useBranchLinkProps } from '@/hooks/useBranch'
import type { PlanDiffEntry, PlanDiffKind } from '@/types'
import { DiffValue } from '../DiffValue'
import { PlanFieldChangeList } from '../PlanFieldChangeList'
import { changeSummary, housekeepingLine, housekeepingReason } from './branchDiffModel'
import {
  ENTITY_LABEL,
  KIND_META,
  RENAMED_META,
  entityEditPath,
  entityPath,
  eventTitle,
  isEmptyStateValue,
  stateKeyLabel,
} from './branchMeta'

// Row actions get a 40px hit area on touch screens (PL-17); the 11px links
// were well under any tap target.
const ROW_ACTION_TOUCH = 'pointer-coarse:min-h-10 pointer-coarse:px-2'

const REVERT_LABEL: Record<PlanDiffKind, string> = {
  added: 'Discard this addition',
  changed: 'Revert all changes',
  removed: 'Restore on this branch',
}

interface ChangeRowProps {
  slug: string
  branchId: string
  entry: PlanDiffEntry
  /** Set when the backend paired this removal with an addition: the name the
   * row now wears on the branch. The row then reads as the rename it is, and
   * its revert undoes the rename rather than restoring a deletion. */
  renamedTo?: string
  /** For a rename, the branch-side id: the surviving row is the *removal*, whose
   * own `entity_id` is the base-side one, so editing it would edit main. The
   * branch-side id lives on the paired addition the list filters out. */
  renamedEntityId?: string | null
  /** A merged or closed branch still renders its diff, but its plan is
   * read-only: the backend answers a write aimed at one with 409 ("Branch 'X'
   * is merged, so its plan is read-only"). Do not offer a shortcut that can
   * only fail. */
  editable: boolean
  /** Omitted for a viewer: reverting writes to the branch. */
  onRevert?: (entry: PlanDiffEntry, field?: string) => void
  /** Branch rows that all carry this removal's scan identity. The revert
   * endpoint refuses such a removal with a 409 rather than guess which row it
   * was renamed into, so the row offers no revert and lists them instead
   * (PLAN-18). */
  revertBlockedBy?: PlanDiffEntry[]
  reverting: boolean
}

export function ChangeRow({
  slug,
  branchId,
  entry,
  renamedTo,
  renamedEntityId,
  editable,
  onRevert,
  revertBlockedBy,
  reverting,
}: ChangeRowProps) {
  const [open, setOpen] = useState(false)
  // For a modification the change is the point; the entity's whole state is
  // context, one click further (PL-12).
  const [stateOpen, setStateOpen] = useState(false)
  const stateId = useId()
  const detailId = useId()
  const branchLink = useBranchLinkProps()
  const { notifyStepCompleted } = useDemoScenarioActions()
  const meta = renamedTo ? RENAMED_META : KIND_META[entry.kind]
  // Removed entities only exist on the base side; everything else shows the
  // branch-side (current) state.
  const fullState = (entry.kind === 'removed' ? entry.before : entry.after) ?? null
  const hasState = !!fullState && Object.keys(fullState).length > 0
  const fieldChanges = entry.field_changes ?? []
  const hasFieldChanges = fieldChanges.length > 0
  // Reviewer notes that are not changes — nothing the summary counts, so they
  // hang under the row rather than in it (tripl-kjhi.1, tripl-kjhi.9).
  const warnings = entry.warnings ?? []
  const title = eventTitle(entry)
  const path = entityPath(slug, entry)
  // A removed entity is gone from the branch — only main still has it. A renamed
  // one has not gone anywhere, but the id on a removed entry is the base-side
  // one, so main is still where that id resolves.
  const link = path ? branchLink(path, entry.kind === 'removed' ? null : branchId) : null
  // The row's own primary action. Gated on "has a branch-side id", not on the
  // kind: a rename is rendered by the removed entry, and that row IS the branch
  // copy the author wants to fix.
  const editableEntityId = !editable
    ? null
    : entry.kind === 'removed'
      ? (renamedTo ? renamedEntityId ?? null : null)
      : entry.entity_id ?? null
  const editPath = editableEntityId
    ? entityEditPath(slug, entry.entity_type, editableEntityId)
    : null
  const editLink = editPath ? branchLink(editPath, branchId) : null
  const blocked = revertBlockedBy && revertBlockedBy.length > 0 ? revertBlockedBy : null

  return (
    <div
      className="border-t"
      style={{
        borderColor: 'var(--border-subtle)',
        background: `color-mix(in oklab, var(--${meta.tone}) 6%, transparent)`,
      }}
    >
      {/* Toggle and Edit are siblings, not nested: a link inside a button is
          invalid markup, and the shortcut has to be visible without first
          performing the very click it saves. The coach mark still wraps the
          toggle alone, because expanding is what completes the demo step. */}
      <div className="flex items-stretch">
      <ScenarioCoachMark
        step="branches/review-diff"
        // The seeded diff carries exactly one modified event; only its row coaches.
        when={entry.kind === 'changed' && entry.name === SCENARIO_SEEDED.changedEventName}
      >
      <button
        type="button"
        onClick={() => {
          // Expanding the change IS reviewing the diff — before/after unfold.
          // Outside the updater: updaters must stay pure under StrictMode.
          if (!open) notifyStepCompleted('branches/review-diff')
          setOpen((value) => !value)
        }}
        aria-expanded={open}
        aria-controls={open ? detailId : undefined}
        className="flex min-w-0 flex-1 items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-[var(--surface-hover)]"
      >
        <ChevronRight
          className="size-3.5 shrink-0 transition-transform"
          style={{ color: 'var(--fg-faint)', transform: open ? 'rotate(90deg)' : 'none' }}
          aria-hidden="true"
        />
        <span
          className="w-4 shrink-0 text-center text-heading font-bold"
          style={{ color: `var(--${meta.tone})` }}
        >
          {meta.sym}
        </span>
        <span className="mono min-w-0 truncate text-body-sm text-fg">
          {entry.name}
        </span>
        {title ? (
          // Muted and after the scan name, not instead of it: the name is what
          // the merge pairs on and what the scanner reports (tripl-kjhi.3).
          <span className="min-w-0 truncate text-caption text-fg-tertiary">
            · {title}
          </span>
        ) : null}
        {/* min-w-0 for the same reason the entity name beside it has one: a
            flex item defaults to `min-width:auto` and refuses to shrink below
            its content, so an entry touching many fields stretched this row —
            and with it the panel and the page. `truncate` keeps the collapsed
            summary to one line; the full before/after is a click away. The
            summary names the fields that changed, so the cut falls after the
            difference, not inside a long quote (PL-9). Below `sm` only the
            count of changed fields fits. */}
        <span
          className="hidden min-w-0 flex-1 truncate text-right text-caption sm:inline text-fg-tertiary"
        >
          {renamedTo ? `→ ${renamedTo}` : changeSummary(entry)}
        </span>
        <span className="flex-1 sm:hidden" aria-hidden="true" />
        {!renamedTo && hasFieldChanges ? (
          <span className="shrink-0 text-caption tnum sm:hidden text-fg-tertiary">
            {fieldChanges.length === 1 ? '1 field' : `${fieldChanges.length} fields`}
          </span>
        ) : null}
        <Chip tone={meta.tone} size="xs">
          {meta.label}
        </Chip>
      </button>
      </ScenarioCoachMark>
      {editLink ? (
        <Link
          {...editLink}
          aria-label={`Edit ${renamedTo ?? entry.name}`}
          className="flex shrink-0 items-center gap-1 pl-1 pr-4 text-caption transition-colors hover:underline pointer-coarse:min-w-10 text-accent"
        >
          <Pencil className="size-3" aria-hidden="true" />
          Edit
        </Link>
      ) : null}
      </div>
      {warnings.length > 0 ? (
        // Indented to the entity name (chevron + gutter symbol + their gaps),
        // so the note reads as belonging to the row above it. One line per
        // warning: the backend writes each as its own sentence.
        <div className="flex flex-col gap-1 pb-2.5 pl-[70px] pr-4">
          {warnings.map((warning) => (
            <p
              key={warning}
              role="note"
              className="flex items-start gap-1.5 text-caption leading-snug text-warning"
            >
              <AlertTriangle className="mt-[1px] size-3 shrink-0" aria-hidden="true" />
              <span>{warning}</span>
            </p>
          ))}
        </div>
      ) : null}
      {open ? (
        <div
          id={detailId}
          className="flex flex-col gap-3 border-t px-4 py-3 border-border-subtle bg-surface"
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-caption text-fg-tertiary">
              {meta.label} {ENTITY_LABEL[entry.entity_type]}
              {entry.parent ? (
                <>
                  {' in '}
                  <span className="mono text-fg">
                    {entry.parent}
                  </span>
                </>
              ) : null}
            </p>
            <div className="flex shrink-0 items-center gap-3">
              {onRevert && !blocked && (
                <button
                  type="button"
                  disabled={reverting}
                  onClick={() => onRevert(entry)}
                  className={`flex items-center gap-1 text-caption hover:underline disabled:opacity-50 ${ROW_ACTION_TOUCH} text-fg-secondary`}
                >
                  <Undo2 className="size-3" aria-hidden="true" />
                  {renamedTo ? 'Undo this rename' : REVERT_LABEL[entry.kind]}
                </button>
              )}
              {link ? (
                <Link
                  {...link}
                  className={`flex items-center gap-1 text-caption hover:underline ${ROW_ACTION_TOUCH} text-accent`}
                >
                  {entry.kind === 'removed'
                    ? 'Open on main'
                    : `Open ${ENTITY_LABEL[entry.entity_type]}`}
                  <ArrowUpRight className="size-3" aria-hidden="true" />
                </Link>
              ) : null}
            </div>
          </div>
          {onRevert && blocked ? (
            <RevertBlockedNote
              slug={slug}
              branchId={branchId}
              entry={entry}
              among={blocked}
              editable={editable}
            />
          ) : null}
          {hasFieldChanges ? (
            <DetailSection title="Field changes">
              <PlanFieldChangeList
                changes={fieldChanges}
                renderAction={
                  onRevert
                    ? (change) => (
                        <button
                          type="button"
                          disabled={reverting}
                          onClick={() => onRevert(entry, change.field)}
                          aria-label={`Revert ${change.field}`}
                          className={`flex items-center gap-1 text-caption hover:underline disabled:opacity-50 ${ROW_ACTION_TOUCH} text-fg-secondary`}
                        >
                          <Undo2 className="size-3" aria-hidden="true" />
                          Revert
                        </button>
                      )
                    : undefined
                }
              />
            </DetailSection>
          ) : null}
          {/* One toggle in both states, so the full state closes again the way
              it opened. */}
          {hasState && entry.kind === 'changed' && hasFieldChanges ? (
            <button
              type="button"
              onClick={() => setStateOpen((open) => !open)}
              aria-expanded={stateOpen}
              aria-controls={stateOpen ? stateId : undefined}
              className={`flex w-fit items-center gap-1 text-caption hover:underline ${ROW_ACTION_TOUCH} text-fg-secondary`}
            >
              <ChevronRight
                className={`size-3 transition-transform ${stateOpen ? 'rotate-90' : ''}`}
                aria-hidden="true"
              />
              {stateOpen ? 'Hide' : 'Show'} full {ENTITY_LABEL[entry.entity_type]} (
              {Object.keys(fullState).length} properties)
            </button>
          ) : null}
          {hasState && (entry.kind !== 'changed' || !hasFieldChanges || stateOpen) ? (
            <DetailSection
              id={stateId}
              title={
                renamedTo
                  ? 'State before the rename'
                  : entry.kind === 'removed'
                    ? 'Removed state'
                    : 'Full state'
              }
            >
              <StateView state={fullState} />
            </DetailSection>
          ) : null}
          {!hasFieldChanges && !hasState ? (
            <p className="text-caption text-fg-tertiary">
              No further detail for this change.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

/** Why a removal cannot be reverted from here, and the rows to fix by hand.
 * The dialog that stood here used to offer "Try anyway" for a request it had
 * just said would be refused (PLAN-18). */
function RevertBlockedNote({
  slug,
  branchId,
  entry,
  among,
  editable,
}: {
  slug: string
  branchId: string
  entry: PlanDiffEntry
  among: PlanDiffEntry[]
  editable: boolean
}) {
  const branchLink = useBranchLinkProps()
  return (
    <div role="note" className="text-caption text-warning">
      <p className="flex items-start gap-1.5">
        <AlertTriangle className="mt-[2px] size-3 shrink-0" aria-hidden="true" />
        <span>
          Can’t revert: {among.length} rows on this branch carry {entry.name}’s scan identity, so
          it is ambiguous which one it was renamed into. Rename all but one of them by hand, then
          revert.
        </span>
      </p>
      <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-1 pl-[18px]">
        {among.map((row) => {
          const path =
            editable && row.entity_id ? entityEditPath(slug, row.entity_type, row.entity_id) : null
          return (
            <li key={row.name} className="mono">
              {path ? (
                <Link
                  {...branchLink(path, branchId)}
                  className="hover:underline text-accent"
                  aria-label={`Edit ${row.name}`}
                >
                  {row.name}
                </Link>
              ) : (
                <span className="text-fg">{row.name}</span>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function DetailSection({ id, title, children }: { id?: string; title: string; children: ReactNode }) {
  return (
    <div id={id}>
      <div
        className="mb-1.5 micro-label text-fg-tertiary"
      >
        {title}
      </div>
      {children}
    </div>
  )
}

function StateView({ state }: { state: Record<string, unknown> }) {
  const uid = useId()
  // Empty properties say nothing and used to take most of the rows ("sunset_at
  // ⌀", "superseded_by ⌀"); they are counted and shown on request (PL-12).
  const [showEmpty, setShowEmpty] = useState(false)
  const allKeys = Object.keys(state)
  const emptyKeys = allKeys.filter((key) => isEmptyStateValue(state[key]))
  const keys = showEmpty ? allKeys : allKeys.filter((key) => !isEmptyStateValue(state[key]))
  return (
    <>
    {/* Stacked below `sm`, key over value, so a long value is not squeezed
        into a column beside a truncated key. */}
    <dl className="grid grid-cols-1 gap-x-3 gap-y-1.5 sm:grid-cols-[minmax(0,140px)_1fr]">
      {keys.map((key) => (
        <Fragment key={key}>
          <dt
            id={`${uid}-${key}`}
            className="truncate text-caption text-fg-tertiary"
            title={key}
          >
            {stateKeyLabel(key)}
          </dt>
          <dd className="min-w-0 break-words">
            {/* Full state is the only thing an event *created* on the branch
                shows — the backend builds an added entry with no field_changes —
                so this is where a collection has to be readable. `table` is
                passed only here: the side-by-side before/after fallback would
                otherwise put two tables next to each other, in a column already
                300px narrower than the page. */}
            <DiffValue value={state[key]} table labelledBy={`${uid}-${key}`} />
          </dd>
        </Fragment>
      ))}
    </dl>
    {emptyKeys.length > 0 ? (
      <button
        type="button"
        onClick={() => setShowEmpty((v) => !v)}
        className={`mt-1.5 text-caption hover:underline ${ROW_ACTION_TOUCH} text-fg-tertiary`}
      >
        {showEmpty
          ? 'Hide empty properties'
          : `${emptyKeys.length === 1 ? '1 empty property' : `${emptyKeys.length} empty properties`} hidden · Show`}
      </button>
    ) : null}
    </>
  )
}

/** The machine's rows, one line, opened on request (tripl-kjhi.12). */
export function HousekeepingFold({ entries }: { entries: PlanDiffEntry[] }) {
  const [expanded, setExpanded] = useState(false)
  const listId = useId()
  return (
    <div className="border-t border-border-subtle">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        // Only while the list exists: an idref to a missing element is an
        // a11y error, which ChangeRow's toggle already avoids (PLAN-20).
        aria-controls={expanded ? listId : undefined}
        className="flex w-full items-center gap-1.5 px-4 py-2.5 text-left text-caption text-fg-tertiary"
      >
        <ChevronRight
          className="size-3 shrink-0 transition-transform"
          style={{ transform: expanded ? 'rotate(90deg)' : undefined }}
          aria-hidden
        />
        <span>{housekeepingLine(entries)}</span>
        <span className="ml-auto text-fg-tertiary">
          not counted
        </span>
      </button>
      {expanded ? (
        <ul id={listId} className="px-4 pb-2.5">
          {entries.map((entry) => (
            <li
              key={`${entry.entity_type}-${entry.parent ?? ''}-${entry.name}`}
              className="flex items-baseline gap-2 py-0.5 text-caption"
            >
              <span className="mono truncate text-fg-secondary">
                {entry.name}
              </span>
              <span className="shrink-0 text-fg-tertiary">
                {ENTITY_LABEL[entry.entity_type] ?? entry.entity_type} · {housekeepingReason(entry.housekeeping ?? '')}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
