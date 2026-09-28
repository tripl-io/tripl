import { describe, expect, it } from 'vitest'
import { defaultExportRange, exportRangeError, utcDay, webhookUrlError } from './auditExportModel'

describe('audit export range (F20)', () => {
  it('starts on the last 30 days in UTC', () => {
    expect(defaultExportRange(new Date('2026-09-28T23:30:00Z'))).toEqual({ from: '2026-08-29', to: '2026-09-28' })
    expect(utcDay(new Date('2026-01-01T00:00:00Z'))).toBe('2026-01-01')
  })

  it('accepts a range of one day up to 366 days, both ends included', () => {
    expect(exportRangeError('2026-01-02', '2026-01-02')).toBeNull() // one whole day
    expect(exportRangeError('2026-01-01', '2026-01-02')).toBeNull()
    expect(exportRangeError('2025-01-02', '2026-01-02')).toBeNull() // 366 days
  })

  it('refuses a missing, malformed, backwards or too long range', () => {
    expect(exportRangeError('', '2026-01-02')).toMatch(/both dates/)
    expect(exportRangeError('2026-02-31', '2026-03-02')).toMatch(/YYYY-MM-DD/)
    expect(exportRangeError('2026-01-03', '2026-01-02')).toMatch(/not be before the start/)
    expect(exportRangeError('2025-01-01', '2026-01-02')).toMatch(/at most 366 days/) // 367 days
  })
})

describe('audit webhook URL (F20)', () => {
  it('takes a public-looking https URL', () => {
    expect(webhookUrlError('https://siem.example.com/hooks/tripl')).toBeNull()
    expect(webhookUrlError('  https://siem.example.com  ')).toBeNull()
  })

  it('refuses an empty, relative, plain-http or credentialed URL', () => {
    expect(webhookUrlError('')).toMatch(/Enter the URL/)
    expect(webhookUrlError('siem.example.com')).toMatch(/full URL/)
    expect(webhookUrlError('http://siem.example.com')).toMatch(/https:\/\//)
    expect(webhookUrlError('https://user:pw@siem.example.com')).toMatch(/credentials/)
  })
})
