import { describe, expect, it } from 'vitest'

import type { PlanDiffEntry } from '@/types'
import {
  paramsWithTarget,
  previewTargetForEntry,
  previewTargetsForOverrides,
  stateMeta,
  targetFromParams,
  visibleRows,
} from './mergedEventModel'

function entry(overrides: Partial<PlanDiffEntry>): PlanDiffEntry {
  return {
    entity_type: 'event',
    kind: 'changed',
    name: 'checkout_started',
    parent: 'track',
    entity_id: 'b-1',
    changes: [],
    ...overrides,
  }
}

describe('stateMeta', () => {
  it('gives every state a word, and colours only the ones that moved', () => {
    expect(stateMeta('added')).toMatchObject({ tone: 'success', label: 'Added' })
    expect(stateMeta('changed')).toMatchObject({ tone: 'warning', label: 'Changed' })
    expect(stateMeta('unchanged')).toMatchObject({ tone: null, label: 'Unchanged' })
    expect(stateMeta('removed')).toMatchObject({ label: 'Removed', strike: true })
    expect(stateMeta('conflict')).toMatchObject({ tone: 'danger', label: 'Conflict' })
  })
})

describe('previewTargetForEntry', () => {
  it('targets the branch-side id of an added or changed event', () => {
    expect(previewTargetForEntry(entry({ kind: 'added' }))).toEqual({ eventId: 'b-1' })
    expect(previewTargetForEntry(entry({ kind: 'changed' }))).toEqual({ eventId: 'b-1' })
  })

  it('targets the base-side id of a removal', () => {
    expect(previewTargetForEntry(entry({ kind: 'removed', entity_id: 'm-1' }))).toEqual({
      eventId: 'm-1',
    })
  })

  it('targets the paired addition for a rename', () => {
    expect(
      previewTargetForEntry(entry({ kind: 'removed', entity_id: 'm-1' }), 'b-9'),
    ).toEqual({ eventId: 'b-9' })
  })

  it('is null for a legacy entry or another entity type', () => {
    expect(previewTargetForEntry(entry({ entity_id: null }))).toBeNull()
    expect(previewTargetForEntry(entry({ entity_type: 'variable' }))).toBeNull()
  })
})

describe('previewTargetsForOverrides', () => {
  const variable = (members: Record<string, unknown>[], keys: string[]) =>
    entry({
      entity_type: 'variable',
      name: 'plan',
      parent: null,
      field_changes: [
        {
          field: 'event_value_overrides',
          before: [],
          after: members,
          items: keys.map((key) => ({ key, kind: 'changed', before: null, after: null })),
        },
      ],
      after: { event_value_overrides: members },
      before: { event_value_overrides: [] },
    })

  it('maps each item to the event its member names, dotted names included', () => {
    const targets = previewTargetsForOverrides(
      variable(
        [
          { event_type_name: 'track', event_name: 'checkout_done', values: ['pro'] },
          { event_type_name: 'track', event_name: 'page.view', values: null },
        ],
        ['track.checkout_done', 'track.page.view'],
      ),
    )
    expect(targets.get('track.checkout_done')).toEqual({
      eventType: 'track',
      eventName: 'checkout_done',
    })
    expect(targets.get('track.page.view')).toEqual({ eventType: 'track', eventName: 'page.view' })
  })

  it('gives no link where two members share the key', () => {
    const targets = previewTargetsForOverrides(
      variable(
        [
          { event_type_name: 'track', event_name: 'dup', values: ['a'] },
          { event_type_name: 'track', event_name: 'dup', values: ['b'] },
        ],
        ['track.dup'],
      ),
    )
    expect(targets.has('track.dup')).toBe(false)
  })

  it('falls back to the before list for a removed member', () => {
    const removed = entry({
      entity_type: 'variable',
      field_changes: [
        {
          field: 'event_value_overrides',
          before: [],
          after: [],
          items: [{ key: 'track.gone', kind: 'removed', before: null, after: null }],
        },
      ],
      after: { event_value_overrides: [] },
      before: { event_value_overrides: [{ event_type_name: 'track', event_name: 'gone' }] },
    })
    expect(previewTargetsForOverrides(removed).get('track.gone')).toEqual({
      eventType: 'track',
      eventName: 'gone',
    })
  })

  it('is empty for an event row', () => {
    expect(previewTargetsForOverrides(entry({})).size).toBe(0)
  })
})

describe('visibleRows', () => {
  it('drops unchanged rows only when asked', () => {
    const rows = [
      { state: 'unchanged' as const },
      { state: 'changed' as const },
      { state: 'conflict' as const },
    ]
    expect(visibleRows(rows, false)).toHaveLength(3)
    expect(visibleRows(rows, true).map((row) => row.state)).toEqual(['changed', 'conflict'])
  })
})

describe('search params', () => {
  it('round-trips an id target and a key target, one replacing the other', () => {
    const base = new URLSearchParams('tab=1')
    const byKey = paramsWithTarget(base, { eventType: 'track', eventName: 'done' })
    expect(targetFromParams(byKey)).toEqual({ eventType: 'track', eventName: 'done' })
    const byId = paramsWithTarget(byKey, { eventId: 'e-1' })
    expect(byId.toString()).toBe('tab=1&merged=e-1')
    expect(targetFromParams(paramsWithTarget(byId, null))).toBeNull()
  })
})
