import { describe, expect, it } from 'vitest'
import type { EventMetricPoint } from '@/types'
import { computeWindowDelta } from '@/pages/events/utils'
import { computeEventStats } from './eventStats'

const HOUR = 60 * 60 * 1000
const NOW = Date.parse('2026-06-10T12:00:00Z')

function hourly(hoursAgo: number, count: number): EventMetricPoint {
  return {
    bucket: new Date(NOW - hoursAgo * HOUR).toISOString(),
    count,
    expected_count: null,
    stddev: null,
    is_anomaly: false,
    anomaly_direction: null,
    z_score: null,
  }
}

describe('computeEventStats', () => {
  it('prints the same delta as the Events list for the same series', () => {
    // 48 full hours: 12/h in the last day, 10/h in the one before.
    const points = Array.from({ length: 48 }, (_, index) => hourly(index + 1, index < 23 ? 12 : 10)).reverse()
    const stats = computeEventStats(points, NOW)
    expect(stats.delta24h).toBe(computeWindowDelta(points, NOW).pct)
    expect(stats.volume24h).toBe(computeWindowDelta(points, NOW).recentTotal)
  })

  it('anchors on now, so a lagging series is flagged partial instead of inflated', () => {
    // Collection stopped 6h ago: the recent half is short, not the prior one.
    const points = Array.from({ length: 42 }, (_, index) => hourly(index + 6, 10)).reverse()
    const stats = computeEventStats(points, NOW)
    expect(stats.partial).toBe(true)
    expect(stats.deltaHint).toMatch(/ends 6h before now/)
  })

  it('reports no series as empty rather than as zero volume', () => {
    const stats = computeEventStats([], NOW)
    expect(stats.volume24h).toBeNull()
    expect(stats.delta24h).toBeNull()
  })
})
