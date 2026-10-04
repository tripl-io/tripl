import { describe, expect, it } from 'vitest'
import type { EventMetricPoint, MonitoringSignal, Variable } from '@/types'
import {
  computeWindowDelta,
  describeWindowDelta,
  deriveRowSignalFromMetrics,
  formatCompactCount,
  formatRelativeTime,
  mapLatestSignals,
  pickLatestSignal,
  changedSlice,
  reorderWithSelection,
  applyEventNameFormat,
  resolveTemplateTokens,
  splitEventName,
  splitTemplateValue,
} from './utils'

function metricPoint(
  overrides: Partial<EventMetricPoint> & { bucket: string },
): EventMetricPoint {
  return {
    count: 0,
    expected_count: null,
    stddev: null,
    is_anomaly: false,
    anomaly_direction: null,
    z_score: null,
    ...overrides,
  }
}

function signal(overrides: Partial<MonitoringSignal>): MonitoringSignal {
  return {
    scan_config_id: 'scan-1',
    scope_type: 'event',
    scope_ref: 'evt-1',
    state: 'recent',
    event_id: 'evt-1',
    event_type_id: null,
    bucket: '2026-06-10T00:00:00Z',
    actual_count: 0,
    expected_count: 0,
    stddev: 0,
    z_score: 0,
    direction: 'drop',
    incident_child: false,
    unit: null,
    detected_at: null,
    ...overrides,
  }
}

describe('formatRelativeTime', () => {
  const now = Date.parse('2026-06-10T12:00:00Z')

  it('returns "never" for missing or unparseable input', () => {
    expect(formatRelativeTime(null, now)).toBe('never')
    expect(formatRelativeTime(undefined, now)).toBe('never')
    expect(formatRelativeTime('not-a-date', now)).toBe('never')
  })

  it('returns "just now" below the 60s threshold', () => {
    expect(formatRelativeTime('2026-06-10T11:59:01Z', now)).toBe('just now')
    expect(formatRelativeTime('2026-06-10T11:59:00Z', now)).toBe('1m ago')
  })

  it('crosses minute/hour/day boundaries correctly', () => {
    expect(formatRelativeTime('2026-06-10T11:00:01Z', now)).toBe('59m ago')
    expect(formatRelativeTime('2026-06-10T11:00:00Z', now)).toBe('1h ago')
    expect(formatRelativeTime('2026-06-09T12:00:01Z', now)).toBe('23h ago')
    expect(formatRelativeTime('2026-06-09T12:00:00Z', now)).toBe('1d ago')
  })

  it('clamps future timestamps to "just now"', () => {
    expect(formatRelativeTime('2026-06-10T12:05:00Z', now)).toBe('just now')
  })

  it('rolls up into months and years', () => {
    expect(formatRelativeTime('2026-05-01T12:00:00Z', now)).toBe('1mo ago')
    expect(formatRelativeTime('2025-01-01T12:00:00Z', now)).toBe('1y ago')
  })
})

describe('pickLatestSignal', () => {
  it('returns the most recent signal of the requested scope', () => {
    const result = pickLatestSignal(
      [
        signal({ scope_type: 'event', scope_ref: 'a', bucket: '2026-06-08T00:00:00Z' }),
        signal({ scope_type: 'event', scope_ref: 'b', bucket: '2026-06-10T00:00:00Z' }),
        signal({ scope_type: 'event_type', scope_ref: 'c', bucket: '2026-06-12T00:00:00Z' }),
      ],
      'event',
    )
    expect(result?.scope_ref).toBe('b')
  })

  it('returns null when no signal matches the scope', () => {
    expect(pickLatestSignal([signal({ scope_type: 'event' })], 'project_total')).toBeNull()
  })
})

describe('mapLatestSignals', () => {
  it('keeps only the latest signal per scope_ref for the scope type', () => {
    const map = mapLatestSignals(
      [
        signal({ scope_ref: 'a', bucket: '2026-06-08T00:00:00Z', z_score: 1 }),
        signal({ scope_ref: 'a', bucket: '2026-06-10T00:00:00Z', z_score: 9 }),
        signal({ scope_ref: 'b', bucket: '2026-06-09T00:00:00Z', z_score: 4 }),
        signal({ scope_type: 'event_type', scope_ref: 'a', bucket: '2026-06-30T00:00:00Z' }),
      ],
      'event',
    )
    expect(map.size).toBe(2)
    expect(map.get('a')?.z_score).toBe(9)
    expect(map.get('b')?.z_score).toBe(4)
  })
})

