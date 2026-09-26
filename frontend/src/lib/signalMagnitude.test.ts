import { describe, expect, it } from 'vitest'
import type { MonitoringSignal } from '@/types'
import {
  MAGNITUDE_PRESETS,
  compareSignalsByMagnitude,
  magnitudePresetLabel,
  relativeEffect,
  selectSignificantSignals,
  signalMagnitudeWord,
} from './signalMagnitude'

function signal(over: Partial<MonitoringSignal>): MonitoringSignal {
  return {
    scan_config_id: 'scan-1',
    scope_type: 'event',
    scope_ref: 'event-1',
    state: 'latest_scan',
    event_id: 'event-1',
    event_type_id: null,
    bucket: '2026-08-19T00:00:00Z',
    actual_count: 0,
    expected_count: 0,
    stddev: 0,
    z_score: 0,
    direction: 'drop',
    scope_name: 'checkout_started',
    incident_child: false,
    unit: null,
    detected_at: null,
    ...over,
  }
}

describe('relativeEffect', () => {
  it('reads the magnitude the server computed', () => {
    // The server is the only side that knows a metric series is fractional, so
    // its value wins over anything derivable here (tripl-yf8c).
    const s = signal({ scope_type: 'metric', actual_count: 0.04, expected_count: 0.12, relative_effect: 0.667 })
    expect(relativeEffect(s)).toBeCloseTo(0.667, 3)
  })

  it('falls back to the count-shaped estimate when the payload carries none', () => {
    expect(relativeEffect(signal({ actual_count: 150, expected_count: 100 }))).toBeCloseTo(0.5, 6)
  })

  it('keeps the floor in the fallback, so a quiet scope cannot outrank a busy one', () => {
    // 2 vs 0.5 expected is +300% without a floor; the floor keeps it at 1.5 so a
    // scope with essentially no traffic does not dominate the list.
    expect(relativeEffect(signal({ actual_count: 2, expected_count: 0.5 }))).toBeCloseTo(1.5, 6)
  })

  it('admits a fractional metric the fallback would have hidden', () => {
    // Same numbers, with and without the server's value: 0.08 is below the 0.5
    // bar, 0.667 is above it. This is the whole defect in one assertion.
    const withServer = signal({ scope_type: 'metric', actual_count: 0.04, expected_count: 0.12, relative_effect: 0.667 })
    const without = signal({ scope_type: 'metric', actual_count: 0.04, expected_count: 0.12 })
    expect(selectSignificantSignals([withServer])).toHaveLength(1)
    expect(selectSignificantSignals([without])).toHaveLength(0)
  })

  it('keeps a zero-baseline signal, which arrives capped rather than as Infinity', () => {
    // JSON carries no Infinity: the backend reports an unbounded move off a zero
    // baseline as a finite ceiling (MAX_RELATIVE_EFFECT). Shipped as inf it
    // arrived here as null and the fallback scored 0.3 — below the bar — so the
    // sidebar badge counted a signal this list hid (tripl-l33u.3).
    const capped = signal({ scope_type: 'metric', actual_count: 0.3, expected_count: 0, relative_effect: 1e6 })
    const asNull = signal({ scope_type: 'metric', actual_count: 0.3, expected_count: 0 })
    expect(selectSignificantSignals([capped])).toHaveLength(1)
    expect(selectSignificantSignals([asNull])).toHaveLength(0)
  })

  it('leaves out signals a triage verdict hid, so the bell and Overview match the badge', () => {
    // Muted or marked expected (MO-4 / JR-5): the backend's badge count skips
    // them, so every client count must too. Acknowledged ones stay.
    const hidden = signal({ scope_ref: 'muted', relative_effect: 3, hidden: true, muted: true })
    const acknowledged = signal({
      scope_ref: 'seen',
      relative_effect: 3,
      acknowledged_at: '2026-08-19T01:00:00Z',
    })
    expect(selectSignificantSignals([hidden, acknowledged]).map(s => s.scope_ref)).toEqual([
      'seen',
    ])
  })

  it('leaves out signals with a verdict, whatever its source (#254)', () => {
    const answered = signal({
      scope_ref: 'answered',
      relative_effect: 3,
      verdict: {
        verdict: 'tracking_bug',
        expected_reason: null,
        note: null,
        author_name: 'Ann',
        created_at: '2026-08-19T01:00:00Z',
        source: 'incident',
      },
    })
    const open = signal({ scope_ref: 'open', relative_effect: 3 })
    expect(selectSignificantSignals([answered, open]).map(s => s.scope_ref)).toEqual(['open'])
  })

  it('sorts by the server value, biggest first', () => {
    const small = signal({ scope_ref: 'a', relative_effect: 0.6 })
    const big = signal({ scope_ref: 'b', relative_effect: 3 })
    expect(selectSignificantSignals([small, big]).map(s => s.scope_ref)).toEqual(['b', 'a'])
  })

  it('breaks a tie on relative effect by |z|, as the Anomalies page does', () => {
    const calm = signal({ scope_ref: 'calm', relative_effect: 1, z_score: 2 })
    const loud = signal({ scope_ref: 'loud', relative_effect: 1, z_score: -9 })
    const list = [calm, loud]
    expect(selectSignificantSignals(list).map(s => s.scope_ref)).toEqual(['loud', 'calm'])
    expect([...list].sort(compareSignalsByMagnitude).map(s => s.scope_ref)).toEqual(['loud', 'calm'])
  })
})

describe('magnitude words and labels (MO-3, JR-31)', () => {
  it('puts the threshold in the filter label, in the % the rows show', () => {
    expect(MAGNITUDE_PRESETS.map(magnitudePresetLabel)).toEqual([
      'All',
      'Significant (≥50%)',
      'Major (≥100%)',
    ])
  })

  it('buckets a signal on the same bars as the filter', () => {
    expect(signalMagnitudeWord(signal({ actual_count: 120, expected_count: 100 }))).toBe('Minor')
    expect(signalMagnitudeWord(signal({ actual_count: 150, expected_count: 100 }))).toBe('Significant')
    expect(signalMagnitudeWord(signal({ actual_count: 200, expected_count: 100 }))).toBe('Major')
    // The server value wins here too.
    expect(signalMagnitudeWord(signal({ actual_count: 1, expected_count: 1, relative_effect: 2 }))).toBe('Major')
  })
})
