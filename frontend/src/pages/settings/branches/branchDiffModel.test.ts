import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api/client'
import type {
  PlanBranchConflictEntity,
  PlanBranchConflicts,
  PlanBranchDiffSummary,
  PlanDiffEntry,
} from '@/types'
import {
  INCOMPLETE_BASE_MESSAGE,
  MERGE_BLOCKED_BY_MAIN,
  behindNote,
  changeSummary,
  conflictChoiceKey,
  describeBranchActionError,
  describeUpdateFromMainError,
  diffView,
  entityChangeLines,
  entityChangeTotal,
  isMainMovedRefusal,
  unresolvedUpdateConflicts,
  updateBlockedMessage,
  entryRowKey,
  housekeepingLine,
  isConflictRefusal,
  mergePrompt,
  pairedDiffCounts,
  parseMinApprovals,
  revertOutcome,
} from './branchDiffModel'

function entry(overrides: Partial<PlanDiffEntry>): PlanDiffEntry {
  return {
    entity_type: 'variable',
    kind: 'changed',
    name: 'x',
    parent: null,
    changes: [],
    ...overrides,
  }
}

function conflict(detail: Record<string, unknown>): ApiError {
  const error = new ApiError('409 Conflict', 409)
  error.detail = detail
  return error
}

const RENAME: PlanBranchDiffSummary = {
  behind_base: false,
  summary: { added: 1, removed: 1, changed: 1 },
  entries: [
    entry({ kind: 'removed', name: 'variant', entity_id: 'base-id' }),
    entry({ kind: 'added', name: 'experiment_variant', entity_id: 'branch-id' }),
    entry({ kind: 'changed', name: 'plan' }),
  ],
  renames: [
    { entity_type: 'variable', parent: null, removed_name: 'variant', added_name: 'experiment_variant' },
  ],
}

describe('pairedDiffCounts', () => {
  it('counts a rename once, out of both halves it arrived as', () => {
    expect(pairedDiffCounts(RENAME)).toEqual({
      added: 0,
      changed: 1,
      removed: 0,
      renamed: 1,
      total: 2,
    })
  })

  it('reads an unloaded diff as empty', () => {
    expect(pairedDiffCounts(undefined).total).toBe(0)
  })
})

describe('diffView', () => {
  it('folds the paired addition into the removal and keeps its branch-side id', () => {
    const view = diffView(RENAME)

    expect(view.visibleEntries.map((e) => e.name)).toEqual(['variant', 'plan'])
    expect([...view.renamedTo.values()]).toEqual(['experiment_variant'])
    expect([...view.renamedEntityId.values()]).toEqual(['branch-id'])
    // A rename keeps the row and its history: nothing to warn about.
    expect(view.removedVariables).toEqual([])
  })

  it('warns about unpaired property removals, but not housekeeping ones', () => {
    const view = diffView({
      behind_base: false,
      summary: { added: 0, removed: 1, changed: 0, housekeeping: 1 },
      entries: [
        entry({ kind: 'removed', name: 'gone' }),
        entry({ kind: 'removed', name: 'retired', housekeeping: 'unused scan variable retired' }),
      ],
    })

    expect(view.removedVariables).toEqual(['gone'])
    expect(view.housekeepingEntries.map((e) => e.name)).toEqual(['retired'])
    expect(view.visibleEntries.map((e) => e.name)).toEqual(['gone'])
  })
})

describe('entryRowKey', () => {
  it('tells a removal from an addition of the same name, and parents apart', () => {
    const keys = new Set([
      entryRowKey(entry({ kind: 'removed', name: 'a', parent: 'p' })),
      entryRowKey(entry({ kind: 'added', name: 'a', parent: 'p' })),
      entryRowKey(entry({ kind: 'added', name: 'a', parent: 'q' })),
    ])
    expect(keys.size).toBe(3)
  })
})

