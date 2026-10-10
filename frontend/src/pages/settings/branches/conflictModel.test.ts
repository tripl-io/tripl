import { describe, expect, it } from 'vitest'

import type { PlanBranchConflictEntity, PlanBranchConflicts } from '@/types'
import {
  bulkChoices,
  isEmptyConflictValue,
  presenceLeft,
  withChoice,
  withChoices,
} from './conflictModel'

/** pv.onboarding/starting_place as production reported it: authored on the
 * branch, created on main by a scan, every row added on both sides. */
const TWIN: PlanBranchConflictEntity = {
  entity_type: 'event',
  name: 'pv.onboarding/starting_place',
  parent: 'pv',
  label: 'onboarding/starting_place',
  added_on_both: true,
  fields: [
    { field: 'title', base: null, ours: '', theirs: 'Starting place', choice: null, dependents: 0 },
    {
      field: 'description',
      base: null,
      ours: 'Seen in onboarding',
      theirs: '',
      choice: null,
      dependents: 0,
    },
    { field: 'status', base: null, ours: 'active', theirs: 'draft', choice: null, dependents: 0 },
    { field: 'owner_id', base: null, ours: null, theirs: 'u-1', choice: null, dependents: 0 },
    {
      field: 'field_values',
      base: null,
      ours: [{ field_name: 'screen', value: 'a' }],
      theirs: [{ field_name: 'screen', value: 'b' }],
      choice: null,
      dependents: 0,
    },
    {
      field: 'meta_values',
      base: null,
      ours: [],
      theirs: [{ meta_field_name: 'team', value: 'growth' }],
      choice: null,
      dependents: 0,
    },
    { field: 'tags', base: null, ours: [], theirs: ['onboarding'], choice: 'ours', dependents: 0 },
  ],
}

const DELETED: PlanBranchConflictEntity = {
  entity_type: 'event',
  name: 'pv.legacy',
  parent: 'pv',
  label: 'legacy',
  added_on_both: false,
  fields: [
    {
      field: '@presence',
      base: 'present',
      ours: 'absent',
      theirs: 'present',
      choice: null,
      dependents: 0,
    },
    { field: 'title', base: 'a', ours: 'b', theirs: 'c', choice: null, dependents: 0 },
  ],
}

const CONFLICTS: PlanBranchConflicts = {
  entities: [TWIN, DELETED],
  unresolved_count: 8,
}

describe('isEmptyConflictValue', () => {
  it.each([
    [null, true],
    [undefined, true],
    ['', true],
    [[], true],
    [{}, true],
    // Whitespace is content; so are a zero and a false.
    [' ', false],
    [0, false],
    [false, false],
    [['a'], false],
    [{ a: 1 }, false],
  ])('%j is empty: %s', (value, empty) => {
    expect(isEmptyConflictValue(value)).toBe(empty)
  })
})

describe('bulkChoices', () => {
  it('keeps whichever is filled in, falling back to the branch', () => {
    const picks = bulkChoices([TWIN], 'filled')
    expect(Object.fromEntries(picks.map((pick) => [pick.field, pick.choice]))).toEqual({
      title: 'theirs',
      description: 'ours',
      status: 'theirs',
      owner_id: 'theirs',
      field_values: 'theirs',
      meta_values: 'theirs',
      // A choice already made is overwritten: the action is "for all".
      tags: 'theirs',
    })
  })

  it('takes one side for every value row and never a deletion', () => {
    const picks = bulkChoices([TWIN, DELETED], 'ours')
    expect(picks).toHaveLength(8)
    expect(picks.every((pick) => pick.choice === 'ours')).toBe(true)
    expect(picks.some((pick) => pick.field === '@presence')).toBe(false)
  })

  it('stays inside its entity', () => {
    const picks = bulkChoices([TWIN, DELETED], 'theirs', {
      entity_type: 'event',
      name: 'pv.legacy',
    })
    expect(picks).toEqual([
      { entity_type: 'event', entity_name: 'pv.legacy', field: 'title', choice: 'theirs' },
    ])
  })
})

describe('presenceLeft', () => {
  it('counts the deletions no choice covers yet', () => {
    expect(presenceLeft([TWIN, DELETED], (_entity, field) => field.choice ?? null)).toBe(1)
    expect(presenceLeft([TWIN, DELETED], () => 'ours')).toBe(0)
    expect(
      presenceLeft([TWIN, DELETED], (_entity, field) => field.choice ?? null, {
        entity_type: 'event',
        name: TWIN.name,
      }),
    ).toBe(0)
  })
})

describe('withChoices', () => {
  it('sets every pick and recounts, leaving the input untouched', () => {
    const next = withChoices(CONFLICTS, bulkChoices(CONFLICTS.entities, 'filled'))

    expect(next.unresolved_count).toBe(1)
    expect(next.entities[0]?.fields.every((field) => field.choice !== null)).toBe(true)
    expect(next.entities[1]?.fields[0]?.choice).toBeNull()
    expect(CONFLICTS.entities[0]?.fields[0]?.choice).toBeNull()
    expect(CONFLICTS.unresolved_count).toBe(8)
  })

  it('clears a pick for a rollback, as withChoice does for one field', () => {
    const picked = withChoices(CONFLICTS, bulkChoices(CONFLICTS.entities, 'ours'))
    const back = withChoice(
      picked,
      { entity_type: 'event', entity_name: 'pv.legacy', field: 'title' },
      null,
    )
    expect(back.entities[1]?.fields[1]?.choice).toBeNull()
    expect(back.unresolved_count).toBe(2)
  })
})
