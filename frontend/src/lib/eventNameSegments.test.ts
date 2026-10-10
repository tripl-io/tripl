import { describe, expect, it } from 'vitest'
import { NAME_SEGMENT_SEPARATOR, splitEventName } from './eventNameSegments'

// The shared EventName component reads these from lib/, not from a page module.
describe('splitEventName', () => {
  it('splits on a colon', () => {
    expect(NAME_SEGMENT_SEPARATOR).toBe(':')
  })

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
