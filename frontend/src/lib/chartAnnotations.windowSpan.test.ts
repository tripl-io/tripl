import { describe, expect, it } from 'vitest'
import { formatTimestamp, formatUtcOffset } from '@/lib/datetime'
import { formatExpectedWindowSpan } from './chartAnnotations'

// The Annotations page printed windows in an unnamed local zone right under
// suggestions written in UTC, and a holiday (a UTC day) read "3:00 AM – 3:00 AM"
// for a reader at UTC+3.
describe('formatExpectedWindowSpan', () => {
  it('prints a holiday as its UTC day, whatever the reader’s zone', () => {
    expect(
      formatExpectedWindowSpan({
        starts_at: '2026-10-03T00:00:00Z',
        ends_at: '2026-10-04T00:00:00Z',
        source: 'holiday',
      }),
    ).toBe('Oct 3, 2026, all day UTC')
  })

  it('prints a holiday spanning days as its first and last UTC day', () => {
    expect(
      formatExpectedWindowSpan({
        starts_at: '2026-12-24T00:00:00Z',
        ends_at: '2026-12-27T00:00:00Z',
        source: 'holiday',
      }),
    ).toBe('Dec 24, 2026 – Dec 26, 2026, all day UTC')
  })

  it('prints any other window in local time with the offset named', () => {
    const starts = '2026-05-02T09:00:00Z'
    const ends = '2026-05-02T17:00:00Z'
    expect(formatExpectedWindowSpan({ starts_at: starts, ends_at: ends, source: 'manual' })).toBe(
      `${formatTimestamp(starts)} – ${formatTimestamp(ends)} ${formatUtcOffset(new Date(starts))}`,
    )
  })

  it('prints nothing for an unreadable start', () => {
    expect(formatExpectedWindowSpan({ starts_at: 'nope', ends_at: 'nope', source: 'manual' })).toBe('')
  })
})
