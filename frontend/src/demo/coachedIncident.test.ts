import { describe, expect, it } from 'vitest'
import type { MonitoringSignal } from '@/types'
import { pickCoachedIncident } from './coachedIncident'

function signal(overrides: Partial<MonitoringSignal>): MonitoringSignal {
  return {
    scan_config_id: 'scan-1',
    scope_type: 'event',
    scope_ref: 'ev-1',
    state: 'latest_scan',
    event_id: 'ev-1',
    event_type_id: null,
    bucket: '2026-07-01T13:00:00Z',
    actual_count: 120,
    expected_count: 80,
    stddev: 5,
    z_score: 8,
    direction: 'spike',
    relative_effect: 0.5,
    scope_name: 'Home Screen View',
    incident_child: false,
    muted: false,
    expected: false,
    hidden: false,
    attribution_status: 'not_computed',
    unit: null,
    detected_at: null,
    ...overrides,
  }
}

describe('pickCoachedIncident', () => {
  it('picks the newest open incident, spike or drop', () => {
    const older = signal({ scope_ref: 'ev-old', bucket: '2026-07-01T13:00:00Z' })
    const newer = signal({ scope_ref: 'ev-new', direction: 'drop', bucket: '2026-07-02T14:00:00Z' })
    expect(pickCoachedIncident([older, newer])).toBe(newer)
    expect(pickCoachedIncident([newer, older])).toBe(newer)
  })

  it('prefers the most concrete chart when one incident opened several scopes', () => {
    const total = signal({ scope_type: 'project_total', scope_ref: 'scan-1', relative_effect: 2 })
    const type = signal({ scope_type: 'event_type', scope_ref: 'et-1', relative_effect: 1.5 })
    const event = signal({ scope_type: 'event', scope_ref: 'ev-1', relative_effect: 0.6 })
    expect(pickCoachedIncident([total, type, event])).toBe(event)
    expect(pickCoachedIncident([total, type])).toBe(type)
  })

  it('breaks a tie on the bigger effect', () => {
    const small = signal({ scope_ref: 'ev-small', relative_effect: 0.6 })
    const big = signal({ scope_ref: 'ev-big', relative_effect: -0.9 })
    expect(pickCoachedIncident([small, big])).toBe(big)
  })

  it('skips what is not an incident to investigate', () => {
    const promo = signal({ scope_ref: 'ev-promo', expected: true, hidden: true, bucket: '2026-07-03T10:00:00Z' })
    const muted = signal({ scope_ref: 'ev-muted', muted: true, bucket: '2026-07-03T09:00:00Z' })
    const metric = signal({ scope_type: 'metric', scope_ref: 'metric-1', bucket: '2026-07-03T08:00:00Z' })
    const drift = signal({ scope_type: 'schema', scope_ref: 'drift-1', bucket: '2026-07-03T07:00:00Z' })
    const incident = signal({ scope_ref: 'ev-real', bucket: '2026-07-01T13:00:00Z' })
    expect(pickCoachedIncident([promo, muted, metric, drift, incident])).toBe(incident)
  })

  it('finds nothing when nothing is open', () => {
    expect(pickCoachedIncident([])).toBeUndefined()
    expect(pickCoachedIncident([signal({ muted: true })])).toBeUndefined()
  })
})
