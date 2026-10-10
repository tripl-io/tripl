import { describe, expect, it } from 'vitest'

import { formatPercentDelta, formatRatioDelta, hasBaseline, ratioDelta } from './percentDelta'

/**
 * The backend's `alert_templates.percent_delta_of`, written out, so the sign
 * rule below is tested against the definition rather than against a
 * hand-copied number.
 */
function backendMagnitude(actual: number, expected: number): number {
  return (Math.abs(actual - expected) / Math.abs(expected)) * 100
}

describe('hasBaseline', () => {
  it('is false only at exactly zero', () => {
    expect(hasBaseline(40)).toBe(true)
    expect(hasBaseline(0.2)).toBe(true)
    expect(hasBaseline(0)).toBe(false)
  })

  it('counts a negative expectation as a real baseline', () => {
    // A signed `fact` sum or a `sql` level below zero: -100 is exactly as
    // substantial an expectation as +100, and the matcher already fires on it.
    // Saying "no baseline" here would contradict the rule
    // that produced the row.
    expect(hasBaseline(-3)).toBe(true)
    expect(hasBaseline(-0.5)).toBe(true)
  })

  it('treats a non-finite expectation as no baseline', () => {
    expect(hasBaseline(Number.NaN)).toBe(false)
    expect(hasBaseline(Number.POSITIVE_INFINITY)).toBe(false)
  })
})

describe('formatPercentDelta', () => {
  it('keeps one decimal and the percentage as sent when there is a baseline', () => {
    expect(formatPercentDelta(70, 40)).toBe('70.0%')
    expect(formatPercentDelta(875.6, 20.5)).toBe('875.6%')
  })

  it('says there was no baseline instead of printing the undefined ratio', () => {
    // A scope that fired 137 times against a baseline of 0 stores percent_delta
    // 0.0 — the largest possible relative move written as the smallest.
    expect(formatPercentDelta(0, 0)).toBe('no baseline')
    expect(formatPercentDelta(null, 0)).toBe('no baseline')
  })

  it('prints the delta of a negative expectation rather than disowning it', () => {
    // -3 observed at -9: the backend stores the magnitude 200.0 and the alert
    // message says "200.0%". The gate used to be `expected > 0`, so this row
    // rendered "no baseline" over the very number that made the rule fire.
    expect(formatPercentDelta(backendMagnitude(-9, -3), -3)).toBe('200.0%')
    expect(formatPercentDelta(200, -3)).toBe('200.0%')
  })

  it('prints a stored signed value from frozen history unchanged', () => {
    // AlertDeliveryItem.percent_delta is NOT NULL and holds history written
    // before the magnitude rule; the renderer must show what the alert said.
    expect(formatPercentDelta(-59.2, 145)).toBe('-59.2%')
  })

  it('treats a fractional baseline as a baseline', () => {
    // Catalog metrics deliver sub-unit expectations; only exactly 0 is "none".
    expect(formatPercentDelta(350, 0.2)).toBe('350.0%')
    expect(formatPercentDelta(350, -0.2)).toBe('350.0%')
  })
})

describe('ratioDelta', () => {
  it('is the signed percentage the value moved against its baseline', () => {
    expect(ratioDelta(200, 100)).toBe(100)
    expect(ratioDelta(50, 100)).toBe(-50)
  })

  it('is null when there is no baseline to divide by', () => {
    // The class the detector admits on purpose: something fired where nothing
    // was expected. The ratio is undefined, not zero.
    expect(ratioDelta(137, 0)).toBeNull()
  })

  it('divides a negative baseline by its magnitude', () => {
    // -3 -> -9 is a 200% move, the same SIZE as 3 -> 9. Dividing by the signed
    // expectation gave 200% for one and -200%... the wrong way round for the
    // other, so the size itself is pinned to the backend's definition.
    expect(ratioDelta(-9, -3)).toBeCloseTo(-backendMagnitude(-9, -3))
    expect(Math.abs(ratioDelta(-9, -3) as number)).toBeCloseTo(200)
    expect(Math.abs(ratioDelta(9, 3) as number)).toBeCloseTo(200)
    expect(ratioDelta(137, -1)).toBeCloseTo(13800)
  })

  it('signs by the direction of travel, the way the backend picks the arrow', () => {
    // `anomaly_detector`: direction = "spike" if actual >= expected else "drop".
    // A fall below a negative baseline is a DROP, so the percentage that sits
    // beside that down arrow has to be negative. The old expression
    // (actual - expected) / expected returned +200% here and the banner read
    // "Volume drop detected — +200% vs. baseline".
    expect(ratioDelta(-9, -3)).toBeLessThan(0)
    // A rise TOWARDS zero is a spike, even though the value is still negative.
    expect(ratioDelta(-1, -3)).toBeGreaterThan(0)
    expect(ratioDelta(-1, -3)).toBeCloseTo(66.667)
    // And the sign never depends on the baseline's own sign.
    expect(ratioDelta(50, 100)).toBeLessThan(0)
    expect(ratioDelta(200, 100)).toBeGreaterThan(0)
  })

  it('treats a fractional baseline as a baseline', () => {
    expect(ratioDelta(0.6, 0.2)).toBeCloseTo(200)
    expect(ratioDelta(-0.6, -0.2)).toBeCloseTo(-200)
  })

  it('is null for a non-finite expectation', () => {
    expect(ratioDelta(137, Number.NaN)).toBeNull()
  })
})

describe('formatRatioDelta', () => {
  it('signs the percentage and rounds to whole percent by default', () => {
    expect(formatRatioDelta(137.4)).toBe('+137%')
    expect(formatRatioDelta(-42.6)).toBe('−43%')
    expect(formatRatioDelta(12.3, 1)).toBe('+12.3%')
  })

  it('signs a drop with the typographic minus and leaves a rounded zero unsigned', () => {
    expect(formatRatioDelta(-12.34, 1)).toBe('−12.3%')
    expect(formatRatioDelta(-0.4)).toBe('0%')
    expect(formatRatioDelta(0.04, 1)).toBe('0.0%')
    expect(formatRatioDelta(1234)).toBe('+1,234%')
  })

  it('reads as a drop for a fall below a negative baseline', () => {
    // End to end: the number the banner actually prints beside its down arrow.
    expect(formatRatioDelta(ratioDelta(-9, -3))).toBe('−200%')
    expect(formatRatioDelta(ratioDelta(-1, -3))).toBe('+67%')
  })

  it('names the undefined ratio with the same words the alert message uses', () => {
    // Not '' — a blank reads as missing data, which is a different problem from
    // one that is undefined by definition.
    expect(formatRatioDelta(null)).toBe('no baseline')
    expect(formatRatioDelta(ratioDelta(137, 0))).toBe('no baseline')
  })
})
