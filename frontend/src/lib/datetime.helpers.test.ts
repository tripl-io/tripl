import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  dayBoundaryIso,
  formatClockTime,
  formatIsoDate,
  formatShortTimestamp,
  formatTimeOfDay,
  formatUtcOffset,
  parseClockTime,
  shortTimestampParts,
  toDateKey,
  toLocalDateTimeValue,
  viewerTimeZone,
} from './datetime'

// The date helpers every page shares: the picker's wire values, the zone a
// local time is named by, and the short time of a tight column. Each used to
// be copied per page, and the copies drifted (a browser-locale date beside an
// app-locale one, a 24-hour clock beside a 12-hour one).

afterEach(() => {
  vi.unstubAllEnvs()
})

// Local wall-clock dates, so the expectations hold in any test time zone.
function local(year: number, monthIndex: number, day: number, hour: number, minute = 0): string {
  return new Date(year, monthIndex, day, hour, minute).toISOString()
}

describe('toDateKey / toLocalDateTimeValue', () => {
  it('builds the picker values from the local calendar day and wall clock', () => {
    const at = new Date(2026, 0, 2, 9, 5)
    expect(toDateKey(at)).toBe('2026-01-02')
    expect(toLocalDateTimeValue(at)).toBe('2026-01-02T09:05')
  })

  it('keeps the local day where the UTC one differs', () => {
    vi.stubEnv('TZ', 'America/Los_Angeles')
    // 03:00Z on the 24th is the evening of the 23rd in Los Angeles.
    const at = new Date('2026-09-24T03:00:00Z')
    expect(toDateKey(at)).toBe('2026-09-23')
    expect(toLocalDateTimeValue(at)).toBe('2026-09-23T20:00')
    expect(formatIsoDate('2026-09-24T03:00:00Z')).toBe(toDateKey(at))
  })
})

describe('dayBoundaryIso', () => {
  it('pins a picked day to the first or last instant of the local day', () => {
    expect(dayBoundaryIso('2026-09-24', 'start')).toBe(new Date('2026-09-24T00:00:00.000').toISOString())
    expect(dayBoundaryIso('2026-09-24', 'end')).toBe(new Date('2026-09-24T23:59:59.999').toISOString())
  })

  it('has no bound for an empty or unreadable day', () => {
    expect(dayBoundaryIso('', 'start')).toBeUndefined()
    expect(dayBoundaryIso('24.09.2026', 'end')).toBeUndefined()
    expect(dayBoundaryIso('2026-09-24T10:00', 'start')).toBeUndefined()
  })
})

describe('formatUtcOffset', () => {
  it('names the UTC offset, with minutes only when there are some', () => {
    const at = (offsetMinutes: number) => ({ getTimezoneOffset: () => offsetMinutes }) as Date
    expect(formatUtcOffset(at(0))).toBe('UTC')
    expect(formatUtcOffset(at(-180))).toBe('UTC+3')
    expect(formatUtcOffset(at(300))).toBe('UTC−5')
    expect(formatUtcOffset(at(-330))).toBe('UTC+5:30')
  })

  it('takes the offset of the instant, so summer and winter differ', () => {
    vi.stubEnv('TZ', 'Europe/Berlin')
    expect(formatUtcOffset(new Date('2026-07-01T12:00:00Z'))).toBe('UTC+2')
    expect(formatUtcOffset(new Date('2026-01-01T12:00:00Z'))).toBe('UTC+1')
  })
})

describe('viewerTimeZone', () => {
  it('names the browser zone for a page to state once', () => {
    expect(viewerTimeZone()).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone)
    expect(viewerTimeZone()).not.toBe('')
  })
})

describe('formatTimeOfDay', () => {
  it('prints the local time of an instant in the 12-hour clock', () => {
    expect(formatTimeOfDay(new Date(2026, 0, 2, 21, 5))).toMatch(/^9:05\sPM$/)
    expect(formatTimeOfDay(local(2026, 0, 2, 0, 30))).toMatch(/^12:30\sAM$/)
  })

  it('prints nothing for an instant it cannot read', () => {
    expect(formatTimeOfDay('not a date')).toBe('')
  })
})

