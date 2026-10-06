import { describe, expect, it } from 'vitest'

import { ApiError } from '@/api/client'
import type { PlanBranchDiffSummary, PlanDiffEntry } from '@/types'
import { entryRowKey } from './branchDiffModel'
import {
  countTransferChanges,
  describeBaseMismatch,
  describeTransferConflict,
  describeTransferItem,
  selectionWithRenames,
  toTransferRefs,
  transferableEntry,
  transferRenamePairs,
  transferConflicts,
  transferErrorMessage,
} from './branchTransferModel'

function entry(overrides: Partial<PlanDiffEntry>): PlanDiffEntry {
  return {
    entity_type: 'variable',
    kind: 'added',
    name: 'currency',
    parent: null,
    entity_id: null,
    changes: [],
    field_changes: [],
    before: null,
    after: null,
    warnings: [],
    housekeeping: null,
    ...overrides,
  } as PlanDiffEntry
}

function diff(entries: PlanDiffEntry[], renames: PlanBranchDiffSummary['renames'] = []) {
  return {
    entries,
    summary: { added: 0, removed: 0, changed: 0 },
    behind_base: false,
    renames,
  } as PlanBranchDiffSummary
}

function refusal(detail: Record<string, unknown>): ApiError {
  const error = new ApiError('409 Conflict', 409)
  error.detail = detail
  return error
}

describe('selectionWithRenames', () => {
  // The revert's own pairing: one branch row carries the removed row's
  // source_name under another name.
  const removed = entry({ kind: 'removed', name: 'currency', before: { source_name: 'S_CUR' } })
  const added = entry({ kind: 'added', name: 'currency_code', after: { source_name: 'S_CUR' } })
  const other = entry({ kind: 'changed', name: 'country' })

  it('ticks both halves of a rename when either is ticked', () => {
    const view = diff([removed, added, other])
    expect(selectionWithRenames(view, new Set([entryRowKey(removed)]))).toEqual([removed, added])
    expect(selectionWithRenames(view, new Set([entryRowKey(added)]))).toEqual([removed, added])
    expect(selectionWithRenames(view, new Set([entryRowKey(other)]))).toEqual([other])
  })

  it('pairs the halves the merge pairs, too', () => {
    const plainRemoved = entry({ kind: 'removed', name: 'a' })
    const plainAdded = entry({ kind: 'added', name: 'b' })
    const view = diff([plainRemoved, plainAdded], [
      { entity_type: 'variable', parent: null, removed_name: 'a', added_name: 'b' },
    ])
    expect(selectionWithRenames(view, new Set([entryRowKey(plainRemoved)]))).toEqual([
      plainRemoved,
      plainAdded,
    ])
  })

  it('never sends housekeeping', () => {
    const chore = entry({ kind: 'removed', name: 'legacy', housekeeping: 'Already gone on main' })
    expect(transferableEntry(chore)).toBe(false)
    expect(selectionWithRenames(diff([chore]), new Set([entryRowKey(chore)]))).toEqual([])
  })
})

describe('countTransferChanges', () => {
  it('counts a rename once, however it is paired', () => {
    const removed = entry({ kind: 'removed', name: 'a' })
    const added = entry({ kind: 'added', name: 'b' })
    const other = entry({ kind: 'changed', name: 'country' })
    const view = diff([removed, added, other], [
      { entity_type: 'variable', parent: null, removed_name: 'a', added_name: 'b' },
    ])
    const pairs = transferRenamePairs(view)
    expect(pairs).toEqual([[removed, added]])
    expect(countTransferChanges([removed, added], pairs)).toBe(1)
    expect(countTransferChanges([removed, added, other], pairs)).toBe(2)
    // The transfer's answer lists the halves as items of the same shape.
    const items = [removed, added].map((row) => ({ ...row, needed_by: null }))
    expect(countTransferChanges(items, pairs)).toBe(1)
  })

  it('lists a pair both pairings find only once', () => {
    const removed = entry({ kind: 'removed', name: 'a', before: { source_name: 'S' } })
    const added = entry({ kind: 'added', name: 'b', after: { source_name: 'S' } })
    const view = diff([removed, added], [
      { entity_type: 'variable', parent: null, removed_name: 'a', added_name: 'b' },
    ])
    expect(transferRenamePairs(view)).toHaveLength(1)
  })
})

describe('toTransferRefs', () => {
  it('keeps the id that tells namesakes apart', () => {
    const event = entry({ entity_type: 'event', name: 'buy', parent: 'track', entity_id: 'e-1' })
    expect(toTransferRefs([event])).toEqual([
      { entity_type: 'event', name: 'buy', parent: 'track', entity_id: 'e-1' },
    ])
  })
})

describe('refusal text', () => {
  it('names the row, the field and why', () => {
    expect(
      describeTransferConflict({
        entity_type: 'variable',
        name: 'currency',
        parent: null,
        field: 'description',
        reason: 'target_changed',
        message: "'currency' changed 'description' on 'TASK-2' too.",
      }),
    ).toBe("Property currency · description: 'currency' changed 'description' on 'TASK-2' too.")
    expect(
      describeTransferItem({
        entity_type: 'field_definition',
        name: 'plan',
        parent: 'track',
        entity_id: null,
        kind: 'added',
        needed_by: 'signup',
      }),
    ).toBe('Field plan (needed by signup)')
  })

  it('reads the three 409 shapes', () => {
    const conflicts = refusal({
      transfer_conflicts: [
        {
          entity_type: 'event',
          name: 'signup',
          parent: 'track',
          field: null,
          reason: 'has_discussion',
          message: 'Copy it instead',
        },
      ],
      message: 'Copy it instead',
    })
    expect(transferConflicts(conflicts)?.[0]?.reason).toBe('has_discussion')
    expect(describeBaseMismatch(conflicts)).toBeNull()

    const mismatch = refusal({
      transfer_base_mismatch: true,
      message: "Run Update from main on 'feature', then retry.",
      behind_branch_ids: ['b-1'],
    })
    expect(describeBaseMismatch(mismatch)).toEqual({
      message: "Run Update from main on 'feature', then retry.",
      behindBranchIds: ['b-1'],
    })
    expect(transferConflicts(mismatch)).toBeNull()

    const other = refusal({ transfer_constraint_violation: true, message: 'Rename it first.' })
    expect(transferErrorMessage(other)).toBe('Rename it first.')
  })
})
