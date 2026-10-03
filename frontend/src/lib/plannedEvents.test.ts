import { describe, expect, it } from 'vitest'
import type { PlannedEvent } from '@/types'
import {
  isValidPlannedWindow,
  plannedEventExpectation,
  snapPlannedEventsToBuckets,
} from './plannedEvents'

const EVENT: PlannedEvent = {
  id: 'pe-1',
  project_id: 'p-1',
  label: 'Spring promo',
  description: null,
  starts_at: '2026-05-02T00:00:00Z',
  ends_at: '2026-05-04T00:00:00Z',
  direction: 'spike',
  scope_type: null,
  scope_ref: null,
  created_by_user_id: null,
  created_at: '2026-05-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
}

const DAYS = ['2026-05-01', '2026-05-02', '2026-05-03', '2026-05-04', '2026-05-05'].map(day => ({
  bucket: `${day}T00:00:00Z`,
}))

describe('snapPlannedEventsToBuckets (F18)', () => {
  it('shades the buckets inside [starts_at, ends_at), the end bucket excluded', () => {
    expect(snapPlannedEventsToBuckets([EVENT], DAYS)).toEqual([
      {
        id: 'pe-1',
        label: 'Spring promo',
        direction: 'spike',
        x1: '2026-05-02T00:00:00Z',
        x2: '2026-05-03T00:00:00Z',
      },
    ])
  })

  it('starts on the bucket holding a mid-bucket start', () => {
    const [window] = snapPlannedEventsToBuckets(
      [{ ...EVENT, starts_at: '2026-05-02T15:00:00Z' }],
      DAYS,
    )
    expect(window?.x1).toBe('2026-05-02T00:00:00Z')
  })

  it('clips a window that began before the drawn range to the first bucket', () => {
    const [window] = snapPlannedEventsToBuckets(
      [{ ...EVENT, starts_at: '2026-04-20T00:00:00Z' }],
      DAYS,
    )
    expect(window?.x1).toBe('2026-05-01T00:00:00Z')
    expect(window?.x2).toBe('2026-05-03T00:00:00Z')
  })

  it('drops windows wholly before or after the drawn range', () => {
    const before = { ...EVENT, id: 'old', starts_at: '2026-04-01T00:00:00Z', ends_at: '2026-04-02T00:00:00Z' }
    const after = { ...EVENT, id: 'next', starts_at: '2026-05-09T00:00:00Z', ends_at: '2026-05-10T00:00:00Z' }
    expect(snapPlannedEventsToBuckets([before, after], DAYS)).toEqual([])
  })

  it('draws nothing without buckets or events', () => {
    expect(snapPlannedEventsToBuckets(undefined, DAYS)).toEqual([])
    expect(snapPlannedEventsToBuckets([EVENT], [])).toEqual([])
  })
})

describe('planned event helpers', () => {
  it('names what the event expects', () => {
    expect(plannedEventExpectation('spike')).toBe('Expected rise')
    expect(plannedEventExpectation('drop')).toBe('Expected drop')
    expect(plannedEventExpectation(null)).toBe('Expected rise or drop')
  })

  it('accepts only a window that ends after it starts', () => {
    expect(isValidPlannedWindow('2026-05-01T10:00', '2026-05-01T11:00')).toBe(true)
    expect(isValidPlannedWindow('2026-05-01T10:00', '2026-05-01T10:00')).toBe(false)
    expect(isValidPlannedWindow('', '2026-05-01T10:00')).toBe(false)
  })
})
