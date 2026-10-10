import { describe, expect, it } from 'vitest'

import { currentZoneName, timeZoneOptions, zoneOffsetLabel } from './timeZones'

const WINTER = new Date('2026-01-15T12:00:00Z')

describe('currentZoneName', () => {
  it('gives a renamed zone its current IANA name', () => {
    expect(currentZoneName('Asia/Calcutta')).toBe('Asia/Kolkata')
    expect(currentZoneName('Asia/Saigon')).toBe('Asia/Ho_Chi_Minh')
    expect(currentZoneName('Europe/Kiev')).toBe('Europe/Kyiv')
  })

  it('leaves every other name as it is', () => {
    expect(currentZoneName('Europe/Moscow')).toBe('Europe/Moscow')
    expect(currentZoneName('America/Argentina/Rio_Gallegos')).toBe('America/Argentina/Rio_Gallegos')
    expect(currentZoneName('Mars/Olympus_Mons')).toBe('Mars/Olympus_Mons')
  })
})

describe('zoneOffsetLabel', () => {
  it('reads the offset as UTC±hh:mm', () => {
    expect(zoneOffsetLabel('Asia/Kolkata', WINTER)).toBe('UTC+05:30')
    expect(zoneOffsetLabel('America/New_York', WINTER)).toBe('UTC-05:00')
    expect(zoneOffsetLabel('UTC', WINTER)).toBe('UTC+00:00')
  })

  it('is null for a zone the browser does not know', () => {
    expect(zoneOffsetLabel('Mars/Olympus_Mons', WINTER)).toBeNull()
  })
})

describe('timeZoneOptions', () => {
  it('offers UTC first, then current names with the offset after the name', () => {
    const options = timeZoneOptions('UTC', WINTER)

    expect(options[0]).toEqual({ value: 'UTC', label: 'UTC' })
    expect(options).toContainEqual({ value: 'Asia/Kolkata', label: 'Asia/Kolkata (UTC+05:30)' })
    // The old name is not offered beside the new one.
    expect(options.map((option) => option.value)).not.toContain('Asia/Calcutta')
    // Each label starts with its zone, so a native select's type-ahead
    // still jumps on "Europe/Moscow".
    for (const option of options) expect(option.label.startsWith(option.value)).toBe(true)
    const values = options.map((option) => option.value)
    expect(new Set(values).size).toBe(values.length)
  })

  it('keeps a zone stored under its old name selectable, so opening the page changes nothing', () => {
    const options = timeZoneOptions('Asia/Calcutta', WINTER)

    expect(options[0]).toEqual({ value: 'Asia/Calcutta', label: 'Asia/Calcutta (UTC+05:30)' })
    expect(options.map((option) => option.value)).toContain('Asia/Kolkata')
  })

  it('flags a stored zone the browser does not know', () => {
    expect(timeZoneOptions('Mars/Olympus_Mons', WINTER)[0]).toEqual({
      value: 'Mars/Olympus_Mons',
      label: 'Mars/Olympus_Mons (not recognised)',
    })
  })
})
