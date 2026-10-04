/**
 * The Branches tab's pure diff logic: how a branch diff is counted, paired,
 * reverted and merged, and how a refused action is worded.
 *
 * Lifted out of BranchesTab.tsx (PLAN-22), where it was reachable only by
 * mounting the whole tab behind a router and five mocked APIs. Nothing here
 * touches React, so branchDiffModel.test.ts calls it directly.
 */

import { ApiError } from '@/api/client'
import { countOf } from '@/lib/plural'
import { getErrorMessage } from '@/lib/utils'
import type {
  EntityChangeCount,
  PlanBranchConflictEntity,
  PlanBranchConflicts,
  PlanBranchDiffSummary,
  PlanDiffEntityType,
  PlanDiffEntry,
  PlanDiffKind,
  PlanDiffRename,
} from '@/types'
import { ENTITY_LABEL, stateKeyLabel } from './branchMeta'

/**
 * The two diff entries the merge will treat as one renamed row, as the backend
 * states them in `PlanBranchDiff.renames`.
 *
 * The diff keys entities by name, so a rename arrives split into a removal of
 * the old name and an addition of the new one. Only the backend can join them
 * back up: the rule pairs the two by `source_name` AND consults main, and this
 * screen holds a base-vs-branch diff. It used to guess anyway — see
 * `variablesDeletedByMerge` — and the guess was wrong in the direction of
 * frightening the reviewer.
 *
 * The field is optional on the response from an older instance, and an empty
 * pairing reads as "no removal here is a rename" — the cautious direction.
 */
export function diffRenames(diff: PlanBranchDiffSummary | undefined): PlanDiffRename[] {
  return diff?.renames ?? []
}

/** A diff's counts as the Changes list renders them: a rename is ONE row. */
export interface PairedDiffCounts {
  added: number
  changed: number
  removed: number
  renamed: number
  /** Rows the Changes panel shows — which is the branch's "ahead" distance. */
  total: number
}

/** Where the selected branch's diff request stands, so the detail pane can
 * tell a diff that is still loading from one that is empty.
 * Mirrors TanStack Query's `status`; `retry` refetches after an error. */
export interface DiffLoad {
  status: 'pending' | 'error' | 'success'
  error: unknown
  retry: () => void
}

/**
 * The header strip, the list row's ahead badge and the Changes panel subtitle
 * all count one diff, so they have to count it one way.
 *
 * Dropping the paired addition from the rendered list while the strip went on
 * reading `summary` straight from the backend made a branch whose only change
 * is one rename say three things at once: "↑2" in the list row, "+1 added · ~0
 * modified · −1 removed" in the strip, and "1 change" over a single Renamed row
 * immediately below. That "−1 removed" is exactly the false deletion signal
 * RENAMED_META was added to remove, so it is the strip that was wrong.
 *
 * Subtracting the pair count cannot underflow. `snapshot_rename_pairs` emits a
 * pair only when the old key is absent from the branch (so the diff really
 * carries that `removed` entry) and the new key is absent from the base (so it
 * really carries that `added` one), the pairs are one-to-one — `pair_renames`
 * keys its result by the old key, and the new keys come from an identity map
 * `_sole_key_by_identity` already proved singular — and `summary` is a plain
 * per-kind tally of `entries` (`_summary_counts`). So each pair cancels exactly
 * one `added` and one `removed`, and the total matches `visibleEntries.length`.
 *
 * Frontend-only on purpose: the backend `summary` stays the raw entry tally,
 * which is what its other callers read it as.
 */
export function pairedDiffCounts(diff: PlanBranchDiffSummary | undefined): PairedDiffCounts {
  const summary = diff?.summary ?? { added: 0, removed: 0, changed: 0 }
  const renamed = diffRenames(diff).length
  return {
    added: summary.added - renamed,
    changed: summary.changed,
    removed: summary.removed - renamed,
    renamed,
    total: summary.added + summary.changed + summary.removed - renamed,
  }
}

/** How a diff entry and a rename half address the same change — the same triple
 * `BranchRevertRequest` uses, so a row and its pairing always agree. */
export function entryKey(
  entityType: PlanDiffEntityType,
  parent: string | null,
  name: string,
): string {
  // JSON and not a separator: an event name has no character class at all
  // (``EventUpdate`` bounds only its length), so any joiner could occur inside
  // one and fuse two different changes into one key.
  return JSON.stringify([entityType, parent, name])
}

