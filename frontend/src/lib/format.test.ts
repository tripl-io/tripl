import { describe, expect, it } from 'vitest'
import { formatCompactNumber, formatNumber, formatPercent, formatShare } from './format'

// The one compact count: the chart axes, the sidebar badges, the Events 48h
// column and the Scans tab's warehouse rows all print through it, so the same
// number reads the same on every screen.
describe('formatCompactNumber', () => {
  it('renders 6-digit values compactly so labels stay short', () => {
    // The bug: "380.0k" (6 chars) clipped its left digits against a fixed axis.
    expect(formatCompactNumber(380_000)).toBe('380k')
    expect(formatCompactNumber(285_000)).toBe('285k')
    expect(formatCompactNumber(750_000)).toBe('750k')
  })

  it('escalates to M before a value would render as "1000k"', () => {
    expect(formatCompactNumber(1_500_000)).toBe('1.5M')
    expect(formatCompactNumber(1_000_000)).toBe('1M')
    expect(formatCompactNumber(999_500)).toBe('1M')
    expect(formatCompactNumber(999_949)).toBe('1M')
    expect(formatCompactNumber(999_499)).toBe('999k')
  })

  it('has a billions unit, so a warehouse row count never reads "2500M"', () => {
    expect(formatCompactNumber(2_500_000_000)).toBe('2.5B')
    expect(formatCompactNumber(12_345_678_901)).toBe('12.3B')
    expect(formatCompactNumber(999_500_000)).toBe('1B')
    expect(formatCompactNumber(999_499_999)).toBe('999M')
  })

  it('keeps small values verbatim and one decimal below 100 units', () => {
    expect(formatCompactNumber(0)).toBe('0')
    expect(formatCompactNumber(842)).toBe('842')
    expect(formatCompactNumber(1_000)).toBe('1k')
    expect(formatCompactNumber(1_500)).toBe('1.5k')
    expect(formatCompactNumber(12_345)).toBe('12.3k')
  })

  it('keeps the millions suffix uppercase, so it cannot be read as minutes', () => {
    // The Events 48h column sits beside "Last seen 1m ago".
    expect(formatCompactNumber(4_000_000)).toBe('4M')
    expect(formatCompactNumber(1_200_000)).toBe('1.2M')
  })

  it('never exceeds 5 characters across the 100k–999B range', () => {
    for (const v of [100_000, 285_000, 380_000, 999_499, 1_500_000, 9_900_000, 250_000_000_000]) {
      expect(formatCompactNumber(v).length).toBeLessThanOrEqual(5)
    }
  })

  it('compacts a negative count by its magnitude', () => {
    expect(formatCompactNumber(-2_000_000)).toBe('-2M')
    expect(formatCompactNumber(-1_500)).toBe('-1.5k')
  })

  it('rounds sub-1000 fractions instead of leaking float noise', () => {
    // A confidence-band bound times the 1.1 overshoot: raw String() would emit
    // "-0.9900000000000001" (19 chars) and blow out the Y-axis width.
    expect(formatCompactNumber(-0.9 * 1.1)).toBe('-1')
    expect(formatCompactNumber(0.11000000000000001)).toBe('0')
    expect(formatCompactNumber(842.6)).toBe('843')
    expect(formatCompactNumber(-0.9 * 1.1).length).toBeLessThanOrEqual(3)
  })
})

describe('formatNumber', () => {
  it('groups in the app locale whatever the browser locale is', () => {
    expect(formatNumber(1234567)).toBe('1,234,567')
    expect(formatNumber(0.35)).toBe('0.35')
  })
})

describe('formatPercent', () => {
  it('prints a fraction with one decimal by default', () => {
    expect(formatPercent(0.123)).toBe('12.3%')
    expect(formatPercent(0)).toBe('0.0%')
    expect(formatPercent(1)).toBe('100.0%')
  })

  it('takes the number of decimals', () => {
    expect(formatPercent(0.1234, 2)).toBe('12.34%')
    expect(formatPercent(0.126, 0)).toBe('13%')
  })
})

describe('formatShare', () => {
  it('prints whole percent, clips to 0..100% and flags a tiny share', () => {
    expect(formatShare(0.92)).toBe('92%')
    expect(formatShare(1.4)).toBe('100%')
    expect(formatShare(-0.2)).toBe('0%')
    expect(formatShare(0.001)).toBe('<1%')
    expect(formatShare(0)).toBe('0%')
    expect(formatShare(Number.NaN)).toBe('0%')
  })
})
