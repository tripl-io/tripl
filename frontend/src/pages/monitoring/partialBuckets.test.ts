import { describe, expect, it } from 'vitest'
import type { EventMetricPoint } from '@/types'
import { partialWindow } from './partialBuckets'

function hour(bucket: string): EventMetricPoint {
  return {
    bucket,
    count: 1,
    expected_count: null,
    stddev: null,
    is_anomaly: false,
    anomaly_direction: null,
    z_score: null,
  }
}

describe('partialWindow', () => {
  it('flags a day series that starts mid-day and ends before midnight', () => {
    const raw = [hour('2026-09-02T14:00:00Z'), hour('2026-09-03T00:00:00Z'), hour('2026-09-25T18:00:00Z')]
    expect(partialWindow(raw, 'day', 'hour')).toEqual({
      first: '2026-09-02T14:00:00Z',
      last: '2026-09-25T19:00:00.000Z',
    })
  })

  it('leaves whole buckets alone', () => {
    const raw = [hour('2026-09-02T00:00:00Z'), hour('2026-09-03T23:00:00Z')]
    expect(partialWindow(raw, 'day', 'hour')).toBeUndefined()
  })

  it('is off at the native granularity', () => {
    const raw = [hour('2026-09-02T14:00:00Z'), hour('2026-09-25T18:00:00Z')]
    expect(partialWindow(raw, 'hour', 'hour')).toBeUndefined()
    expect(partialWindow(raw, 'day', null)).toBeUndefined()
    expect(partialWindow([], 'day', 'hour')).toBeUndefined()
  })
})