/** A stable React key for one diff row. Not the list index: a revert removes a
 * row, and index keys would then shift every later row onto a different
 * component and collapse whatever the reviewer had expanded (PLAN-15). The
 * kind is part of it because a removal and an addition can share a name. */
export function entryRowKey(entry: PlanDiffEntry): string {
  return `${entryKey(entry.entity_type, entry.parent, entry.name)}:${entry.kind}`
}

/** The scan identity a plan snapshot records on a row, or null where it records
 * none. `_public_state` keeps `source_name` on a diff entry's `before`/`after`
 * — it is neither `id` nor a `*_id` — so both halves of a rename carry it. */
function sourceNameOf(state: Record<string, unknown> | null | undefined): string | null {
  const value = state?.source_name
  return typeof value === 'string' && value !== '' ? value : null
}

/** What reverting one diff entry will actually do to the branch. `among` holds
 * the candidate rows themselves, so the refusal can link to each of them. */
export type RevertOutcome =
  | { kind: 'restore' }
  | { kind: 'rename'; to: string }
  | { kind: 'ambiguous'; among: PlanDiffEntry[] }

/**
 * Answer the question the REVERT asks, which is not the question `renames`
 * answers.
 *
 * `renames` is the merge's pairing and consults main, so a branch rename a->b
 * is not paired there once main has independently grown its own b — the merge
 * would be putting two rows on one name. The revert endpoint asks something
 * strictly narrower and deliberately main-free: `_row_renamed_from` looks for a
 * branch row still carrying the removed row's `source_name` ("a rename main
 * happens to have raced is still a rename here") and moves the name back onto
 * it. So on exactly that branch the dialog used to promise a restore and the
 * button performed a rename: the addition the reviewer was looking at vanished
 * and nothing came back.
 *
 * That narrower question is answerable here, because the diff entries carry
 * `source_name` on both sides. This mirrors the endpoint's rule: one candidate
 * moves the name, two or more is ambiguous and refused rather than guessed at.
 *
 * It scans the diff entries and not the whole branch, so it can only see rows
 * that differ from the base. Live rows cannot hold one identity twice — events
 * carry `uq_event_scan_identity` per event type, variables
 * `uq_variable_project_source_name` — but the base is a snapshot payload, data
 * the key never checked, and one taken before the key can name an identity on
 * two rows. A branch row identical to such a base row is then invisible here
 * while the endpoint's query sees it. The gap is a base-side ambiguity the
 * endpoint reports as a 409 either way; this is the dialog's best honest
 * answer, not a second copy of the endpoint.
 */
export function revertOutcome(entries: PlanDiffEntry[], entry: PlanDiffEntry): RevertOutcome {
  // Only a removal can be a rename in disguise, and only for the two kinds
  // `_row_renamed_from` dispatches on — the only two `build_plan_snapshot`
  // records a `source_name` for. For everything else a removal is a removal.
  const renameable = entry.entity_type === 'variable' || entry.entity_type === 'event'
  if (entry.kind !== 'removed' || !renameable) return { kind: 'restore' }
  // "A base row with no source_name identifies nothing and falls through to the
  // plain rebuild" — `_row_renamed_from` returns before it queries anything.
  const sourceName = sourceNameOf(entry.before)
  if (sourceName === null) return { kind: 'restore' }
  // Scoped by parent like the endpoint's event query is scoped by event type:
  // two events under different types may share a `source_name`, and only one
  // under THIS type can be the row that moved.
  const carriers = entries.filter(
    (row) =>
      row.kind !== 'removed' &&
      row.entity_type === entry.entity_type &&
      row.parent === entry.parent &&
      sourceNameOf(row.after) === sourceName,
  )
  const [onlyCarrier] = carriers
  if (!onlyCarrier) return { kind: 'restore' }
  if (carriers.length === 1) return { kind: 'rename', to: onlyCarrier.name }
  return {
    kind: 'ambiguous',
    among: [...carriers].sort((a, b) => a.name.localeCompare(b.name)),
  }
}

/** The three fields `confirm` needs; built away from the component so each
 * wording can be read beside the outcome that earns it. */
