import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  axisWidthForValues,
  dayBoundaryTicks,
  EVENTS_NOUN,
  formatAnomalyCount,
  formatCount,
  formatDayTick,
  formatSeriesValue,
  formatTick,
  formatTooltipLabel,
  SERIES_COLORS,
  seriesNounPlural,
  summarizeBuckets,
  summarizeForecastRange,
} from './chart-format'

/**
 * Calendar buckets (day / week / month) name a UTC bucket start, so they render
 * in UTC. Rendered in the viewer's local zone, a Monday week bucket prints as
 * "Week of Jun 7" (Sunday) west of Greenwich and a day bucket prints under the
 * wrong date — the axis would then disagree with the bucket the server
 * computed. Sub-day buckets are instants and render in local
 * time, like every other timestamp in the app. Node re-reads
 * `process.env.TZ` on every Date operation, so stubbing it swings the host zone
 * under the formatter.
 */
describe('bucket labels follow the zone policy', () => {
  const ZONES = ['UTC', 'Pacific/Kiritimati', 'America/Anchorage']

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

  // Monday 2026-06-08 at UTC midnight: Sunday the 7th in Anchorage, and already
  // Monday afternoon of the 8th... in Kiritimati it is 14:00 on the 8th.
  const MONDAY = '2026-06-08T00:00:00.000Z'

  it('labels the week bucket by its Monday in every zone', () => {
    for (const timeZone of ZONES) {
      expect(inZone(timeZone, () => formatTooltipLabel(MONDAY, 'week')))
        .toBe('Week of Jun 8, 2026')
      expect(inZone(timeZone, () => formatTick(MONDAY, 'week'))).toBe('Jun 8')
    }
  })

  it('labels a day bucket by its UTC date in every zone', () => {
    for (const timeZone of ZONES) {
      expect(inZone(timeZone, () => formatTooltipLabel(MONDAY, 'day'))).toBe('Jun 8, 2026')
      expect(inZone(timeZone, () => formatTick(MONDAY, 'day'))).toBe('Jun 8')
    }
  })

  it('labels an hour bucket by its LOCAL hour, like the rest of the app', () => {
    // 23:00 UTC is 13:00 on the 8th in Kiritimati (UTC+14) and 15:00 on the
    // 7th in Anchorage (UTC-8 in June). The chart used to print "11 PM" in
    // every zone while the signal card beside it printed local time.
    const lateHour = '2026-06-07T23:00:00.000Z'
    expect(inZone('UTC', () => formatTooltipLabel(lateHour, 'hour'))).toBe('Jun 7, 11:00 PM')
    expect(inZone('UTC', () => formatTick(lateHour, 'hour'))).toBe('Jun 7, 11 PM')
    expect(inZone('Pacific/Kiritimati', () => formatTooltipLabel(lateHour, 'hour')))
      .toBe('Jun 8, 01:00 PM')
    expect(inZone('America/Anchorage', () => formatTick(lateHour, 'hour'))).toBe('Jun 7, 03 PM')
    expect(inZone('America/Anchorage', () => formatTick(lateHour, '15min')))
      .toBe('Jun 7, 03:00 PM')
  })

  it('labels a month bucket by its UTC month in every zone', () => {
    // The first instant of July UTC is still June 30 in Anchorage.
    const july = '2026-07-01T00:00:00.000Z'
    for (const timeZone of ZONES) {
      expect(inZone(timeZone, () => formatTooltipLabel(july, 'month'))).toBe('July 2026')
      expect(inZone(timeZone, () => formatTick(july, 'month'))).toBe('Jul 2026')
    }
  })
})

