import { describe, expect, it } from 'vitest'

import type { MonitoringSignal, SignalSeries } from '@/types'
import {
  SIGNAL_SERIES_MAX_SCOPES,
  signalRowKey,
  signalSeriesLookupKey,
  signalSeriesScopes,
  signalSparkline,
} from './monitoringSignalSeries'

function signal(overrides: Partial<MonitoringSignal>): MonitoringSignal {
  return {
    scan_config_id: 'scan-1',
    scope_type: 'event',
    scope_ref: 'ev-1',
    state: 'latest_scan',
    event_id: null,
    event_type_id: null,
    bucket: '2026-07-01T20:00:00Z',
    actual_count: 60,
    expected_count: 10,
    stddev: 2,
    z_score: 9,
    direction: 'spike',
    scope_name: null,
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

function series(overrides: Partial<SignalSeries>): SignalSeries {
  return {
    scan_config_id: 'scan-1',
    scope_type: 'event',
    scope_ref: 'ev-1',
    bucket: '2026-07-01T20:00:00Z',
    interval: '1h',
    data: [],
    ...overrides,
  }
}

describe('signalSeriesScopes', () => {
  it('leaves out catalog metrics and signals without a scan', () => {
    expect(
      signalSeriesScopes([
        signal({}),
        signal({ scope_type: 'metric', scope_ref: 'm-1' }),
        signal({ scope_type: 'project_total', scan_config_id: null }),
      ]),
    ).toEqual([
      { scan_config_id: 'scan-1', scope_type: 'event', scope_ref: 'ev-1', bucket: '2026-07-01T20:00:00Z' },
    ])
  })

  it('stops at the batch limit the server accepts', () => {
    const many = Array.from({ length: SIGNAL_SERIES_MAX_SCOPES + 3 }, (_, i) =>
      signal({ scope_ref: `ev-${i}` }),
    )
    expect(signalSeriesScopes(many)).toHaveLength(SIGNAL_SERIES_MAX_SCOPES)
  })
})

describe('signalSparkline', () => {
  it('marks the flagged bucket however the server spells it', () => {
    const result = signalSparkline(
      series({
        bucket: '2026-07-01T20:00:00+00:00',
        data: [
          { bucket: '2026-07-01T19:00:00Z', count: 12 },
          { bucket: '2026-07-01T20:00:00Z', count: 60 },
          { bucket: '2026-07-01T21:00:00Z', count: 58 },
        ],
      }),
    )
    expect(result).toEqual({ data: [12, 60, 58], anomalyIdx: 1 })
  })

  it('draws nothing from fewer than two points', () => {
    expect(signalSparkline(undefined)).toBeNull()
    expect(signalSparkline(series({ data: [{ bucket: '2026-07-01T20:00:00Z', count: 1 }] }))).toBeNull()
  })
})

describe('signalSeriesLookupKey', () => {
  it('matches a signal to its series across bucket spellings', () => {
    expect(signalSeriesLookupKey(signal({}))).toBe(
      signalSeriesLookupKey(series({ bucket: '2026-07-01T20:00:00.000+00:00' })),
    )
    expect(signalRowKey(signal({}))).toBe('scan-1:event:ev-1:2026-07-01T20:00:00Z')
  })
})