export interface ConfirmPrompt {
  title: string
  message: string
  confirmLabel: string
  variant: 'danger' | 'primary'
}

// Every revert restores the branch to its base state and leaves main alone; the
// wording changes because discarding an addition, undoing an edit and bringing
// back a deletion read as three different acts to the reviewer.
const REVERT_PROMPT: Record<PlanDiffKind, (name: string) => string> = {
  added: (name) => `Discard ${name}? This branch added it — it will be deleted from the branch.`,
  changed: (name) => `Revert every change to ${name}, back to the state it had when this branch was opened?`,
  removed: (name) => `Restore ${name} on this branch? It comes back as it was when the branch was opened; its photos are not restored.`,
}

export function fieldRevertPrompt(entry: PlanDiffEntry, field: string): ConfirmPrompt {
  return {
    title: 'Revert field',
    message: `Revert the change to "${field}" on ${entry.name}, back to the value it had when this branch was opened? Main is untouched.`,
    confirmLabel: 'Revert',
    variant: 'danger',
  }
}

/**
 * The confirm for a whole-entry revert. There is none for an ambiguous rename:
 * `_row_renamed_from` answers that with a 409 rather than rename a sibling the
 * reviewer never looked at, so the row offers no revert at all and says why
 * instead of asking consent for a request it knows will fail (PLAN-18).
 */
export function entryRevertPrompt(
  entry: PlanDiffEntry,
  outcome: Exclude<RevertOutcome, { kind: 'ambiguous' }>,
): ConfirmPrompt {
  if (outcome.kind === 'rename') {
    // Undoing a rename moves the name back onto the row that is still there,
    // so it promises none of the loss the plain "Restore" wording warns
    // about — and the reviewer must not be told to expect any.
    return {
      title: 'Undo rename',
      message: `Undo the rename of ${entry.name} to ${outcome.to}? The row stays on this branch and takes its old name back; its documented values and history are untouched. Main is untouched.`,
      confirmLabel: 'Undo rename',
      variant: 'danger',
    }
  }
  return {
    title: 'Revert change',
    message: `${REVERT_PROMPT[entry.kind](entry.name)} Main is untouched.`,
    confirmLabel: entry.kind === 'removed' ? 'Restore' : 'Revert',
    variant: 'danger',
  }
}

/**
 * The variables a merge really deletes from main: the ones the branch removed,
 * minus the ones it merely renamed.
 *
 * A rename is paired away by `_plan_branch_renames.pair_renames`, which renames
 * main's row in place instead of replacing it — so the id survives, and the
 * observed values, per-event overrides and drift history hanging off that id
 * survive with it. Warning about those would scare a reviewer out of a merge
 * that deletes nothing.
 *
 * This used to re-derive the pairing here by tallying `source_name` across the
 * removals and the additions, and it could not be made correct: the real rule
 * also refuses a move onto a name a STAYING MAIN row holds, and this diff
 * compares the base with the branch. The stand-in was to warn about every
 * removal whenever the branch was behind its base — safe, and wrong often
 * enough to be noise. The backend now states the pairing it will actually
 * perform, so this reads it.
 *
 * An empty pairing means "no removal here is a rename", which is also what a
 * response without the field says. That errs towards warning, which is the
 * direction a warning about deleted history should err in.
 */
export function variablesDeletedByMerge(
  entries: PlanDiffEntry[],
  renames: PlanDiffRename[],
): string[] {
  const paired = new Set(
    renames.filter((r) => r.entity_type === 'variable').map((r) => r.removed_name),
  )
  return entries
    .filter((e) => e.entity_type === 'variable' && e.kind === 'removed' && !paired.has(e.name))
    .map((e) => e.name)
}

/** One diff, split into what the Changes panel shows and how. */
export interface DiffView {
  /** Author's rows, with each rename's paired addition folded into its removal. */
  visibleEntries: PlanDiffEntry[]
  /** Machine removals, folded into one line below the list. */
  housekeepingEntries: PlanDiffEntry[]
  /** entryKey of a paired removal -> the name the row took on the branch. */
  renamedTo: Map<string, string>
  /** entryKey of a paired removal -> the branch-side id of the renamed row. */
  renamedEntityId: Map<string, string | null>
  /** Variables the merge deletes from main, with their history. */
  removedVariables: string[]
}

