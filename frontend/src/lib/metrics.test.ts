import { afterEach, describe, expect, it, vi } from 'vitest'
import type { EventMetricPoint } from '@/types'
import {
  aggregateMetricPoints,
  clampGranularityToRange,
  coarserGranularity,
  defaultGranularityForRange,
  getBucketStart,
  granularityFitsRange,
} from './metrics'
import { at } from '@/test/at'

function point(overrides: Partial<EventMetricPoint> & { bucket: string }): EventMetricPoint {
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

describe('defaultGranularityForRange', () => {
  it('keeps hourly buckets for a week or less', () => {
    expect(defaultGranularityForRange(1)).toBe('hour')
    expect(defaultGranularityForRange(7)).toBe('hour')
  })

  it('steps up to daily buckets past a week through a month', () => {
    // 30d hourly would be ~720 points — an unreadable comb.
    expect(defaultGranularityForRange(8)).toBe('day')
    expect(defaultGranularityForRange(30)).toBe('day')
  })

  it('steps up to weekly buckets beyond a month', () => {
    expect(defaultGranularityForRange(31)).toBe('week')
    expect(defaultGranularityForRange(90)).toBe('week')
  })
})

describe('getBucketStart', () => {
  it('floors to the start of the UTC 15-minute bucket', () => {
    // Every backend interval must have a granularity that matches it. A 15m metric
    // used to chart under "Hours", naming the axis after a bucket width the data
    // does not have.
    expect(getBucketStart('2026-06-10T13:47:31.500Z', '15min'))
      .toBe('2026-06-10T13:45:00.000Z')
    expect(getBucketStart('2026-06-10T13:00:00.000Z', '15min'))
      .toBe('2026-06-10T13:00:00.000Z')
    expect(getBucketStart('2026-06-10T13:14:59.999Z', '15min'))
      .toBe('2026-06-10T13:00:00.000Z')
  })

  it('keeps the four 15-minute buckets of an hour distinct', () => {
    // The whole point of the granularity: they must not collapse onto one bucket.
    const quarters = ['13:00', '13:15', '13:30', '13:45'].map((hm) =>
      getBucketStart(`2026-06-10T${hm}:07Z`, '15min'),
    )
    expect(new Set(quarters).size).toBe(4)
  })

  it('floors to the start of the UTC 6-hour bucket', () => {
    // 6h divides a UTC day evenly, so the epoch grid lands on 00/06/12/18.
    expect(getBucketStart('2026-06-10T13:47:31.500Z', '6h'))
      .toBe('2026-06-10T12:00:00.000Z')
    expect(getBucketStart('2026-06-10T05:59:59.999Z', '6h'))
      .toBe('2026-06-10T00:00:00.000Z')
    expect(getBucketStart('2026-06-10T18:00:00.000Z', '6h'))
      .toBe('2026-06-10T18:00:00.000Z')
  })

  it('floors to the start of the UTC hour', () => {
    expect(getBucketStart('2026-06-10T13:47:31.500Z', 'hour'))
      .toBe('2026-06-10T13:00:00.000Z')
  })

  it('floors to the start of the UTC day', () => {
    expect(getBucketStart('2026-06-10T13:47:31.500Z', 'day'))
      .toBe('2026-06-10T00:00:00.000Z')
  })

  it('floors to the first of the UTC month', () => {
    expect(getBucketStart('2026-06-10T13:47:31.500Z', 'month'))
      .toBe('2026-06-01T00:00:00.000Z')
  })

  // Buckets are half-open [start, next): a timestamp exactly on a boundary
  // belongs to the bucket it opens, never to the one it closes. Matches the
  // window contract in backend/src/tripl/core/bucketing.py.
  it('keeps a timestamp exactly on a boundary in the bucket it opens', () => {
    expect(getBucketStart('2026-06-10T13:00:00.000Z', 'hour'))
      .toBe('2026-06-10T13:00:00.000Z')
    expect(getBucketStart('2026-06-10T00:00:00.000Z', 'day'))
      .toBe('2026-06-10T00:00:00.000Z')
    // 2026-06-08 is a Monday — the start of its own week bucket.
    expect(getBucketStart('2026-06-08T00:00:00.000Z', 'week'))
      .toBe('2026-06-08T00:00:00.000Z')
    expect(getBucketStart('2026-06-01T00:00:00.000Z', 'month'))
      .toBe('2026-06-01T00:00:00.000Z')
  })

  it('puts the last millisecond of a bucket in that bucket, not the next', () => {
    expect(getBucketStart('2026-06-10T13:59:59.999Z', 'hour'))
      .toBe('2026-06-10T13:00:00.000Z')
    expect(getBucketStart('2026-06-10T23:59:59.999Z', 'day'))
      .toBe('2026-06-10T00:00:00.000Z')
    // Sunday 2026-06-14 23:59:59.999 still belongs to the Monday 06-08 week.
    expect(getBucketStart('2026-06-14T23:59:59.999Z', 'week'))
      .toBe('2026-06-08T00:00:00.000Z')
  })

  // Weeks START ON MONDAY, anchored at 1970-01-05 (the first Monday of the
  // epoch) — see WEEK_ORIGIN in backend/src/tripl/core/bucketing.py. Binning
  // straight off the epoch lands weeks on a THURSDAY (1970-01-01 was one),
  // which is exactly the bug this suite now guards.
  it('snaps a week bucket back to Monday', () => {
    // 2026-06-10 is a Wednesday -> back to Monday 2026-06-08.
    expect(getBucketStart('2026-06-10T23:59:59Z', 'week'))
      .toBe('2026-06-08T00:00:00.000Z')
  })

  it('keeps Sunday in the week its Monday opened', () => {
    // 2026-06-14 is a Sunday -> back to Monday 2026-06-08, NOT forward.
    expect(getBucketStart('2026-06-14T12:00:00Z', 'week'))
      .toBe('2026-06-08T00:00:00.000Z')
  })

  it('starts a new week bucket on the following Monday', () => {
    // 2026-06-15 is the next Monday -> opens its own bucket.
    expect(getBucketStart('2026-06-15T00:00:00Z', 'week'))
      .toBe('2026-06-15T00:00:00.000Z')
  })

  it('crosses a month boundary when the week Monday is in the previous month', () => {
    // 2026-07-01 is a Wednesday -> back to Monday 2026-06-29.
    expect(getBucketStart('2026-07-01T00:00:00Z', 'week'))
      .toBe('2026-06-29T00:00:00.000Z')
  })

  it('lands every week bucket on a Monday across a full year of samples', () => {
    // The regression that started this: an epoch-anchored 7-day grid puts every
    // boundary on a Thursday. Sweep a year of hourly-ish samples and assert the
    // floor is always a Monday at UTC midnight.
    const start = Date.UTC(2026, 0, 1)
    for (let hours = 0; hours < 365 * 24; hours += 7) {
      const sample = new Date(start + hours * 60 * 60 * 1000).toISOString()
      const bucket = new Date(getBucketStart(sample, 'week'))
      expect(bucket.getUTCDay()).toBe(1) // 1 = Monday
      expect(bucket.getUTCHours()).toBe(0)
      expect(bucket.getUTCMinutes()).toBe(0)
      expect(bucket.getUTCSeconds()).toBe(0)
      expect(bucket.getUTCMilliseconds()).toBe(0)
      // Half-open: the bucket never starts after the sample it contains.
      expect(bucket.getTime()).toBeLessThanOrEqual(Date.parse(sample))
    }
  })
})

/**
 * The bucket a point lands in must be a property of the UTC instant alone. If
 * any flooring reaches for local-time date math (getDate/getHours/setHours), a
 * viewer in UTC+14 buckets a timestamp a day away from the one the warehouse
 * computed and stored — the chart would then draw an anomaly against the wrong
 * bucket. Node re-reads `process.env.TZ` on every Date operation, so stubbing it
 * exercises the real regression rather than trusting CI to run under UTC.
 */
describe('getBucketStart is timezone-independent', () => {
  const ZONES = [
    'UTC',
    'Pacific/Kiritimati', // UTC+14: local date runs AHEAD of UTC
    'America/Anchorage', // UTC-9: local date runs BEHIND UTC
    'Asia/Kathmandu', // UTC+5:45: not even a whole-hour offset
  ]

  afterEach(() => {
    vi.unstubAllEnvs()
  })

  function inZone<T>(timeZone: string, run: () => T): T {
    vi.stubEnv('TZ', timeZone)
    try {
      return run()
    } finally {
      vi.unstubAllEnvs()
    }
  }

  // Instants deliberately parked next to a UTC midnight, where the local
  // calendar date differs from the UTC one in at least one zone above.
  const CASES: { instant: string; hour: string; day: string; week: string; month: string }[] = [
    {
      // Late Sunday UTC: already Monday in Kiritimati.
      instant: '2026-06-07T23:30:00Z',
      hour: '2026-06-07T23:00:00.000Z',
      day: '2026-06-07T00:00:00.000Z',
      week: '2026-06-01T00:00:00.000Z',
      month: '2026-06-01T00:00:00.000Z',
    },
    {
      // Just past Monday UTC midnight: still Sunday in Anchorage.
      instant: '2026-06-08T00:30:00Z',
      hour: '2026-06-08T00:00:00.000Z',
      day: '2026-06-08T00:00:00.000Z',
      week: '2026-06-08T00:00:00.000Z',
      month: '2026-06-01T00:00:00.000Z',
    },
    {
      // First instant of a UTC month, which is still the previous month locally
      // west of Greenwich.
      instant: '2026-07-01T00:00:00Z',
      hour: '2026-07-01T00:00:00.000Z',
      day: '2026-07-01T00:00:00.000Z',
      week: '2026-06-29T00:00:00.000Z',
      month: '2026-07-01T00:00:00.000Z',
    },
  ]

  for (const timeZone of ZONES) {
    it(`floors identically under ${timeZone}`, () => {
      inZone(timeZone, () => {
        for (const expected of CASES) {
          expect(getBucketStart(expected.instant, 'hour')).toBe(expected.hour)
          expect(getBucketStart(expected.instant, 'day')).toBe(expected.day)
          expect(getBucketStart(expected.instant, 'week')).toBe(expected.week)
          expect(getBucketStart(expected.instant, 'month')).toBe(expected.month)
        }
      })
    })
  }

  it('groups the same points into the same buckets in every zone', () => {
    // Two hours that straddle UTC midnight: one calendar day in UTC, but two
    // different local days in both Kiritimati and Anchorage.
    const points = [
      point({ bucket: '2026-06-08T00:30:00Z', count: 3 }),
      point({ bucket: '2026-06-08T23:30:00Z', count: 4 }),
    ]

    const byZone = ZONES.map(timeZone =>
      inZone(timeZone, () => aggregateMetricPoints(points, 'day')),
    )

    for (const result of byZone) {
      expect(result).toHaveLength(1)
      expect(at(result, 0).bucket).toBe('2026-06-08T00:00:00.000Z')
      expect(at(result, 0).count).toBe(7)
    }
  })
})

describe('aggregateMetricPoints', () => {
  it('groups points into buckets and sums counts, returning sorted buckets', () => {
    const result = aggregateMetricPoints(
      [
        point({ bucket: '2026-06-10T11:30:00Z', count: 5 }),
        point({ bucket: '2026-06-10T10:00:00Z', count: 2 }),
        point({ bucket: '2026-06-10T10:45:00Z', count: 3 }),
      ],
      'hour',
    )
    expect(result.map(p => p.bucket)).toEqual([
      '2026-06-10T10:00:00.000Z',
      '2026-06-10T11:00:00.000Z',
    ])
    expect(at(result, 0).count).toBe(5)
    expect(at(result, 1).count).toBe(5)
  })

  it('adds variance across buckets: stddev = sqrt(sum of squares)', () => {
    const result = aggregateMetricPoints(
      [
        point({ bucket: '2026-06-10T10:00:00Z', count: 1, stddev: 3 }),
        point({ bucket: '2026-06-10T10:30:00Z', count: 1, stddev: 4 }),
      ],
      'hour',
    )
    // sqrt(3^2 + 4^2) = sqrt(25) = 5
    expect(result).toHaveLength(1)
    expect(at(result, 0).stddev).toBe(5)
  })

  it('returns null stddev when every source bucket has null stddev', () => {
    const result = aggregateMetricPoints(
      [
        point({ bucket: '2026-06-10T10:00:00Z', count: 1, stddev: null }),
        point({ bucket: '2026-06-10T10:30:00Z', count: 2, stddev: null }),
      ],
      'hour',
    )
    expect(at(result, 0).stddev).toBeNull()
  })

  it('treats all-zero stddevs as zero, not null', () => {
    const result = aggregateMetricPoints(
      [
        point({ bucket: '2026-06-10T10:00:00Z', count: 1, stddev: 0 }),
        point({ bucket: '2026-06-10T10:30:00Z', count: 2, stddev: 0 }),
      ],
      'hour',
    )
    expect(at(result, 0).stddev).toBe(0)
  })

  it('rolls up expected_count only when every source carries a baseline', () => {
    // A partial baseline (only one source hour scored) must NOT be summed
    // against a full-count aggregate — that yields expected ~1/N of the count
    // and a nonsensical tooltip. Drop it to null instead.
    const mixed = aggregateMetricPoints(
      [
        point({ bucket: '2026-06-10T10:00:00Z', expected_count: null }),
        point({ bucket: '2026-06-10T10:30:00Z', expected_count: 7 }),
      ],
      'hour',
    )
    expect(at(mixed, 0).expected_count).toBeNull()

    const allPresent = aggregateMetricPoints(
      [
        point({ bucket: '2026-06-10T10:00:00Z', expected_count: 5 }),
        point({ bucket: '2026-06-10T10:30:00Z', expected_count: 7 }),
      ],
      'hour',
    )
    expect(at(allPresent, 0).expected_count).toBe(12)

    const allNull = aggregateMetricPoints(
      [
        point({ bucket: '2026-06-10T10:00:00Z', expected_count: null }),
        point({ bucket: '2026-06-10T10:30:00Z', expected_count: null }),
      ],
      'hour',
    )
    expect(at(allNull, 0).expected_count).toBeNull()
  })

  it('does not flag an aggregated day when one anomalous hour leaves the day total unremarkable', () => {
    // Six normal hours (count 100, expected 100, stddev 10) plus one hour that
    // was flagged upstream with a mild spike (count 130). The rolled-up day is
    // count 730 vs expected 700, stddev sqrt(7*100) ~= 26.5, z ~= 1.13 — well
    // under the ~3 sigma bar, so the day must stay un-reddened.
    const hourly: EventMetricPoint[] = []
    for (let hour = 0; hour < 6; hour += 1) {
      const stamp = String(hour).padStart(2, '0')
      hourly.push(
        point({ bucket: `2026-06-10T${stamp}:00:00Z`, count: 100, expected_count: 100, stddev: 10 }),
      )
    }
    hourly.push(
      point({
        bucket: '2026-06-10T06:00:00Z',
        count: 130,
        expected_count: 100,
        stddev: 10,
        is_anomaly: true,
        anomaly_direction: 'spike',
        z_score: 3,
      }),
    )

    const day = at(aggregateMetricPoints(hourly, 'day'), 0)

    expect(day.is_anomaly).toBe(false)
    expect(day.z_score).toBeNull()
    expect(day.anomaly_direction).toBeNull()
    // Expected is the coherent sum of the seven baselines, not ~1/7 of count.
    expect(day.count).toBe(730)
    expect(day.expected_count).toBe(700)
    expect(day.expected_count).toBeGreaterThan(day.count / 2)
  })

  it('flags an aggregated day when the rolled-up total is itself significant', () => {
    // Every hour is elevated (count 160 vs expected 100, stddev 10): the day
    // total is 960 vs expected 600, stddev sqrt(600) ~= 24.5, z ~= 14.7.
    const hourly: EventMetricPoint[] = []
    for (let hour = 0; hour < 6; hour += 1) {
      const stamp = String(hour).padStart(2, '0')
      hourly.push(
        point({
          bucket: `2026-06-11T${stamp}:00:00Z`,
          count: 160,
          expected_count: 100,
          stddev: 10,
          is_anomaly: true,
          anomaly_direction: 'spike',
          z_score: 6,
        }),
      )
    }

    const day = at(aggregateMetricPoints(hourly, 'day'), 0)

    expect(day.is_anomaly).toBe(true)
    expect(day.anomaly_direction).toBe('spike')
    expect(day.z_score).not.toBeNull()
    // Comfortably past the ~3 sigma bar the aggregate re-test applies.
    expect(day.z_score as number).toBeGreaterThan(3)
    expect(day.count).toBe(960)
    expect(day.expected_count).toBe(600)
  })

  it('selects the strongest anomaly by absolute z-score within a bucket', () => {
    const result = aggregateMetricPoints(
      [
        point({
          bucket: '2026-06-10T10:00:00Z',
          is_anomaly: true,
          anomaly_direction: 'drop',
          z_score: -2,
        }),
        point({
          bucket: '2026-06-10T10:30:00Z',
          is_anomaly: true,
          anomaly_direction: 'spike',
          z_score: 5,
        }),
        point({
          bucket: '2026-06-10T10:45:00Z',
          is_anomaly: false,
          z_score: 100,
        }),
      ],
      'hour',
    )
    expect(at(result, 0).is_anomaly).toBe(true)
    expect(at(result, 0).z_score).toBe(5)
    expect(at(result, 0).anomaly_direction).toBe('spike')
  })

  it('picks the largest magnitude even when the strongest is negative', () => {
    const result = aggregateMetricPoints(
      [
        point({
          bucket: '2026-06-10T10:00:00Z',
          is_anomaly: true,
          anomaly_direction: 'drop',
          z_score: -8,
        }),
        point({
          bucket: '2026-06-10T10:30:00Z',
          is_anomaly: true,
          anomaly_direction: 'spike',
          z_score: 3,
        }),
      ],
      'hour',
    )
    expect(at(result, 0).z_score).toBe(-8)
    expect(at(result, 0).anomaly_direction).toBe('drop')
  })

  it('marks a bucket as non-anomalous when no source point is an anomaly', () => {
    const result = aggregateMetricPoints(
      [point({ bucket: '2026-06-10T10:00:00Z', count: 4, is_anomaly: false })],
      'hour',
    )
    expect(at(result, 0).is_anomaly).toBe(false)
    expect(at(result, 0).z_score).toBeNull()
    expect(at(result, 0).anomaly_direction).toBeNull()
  })
})

describe('aggregateMetricPoints rollup mode', () => {
  // 24 hourly readings of an 8 % conversion rate, stored as the fraction 0.08.
  const hourlyRatio = Array.from({ length: 24 }, (_, hour) =>
    point({
      bucket: `2026-06-10T${String(hour).padStart(2, '0')}:00:00Z`,
      count: 0.08,
      expected_count: 0.07,
      stddev: 0.01,
    }))

  it('averages a non-additive metric instead of summing it', () => {
    const day = at(aggregateMetricPoints(hourlyRatio, 'day', 'mean'), 0)
    // Summing would plot 1.92 — "192 %" for an 8 % rate.
    expect(day.count).toBeCloseTo(0.08, 10)
  })

  it('drops the baseline and does not re-test a merged mean bucket', () => {
    const [day] = aggregateMetricPoints(
      hourlyRatio.map((p, index) => (index === 3
        ? { ...p, is_anomaly: true, anomaly_direction: 'spike' as const, z_score: 5 }
        : p)),
      'day',
      'mean',
    )
    if (!day) throw new Error('expected one day bucket')
    expect(day.expected_count).toBeNull()
    expect(day.stddev).toBeNull()
    // The flagged hour still shows: there is no rolled-up baseline to clear it.
    expect(day.is_anomaly).toBe(true)
    expect(day.z_score).toBe(5)
  })

  it('passes a single-point bucket through untouched in mean mode', () => {
    const first = at(hourlyRatio, 0)
    const [only] = aggregateMetricPoints([first], 'day', 'mean')
    expect(only).toEqual({ ...first, bucket: '2026-06-10T00:00:00.000Z' })
  })

  it('still sums additive series by default', () => {
    const [day] = aggregateMetricPoints(
      [
        point({ bucket: '2026-06-10T01:00:00Z', count: 5 }),
        point({ bucket: '2026-06-10T02:00:00Z', count: 7 }),
      ],
      'day',
    )
    expect(day?.count).toBe(12)
  })
})

describe('granularity point cap', () => {
  it('orders granularities by bucket width', () => {
    expect(coarserGranularity('hour', 'day')).toBe('day')
    expect(coarserGranularity('week', '6h')).toBe('week')
    expect(coarserGranularity('15min', '15min')).toBe('15min')
  })

  it('refuses a granularity that would draw more than 500 points', () => {
    expect(granularityFitsRange('hour', 7)).toBe(true)
    expect(granularityFitsRange('15min', 7)).toBe(false)
    expect(granularityFitsRange('hour', 30)).toBe(false)
    expect(granularityFitsRange('6h', 90)).toBe(true)
  })

  it('bumps a too-fine pick up to the finest one that fits the range', () => {
    expect(clampGranularityToRange('15min', 7)).toBe('hour')
    expect(clampGranularityToRange('15min', 90)).toBe('6h')
    expect(clampGranularityToRange('hour', 30)).toBe('6h')
    // A pick that already fits is left alone.
    expect(clampGranularityToRange('week', 7)).toBe('week')
  })

  it('always allows the native collection granularity, however many points it draws', () => {
    // A 15 min series at the smallest preset: over the cap, still offered.
    expect(granularityFitsRange('15min', 7, '15min')).toBe(true)
    expect(granularityFitsRange('15min', 90, '15min')).toBe(true)
    expect(granularityFitsRange('hour', 90, 'hour')).toBe(true)
    // Finer than native is still held to the cap.
    expect(granularityFitsRange('15min', 7, 'hour')).toBe(false)
    expect(granularityFitsRange('hour', 90, '15min')).toBe(false)
  })

  it('never bumps the native granularity, only picks finer than it', () => {
    expect(clampGranularityToRange('15min', 7, '15min')).toBe('15min')
    expect(clampGranularityToRange('15min', 90, '15min')).toBe('15min')
    expect(clampGranularityToRange('15min', 7, 'hour')).toBe('hour')
    // A too-fine pick stops at native instead of jumping past it.
    expect(clampGranularityToRange('15min', 90, 'hour')).toBe('hour')
    // A non-native pick over the cap is still clamped for a 15 min series.
    expect(clampGranularityToRange('hour', 90, '15min')).toBe('6h')
  })
})