describe('deriveRowSignalFromMetrics', () => {
  it('returns null when there are no directional anomalies', () => {
    expect(
      deriveRowSignalFromMetrics('evt-1', 'scan-1', [
        metricPoint({ bucket: '2026-06-10T00:00:00Z', is_anomaly: false }),
        metricPoint({ bucket: '2026-06-10T01:00:00Z', is_anomaly: true, anomaly_direction: null }),
      ]),
    ).toBeNull()
  })

  it('flags latest_scan when the anomaly is the final bucket', () => {
    const result = deriveRowSignalFromMetrics('evt-1', 'scan-1', [
      metricPoint({ bucket: '2026-06-10T00:00:00Z', count: 5 }),
      metricPoint({
        bucket: '2026-06-10T01:00:00Z',
        count: 1,
        expected_count: 9,
        is_anomaly: true,
        anomaly_direction: 'drop',
        z_score: -4,
      }),
    ])
    expect(result).toMatchObject({
      scope_ref: 'evt-1',
      scope_type: 'event',
      state: 'latest_scan',
      actual_count: 1,
      expected_count: 9,
      z_score: -4,
      direction: 'drop',
      bucket: '2026-06-10T01:00:00Z',
      // Nothing on a series point says either, so nothing is invented.
      unit: null,
      detected_at: null,
    })
  })

  it('flags recent when a later non-anomalous bucket follows the anomaly', () => {
    const result = deriveRowSignalFromMetrics('evt-1', undefined, [
      metricPoint({
        bucket: '2026-06-10T00:00:00Z',
        is_anomaly: true,
        anomaly_direction: 'spike',
        z_score: 6,
      }),
      metricPoint({ bucket: '2026-06-10T01:00:00Z', is_anomaly: false }),
    ])
    expect(result?.state).toBe('recent')
    expect(result?.scan_config_id).toBe('')
  })

  it('falls back to actual count for expected_count when null', () => {
    const result = deriveRowSignalFromMetrics('evt-1', 'scan-1', [
      metricPoint({
        bucket: '2026-06-10T00:00:00Z',
        count: 3,
        expected_count: null,
        is_anomaly: true,
        anomaly_direction: 'spike',
        z_score: null,
      }),
    ])
    expect(result?.expected_count).toBe(3)
    expect(result?.z_score).toBe(0)
  })
})

describe('reorderWithSelection', () => {
  const ids = ['a', 'b', 'c', 'd', 'e']

  it('returns null for no-op and unknown drops', () => {
    expect(reorderWithSelection(ids, new Set(), 'a', 'a')).toBeNull()
    expect(reorderWithSelection(ids, new Set(), 'a', 'z')).toBeNull()
    expect(reorderWithSelection(ids, new Set(), 'z', 'b')).toBeNull()
  })

  it('moves a single row when nothing (or only itself) is selected', () => {
    expect(reorderWithSelection(ids, new Set(), 'a', 'c')).toEqual(['b', 'c', 'a', 'd', 'e'])
    expect(reorderWithSelection(ids, new Set(), 'd', 'b')).toEqual(['a', 'd', 'b', 'c', 'e'])
    // A selection of one still behaves as a single-row move.
    expect(reorderWithSelection(ids, new Set(['a']), 'a', 'c')).toEqual(['b', 'c', 'a', 'd', 'e'])
  })

  it('moves the whole selection as a block when dragging downward', () => {
    // Drag selected {a, c} onto d → block lands after d, preserving a-before-c.
    expect(reorderWithSelection(ids, new Set(['a', 'c']), 'a', 'd')).toEqual(['b', 'd', 'a', 'c', 'e'])
  })

  it('moves the whole selection as a block when dragging upward', () => {
    // Drag selected {c, e} onto b → block lands before b.
    expect(reorderWithSelection(ids, new Set(['c', 'e']), 'e', 'b')).toEqual(['a', 'c', 'e', 'b', 'd'])
  })

  it('keeps the selected rows in their original relative order', () => {
    // Dragging the lower member (e) still preserves c-before-e in the block.
    expect(reorderWithSelection(ids, new Set(['c', 'e']), 'e', 'a')).toEqual(['c', 'e', 'a', 'b', 'd'])
  })

  it('returns null when the block is dropped onto one of its own rows', () => {
    expect(reorderWithSelection(ids, new Set(['a', 'c']), 'a', 'c')).toBeNull()
  })
})

