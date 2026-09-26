import { describe, expect, it } from 'vitest'
import type { MonitoringSignal, SignalVerdict } from '@/types'
import {
  MUTE_OPTIONS,
  canClearVerdict,
  canSetVerdict,
  canTriageSignal,
  countHiddenSignals,
  triageScopeOf,
  triageStatusLabel,
} from './signalTriage'

function signal(over: Partial<MonitoringSignal> = {}): MonitoringSignal {
  return {
    scan_config_id: 'scan-1',
    scope_type: 'event_type',
    scope_ref: 'et-1',
    state: 'latest_scan',
    event_id: null,
    event_type_id: 'et-1',
    bucket: '2026-09-25T18:00:00Z',
    actual_count: 120,
    expected_count: 40,
    stddev: 5,
    z_score: 8,
    direction: 'spike',
    scope_name: 'Signup',
    incident_child: false,
    unit: null,
    detected_at: null,
    ...over,
  }
}

describe('signal triage helpers (MO-4 / JR-5)', () => {
  it('offers triage only on a signal no rule routed to an incident', () => {
    expect(canTriageSignal(signal())).toBe(true)
    expect(canTriageSignal(signal({ incident_id: 'group-1' }))).toBe(false)
  })

  it('keys a catalog metric with no scan config, whatever the payload carries', () => {
    expect(triageScopeOf(signal({ scope_type: 'metric', scope_ref: 'm-1', scan_config_id: 'x' })))
      .toEqual({
        scan_config_id: null,
        scope_type: 'metric',
        scope_ref: 'm-1',
        bucket: '2026-09-25T18:00:00Z',
      })
    expect(triageScopeOf(signal()).scan_config_id).toBe('scan-1')
  })

  it('names the verdict, expected first, then muted, then acknowledged', () => {
    expect(triageStatusLabel(signal())).toBeNull()
    expect(triageStatusLabel(signal({ acknowledged_at: '2026-09-25T19:00:00Z' }))).toBe(
      'Acknowledged',
    )
    expect(triageStatusLabel(signal({ muted: true, muted_until: null }))).toBe('Muted')
    expect(
      triageStatusLabel(signal({ muted: true, muted_until: '2026-09-26T18:00:00Z' })),
    ).toMatch(/^Muted until /)
    expect(
      triageStatusLabel(signal({ expected: true, muted: true, acknowledged_at: 'x' })),
    ).toBe('Expected')
  })

  it('offers a verdict on routed and unrouted signals alike (#254)', () => {
    expect(canSetVerdict(signal())).toBe(true)
    expect(canSetVerdict(signal({ incident: { id: 'group-1', status: 'open' } }))).toBe(true)
    expect(canTriageSignal(signal({ incident: { id: 'group-1', status: 'open' } }))).toBe(false)
    expect(canSetVerdict(signal({ scope_type: 'schema' }))).toBe(false)
  })

  it('clears only a verdict the signal itself carries, not its incident\'s', () => {
    const verdict: SignalVerdict = {
      verdict: 'real_issue',
      expected_reason: null,
      note: null,
      author_name: 'Ann',
      created_at: '2026-09-25T19:00:00Z',
      source: 'signal',
    }
    expect(canClearVerdict(signal())).toBe(false)
    expect(canClearVerdict(signal({ verdict }))).toBe(true)
    expect(canClearVerdict(signal({ verdict: { ...verdict, source: 'incident' } }))).toBe(false)
  })

  it('names a recorded verdict ahead of the older triage states', () => {
    expect(
      triageStatusLabel(
        signal({
          expected: true,
          verdict: {
            verdict: 'expected',
            expected_reason: 'release',
            note: null,
            author_name: null,
            created_at: '2026-09-25T19:00:00Z',
            source: 'signal',
          },
        }),
      ),
    ).toBe('Expected · release')
  })

  it('counts only hidden signals', () => {
    expect(
      countHiddenSignals([signal({ hidden: true }), signal(), signal({ acknowledged_at: 'x' })]),
    ).toBe(1)
  })

  it('offers the three mute lengths the server accepts', () => {
    expect(MUTE_OPTIONS.map((option) => option.duration)).toEqual(['24h', '7d', 'until_unmuted'])
  })
})