describe('revertOutcome', () => {
  const removed = entry({
    entity_type: 'event',
    kind: 'removed',
    name: 'promo_applied',
    parent: 'track',
    before: { source_name: 'promo' },
  })

  it('restores a removal nothing on the branch answers to', () => {
    expect(revertOutcome([removed], removed)).toEqual({ kind: 'restore' })
  })

  it('undoes a rename into the one row carrying the identity', () => {
    const carrier = entry({
      entity_type: 'event',
      kind: 'added',
      name: 'promo_code_applied',
      parent: 'track',
      after: { source_name: 'promo' },
    })
    expect(revertOutcome([removed, carrier], removed)).toEqual({
      kind: 'rename',
      to: 'promo_code_applied',
    })
  })

  it('refuses to guess between two carriers, and ignores other parents', () => {
    const carriers = ['b_applied', 'a_applied'].map((name) =>
      entry({ entity_type: 'event', kind: 'added', name, parent: 'track', after: { source_name: 'promo' } }),
    )
    const elsewhere = entry({
      entity_type: 'event',
      kind: 'added',
      name: 'promo_viewed',
      parent: 'screen',
      after: { source_name: 'promo' },
    })

    const outcome = revertOutcome([removed, ...carriers, elsewhere], removed)

    expect(outcome.kind).toBe('ambiguous')
    expect(outcome.kind === 'ambiguous' && outcome.among.map((e) => e.name)).toEqual([
      'a_applied',
      'b_applied',
    ])
  })
})

describe('changeSummary', () => {
  it('names changed fields in words, not their keys', () => {
    expect(
      changeSummary(
        entry({
          field_changes: [
            { field: 'status', before: 'live', after: 'deprecated' },
            { field: 'metric_breakdown_columns', before: ['a'], after: ['a', 'b'] },
          ],
        }),
      ),
    ).toBe('Status live → deprecated, Metric breakdowns edited')
  })
})

describe('mergePrompt', () => {
  it('always asks, naming what lands on main', () => {
    const prompt = mergePrompt(pairedDiffCounts(RENAME), [], false)

    expect(prompt.title).toBe('Merge to main')
    expect(prompt.confirmLabel).toBe('Merge')
    // Words, not git's "+0 ~1 −0" tally, and the names that land.
    expect(prompt.message).toContain('Merge 2 changes into main (1 modified, 1 renamed)?')
    expect(prompt.message).toContain('open a new branch that reverts')
    expect(prompt.message).not.toContain('cannot be undone here')
    expect(prompt.message).not.toContain('Main has moved on')
  })

  it('carries the property deletion and the behind-main warnings in the same dialog', () => {
    const prompt = mergePrompt(pairedDiffCounts(undefined), ['variant'], true, [], 2)

    expect(prompt.title).toBe('Merge deletes properties from main')
    expect(prompt.confirmLabel).toBe('Merge anyway')
    expect(prompt.variant).toBe('danger')
    expect(prompt.message).toContain('removes 1 property from main: variant')
    expect(prompt.message).toContain('Main has moved on')
    expect(prompt.message).toContain('2 fields you changed were also changed there')
    // An action to take, not "recreate the branch".
    expect(prompt.message).toContain('Pick the value to keep for each in the Conflicts panel below')
    expect(prompt.message).toContain('or update from main')
    expect(prompt.message).not.toMatch(/recreate/i)
  })

  it('says nothing about main having moved on when nothing overlaps', () => {
    const prompt = mergePrompt(pairedDiffCounts(RENAME), [], true, [], 0)

    expect(prompt.message).not.toContain('Main has moved on')
    expect(prompt.message).not.toContain('refused')
  })
})

