import { describe, expect, it } from 'vitest'
import type { SourceFreshness, SourceFreshnessItem } from '@/types'
import {
  expectedWithinSeconds,
  formatLag,
  freshnessChipContent,
  freshnessCounts,
  holdingItems,
  isHoldingSignals,
  worstFreshness,
} from './sourceFreshness'

const HOUR = 3600
const NOW = Date.parse('2026-09-26T10:00:00Z')

function reading(overrides: Partial<SourceFreshness> = {}): SourceFreshness {
  return {
    status: 'fresh',
    lag_seconds: 600,
    last_event_at: '2026-09-26T09:50:00Z',
    last_collection_at: '2026-09-26T09:55:00Z',
    expected_by: '2026-09-26T12:50:00Z',
    ...overrides,
  }
}

function item(id: string, freshness: SourceFreshness, dataSourceId = 'ds-1'): SourceFreshnessItem {
  return { id, name: `Scan ${id}`, data_source_id: dataSourceId, freshness }
}

describe('formatLag', () => {
  it('reads in one unit', () => {
    expect(formatLag(20)).toBe('1m')
    expect(formatLag(45 * 60)).toBe('45m')
    expect(formatLag(7 * HOUR)).toBe('7h')
    expect(formatLag(30 * HOUR)).toBe('30h')
    expect(formatLag(3 * 24 * HOUR)).toBe('3d')
    expect(formatLag(-5)).toBe('1m')
  })
})

describe('isHoldingSignals', () => {
  it('holds while late or overdue, never otherwise', () => {
    expect(isHoldingSignals(reading({ status: 'late' }))).toBe(true)
    expect(isHoldingSignals(reading({ status: 'overdue' }))).toBe(true)
    expect(isHoldingSignals(reading({ status: 'fresh' }))).toBe(false)
    expect(isHoldingSignals(reading({ status: 'unknown' }))).toBe(false)
    expect(isHoldingSignals(null)).toBe(false)
    expect(isHoldingSignals(undefined)).toBe(false)
  })
})

describe('expectedWithinSeconds', () => {
  it('is the gap between the newest event and the late threshold', () => {
    const late = reading({ last_event_at: '2026-09-26T03:00:00Z', expected_by: '2026-09-26T06:00:00Z' })
    expect(expectedWithinSeconds(late)).toBe(3 * HOUR)
  })

  it('is null without both stamps', () => {
    expect(expectedWithinSeconds(reading({ expected_by: null }))).toBeNull()
    expect(expectedWithinSeconds(reading({ last_event_at: null }))).toBeNull()
  })
})

describe('freshnessChipContent', () => {
  it('says nothing for a fresh or unknown source', () => {
    expect(freshnessChipContent(reading())).toBeNull()
    expect(freshnessChipContent(reading({ status: 'unknown' }))).toBeNull()
    expect(freshnessChipContent(null)).toBeNull()
  })

  it('words a late source with its lag and threshold', () => {
    const content = freshnessChipContent(reading({
      status: 'late',
      lag_seconds: 7 * HOUR,
      last_event_at: '2026-09-26T03:00:00Z',
      expected_by: '2026-09-26T06:00:00Z',
    }), NOW)
    expect(content).toMatchObject({ label: 'Data late · 7h', tone: 'warning' })
    expect(content?.description).toContain('Newest event 7h ago (expected within 3h)')
  })

  it('falls back to a bare label when the lag is unknown', () => {
    expect(freshnessChipContent(reading({ status: 'late', lag_seconds: null }))?.label).toBe('Data late')
  })

  it('words an overdue scan as danger', () => {
    const content = freshnessChipContent(reading({
      status: 'overdue',
      last_collection_at: '2026-09-26T05:00:00Z',
    }), NOW)
    expect(content).toMatchObject({ label: 'Scan overdue', tone: 'danger' })
    expect(content?.description).toContain('Last collection 5h ago')
  })
})

describe('worstFreshness', () => {
  it('ranks overdue over late over fresh over unknown', () => {
    const fresh = reading()
    const late = reading({ status: 'late', lag_seconds: 5 * HOUR })
    const overdue = reading({ status: 'overdue' })
    expect(worstFreshness([fresh, late, overdue, null])).toBe(overdue)
    expect(worstFreshness([reading({ status: 'unknown' }), fresh])).toBe(fresh)
    expect(worstFreshness([])).toBeNull()
  })

  it('names the longer delay between two late sources', () => {
    const shorter = reading({ status: 'late', lag_seconds: 4 * HOUR })
    const longer = reading({ status: 'late', lag_seconds: 9 * HOUR })
    expect(worstFreshness([shorter, longer])).toBe(longer)
  })
})

describe('holdingItems / freshnessCounts', () => {
  const items = [
    item('a', reading({ status: 'late' })),
    item('b', reading()),
    item('c', reading({ status: 'overdue' })),
  ]

  it('keeps the holding scans, optionally narrowed to some ids', () => {
    expect(holdingItems(items).map(i => i.id)).toEqual(['a', 'c'])
    expect(holdingItems(items, ['c', 'b']).map(i => i.id)).toEqual(['c'])
    expect(holdingItems(undefined)).toEqual([])
  })

  it('counts every status', () => {
    expect(freshnessCounts(items)).toEqual({ fresh: 1, late: 1, overdue: 1, unknown: 0 })
  })
})

describe('sourceFreshness edge cases', () => {
  it('reads unparseable or inverted stamps as no allowance', () => {
    expect(expectedWithinSeconds(reading({ expected_by: 'not a date', last_event_at: '2026-09-26T03:00:00Z' }))).toBeNull()
    expect(expectedWithinSeconds(reading({ expected_by: null }))).toBeNull()
    expect(
      expectedWithinSeconds(reading({ expected_by: '2026-09-26T02:00:00Z', last_event_at: '2026-09-26T03:00:00Z' })),
    ).toBeNull()
  })

  it('words a late source with no lag or allowance plainly', () => {
    const chip = freshnessChipContent(
      reading({ status: 'late', lag_seconds: null, expected_by: null, last_event_at: null }),
      NOW,
    )
    expect(chip?.label).toBe('Data late')
    expect(chip?.description).toBe('No new events. Drop signals are held until data arrives.')
  })

  it('says no collection has finished for an overdue scan that never collected', () => {
    const chip = freshnessChipContent(reading({ status: 'overdue', last_collection_at: null }), NOW)
    expect(chip?.label).toBe('Scan overdue')
    expect(chip?.description).toMatch(/^No collection has finished;/)
    expect(freshnessChipContent(null, NOW)).toBeNull()
    expect(freshnessChipContent(reading({ status: 'unknown' }), NOW)).toBeNull()
  })

  it('keeps the longer delay among equal statuses, treating a missing lag as none', () => {
    const shortLate = reading({ status: 'late', lag_seconds: 4 * HOUR })
    const noLag = reading({ status: 'late', lag_seconds: null })
    const longLate = reading({ status: 'late', lag_seconds: 9 * HOUR })
    expect(worstFreshness([noLag, shortLate, null, longLate, noLag])).toBe(longLate)
    expect(worstFreshness([shortLate, noLag])).toBe(shortLate)
    expect(worstFreshness([])).toBeNull()
  })

  it('returns no holding items without data, and all of them without a scan filter', () => {
    const late = item('sc-1', reading({ status: 'late' }))
    expect(holdingItems(undefined)).toEqual([])
    expect(holdingItems([late], null)).toEqual([late])
  })
})
