import { describe, expect, it } from 'vitest'
import type { PropertyDrift } from '@/api/propertyDrifts'
import {
  acceptLabel,
  describePropertyDrift,
  isActivePropertyDrift,
  sortPropertyDrifts,
} from './propertyDrift'

function drift(overrides: Partial<PropertyDrift>): PropertyDrift {
  return {
    id: 'd',
    variable_id: 'v',
    variable_name: 'plan',
    event_id: 'e',
    event_name: 'signup',
    scan_config_id: null,
    kind: 'new_property',
    detail: {},
    status: 'open',
    resolution_note: null,
    snoozed_until: null,
    resolved_at: null,
    resolved_by: null,
    detected_at: '2026-10-01T09:00:00Z',
    ...overrides,
  }
}

describe('property drift words (F23)', () => {
  it('says a required property absent from every row as such', () => {
    expect(describePropertyDrift(drift({ kind: 'missing_required', detail: { presence_rate: 0, threshold: 0.95 } })))
      .toBe('Required, but absent from every row the scan read.')
  })

  it('names what Accept does for each kind', () => {
    expect(acceptLabel(drift({ kind: 'new_property' }))).toBe('Add to list')
    expect(acceptLabel(drift({ kind: 'missing_required' }))).toBe('Make optional')
    expect(acceptLabel(drift({ kind: 'type_change', detail: { observed_type: 'datetime' } }))).toBe('Retype to datetime')
  })

  it('sorts the serious kinds first, then by property', () => {
    const sorted = sortPropertyDrifts([
      drift({ id: '1', kind: 'new_property', variable_name: 'a' }),
      drift({ id: '2', kind: 'type_change', variable_name: 'b' }),
      drift({ id: '3', kind: 'missing_required', variable_name: 'z' }),
      drift({ id: '4', kind: 'missing_required', variable_name: 'c' }),
    ])
    expect(sorted.map((row) => row.id)).toEqual(['4', '3', '2', '1'])
  })

  it('counts a lapsed snooze as active, like the backend', () => {
    const now = Date.parse('2026-10-02T00:00:00Z')
    expect(isActivePropertyDrift(drift({ status: 'open' }), now)).toBe(true)
    expect(isActivePropertyDrift(drift({ status: 'snoozed', snoozed_until: '2026-10-01T00:00:00Z' }), now)).toBe(true)
    expect(isActivePropertyDrift(drift({ status: 'snoozed', snoozed_until: '2026-10-03T00:00:00Z' }), now)).toBe(false)
    expect(isActivePropertyDrift(drift({ status: 'accepted' }), now)).toBe(false)
    expect(isActivePropertyDrift(drift({ status: 'false_positive' }), now)).toBe(false)
  })
})