export function diffView(diff: PlanBranchDiffSummary | undefined): DiffView {
  const entries = diff?.entries ?? []
  const renames = diffRenames(diff)
  // A rename's two entries, joined back up by the backend: the removal carries
  // the row (and the revert that undoes the rename), so it is the half that
  // stays, wearing the new name; the addition is dropped rather than shown as an
  // unrelated creation of a row that already existed.
  const renamedTo = new Map(
    renames.map((r) => [entryKey(r.entity_type, r.parent, r.removed_name), r.added_name] as const),
  )
  const pairedAdditions = new Set(
    renames.map((r) => entryKey(r.entity_type, r.parent, r.added_name)),
  )
  // The addition is dropped from the list, but its id is the only branch-side
  // one a renamed row has — the removal it is paired with carries the base-side
  // id. Keep it so the row's Edit action edits the branch copy, not main's.
  const additionIds = new Map(
    entries
      .filter((entry) => entry.kind === 'added')
      .map(
        (entry) =>
          [entryKey(entry.entity_type, entry.parent, entry.name), entry.entity_id ?? null] as const,
      ),
  )
  const renamedEntityId = new Map(
    renames.map(
      (r) =>
        [
          entryKey(r.entity_type, r.parent, r.removed_name),
          additionIds.get(entryKey(r.entity_type, r.parent, r.added_name)) ?? null,
        ] as const,
    ),
  )
  const housekeepingEntries = entries.filter((entry) => Boolean(entry.housekeeping))
  const visibleEntries = entries.filter(
    (entry) =>
      !entry.housekeeping &&
      (entry.kind !== 'added' ||
        !pairedAdditions.has(entryKey(entry.entity_type, entry.parent, entry.name))),
  )
  // A variable removed relative to the branch base and not paired with an
  // addition is an intentional deletion; warn because its observed values,
  // overrides and drift history cascade. A rename is paired away — it keeps
  // all three. Housekeeping rows are not the author's deletions: a removal
  // main has already made is nothing the merge does, and a retired scan
  // variable nobody bound, documented or referenced has none of the three
  // things this dialog exists to protect — so neither is warned about, which
  // keeps the dialog consistent with the counts.
  const removedVariables = variablesDeletedByMerge(
    entries.filter((entry) => !entry.housekeeping),
    renames,
  )
  return { visibleEntries, housekeepingEntries, renamedTo, renamedEntityId, removedVariables }
}

/**
 * The confirm every merge asks for (PLAN-8). A merge rewrites production's plan
 * and the UI cannot undo it, so it is never one click — and the question names
 * what is about to land, in the same paired counts the strip shows. The
 * variable-deletion warning and the behind-main note ride in the same dialog
 * rather than as a second one. The behind-main note only appears when main's
 * newer changes overlap this branch's: a main that merely moved on does not
 * stop the merge, and warning about it on every branch was noise (PL-8).
 */
export function mergePrompt(
  counts: PairedDiffCounts,
  removedVariables: string[],
  behindBase: boolean,
  /** What lands, by name; the first five are listed (PL-29). */
  names: readonly string[] = [],
  /** Fields changed both here and on main since the branch opened (PL-8). */
  unresolvedConflicts = 0,
): ConfirmPrompt {
  // Words, not git's "+0 ~1 −0" tally, which needed decoding (PL-29).
  const kinds = [
    counts.changed > 0 ? `${counts.changed} modified` : null,
    counts.added > 0 ? `${counts.added} added` : null,
    counts.removed > 0 ? `${counts.removed} removed` : null,
    counts.renamed > 0 ? `${counts.renamed} renamed` : null,
  ].filter((part): part is string => part !== null)
  const lines = [
    `Merge ${countOf(counts.total, 'change', 'changes')} into main${kinds.length > 0 ? ` (${kinds.join(', ')})` : ''}?`,
  ]
  if (names.length > 0) {
    const more = names.length > 5 ? ` and ${names.length - 5} more` : ''
    lines.push(`Lands: ${names.slice(0, 5).join(', ')}${more}.`)
  }
  if (removedVariables.length > 0) {
    const shown = removedVariables.slice(0, 8).join(', ')
    const more = removedVariables.length > 8 ? ` and ${removedVariables.length - 8} more` : ''
    lines.push(
      `Merging removes ${countOf(removedVariables.length, 'property', 'properties')} from main: ${shown}${more}. Their documented values, per-event overrides and drift history are deleted with them.`,
    )
  }
  if (behindBase && unresolvedConflicts > 0) {
    lines.push(
      `Main has moved on since this branch was created, and ${countOf(unresolvedConflicts, 'field you changed was', 'fields you changed were')} also changed there. Pick the value to keep for each in the Conflicts panel below, or update from main, before merging.`,
    )
  }
  lines.push(
    'The changes become part of the live tracking plan for everyone. To undo them later, open a new branch that reverts them.',
  )
  const deletes = removedVariables.length > 0
  return {
    title: deletes ? 'Merge deletes properties from main' : 'Merge to main',
    message: lines.join(' '),
    confirmLabel: deletes ? 'Merge anyway' : 'Merge',
    variant: deletes ? 'danger' : 'primary',
  }
}