describe('describeBranchActionError', () => {
  it('decodes the approval quota', () => {
    expect(
      describeBranchActionError(conflict({ insufficient_approvals: { required: 2, current: 1, stale: 1 } })),
    ).toBe(
      'Not enough approvals to merge: 1 of 2 required. 1 approval went stale after later edits — re-approve.',
    )
  })

  it('counts stale approvals in words, not "approval(s)"', () => {
    expect(
      describeBranchActionError(conflict({ insufficient_approvals: { required: 3, current: 0, stale: 2 } })),
    ).toBe(
      'Not enough approvals to merge: 0 of 3 required. 2 approvals went stale after later edits — re-approve.',
    )
  })

  it("prefers this page's wording, then the gate's own message, then the status", () => {
    expect(
      describeBranchActionError(conflict({ branch_behind_base: true, message: 'raw' })),
    ).toBe(MERGE_BLOCKED_BY_MAIN)
    expect(describeBranchActionError(conflict({ other: true, message: 'Do this.' }))).toBe('Do this.')
    expect(describeBranchActionError(new Error('boom'))).toBe('boom')
  })

  it("reads an extension's plan-policy refusal through its own message", () => {
    expect(
      describeBranchActionError(
        conflict({
          message: 'Blocked by 1 plan rule: Event names must be snake_case.',
          policy_violations: [{ rule: 'naming.event', message: 'Event names must be snake_case.' }],
        }),
      ),
    ).toBe('Blocked by 1 plan rule: Event names must be snake_case.')
  })
})

describe('describeBranchActionError', () => {
  it('points a main-side refusal at Update from main, never at recreating the branch', () => {
    expect(describeBranchActionError(conflict({ conflicts: [{}] }))).toBe(
      'Merge blocked: main changed the same entities. Update the branch from main, then merge.',
    )
    // Field conflicts still say to resolve them below.
    expect(
      describeBranchActionError(conflict({ unresolved_field_conflicts: [{}], conflicts: [{}] })),
    ).toBe('Merge blocked: resolve the field conflicts below first.')
    expect(
      describeBranchActionError(conflict({ incomplete_base_snapshot: true, message: 'Recreate it.' })),
    ).toBe(INCOMPLETE_BASE_MESSAGE)
    expect(INCOMPLETE_BASE_MESSAGE).toMatch(/Copy your changes to a new branch/)
    expect(MERGE_BLOCKED_BY_MAIN).not.toMatch(/recreate/i)
  })
})

describe('behindNote', () => {
  const base: PlanBranchConflicts = { entities: [], unresolved_count: 0 }

  it('is nothing while the branch has everything on main', () => {
    expect(behindNote({ ...base, behind: false }, true)).toEqual({ kind: 'none' })
    expect(behindNote(undefined, false)).toEqual({ kind: 'none' })
  })

  it('is neutral when main moved on elsewhere only', () => {
    expect(behindNote({ ...base, behind: true, overlap_count: 0, merge_blocked: false }, false)).toEqual({
      kind: 'safe',
    })
  })

  it('counts the overlapping entities', () => {
    expect(behindNote({ ...base, behind: true, overlap_count: 2, merge_blocked: true }, false)).toEqual({
      kind: 'overlap',
      count: 2,
    })
  })

  it('says the merge would refuse even with no overlap row', () => {
    expect(behindNote({ ...base, behind: true, overlap_count: 0, merge_blocked: true }, false)).toEqual({
      kind: 'blocked',
    })
  })

  it("falls back to the diff's behind_base for an older instance, never saying safe", () => {
    expect(behindNote(base, true)).toEqual({ kind: 'moved' })
  })

  it('never says safe while the overlap check is loading or failed', () => {
    expect(behindNote(undefined, true)).toEqual({ kind: 'moved' })
  })

  it('says a legacy base cannot be updated', () => {
    expect(
      behindNote(
        { ...base, behind: true, overlap_count: 0, merge_blocked: true, updatable: false },
        false,
      ),
    ).toEqual({ kind: 'legacy' })
  })
})