describe('formatCount', () => {
  it('renders 6-digit values compactly so labels stay short', () => {
    // The bug: "380.0k" (6 chars) clipped its left digits against a fixed axis.
    expect(formatCount(380_000)).toBe('380k')
    expect(formatCount(285_000)).toBe('285k')
    expect(formatCount(750_000)).toBe('750k')
  })

  it('escalates to M before a value would render as "1000k"', () => {
    expect(formatCount(1_500_000)).toBe('1.5M')
    expect(formatCount(1_000_000)).toBe('1M')
    expect(formatCount(999_500)).toBe('1M')
  })

  it('keeps small values verbatim and one decimal below 100 units', () => {
    expect(formatCount(0)).toBe('0')
    expect(formatCount(842)).toBe('842')
    expect(formatCount(1_500)).toBe('1.5k')
  })

  it('never exceeds 5 characters across the 100k–9.9M range', () => {
    for (const v of [100_000, 285_000, 380_000, 999_499, 1_500_000, 9_900_000]) {
      expect(formatCount(v).length).toBeLessThanOrEqual(5)
    }
  })

  it('rounds sub-1000 fractions instead of leaking float noise', () => {
    // A confidence-band bound times the 1.1 overshoot: raw String() would emit
    // "-0.9900000000000001" (19 chars) and blow out the Y-axis width.
    expect(formatCount(-0.9 * 1.1)).toBe('-1')
    expect(formatCount(0.11000000000000001)).toBe('0')
    expect(formatCount(842.6)).toBe('843')
    expect(formatCount(-0.9 * 1.1).length).toBeLessThanOrEqual(3)
  })
})

describe('axisWidthForValues', () => {
  it('reserves more width for wide count labels than for small ones', () => {
    const narrow = axisWidthForValues([0, 10, 80], formatCount)
    const wide = axisWidthForValues([0, 285_000, 380_000], formatCount)
    expect(wide).toBeGreaterThan(narrow)
  })

  it('sizes the axis through a caller-supplied formatter (percent units)', () => {
    const pct = (v: number) => `${Math.round(v * 100)}%`
    const width = axisWidthForValues([0, 0.08, 0.05], pct)
    // "8%"/"5%" are short — axis stays near the floor, not blown out.
    expect(width).toBeGreaterThanOrEqual(40)
    expect(width).toBeLessThan(axisWidthForValues([0, 285_000], formatCount))
  })

  it('falls back to the minimum width for an empty series', () => {
    expect(axisWidthForValues([], formatCount)).toBe(40)
  })

  it('does not blow out when the min is a small negative fraction', () => {
    // Regression: the Structured Event tab fed a band lower bound of ~-0.9, and
    // min*1.1 float noise reserved a ~140px axis that shoved the plot right.
    const width = axisWidthForValues([-0.9, 550_000, 2_300_000], formatCount)
    expect(width).toBeLessThanOrEqual(64)
  })

  it('clamps pathological (non-finite / oversized) labels to the max width', () => {
    expect(axisWidthForValues([0, Infinity, NaN, 2_300_000], formatCount)).toBeLessThanOrEqual(80)
    const huge = (v: number) => `${v}`.padStart(40, '0')
    expect(axisWidthForValues([0, 1, 2], huge)).toBe(80)
  })
})

describe('formatAnomalyCount', () => {
  it('uses the singular for exactly one anomaly', () => {
    expect(formatAnomalyCount(1)).toBe('1 anomaly detected')
  })

  it('uses the plural for zero and for many', () => {
    expect(formatAnomalyCount(0)).toBe('0 anomalies detected')
    expect(formatAnomalyCount(2)).toBe('2 anomalies detected')
    expect(formatAnomalyCount(13)).toBe('13 anomalies detected')
  })
})

// Consecutive UTC day buckets; formatTooltipLabel renders day buckets in UTC (see the
// "bucket labels follow the zone policy" suite above), so the strings are stable.
const DAY_BUCKETS = [
  '2026-06-08T00:00:00.000Z',
  '2026-06-09T00:00:00.000Z',
  '2026-06-10T00:00:00.000Z',
  '2026-06-11T00:00:00.000Z',
  '2026-06-12T00:00:00.000Z',
  '2026-06-13T00:00:00.000Z',
]

