import { describe, expect, it } from 'vitest'

import type { EventMetricPoint } from '@/types'
import { at } from '@/test/at'
import { buildChartData, hasExpectedDot } from './chart'

// the detector stores the baseline of every bucket it scores,
// so the band is drawn on unflagged buckets too, not only on anomalies.
function point(bucket: string, overrides: Partial<EventMetricPoint> = {}): EventMetricPoint {
  return {
    bucket,
    count: 10,
    expected_count: null,
    stddev: null,
    is_anomaly: false,
    anomaly_direction: null,
    z_score: null,
    ...overrides,
  }
}

describe('buildChartData stored baseline band', () => {
  it('draws the band and expected value on an unflagged scored bucket', () => {
    const built = at(
      buildChartData([point('2026-01-01T10:00:00Z', { baseline_expected: 12, baseline_stddev: 1 })], [], 3),
      0,
    )
    expect(built.expected_count).toBe(12)
    expect(built.band).toEqual([9, 15])
  })

  it('keeps the anomaly row authoritative on a flagged bucket', () => {
    const flagged = point('2026-01-01T11:00:00Z', {
      count: 0,
      expected_count: 20,
      stddev: 2,
      is_anomaly: true,
      anomaly_direction: 'drop',
      z_score: -10,
      baseline_expected: 14,
      baseline_stddev: 1,
    })
    const built = at(buildChartData([flagged], [], 3), 0)
    expect(built.expected_count).toBe(20)
    expect(built.band).toEqual([14, 26])
    expect(built.expected_error).toEqual([6, 6])
  })

  it('leaves a bucket with no stored baseline bandless', () => {
    const built = at(buildChartData([point('2026-01-01T10:00:00Z')], [], 3), 0)
    expect(built.band).toBeUndefined()
    expect(built.expected_count).toBeNull()
  })

  it('whiskers only the buckets the band area cannot paint', () => {
    const built = buildChartData(
      [
        point('2026-01-01T10:00:00Z', { baseline_expected: 10, baseline_stddev: 1 }),
        point('2026-01-01T11:00:00Z', { baseline_expected: 10, baseline_stddev: 1 }),
        point('2026-01-01T12:00:00Z'),
        point('2026-01-01T13:00:00Z', { baseline_expected: 10, baseline_stddev: 1 }),
      ],
      [],
      2,
    )
    // A run of two: the area fills it, so no whisker.
    expect(at(built, 0).expected_error).toBeUndefined()
    expect(at(built, 1).expected_error).toBeUndefined()
    // Unscored bucket between them breaks the band.
    expect(at(built, 2).band).toBeUndefined()
    // An isolated scored bucket gets the whisker.
    expect(at(built, 3).expected_error).toEqual([2, 2])
  })

  it('clamps a count baseline band at zero', () => {
    const built = at(
      buildChartData([point('2026-01-01T10:00:00Z', { baseline_expected: 2, baseline_stddev: 1 })], [], 4, true),
      0,
    )
    expect(built.band).toEqual([0, 6])
  })
})

describe('hasExpectedDot', () => {
  it('paints the hollow point exactly where the whisker is, however long the chart', () => {
    // Longer than the old whole-chart dot threshold (30 scored rows).
    const data = Array.from({ length: 40 }, (_, index) =>
      point(`2026-01-02T${String(index % 24).padStart(2, '0')}:00:00Z`, {
        baseline_expected: 10,
        baseline_stddev: 1,
      }),
    )
    data[20] = point('2026-01-02T20:00:00Z', {
      count: 0,
      expected_count: 10,
      stddev: 1,
      is_anomaly: true,
      anomaly_direction: 'drop',
      z_score: -10,
    })
    data[35] = point('2026-01-02T11:00:00Z')
    data[37] = point('2026-01-02T13:00:00Z')
    const built = buildChartData(data, [], 2)

    // A flagged bucket and an isolated scored bucket keep their dot.
    expect(hasExpectedDot(at(built, 20))).toBe(true)
    expect(hasExpectedDot(at(built, 36))).toBe(true)
    // A run of scored buckets stays a plain dashed line.
    expect(hasExpectedDot(at(built, 5))).toBe(false)
    // An unscored bucket draws nothing.
    expect(hasExpectedDot(at(built, 35))).toBe(false)
    // Every whisker has a dot, so none is a bare error bar.
    for (const row of built) {
      expect(hasExpectedDot(row)).toBe(row.expected_error != null)
    }
  })

  it('draws nothing for a missing payload', () => {
    expect(hasExpectedDot(undefined)).toBe(false)
  })
})
