import { describe, expect, it, vi } from 'vitest'
import { formatTimestamp } from './datetime'

describe('formatTimestamp with the zone named', () => {
  const ISO = '2026-10-09T11:35:12Z'

  it('adds the zone to the full timestamp a hover title shows', () => {
    const plain = formatTimestamp(ISO, { seconds: true })
    const zoned = formatTimestamp(ISO, { seconds: true, zone: true })
    expect(zoned.startsWith(`${plain} `)).toBe(true)
    // "UTC", "UTC+2", "UTC−5:30": the offset every surface names a local time by.
    expect(zoned.slice(plain.length + 1)).toMatch(/^UTC(?:[+−]\d{1,2}(?::\d{2})?)?$/)
  })

  it('names the zone without seconds too', () => {
    const plain = formatTimestamp(ISO)
    expect(formatTimestamp(ISO, { zone: true }).startsWith(`${plain} `)).toBe(true)
  })

  it('stays empty for an unparseable instant', () => {
    expect(formatTimestamp('not a date', { zone: true })).toBe('')
  })

  it('names the offset of the instant itself, the way the forms do', () => {
    vi.stubEnv('TZ', 'Europe/Berlin')
    try {
      expect(formatTimestamp('2026-07-01T10:00:00Z', { zone: true })).toMatch(/ UTC\+2$/)
      expect(formatTimestamp('2026-01-01T10:00:00Z', { zone: true })).toMatch(/ UTC\+1$/)
    } finally {
      vi.unstubAllEnvs()
    }
  })
})