describe('summarizeBuckets', () => {
  it('returns an empty string when there are no buckets', () => {
    expect(summarizeBuckets([], 'day')).toBe('')
  })

  it('humanizes bucket instants instead of leaking raw ISO', () => {
    expect(summarizeBuckets(DAY_BUCKETS.slice(0, 2), 'day')).toBe('Jun 8, 2026, Jun 9, 2026')
  })

  it('caps the enumeration at 5 and collapses the tail into "and N more"', () => {
    const summary = summarizeBuckets(DAY_BUCKETS, 'day') // 6 buckets
    expect(summary).toBe(
      'Jun 8, 2026, Jun 9, 2026, Jun 10, 2026, Jun 11, 2026, Jun 12, 2026, and 1 more',
    )
    expect(summary).not.toContain('2026-06-13') // never reads out the raw tail
  })

  it('shows every bucket with no "more" suffix at exactly the cap', () => {
    const summary = summarizeBuckets(DAY_BUCKETS.slice(0, 5), 'day')
    expect(summary).not.toContain('more')
    expect(summary.endsWith('Jun 12, 2026')).toBe(true)
  })
})

describe('summarizeForecastRange', () => {
  it('returns an empty string when there is no forecast', () => {
    expect(summarizeForecastRange([], 'day')).toBe('')
  })

  it('names a single forecast bucket', () => {
    expect(summarizeForecastRange(['2026-06-08T00:00:00.000Z'], 'day')).toBe('Forecast for Jun 8, 2026')
  })

  it('collapses a multi-bucket forecast into a single range', () => {
    expect(
      summarizeForecastRange(['2026-06-08T00:00:00.000Z', '2026-06-12T00:00:00.000Z'], 'day'),
    ).toBe('Forecast from Jun 8, 2026 to Jun 12, 2026')
  })
})

// a 7-day hourly axis used to tick every ~21 hours at odd times.
describe('dayBoundaryTicks', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
  })

  function hourly(startIso: string, hours: number): string[] {
    const start = Date.parse(startIso)
    return Array.from({ length: hours }, (_, index) =>
      new Date(start + index * 3_600_000).toISOString(),
    )
  }

  it('puts one tick on each local midnight for a multi-day hourly series', () => {
    vi.stubEnv('TZ', 'UTC')
    const buckets = hourly('2026-09-20T13:00:00.000Z', 24 * 3)
    const ticks = dayBoundaryTicks(buckets, 'hour')
    // The first bucket is mid-day, so it gets no tick of its own.
    expect(ticks).toEqual([
      '2026-09-21T00:00:00.000Z',
      '2026-09-22T00:00:00.000Z',
      '2026-09-23T00:00:00.000Z',
    ])
    expect(ticks?.map(formatDayTick)).toEqual(['Sep 21', 'Sep 22', 'Sep 23'])
  })

  it('follows the local calendar day, not the UTC one', () => {
    vi.stubEnv('TZ', 'Asia/Tokyo') // UTC+9: local midnight is 15:00 UTC
    const ticks = dayBoundaryTicks(hourly('2026-09-20T00:00:00.000Z', 24 * 3), 'hour')
    expect(ticks?.[0]).toBe('2026-09-20T15:00:00.000Z')
  })

  // A UTC hour bucket starts at xx:30 in a half-hour zone; an hour-only tick
  // read "05 AM" under a "05:30 AM" tooltip.
  it('keeps the minutes on an hourly tick in a half-hour zone', () => {
    const bucket = '2026-09-20T00:00:00.000Z'
    vi.stubEnv('TZ', 'Asia/Kolkata')
    expect(formatTick(bucket, 'hour')).toMatch(/05:30\sAM/)
    expect(formatTick(bucket, '6h')).toMatch(/05:30\sAM/)
    vi.stubEnv('TZ', 'UTC')
    expect(formatTick(bucket, 'hour')).toMatch(/12\sAM$/)
    expect(formatTick(bucket, 'hour')).not.toMatch(/:/)
  })

  it('counts a first bucket that sits exactly on local midnight', () => {
    vi.stubEnv('TZ', 'UTC')
    const ticks = dayBoundaryTicks(hourly('2026-09-20T00:00:00.000Z', 24 * 2 + 1), 'hour')
    expect(ticks?.[0]).toBe('2026-09-20T00:00:00.000Z')
  })

  it('thins the ticks to at most eight on a long range', () => {
    vi.stubEnv('TZ', 'UTC')
    const ticks = dayBoundaryTicks(hourly('2026-09-01T00:00:00.000Z', 24 * 30), 'hour')
    expect(ticks).not.toBeNull()
    expect(ticks!.length).toBeLessThanOrEqual(8)
    expect(ticks![0]).toBe('2026-09-01T00:00:00.000Z')
  })

  it('keeps the default ticks under two days and for calendar buckets', () => {
    vi.stubEnv('TZ', 'UTC')
    expect(dayBoundaryTicks(hourly('2026-09-20T00:00:00.000Z', 30), 'hour')).toBeNull()
    expect(
      dayBoundaryTicks(['2026-09-20T00:00:00.000Z', '2026-09-27T00:00:00.000Z'], 'day'),
    ).toBeNull()
  })
})