export function diffEntryDetail(entry: PlanDiffEntry): string {
  if (entry.changes.length > 0) return entry.changes.join(', ')
  return entry.parent ? `${entry.entity_type} · ${entry.parent}` : entry.entity_type
}

function structuredDetail(error: unknown): Record<string, unknown> | null {
  if (error instanceof ApiError && error.detail && typeof error.detail === 'object') {
    return error.detail as Record<string, unknown>
  }
  return null
}

/** A merge the gate refused over field conflicts, which the Conflicts panel must
 * then be showing — so it is refetched rather than left on its cached answer. */
export function isConflictRefusal(error: unknown): boolean {
  const detail = structuredDetail(error)
  return Boolean(detail && (detail.unresolved_field_conflicts || detail.conflicts))
}

export const MERGE_BLOCKED_BY_MAIN =
  'Merge blocked: main changed the same entities. Update the branch from main, then merge.'

/** Update from main cannot help a branch whose base predates complete
 * snapshots: there is no base to merge three ways against (PL-8). */
export const INCOMPLETE_BASE_MESSAGE =
  'This branch predates the complete merge baseline, so updating it from main cannot help. Copy your changes to a new branch.'

/** Human-readable message for a failed transition/merge, decoding the merge
 * gate's structured 409 payloads where the generic message would only say
 * "409 Conflict". */
export function describeBranchActionError(error: unknown): string {
  const detail = structuredDetail(error)
  if (detail) {
    const quota = detail.insufficient_approvals as
      | { required?: number; current?: number; stale?: number }
      | undefined
    if (quota) {
      const stale =
        (quota.stale ?? 0) > 0
          ? ` ${quota.stale} approval(s) went stale after later edits — re-approve.`
          : ''
      return `Not enough approvals to merge: ${quota.current ?? 0} of ${quota.required ?? 0} required.${stale}`
    }
    if (detail.missing_owner_approvals) {
      return 'Merge blocked: owners of the touched event types have not approved.'
    }
    // "Recreate the branch" meant redoing the work; the branch can take
    // main's changes in place now (PL-8).
    if (detail.branch_behind_base || (detail.conflicts && !detail.unresolved_field_conflicts)) {
      return MERGE_BLOCKED_BY_MAIN
    }
    if (detail.unresolved_field_conflicts) {
      return 'Merge blocked: resolve the field conflicts below first.'
    }
    if (detail.incomplete_base_snapshot) {
      return INCOMPLETE_BASE_MESSAGE
    }
    // LAST, so the five hand-written wordings above still win for the shapes
    // that have them, and this only catches what none of them names. Every
    // structured 409 the merge gate raises carries its own instruction in
    // `message`, and api/client.ts promotes only a STRING `detail` into
    // ApiError.message — an object one is parked on `error.detail` and the
    // message falls back to the literal "409 Conflict". So an undecoded payload
    // reaches the reviewer as a status line with no instruction at all: that is
    // what `_commit_merged_plan`'s new `merge_constraint_violation` would have
    // done, and what the pre-existing `incomplete_base_snapshot`
    // has been doing all along — verified against plan_branch_merge_service.py,
    // where both raise `{flag: True, "message": ...}` and neither has an arm
    // here.
    if (typeof detail.message === 'string' && detail.message.trim()) {
      return detail.message
    }
  }
  return getErrorMessage(error)
}

