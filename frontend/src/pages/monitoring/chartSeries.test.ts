import { describe, expect, it } from 'vitest'
import type { AppVersionMetricSeries, EventMetricBreakdownSeries, EventMetricPoint } from '@/types'
import {
  buildBreakdownEntries,
  buildVersionChartSeries,
  formatVersionLabel,
  legendValueKindFor,
  selectBreakdownChartSeries,
  seriesSlot,
} from './chartSeries'
import { at } from '@/test/at'

function pt(bucket: string, count: number): EventMetricPoint {
  return {
    bucket,
    count,
    expected_count: null,
    stddev: null,
    is_anomaly: false,
    anomaly_direction: null,
    z_score: null,
  }
}

function version(overrides: Partial<AppVersionMetricSeries>): AppVersionMetricSeries {
  return {
    version: '1.0.0',
    is_other: false,
    is_latest: false,
    is_active: true,
    total_count: 0,
    data: [],
    ...overrides,
  }
}

function breakdown(value: string, data: EventMetricPoint[], total = 0): EventMetricBreakdownSeries {
  return { breakdown_value: value, is_other: false, total_count: total, data, parity_anomalies: [] }
}

describe('seriesSlot', () => {
  it('repeats the eight hues with a dash so the ninth series is not a twin of the first', () => {
    expect(seriesSlot(0).dash).toBeUndefined()
    expect(seriesSlot(8).color).toBe(seriesSlot(0).color)
    expect(seriesSlot(8).dash).toBeDefined()
    expect(seriesSlot(16).dash).not.toBe(seriesSlot(8).dash)
  })
})

describe('formatVersionLabel', () => {
  it('names the rolled-out newest release latest and an unrolled one pre-release', () => {
    expect(formatVersionLabel(version({ version: '2.0.0', is_latest: true }))).toBe('2.0.0 · latest')
    expect(formatVersionLabel(version({ version: '2.0.0', is_latest: true, is_active: false })))
      .toBe('2.0.0 · pre-release')
    expect(formatVersionLabel(version({ version: '', is_other: true, is_latest: true }))).toBe('Other')
    expect(formatVersionLabel(version({ version: '' }))).toBe('(empty)')
  })
})

describe('buildVersionChartSeries', () => {
  const series = [
    version({
      version: '2.0.0',
      is_latest: true,
      total_count: 0.3,
      data: [pt('2026-01-01T00:00:00Z', 0.1), pt('2026-01-01T12:00:00Z', 0.2)],
    }),
    version({ version: '1.9.0', total_count: 5, data: [pt('2026-01-01T00:00:00Z', 5)] }),
  ]

  it('keeps only the latest release under the Latest filter', () => {
    const built = buildVersionChartSeries(series, 'hour', 'latest', '2.0.0')
    expect(built.map(item => item.label)).toEqual(['2.0.0 · latest'])
    expect(at(built, 0).isHighlighted).toBe(true)
  })

  it('prints the newest value for a non-additive metric and averages its rollup', () => {
    const latest = at(buildVersionChartSeries(series, 'day', 'all', '2.0.0', 'mean'), 0)
    expect(latest.legendValue).toBe(0.2)
    expect(latest.data).toHaveLength(1)
    expect(at(latest.data, 0).count).toBeCloseTo(0.15, 10)
  })

  it('prints the window total and sums for an additive series', () => {
    const latest = at(buildVersionChartSeries(series, 'day', 'all', '2.0.0'), 0)
    expect(latest.legendValue).toBe(0.3)
    expect(at(latest.data, 0).count).toBeCloseTo(0.3, 10)
  })

  // The 7th slot used to fall back to --fg-subtle, the grey "Other" draws in,
  // and the 1st to the accent the latest release draws in.
  it('never draws two solid series in one colour', () => {
    const many = [
      version({ version: '3.0.0', is_latest: true }),
      ...Array.from({ length: 7 }, (_, index) => version({ version: `2.${index}.0` })),
      version({ version: '', is_other: true }),
    ]
    const built = buildVersionChartSeries(many, 'day', 'all', '3.0.0')
    expect(built).toHaveLength(9)
    const colors = built.map(item => item.color)
    expect(new Set(colors).size).toBe(colors.length)

    const preRelease = buildVersionChartSeries(
      [version({ version: '3.0.0', is_latest: true, is_active: false }), ...many.slice(1)],
      'day',
      'all',
      '3.0.0',
    )
    const preColors = preRelease.map(item => item.color)
    expect(new Set(preColors).size).toBe(preColors.length)
  })
})

describe('breakdown series', () => {
  const entries = buildBreakdownEntries(
    Array.from({ length: 10 }, (_, index) =>
      breakdown(`v${index}`, [pt('2026-01-01T00:00:00Z', 0.1), pt('2026-01-01T01:00:00Z', 0.3)], 0.4)),
    legendValueKindFor('mean'),
  )

  it('labels a chip with the newest value, not a sum of ratios', () => {
    expect(at(entries, 0).legendValue).toBe(0.3)
  })

  it('caps the chart at eight series and reports how many it left out', () => {
    const { series, hiddenCount } = selectBreakdownChartSeries(entries, [], 'hour', 'mean')
    expect(series).toHaveLength(8)
    expect(hiddenCount).toBe(2)
  })

  it('draws a picked value from beyond the cap, keeping its own slot', () => {
    const { series, hiddenCount } = selectBreakdownChartSeries(entries, ['v9'], 'day', 'mean')
    expect(series.map(item => item.label)).toEqual(['v9'])
    expect(at(series, 0).dash).toBe(seriesSlot(9).dash)
    expect(at(at(series, 0).data, 0).count).toBeCloseTo(0.2, 10)
    expect(hiddenCount).toBe(0)
  })
})
