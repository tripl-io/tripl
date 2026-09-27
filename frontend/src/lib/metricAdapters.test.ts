import { describe, expect, it } from 'vitest'
import type {
  MetricBreakdownsResponse,
  MetricSeriesPoint,
  MetricSeriesResponse,
  MetricSignalResponse,
  MetricVersionSeriesResponse,
} from '@/types'
import {
  adaptMetricBreakdowns,
  adaptMetricSeries,
  adaptMetricVersions,
  defaultDrilldownGranularity,
  granularityForInterval,
  metricPointToEventPoint,
  metricRollupMode,
  metricSignalToMonitoringSignal,
} from './metricAdapters'
import { at } from '@/test/at'

function seriesPoint(overrides: Partial<MetricSeriesPoint> = {}): MetricSeriesPoint {
  return {
    bucket: '2026-06-10T00:00:00Z',
    value: 0.08,
    expected_count: null,
    stddev: null,
    is_anomaly: false,
    anomaly_direction: null,
    z_score: null,
    ...overrides,
  } as MetricSeriesPoint
}

describe('metricRollupMode (MON-2 / MET-12)', () => {
  it('sums only additive metrics', () => {
    expect(metricRollupMode(undefined)).toBe('sum')
    expect(metricRollupMode({ kind: 'event_composition', composition: 'single' })).toBe('sum')
    expect(metricRollupMode({ kind: 'fact', aggregation: 'count' })).toBe('sum')
    expect(metricRollupMode({ kind: 'fact', aggregation: 'sum' })).toBe('sum')
  })

  it('averages ratios, averages, extrema, distinct counts and free SQL', () => {
    expect(metricRollupMode({ kind: 'event_composition', composition: 'ratio' })).toBe('mean')
    expect(metricRollupMode({ kind: 'fact', composition: 'ratio', aggregation: 'count' })).toBe('mean')
    expect(metricRollupMode({ kind: 'fact', aggregation: 'avg' })).toBe('mean')
    expect(metricRollupMode({ kind: 'fact', aggregation: 'max' })).toBe('mean')
    expect(metricRollupMode({ kind: 'fact', aggregation: 'count_distinct' })).toBe('mean')
    expect(metricRollupMode({ kind: 'sql' })).toBe('mean')
  })
})

describe('drilldown granularity defaults (MON-43)', () => {
  it('maps every backend interval, and nothing else', () => {
    expect(granularityForInterval('15m')).toBe('15min')
    expect(granularityForInterval('6h')).toBe('6h')
    expect(granularityForInterval('1w')).toBe('week')
    expect(granularityForInterval(null)).toBeNull()
    expect(granularityForInterval('2h')).toBeNull()
  })

  it('takes the coarser of the range default and the collection interval', () => {
    expect(defaultDrilldownGranularity(7, '1h')).toBe('hour')
    expect(defaultDrilldownGranularity(7, '1d')).toBe('day')
    expect(defaultDrilldownGranularity(7, '15m')).toBe('hour')
    expect(defaultDrilldownGranularity(30, '1h')).toBe('day')
    expect(defaultDrilldownGranularity(90, null)).toBe('week')
  })
})

describe('catalog metric adapters', () => {
  it('maps a metric value onto the event count field', () => {
    expect(metricPointToEventPoint(seriesPoint({ value: 0.25, z_score: 3 }))).toEqual({
      bucket: '2026-06-10T00:00:00Z',
      count: 0.25,
      expected_count: null,
      stddev: null,
      is_anomaly: false,
      anomaly_direction: null,
      z_score: 3,
    })
  })

  it('drops the forecast and keeps the served sigma for a metric series', () => {
    const adapted = adaptMetricSeries({
      metric_id: 'm-1',
      scan_config_id: null,
      interval: '1h',
      latest_signal: null,
      data: [seriesPoint()],
      sigma_threshold: 6,
    } as unknown as MetricSeriesResponse)
    expect(adapted.forecast).toEqual([])
    expect(adapted.sigma_threshold).toBe(6)
    expect(at(adapted.data, 0).count).toBe(0.08)
  })

  it('reads is_active off the versions catalog so a pre-release stays one', () => {
    const adapted = adaptMetricVersions({
      metric_id: 'm-1',
      scan_config_id: null,
      app_version_column: 'app_version',
      interval: '1d',
      latest_version: '2.0.0',
      versions: [{ version: '2.0.0', is_other: false, is_latest: true, is_active: false }],
      series: [
        { version: '2.0.0', is_other: false, is_latest: true, is_active: true, total_value: 0.3, data: [] },
      ],
    } as unknown as MetricVersionSeriesResponse)
    expect(at(adapted.series, 0).is_active).toBe(false)
    expect(at(adapted.series, 0).total_count).toBe(0.3)
  })

  it('carries breakdown totals and no parity anomalies', () => {
    const adapted = adaptMetricBreakdowns({
      metric_id: 'm-1',
      scan_config_id: null,
      interval: '1d',
      columns: ['platform'],
      selected_column: 'platform',
      series: [{ breakdown_value: 'ios', is_other: false, total_value: 0.4, data: [seriesPoint()] }],
    } as unknown as MetricBreakdownsResponse)
    expect(adapted.series[0]).toMatchObject({ breakdown_value: 'ios', total_count: 0.4, parity_anomalies: [] })
  })
})

describe('metricSignalToMonitoringSignal', () => {
  const base: MetricSignalResponse = {
    scope_type: 'metric',
    scope_ref: 'metric-1',
    state: 'latest_scan',
    bucket: '2026-06-10T00:00:00Z',
    actual_count: 0.04,
    expected_count: 0.12,
    stddev: 0.01,
    z_score: -8,
    direction: 'drop',
    incident_child: false,
    expected: false,
    hidden: false,
    muted: false,
    attribution_status: 'not_computed',
  }

  it('carries the unit and detection time the server sent (MON-34, MON-40)', () => {
    const signal = metricSignalToMonitoringSignal({
      ...base,
      unit: '%',
      detected_at: '2026-06-10T01:05:00Z',
    })
    expect(signal).toMatchObject({ unit: '%', detected_at: '2026-06-10T01:05:00Z' })
  })

  it('carries the verdict and incident a payload names (#254)', () => {
    const verdict = {
      verdict: 'false_positive',
      expected_reason: null,
      note: null,
      author_name: 'Ann',
      created_at: '2026-06-10T02:00:00Z',
      source: 'incident',
    }
    const signal = metricSignalToMonitoringSignal({
      ...base,
      verdict,
      incident: { id: 'group-1', status: 'false_positive' },
    } as MetricSignalResponse)
    expect(signal.verdict).toEqual(verdict)
    expect(signal.incident).toEqual({ id: 'group-1', status: 'false_positive' })
  })

  it('says null for a field the payload left out, never undefined', () => {
    const signal = metricSignalToMonitoringSignal(base)
    expect(signal.unit).toBeNull()
    expect(signal.detected_at).toBeNull()
  })
})