describe('formatClockTime / parseClockTime', () => {
  it('prints a 24-hour value in the app clock', () => {
    expect(formatClockTime('21:30')).toMatch(/^9:30\sPM$/)
    expect(formatClockTime('00:05')).toMatch(/^12:05\sAM$/)
    expect(formatClockTime('12:00')).toMatch(/^12:00\sPM$/)
  })

  it('reads a time typed in either clock', () => {
    expect(parseClockTime('9:30 PM')).toBe('21:30')
    expect(parseClockTime('9:30pm')).toBe('21:30')
    expect(parseClockTime('9 pm')).toBe('21:00')
    expect(parseClockTime('9:30 p.m.')).toBe('21:30')
    expect(parseClockTime('12:15 am')).toBe('00:15')
    expect(parseClockTime('12 PM')).toBe('12:00')
    expect(parseClockTime('21:30')).toBe('21:30')
    expect(parseClockTime('09:15')).toBe('09:15')
    expect(parseClockTime('0930')).toBe('09:30')
    expect(parseClockTime(' 7 ')).toBe('07:00')
  })

  it('reads nothing until the text is a time', () => {
    expect(parseClockTime('')).toBeNull()
    expect(parseClockTime('9:3')).toBeNull()
    expect(parseClockTime('24:00')).toBeNull()
    expect(parseClockTime('9:60')).toBeNull()
    expect(parseClockTime('13 pm')).toBeNull()
    expect(parseClockTime('0 am')).toBeNull()
    expect(parseClockTime('noon')).toBeNull()
  })

  it('round-trips what it prints', () => {
    for (const time of ['00:00', '07:05', '12:00', '12:30', '23:59']) {
      expect(parseClockTime(formatClockTime(time))).toBe(time)
    }
  })
})

// One short time for every compact column: the Anomalies and Overview signal
// rows, the delivery log and the rule replay. They were four private copies on
// three clocks (12-hour, 24-hour and the browser's).
describe('formatShortTimestamp / shortTimestampParts', () => {
  const NOW = new Date(2026, 8, 25, 20, 0)

  it('says "Today" for an instant on the current day, when asked', () => {
    expect(formatShortTimestamp(local(2026, 8, 25, 18), { now: NOW, today: true })).toMatch(/^Today 6:00\sPM$/)
    expect(formatShortTimestamp(local(2026, 8, 25, 18), { now: NOW })).toMatch(/^Sep 25, 6:00\sPM$/)
  })

  it('names the day, and the year only when it differs', () => {
    expect(formatShortTimestamp(local(2026, 8, 24, 18), { now: NOW, today: true })).toMatch(/^Sep 24, 6:00\sPM$/)
    expect(formatShortTimestamp(local(2025, 8, 24, 18), { now: NOW })).toMatch(/^Sep 24, 2025, 6:00\sPM$/)
  })

  it('uses the 12-hour clock of every other timestamp, midnight included', () => {
    expect(formatShortTimestamp(local(2026, 8, 24, 0), { now: NOW })).toMatch(/^Sep 24, 12:00\sAM$/)
  })

  it('splits the date from the time for a two-line cell', () => {
    const parts = shortTimestampParts(local(2026, 7, 12, 14, 2), NOW)
    expect(parts?.date).toBe('Aug 12')
    expect(parts?.time).toMatch(/^2:02\sPM$/)
    expect(shortTimestampParts(local(2025, 7, 12, 14, 2), NOW)?.date).toBe('Aug 12, 2025')
  })

  it('prints nothing for an instant it cannot read', () => {
    expect(formatShortTimestamp('not a date', { now: NOW })).toBe('')
    expect(shortTimestampParts('not a date', NOW)).toBeNull()
  })
})