/** The reasons the backend stamps on `PlanDiffEntry.housekeeping`
 * (`services/_plan_diff_housekeeping.py`), and how each reads as a count:
 * "7 unused scan variables retired". */
/** One housekeeping reason as a row says it, singular (the server's text otherwise). */
export function housekeepingReason(reason: string): string {
  return HOUSEKEEPING_WORDING[reason]?.[0] ?? reason
}

const HOUSEKEEPING_WORDING: Record<string, [string, string]> = {
  'unused scan variable retired': ['unused scan property retired', 'unused scan properties retired'],
  'already removed on main': ['removal already made on main', 'removals already made on main'],
}

export function housekeepingLine(entries: PlanDiffEntry[]): string {
  const byReason = new Map<string, number>()
  for (const entry of entries) {
    const reason = entry.housekeeping ?? ''
    byReason.set(reason, (byReason.get(reason) ?? 0) + 1)
  }
  return [...byReason]
    .map(([reason, count]) => {
      const wording = HOUSEKEEPING_WORDING[reason]
      return wording ? countOf(count, wording[0], wording[1]) : `${count} × ${reason}`
    })
    .join(' · ')
}

/** Parse the merge policy's "Required approvals" input: a whole number from 0
 * to 100 (the backend's bound), or null. `parseInt` used to accept "1.5" as 1
 * and "150" as 150 and send them (PLAN-21). */
export function parseMinApprovals(raw: string): number | null {
  const trimmed = raw.trim()
  if (!/^\d+$/.test(trimmed)) return null
  const value = Number(trimmed)
  return value >= 0 && value <= 100 ? value : null
}

/** A value short enough to print inline in a collapsed row: a scalar of at
 * most 16 characters without spaces ("live", "enum", 3). */
function shortScalar(value: unknown): string | null {
  if (value === null || value === undefined) return '∅'
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (typeof value === 'string' && value.length <= 16 && !/\s/.test(value)) return value || '∅'
  return null
}

/**
 * The collapsed change row's one-line summary, built from the field names
 * rather than the backend's quoted before/after strings: two long quotes with
 * a shared prefix truncated before the actual difference (PL-9). Short scalars
 * read "Status live → deprecated", everything else "Description edited", at
 * most three fields plus "+N more". Falls back to the backend's own text when
 * the entry has no field changes (a new event, a removal).
 */
export function changeSummary(entry: PlanDiffEntry): string {
  const changes = entry.field_changes ?? []
  if (changes.length === 0) return diffEntryDetail(entry)
  const shown = changes.slice(0, 3).map((change) => {
    const before = shortScalar(change.before)
    const after = shortScalar(change.after)
    // "Metric breakdowns edited", not "metric_breakdown_columns edited" (PL-12).
    const label = stateKeyLabel(change.field)
    return before !== null && after !== null
      ? `${label} ${before} → ${after}`
      : `${label} edited`
  })
  const more = changes.length > 3 ? ` +${changes.length - 3} more` : ''
  return `${shown.join(', ')}${more}`
}

// ---------------------------------------------------------------------------
// Update from main (PL-8)
// ---------------------------------------------------------------------------

/** The header note under the diff counts once main has moved on: neutral when
 * nothing overlaps (or before the overlap check has answered), amber when it
 * does, when the merge would refuse, or when the branch cannot be updated. */
export type BehindNote =
  | { kind: 'none' }
  /** Main moved on; the overlap check has not answered (yet). Never "safe". */
  | { kind: 'moved' }
  | { kind: 'safe' }
  | { kind: 'overlap'; count: number }
  | { kind: 'blocked' }
  /** The base predates complete merge baselines: Update from main cannot run. */
  | { kind: 'legacy' }

export function behindNote(
  conflicts: PlanBranchConflicts | undefined,
  /** The diff's `behind_base`, for a conflicts answer from an older instance
   * that carries no `behind`. */
  diffBehind: boolean,
): BehindNote {
  const behind = conflicts?.behind ?? diffBehind
  if (!behind) return { kind: 'none' }
  // Loading, failed, or an older instance that cannot say whether anything
  // overlaps: "safe to merge" is only ever said on a confirmed answer.
  if (!conflicts || conflicts.overlap_count === undefined || conflicts.merge_blocked === undefined) {
    const overlap = conflicts?.entities.length ?? 0
    return overlap > 0 ? { kind: 'overlap', count: overlap } : { kind: 'moved' }
  }
  if (conflicts.updatable === false) return { kind: 'legacy' }
  if (conflicts.overlap_count > 0) return { kind: 'overlap', count: conflicts.overlap_count }
  if (conflicts.merge_blocked) return { kind: 'blocked' }
  return { kind: 'safe' }
}