describe('entityChangeLines', () => {
  it('writes one line per touched entity type, zero kinds left out', () => {
    const counts = [
      { entity_type: 'event' as const, added: 1, changed: 3, removed: 0, renamed: 0 },
      { entity_type: 'variable' as const, added: 0, changed: 0, removed: 0, renamed: 0 },
      { entity_type: 'meta_field' as const, added: 0, changed: 0, removed: 2, renamed: 1 },
    ]
    expect(entityChangeLines(counts)).toEqual([
      'Events: 3 changed, 1 added',
      'Meta fields: 2 removed, 1 renamed',
    ])
    expect(entityChangeTotal(counts)).toBe(7)
  })
})

describe('update-from-main refusals', () => {
  it('reads the conflicts off an unresolved refusal', () => {
    const conflicts: PlanBranchConflicts = {
      entities: [{ entity_type: 'variable', name: 'plan', label: 'plan', added_on_both: false, fields: [] }],
      unresolved_count: 1,
    }
    expect(
      unresolvedUpdateConflicts(conflict({ unresolved_conflicts: [{}], conflicts })),
    ).toEqual(conflicts)
    expect(unresolvedUpdateConflicts(conflict({ main_moved: true }))).toBeNull()
  })

  it('words a blocked update from its blockers', () => {
    const error = conflict({
      update_blocked: [{ kind: 'identity_clash', message: 'Rename the branch one.' }],
      message: 'Rename the branch one.',
    })
    expect(updateBlockedMessage(error)).toBe('Rename the branch one.')
    expect(describeUpdateFromMainError(error)).toBe('Rename the branch one.')
    expect(updateBlockedMessage(conflict({ main_moved: true }))).toBeNull()
  })

  it('recognises main having moved', () => {
    expect(isMainMovedRefusal(conflict({ main_moved: true }))).toBe(true)
    expect(isMainMovedRefusal(new Error('x'))).toBe(false)
  })

  it('words the other refusals', () => {
    expect(describeUpdateFromMainError(conflict({ incomplete_base_snapshot: true }))).toBe(
      INCOMPLETE_BASE_MESSAGE,
    )
    expect(
      describeUpdateFromMainError(conflict({ update_constraint_violation: true, message: 'Rename x.' })),
    ).toBe('Rename x.')
    expect(describeUpdateFromMainError(new Error('boom'))).toBe('boom')
  })

  it('keys a choice by entity type, name and field', () => {
    const entity: PlanBranchConflictEntity = {
      entity_type: 'event',
      name: 'a',
      label: 'a',
      added_on_both: false,
      fields: [],
    }
    expect(conflictChoiceKey(entity, 'description')).not.toBe(
      conflictChoiceKey({ ...entity, entity_type: 'variable' }, 'description'),
    )
  })
})

describe('isConflictRefusal', () => {
  it('is true only for the field-conflict shapes', () => {
    expect(isConflictRefusal(conflict({ unresolved_field_conflicts: [{}] }))).toBe(true)
    expect(isConflictRefusal(conflict({ conflicts: [{}] }))).toBe(true)
    expect(isConflictRefusal(conflict({ branch_behind_base: true }))).toBe(false)
    expect(isConflictRefusal(new Error('x'))).toBe(false)
  })
})

describe('housekeepingLine', () => {
  it('counts each reason in words', () => {
    expect(
      housekeepingLine([
        entry({ housekeeping: 'unused scan variable retired' }),
        entry({ housekeeping: 'unused scan variable retired' }),
        entry({ housekeeping: 'already removed on main' }),
      ]),
    ).toBe('2 unused scan properties retired · 1 removal already made on main')
  })
})

describe('parseMinApprovals', () => {
  it('accepts whole numbers from 0 to 100 only', () => {
    expect(parseMinApprovals('0')).toBe(0)
    expect(parseMinApprovals(' 100 ')).toBe(100)
    expect(parseMinApprovals('150')).toBeNull()
    expect(parseMinApprovals('1.5')).toBeNull()
    expect(parseMinApprovals('-1')).toBeNull()
    expect(parseMinApprovals('')).toBeNull()
  })
})