// "1 events" in a single-event bucket, and two locales in one tooltip.
describe('formatSeriesValue', () => {
  it('agrees the default noun with the count', () => {
    expect(formatSeriesValue(1, EVENTS_NOUN)).toBe('1 event')
    expect(formatSeriesValue(1234, EVENTS_NOUN)).toBe('1,234 events')
    expect(formatSeriesValue(0, EVENTS_NOUN)).toBe('0 events')
  })

  it('treats the bare "events" string as the default noun', () => {
    expect(formatSeriesValue(1, 'events')).toBe('1 event')
  })

  it('prints any other string as written', () => {
    expect(formatSeriesValue(0.08, '%')).toBe('0.08 %')
    expect(formatSeriesValue(2, { singular: 'row', plural: 'rows' })).toBe('2 rows')
  })

  it('names the series by its plural', () => {
    expect(seriesNounPlural(EVENTS_NOUN)).toBe('events')
    expect(seriesNounPlural('events (p95)')).toBe('events (p95)')
  })
})

// DS-23 / MON-36: fixed hexes that ignored dark mode, and a danger-red series.
describe('SERIES_COLORS', () => {
  it('reads a theme token for every slot and never the danger colour', () => {
    expect(SERIES_COLORS).toHaveLength(8)
    SERIES_COLORS.forEach((color, index) => {
      expect(color.startsWith(`var(--series-${index + 1},`)).toBe(true)
      expect(color).not.toMatch(/#[0-9a-f]{3,6}/i)
      expect(color).not.toContain('--chart-5')
      expect(color).not.toMatch(/^var\(--series-\d+, var\(--(danger|destructive)\)\)$/)
    })
  })

  // The By version chart draws the latest release in --primary (the accent), a
  // pre-release in --warning and "Other" in --fg-tertiary (once --muted-foreground):
  // a slot built from any of those drew two lines in one colour, and under the
  // rose accent --chart-1 was the anomaly red.
  it('builds no fallback from a neutral, accent or status token', () => {
    const reserved = /--(primary|accent|chart-\d|warning|danger|destructive|success|info|muted-foreground|fg-tertiary|fg-subtle|fg)\b/
    SERIES_COLORS.forEach(color => {
      const fallback = color.replace(/^var\(--series-\d+,\s*/, '')
      expect(fallback).not.toMatch(reserved)
      expect(fallback).toMatch(/^light-dark\(oklch\([^)]*\), oklch\([^)]*\)\)\)$/)
    })
    expect(new Set(SERIES_COLORS).size).toBe(SERIES_COLORS.length)
  })

  it('keeps every fallback hue out of the danger / warning band', () => {
    SERIES_COLORS.forEach(color => {
      const hues = [...color.matchAll(/oklch\([\d.]+ [\d.]+ ([\d.]+)\)/g)].map(m => Number(m[1]))
      expect(hues).toHaveLength(2)
      hues.forEach(hue => {
        expect(hue < 0 || hue > 85).toBe(true)
        expect(hue).toBeLessThan(355)
      })
    })
  })
})
