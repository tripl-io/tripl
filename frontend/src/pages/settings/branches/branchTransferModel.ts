/**
 * "Move or copy to branch": which diff rows a selection sends, and how the
 * transfer endpoint's refusals read. Pure, like branchDiffModel.ts beside it.
 *
 * The server is authoritative about what a selection takes along (the dry
 * run lists it); this only makes the ticked rows a request, with both halves
 * of a rename ticked together the way the revert pairs them.
 */

import { ApiError } from '@/api/client'
import type {
  BranchTransferConflict,
  BranchTransferEntryRef,
  BranchTransferItem,
} from '@/api/planBranches'
import { getErrorMessage } from '@/lib/utils'
import type { PlanBranchDiffSummary, PlanDiffEntry } from '@/types'
import { diffRenames, entryKey, entryRowKey, revertOutcome } from './branchDiffModel'
import { ENTITY_LABEL } from './branchMeta'

/** A row the transfer can take: anything but housekeeping, which is the
 * machine's doing and not a change of the branch's. */
export function transferableEntry(entry: PlanDiffEntry): boolean {
  return !entry.housekeeping
}

/**
 * The diff entries a set of ticked rows (by `entryRowKey`) sends.
 *
 * A rename is two entries, a removal and an addition, and the transfer moves
 * them as one row, so ticking either ticks both. Paired two ways, because the
 * Changes list shows a rename two ways: the merge's pairing (`renames`, the
 * one row the list renders for it) and the revert's own (`revertOutcome`,
 * which a move uses to undo the rename on this branch). The server pairs once
 * more on its own and lists what it took in the dry run.
 */
export function selectionWithRenames(
  diff: PlanBranchDiffSummary | undefined,
  selected: ReadonlySet<string>,
): PlanDiffEntry[] {
  const entries = (diff?.entries ?? []).filter(transferableEntry)
  const keys = new Set(selected)
  for (const [removed, added] of transferRenamePairs(diff)) {
    if (keys.has(entryRowKey(removed)) || keys.has(entryRowKey(added))) {
      keys.add(entryRowKey(removed))
      keys.add(entryRowKey(added))
    }
  }
  return entries.filter((entry) => keys.has(entryRowKey(entry)))
}

/**
 * Each rename of the diff as its (removal, addition) halves, paired both
 * ways `selectionWithRenames` pairs them, once per pair.
 */
export function transferRenamePairs(
  diff: PlanBranchDiffSummary | undefined,
): [PlanDiffEntry, PlanDiffEntry][] {
  const entries = (diff?.entries ?? []).filter(transferableEntry)
  const halves = (removed: PlanDiffEntry, to: string) =>
    entries.find(
      (entry) =>
        entry.kind === 'added' &&
        entry.entity_type === removed.entity_type &&
        entry.parent === removed.parent &&
        entry.name === to,
    )
  const pairs = new Map<string, [PlanDiffEntry, PlanDiffEntry]>()
  const add = (removed: PlanDiffEntry, added: PlanDiffEntry) =>
    pairs.set(`${entryRowKey(removed)}\u0000${entryRowKey(added)}`, [removed, added])
  for (const rename of diffRenames(diff)) {
    const removed = entries.find(
      (entry) =>
        entry.kind === 'removed' &&
        entryKey(entry.entity_type, entry.parent, entry.name) ===
          entryKey(rename.entity_type, rename.parent ?? null, rename.removed_name),
    )
    const added = removed ? halves(removed, rename.added_name) : undefined
    if (removed && added) add(removed, added)
  }
  for (const entry of entries) {
    const outcome = revertOutcome(entries, entry)
    if (outcome.kind !== 'rename') continue
    const added = halves(entry, outcome.to)
    if (added) add(entry, added)
  }
  return [...pairs.values()]
}

/** A diff row or a transfer item, as far as counting changes needs. */
type CountedRow = Pick<PlanDiffEntry, 'entity_type' | 'name' | 'kind'> & {
  parent?: string | null
}

/**
 * How many changes a list of rows is, a rename counted once: the request and
 * the transfer's answer list a rename as its two halves, and the user ticked
 * one change.
 */
export function countTransferChanges(
  rows: readonly CountedRow[],
  renamePairs: readonly (readonly [PlanDiffEntry, PlanDiffEntry])[],
): number {
  const keyOf = (row: CountedRow) =>
    `${entryKey(row.entity_type, row.parent ?? null, row.name)}:${row.kind}`
  const present = new Set(rows.map(keyOf))
  const collapsed = renamePairs.filter(
    ([removed, added]) => present.has(keyOf(removed)) && present.has(keyOf(added)),
  ).length
  return rows.length - collapsed
}

/** The request's `entries`, with the id that tells namesakes apart. */
export function toTransferRefs(entries: readonly PlanDiffEntry[]): BranchTransferEntryRef[] {
  return entries.map((entry) => ({
    entity_type: entry.entity_type,
    name: entry.name,
    parent: entry.parent,
    entity_id: entry.entity_id ?? null,
  }))
}

function typeLabel(entityType: BranchTransferConflict['entity_type']): string {
  const label = ENTITY_LABEL[entityType] ?? entityType
  return label.charAt(0).toUpperCase() + label.slice(1)
}

/** One refused row as the dialog lists it: what, which field, and why. */
export function describeTransferConflict(conflict: BranchTransferConflict): string {
  const where = conflict.field ? ` · ${conflict.field}` : ''
  return `${typeLabel(conflict.entity_type)} ${conflict.name}${where}: ${conflict.message}`
}

/** One listed row of a dry run, with what it is needed by when it was carried. */
export function describeTransferItem(item: BranchTransferItem): string {
  const label = `${typeLabel(item.entity_type)} ${item.name}`
  return item.needed_by ? `${label} (needed by ${item.needed_by})` : label
}

function structuredDetail(error: unknown): Record<string, unknown> | null {
  if (error instanceof ApiError && error.detail && typeof error.detail === 'object') {
    return error.detail as Record<string, unknown>
  }
  return null
}

/** The refused rows of a 409 `transfer_conflicts`, or null for any other error. */
export function transferConflicts(error: unknown): BranchTransferConflict[] | null {
  const rows = structuredDetail(error)?.transfer_conflicts
  return Array.isArray(rows) && rows.length > 0 ? (rows as BranchTransferConflict[]) : null
}

export interface BaseMismatch {
  message: string
  /** The branches to run "Update from main" on, as the server names them. */
  behindBranchIds: string[]
}

/** The two branches were cut from different mains: what to update, or null. */
export function describeBaseMismatch(error: unknown): BaseMismatch | null {
  const detail = structuredDetail(error)
  if (!detail?.transfer_base_mismatch) return null
  const ids = Array.isArray(detail.behind_branch_ids)
    ? detail.behind_branch_ids.filter((id): id is string => typeof id === 'string')
    : []
  return {
    message: typeof detail.message === 'string' ? detail.message : getErrorMessage(error),
    behindBranchIds: ids,
  }
}

/** Any other refusal, in the words the server chose. */
export function transferErrorMessage(error: unknown): string {
  const detail = structuredDetail(error)
  if (detail && typeof detail.message === 'string') return detail.message
  return getErrorMessage(error)
}