/** "Events: 3 changed, 1 added" — one line per entity type main touched,
 * zero kinds left out, types with nothing left out entirely. */
export function entityChangeLines(counts: readonly EntityChangeCount[]): string[] {
  return counts.flatMap((row) => {
    const parts = [
      row.changed > 0 ? `${row.changed} changed` : null,
      row.added > 0 ? `${row.added} added` : null,
      row.removed > 0 ? `${row.removed} removed` : null,
      row.renamed > 0 ? `${row.renamed} renamed` : null,
    ].filter((part): part is string => part !== null)
    if (parts.length === 0) return []
    return [`${ENTITY_PLURAL_LABEL[row.entity_type] ?? row.entity_type}: ${parts.join(', ')}`]
  })
}

/** The total of an update's counts, for "N changes brought in". */
export function entityChangeTotal(counts: readonly EntityChangeCount[]): number {
  return counts.reduce((sum, row) => sum + row.added + row.changed + row.removed + row.renamed, 0)
}

const ENTITY_PLURAL_LABEL: Record<PlanDiffEntityType, string> = {
  event_type: 'Event types',
  field_definition: 'Fields',
  event: 'Events',
  variable: 'Properties',
  meta_field: 'Meta fields',
  relation: 'Relations',
}

/** The singular label with a capital, for a conflict group's heading. */
export function entityTypeTitle(type: string): string {
  const label = (ENTITY_LABEL as Record<string, string | undefined>)[type]
  return label ? label.charAt(0).toUpperCase() + label.slice(1) : type
}

/** The 409 the update answers with while an overlap has no choice: the
 * conflicts as the server saw them, to re-render from. */
export function unresolvedUpdateConflicts(error: unknown): PlanBranchConflicts | null {
  const detail = structuredDetail(error)
  if (!detail || !detail.unresolved_conflicts) return null
  const conflicts = detail.conflicts as PlanBranchConflicts | undefined
  return conflicts && Array.isArray(conflicts.entities) ? conflicts : null
}

/** The update stopped on something no choice settles (the preview's
 * `blockers`, as the refusal carries them). */
export function updateBlockedMessage(error: unknown): string | null {
  const detail = structuredDetail(error)
  const blockers = detail?.update_blocked
  if (!Array.isArray(blockers) || blockers.length === 0) return null
  const messages = blockers
    .map((blocker) =>
      blocker && typeof blocker === 'object' && typeof (blocker as { message?: unknown }).message === 'string'
        ? (blocker as { message: string }).message
        : null,
    )
    .filter((message): message is string => message !== null)
  return messages.length > 0 ? messages.join(' ') : null
}

/** Main changed between the preview and the click. */
export function isMainMovedRefusal(error: unknown): boolean {
  return Boolean(structuredDetail(error)?.main_moved)
}

/** Every other refusal of the update, in words. */
export function describeUpdateFromMainError(error: unknown): string {
  const detail = structuredDetail(error)
  if (detail) {
    if (detail.unresolved_conflicts) return 'Choose a side for every overlap first.'
    if (detail.main_moved) return 'Main changed again — review the new changes.'
    if (detail.incomplete_base_snapshot) return INCOMPLETE_BASE_MESSAGE
    const blocked = updateBlockedMessage(error)
    if (blocked) return blocked
    if (detail.update_constraint_violation) {
      return typeof detail.message === 'string' && detail.message.trim()
        ? detail.message
        : 'The update would break a uniqueness rule on this branch — most often two rows with the same name. Rename the clashing entity and try again.'
    }
    if (typeof detail.message === 'string' && detail.message.trim()) return detail.message
  }
  return getErrorMessage(error)
}

/** The key a choice is held under while the update dialog is open. */
export function conflictChoiceKey(entity: PlanBranchConflictEntity, field: string): string {
  return `${entity.entity_type}\u0000${entity.name}\u0000${field}`
}
