import { describe, expect, it } from 'vitest'
import { formatSignalValues, signalTimeTitle } from './signalMetricFormat'

// Local wall-clock dates, so the expectations hold in any test time zone.
function local(year: number, monthIndex: number, day: number, hour: number): string {
  return new Date(year, monthIndex, day, hour, 0).toISOString()
}

describe('signalTimeTitle', () => {
  it('says what the time is and names the zone it is shown in', () => {
    const title = signalTimeTitle('Bucket starting', local(2026, 8, 25, 18))
    expect(title).toMatch(/^Bucket starting Sep 25, 2026, 6:00\sPM \(UTC([+−]\d{1,2}(:\d{2})?)?\)$/)
  })

  it('says only what the time is when there is no time to name', () => {
    expect(signalTimeTitle('Detected', 'not a date')).toBe('Detected')
  })
})

// The Overview row appends " expected" itself; the verdict copy in
// signalVerdict does the same, so the formatter must not.
describe('formatSignalValues', () => {
  it('reads "actual vs expected" without naming the baseline', () => {
    expect(formatSignalValues({ actual_count: 7173, expected_count: 2403, unit: null })).toBe(
      '7,173 vs 2,403',
    )
  })
})