describe('splitEventName', () => {
  it('returns null for ordinary names so they render unchanged', () => {
    expect(splitEventName('spot:open:fishing')).toBeNull()
    expect(splitEventName('checkout')).toBeNull()
  })

  it('splits a name with an empty middle segment (spot::services)', () => {
    expect(splitEventName('spot::services')).toEqual([
      { text: 'spot', empty: false },
      { text: '', empty: true },
      { text: 'services', empty: false },
    ])
  })

  it('treats the serialized "0" sentinel as an empty segment', () => {
    expect(splitEventName('0:forecast_for_4:0')).toEqual([
      { text: '0', empty: true },
      { text: 'forecast_for_4', empty: false },
      { text: '0', empty: true },
    ])
  })

  it('handles leading and trailing empty segments', () => {
    expect(splitEventName(':services')).toEqual([
      { text: '', empty: true },
      { text: 'services', empty: false },
    ])
    expect(splitEventName('spot:')).toEqual([
      { text: 'spot', empty: false },
      { text: '', empty: true },
    ])
  })
})

describe('splitTemplateValue', () => {
  it('returns a single non-token part for plain values', () => {
    expect(splitTemplateValue('hello')).toEqual([{ text: 'hello', token: false }])
  })

  it('marks ${…} template tokens so they can be tinted', () => {
    expect(splitTemplateValue('id=${event.property.spot_id}')).toEqual([
      { text: 'id=', token: false },
      { text: '${event.property.spot_id}', token: true },
    ])
  })

  it('handles a value that is only a token', () => {
    expect(splitTemplateValue('${rule_name}')).toEqual([
      { text: '${rule_name}', token: true },
    ])
  })

  it('treats a real zero as plain text, not a token', () => {
    expect(splitTemplateValue('0')).toEqual([{ text: '0', token: false }])
  })

  it('marks every token across multiple tokens', () => {
    expect(splitTemplateValue('${a}/${b}')).toEqual([
      { text: '${a}', token: true },
      { text: '/', token: false },
      { text: '${b}', token: true },
    ])
  })
})

describe('resolveTemplateTokens', () => {
  const variables: Variable[] = [
    {
      id: 'var-1',
      project_id: 'project-1',
      name: 'variant',
      source_name: 'legacy.variant',
      variable_type: 'string',
      description: 'Experiment variant',
      allowed_values: ['control', 'treatment'],
      bindings: ['payload.variant'],
    },
  ]

  it('resolves canonical names, legacy source names, and bindings', () => {
    const resolved = resolveTemplateTokens(
      '${variant}/${legacy.variant}/${payload.variant}/${missing}',
      variables,
    )

    expect(resolved.map(({ token, variable }) => ({ token, variable: variable?.name ?? null }))).toEqual([
      { token: '${variant}', variable: 'variant' },
      { token: '${legacy.variant}', variable: 'variant' },
      { token: '${payload.variant}', variable: 'variant' },
      { token: '${missing}', variable: null },
    ])
  })

  it('ignores incomplete placeholders and marks unknown complete tokens in value parts', () => {
    expect(resolveTemplateTokens('literal ${ and ${missing}', variables)).toEqual([
      expect.objectContaining({ token: '${missing}', variable: null }),
    ])
    expect(splitTemplateValue('${variant}/${missing}', variables)).toEqual([
      { text: '${variant}', token: true, known: true },
      { text: '/', token: false },
      { text: '${missing}', token: true, known: false },
    ])
  })
})

