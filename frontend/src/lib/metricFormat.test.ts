import { describe, expect, it } from 'vitest'
import {
  METRIC_INTERVAL_LABEL,
  formatMetricValue,
  isIntervalFinerThan,
  isPercentUnit,
  metricAxisFormatter,
  metricCadence,
} from './metricFormat'

describe('isPercentUnit', () => {
  it('is true for "%" including surrounding whitespace', () => {
    expect(isPercentUnit('%')).toBe(true)
    expect(isPercentUnit(' % ')).toBe(true)
  })

  it('is false for other units, empty, null, and undefined', () => {
    expect(isPercentUnit('ms')).toBe(false)
    expect(isPercentUnit('pct')).toBe(false)
    expect(isPercentUnit('')).toBe(false)
    expect(isPercentUnit(null)).toBe(false)
    expect(isPercentUnit(undefined)).toBe(false)
  })
})

describe('formatMetricValue', () => {
  it('renders an em dash for null and undefined', () => {
    expect(formatMetricValue(null, '%')).toBe('—')
    expect(formatMetricValue(undefined, null)).toBe('—')
  })

  it('multiplies percent-unit fractions by 100', () => {
    expect(formatMetricValue(0.08, '%')).toBe('8%')
    expect(formatMetricValue(0.0812, '%')).toBe('8.12%')
    expect(formatMetricValue(0.5, '%')).toBe('50%')
  })

  it('rounds percent displays >= 100 to whole numbers', () => {
    // 1.056 → 105.6% → whole-number rounding above the 100 threshold.
    expect(formatMetricValue(1.056, '%')).toBe('106%')
    expect(formatMetricValue(42, '%')).toBe('4,200%')
  })

  it('spells a percent the way the chart axis does', () => {
    expect(formatMetricValue(0.08, '%')).toBe(metricAxisFormatter('%')(0.08))
  })

  it('keeps two decimals for other units from 1 up', () => {
    expect(formatMetricValue(0.08, 'ratio')).toBe('0.08 ratio')
    expect(formatMetricValue(12.346, null)).toBe('12.35')
    expect(formatMetricValue(1234.56, 'ms')).toBe('1,235 ms')
  })

  it('keeps small magnitudes visible instead of rounding them to 0', () => {
    expect(formatMetricValue(0.004, 's')).toBe('0.004 s')
    expect(formatMetricValue(0.00123, null)).toBe('0.0012')
    expect(formatMetricValue(0, 'ms')).toBe('0 ms')
  })

  it('puts a currency unit in front of the number', () => {
    expect(formatMetricValue(1234, '$')).toBe('$1,234')
    expect(formatMetricValue(-12.5, '€')).toBe('-€12.5')
  })
})

describe('metricAxisFormatter', () => {
  it('formats percent ticks compactly ×100', () => {
    const format = metricAxisFormatter('%')
    expect(format(0.08)).toBe('8%')
    expect(format(0.005)).toBe('0.5%')
    expect(format(1.5)).toBe('150%')
  })

  it('keeps the chart default compact-count behavior without a unit', () => {
    const format = metricAxisFormatter(null)
    expect(format(8)).toBe('8')
    expect(format(0.05)).toBe('0.05')
    expect(format(1500)).toBe('1.5k')
    expect(format(2_500_000)).toBe('2.5M')
  })

  it('compacts negatives and never prints float noise', () => {
    const format = metricAxisFormatter(null)
    expect(format(-2_000_000)).toBe('-2M')
    expect(format(-1_234_567)).toBe('-1.2M')
    expect(format(0.1 + 0.2)).toBe('0.3')
  })

  it('treats trailing units the same as no unit', () => {
    expect(metricAxisFormatter('ms')(1500)).toBe('1.5k')
  })

  it('keeps a currency prefix on the axis', () => {
    expect(metricAxisFormatter('$')(1500)).toBe('$1.5k')
  })
})

describe('interval helpers', () => {
  it('labels every interval', () => {
    expect(METRIC_INTERVAL_LABEL['1h']).toBe('Hourly')
    expect(METRIC_INTERVAL_LABEL['15m']).toBe('Every 15 min')
  })

  it('orders intervals by span', () => {
    expect(isIntervalFinerThan('1d', '1w')).toBe(true)
    expect(isIntervalFinerThan('1w', '1d')).toBe(false)
    expect(isIntervalFinerThan('1h', '1h')).toBe(false)
  })
})

describe('metricCadence', () => {
  it('words a known interval for running text', () => {
    // "collected hourly", never "collected every 1h" or "every hourly".
    expect(metricCadence('1h')).toBe('hourly')
    expect(metricCadence('15m')).toBe('every 15 min')
  })

  it('gives nothing for no interval or a token it does not know', () => {
    expect(metricCadence(null)).toBeNull()
    expect(metricCadence(undefined)).toBeNull()
    expect(metricCadence('')).toBeNull()
    expect(metricCadence('3h')).toBeNull()
    // An inherited key is not an interval.
    expect(metricCadence('toString')).toBeNull()
  })
})
