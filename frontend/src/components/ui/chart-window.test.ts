import { describe, expect, it } from 'vitest'
import { windowPaddingBuckets } from './chart-window'

// a series that starts late in the window is drawn at its real place.
describe('windowPaddingBuckets', () => {
  it('pads both ends out to the window, keeping the series alignment', () => {
    expect(
      windowPaddingBuckets(
        '2026-01-03T00:00:00Z',
        '2026-01-04T00:00:00Z',
        { from: '2026-01-01T00:00:00Z', to: '2026-01-06T00:00:00Z' },
        'day',
      ),
    ).toEqual({
      before: ['2026-01-01T00:00:00.000Z', '2026-01-02T00:00:00.000Z'],
      after: ['2026-01-05T00:00:00.000Z'],
    })
  })

  it('keeps a bucket the window starts inside of', () => {
    const { before } = windowPaddingBuckets(
      '2026-01-01T12:00:00Z',
      '2026-01-01T12:00:00Z',
      { from: '2026-01-01T10:30:00Z' },
      'hour',
    )
    expect(before).toEqual(['2026-01-01T10:00:00.000Z', '2026-01-01T11:00:00.000Z'])
  })

  it('steps whole calendar months', () => {
    const { before } = windowPaddingBuckets(
      '2026-03-01T00:00:00Z',
      '2026-03-01T00:00:00Z',
      { from: '2026-01-01T00:00:00Z' },
      'month',
    )
    expect(before).toEqual(['2026-01-01T00:00:00.000Z', '2026-02-01T00:00:00.000Z'])
  })

  it('adds nothing without a window or for unreadable bounds', () => {
    expect(windowPaddingBuckets('2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z', {}, 'day')).toEqual({
      before: [],
      after: [],
    })
    expect(
      windowPaddingBuckets('nope', '2026-01-02T00:00:00Z', { from: '2026-01-01T00:00:00Z' }, 'day'),
    ).toEqual({ before: [], after: [] })
  })

  it('never pads more than a bounded number of buckets', () => {
    const { before } = windowPaddingBuckets(
      '2026-01-01T00:00:00Z',
      '2026-01-01T00:00:00Z',
      { from: '2000-01-01T00:00:00Z' },
      '15min',
    )
    expect(before.length).toBe(500)
  })
})