describe('computeWindowDelta', () => {
  const HOUR_MS = 60 * 60 * 1000
  // Both halves are anchored on NOW, so every fixture states the `now` it was
  // captured at instead of leaning on the wall clock.
  const NOW = Date.parse('2026-06-10T23:00:00Z')

  // Builds a 48h hourly series where the prior 24h buckets carry `priorPerHour`
  // and the most recent 24h carry `recentPerHour`, newest bucket at `latest`.
  function windowSeries(
    priorPerHour: number,
    recentPerHour: number,
    latest = NOW,
  ): EventMetricPoint[] {
    const points: EventMetricPoint[] = []
    for (let hoursAgo = 47; hoursAgo >= 0; hoursAgo -= 1) {
      points.push(
        metricPoint({
          bucket: new Date(latest - hoursAgo * HOUR_MS).toISOString(),
          count: hoursAgo < 24 ? recentPerHour : priorPerHour,
        }),
      )
    }
    return points
  }

  it('returns the percent change of the recent 24h versus the prior 24h', () => {
    // prior window = 24 * 10 = 240, recent window = 24 * 20 = 480 → +100%.
    const delta = computeWindowDelta(windowSeries(10, 20), NOW)
    expect(delta.pct).toBeCloseTo(100)
    expect(delta.status).toBe('ok')
    expect(delta.partial).toBe(false)
  })

  it('reports a negative delta when recent volume drops', () => {
    // prior = 240, recent = 120 → -50%.
    expect(computeWindowDelta(windowSeries(10, 5), NOW).pct).toBeCloseTo(-50)
  })

  it('returns null when there is no prior-window volume to divide by', () => {
    const delta = computeWindowDelta(windowSeries(0, 20), NOW)
    expect(delta.pct).toBeNull()
    // The one empty state that survives: prior really is 0, so there is nothing
    // to divide by — and the copy says exactly that rather than claiming there
    // is no prior window.
    expect(delta.status).toBe('no-prior-volume')
    expect(describeWindowDelta(delta)).toBe(
      'No volume in the prior 24h to compare against — the last 24h carry 480.',
    )
  })

  it('returns null for an empty series', () => {
    expect(computeWindowDelta([], NOW).pct).toBeNull()
    expect(computeWindowDelta([], NOW).status).toBe('no-series')
  })

  it('does not rely on per-bucket expected_count (the old broken source)', () => {
    // Every point has expected_count null (non-anomaly) — the delta must still
    // resolve from raw volume, which was the actual bug.
    const points = windowSeries(4, 8)
    expect(points.every((p) => p.expected_count === null)).toBe(true)
    expect(computeWindowDelta(points, NOW).pct).toBeCloseTo(100)
  })

  it('prints the delta of a 46h span whose two windows are both populated', () => {
    // The exact payload measured on a fresh demo: 47 hourly points spanning
    // 46.0h, because collection ends ~2h before now. The old blanket span guard
    // (< 47h ⇒ render nothing) blanked the ENTIRE column on this, while the same
    // points carry recent 51,456 against prior 45,812 — a sound +12%.
    const oldestHoursAgo = 48
    const recentBuckets = 22 // ages 2h…23h
    const priorBuckets = 25 // ages 24h…48h, the far edge inclusive
    const spread = (total: number, buckets: number): number[] => {
      const base = Math.floor(total / buckets)
      return Array.from({ length: buckets }, (_, i) =>
        i === buckets - 1 ? total - base * (buckets - 1) : base,
      )
    }
    const recentCounts = spread(51_456, recentBuckets)
    const priorCounts = spread(45_812, priorBuckets)
    const lagging: EventMetricPoint[] = []
    for (let hoursAgo = oldestHoursAgo; hoursAgo >= 2; hoursAgo -= 1) {
      const isRecent = hoursAgo < 24
      const count = isRecent
        ? recentCounts[23 - hoursAgo]
        : priorCounts[oldestHoursAgo - hoursAgo]
      lagging.push(
        metricPoint({ bucket: new Date(NOW - hoursAgo * HOUR_MS).toISOString(), count }),
      )
    }
    expect(lagging).toHaveLength(47)

    const delta = computeWindowDelta(lagging, NOW)
    expect(delta.status).toBe('ok')
    expect(delta.recentTotal).toBe(51_456)
    expect(delta.priorTotal).toBe(45_812)
    expect(delta.pct).toBeCloseTo(12.32, 1)
    // Shown, and marked: the recent half stops at the newest bucket, 2h short
    // of now, so it compares 22 hours against a whole one.
    expect(delta.partial).toBe(true)
    expect(delta.recentCoveredHours).toBe(22)
    expect(delta.priorCoveredHours).toBe(24)
    expect(delta.trailingGapHours).toBe(2)
    expect(describeWindowDelta(delta)).toBe(
      'Last 24h 51,456 vs 45,812 in the 24h before it. Incomplete window: the last '
      + '24h are covered to 22 of 24 hours and the 24h before it to 24 of 24; the '
      + 'series ends 2h before now, so this compares less than a full window.',
    )
  })

  it('refuses to divide one window by another it barely covers', () => {
    // Collection lagging ~23h behind the 48h fetch window. Anchored on NOW the
    // thin half is the recent one (1 of 24 hours), where anchoring on the newest
    // bucket used to thin the PRIOR half instead and divide 24h of volume by
    // ~1.25h of it — every row on that stand came out between +1673% and +1907%,
    // beside rows whose Last seen read "never".
    const latest = NOW - 23 * HOUR_MS
    const lagged: EventMetricPoint[] = []
    for (let hoursAgo = 24; hoursAgo >= 0; hoursAgo -= 1) {
      lagged.push(
        metricPoint({
          bucket: new Date(latest - hoursAgo * HOUR_MS).toISOString(),
          count: 200,
        }),
      )
    }

    const delta = computeWindowDelta(lagged, NOW)
    expect(delta.pct).toBeNull()
    expect(delta.status).toBe('window-too-thin')
    expect(delta.recentCoveredHours).toBe(1)
    expect(describeWindowDelta(delta)).toBe(
      'Too little of the window to compare: the last 24h are covered to 1 of 24 '
      + 'hours and the 24h before it to 24 of 24; the series ends 23h before now.',
    )
  })

  it('still compares two buckets on a daily collection grid', () => {
    // Coverage is read off the grid, so a scan whose interval puts exactly two
    // buckets in the 48h window keeps its delta — one daily bucket IS 24 hours.
    const daily = [
      metricPoint({ bucket: new Date(NOW - 24 * HOUR_MS).toISOString(), count: 100 }),
      metricPoint({ bucket: new Date(NOW).toISOString(), count: 150 }),
    ]

    const delta = computeWindowDelta(daily, NOW)
    expect(delta.pct).toBeCloseTo(50)
    expect(delta.partial).toBe(false)
  })

  it('returns null for a single bucket, which has no prior window at all', () => {
    const lone = [metricPoint({ bucket: '2026-06-10T23:00:00Z', count: 900 })]

    expect(computeWindowDelta(lone, NOW).pct).toBeNull()
    expect(computeWindowDelta(lone, NOW).status).toBe('no-series')
  })
})

describe('formatCompactCount', () => {
  // The 48h column sits next to "Last seen", which renders "1m ago"/"1h ago".
  // A lowercase "1m" volume there reads as one minute, not one million.
  it('keeps the millions suffix uppercase so it cannot be read as a duration', () => {
    expect(formatCompactCount(4_000_000)).toBe('4M')
    expect(formatCompactCount(1_200_000)).toBe('1M')
  })

  it('leaves the Intl casing alone for thousands and bare counts', () => {
    expect(formatCompactCount(505_000)).toBe('505K')
    expect(formatCompactCount(12)).toBe('12')
  })
})

describe('applyEventNameFormat', () => {
  it('substitutes field values and walks dotted JSON paths', () => {
    const result = applyEventNameFormat('pv:{screen}:{payload.extra.variant}', {
      screen: 'onboarding',
      payload: '{"extra": {"variant": "b2"}}',
    })
    expect(result).toEqual({ name: 'pv:onboarding:b2', missing: [] })
  })

  it('reports unresolved keys and keeps them literal', () => {
    const result = applyEventNameFormat('pv:{screen}:{payload.extra.variant}', {
      payload: '{"extra": {}}',
    })
    expect(result.name).toBe('pv:{screen}:{payload.extra.variant}')
    expect(result.missing).toEqual(['screen', 'payload.extra.variant'])
  })
})

describe('changedSlice (EVT-3)', () => {
  it('returns only the span a drag changed', () => {
    const prev = ['a', 'b', 'c', 'd', 'e', 'f']
    expect(changedSlice(prev, ['a', 'c', 'd', 'b', 'e', 'f'])).toEqual(['c', 'd', 'b'])
    expect(changedSlice(prev, ['f', 'a', 'b', 'c', 'd', 'e'])).toEqual(['f', 'a', 'b', 'c', 'd', 'e'])
  })

  it('returns nothing when nothing moved', () => {
    expect(changedSlice(['a', 'b'], ['a', 'b'])).toEqual([])
  })
})
