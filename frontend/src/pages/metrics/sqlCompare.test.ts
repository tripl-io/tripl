import { describe, expect, it } from 'vitest'
import type { MetricPreviewPoint } from '@/types'
import { compareSeries } from './sqlCompare'

const point = (hour: number, value: number): MetricPreviewPoint => ({
  bucket: new Date(Date.UTC(2026, 10, 20, hour)).toISOString(),
  value,
})

describe('compareSeries', () => {
  it('reports absolute and percentage change for buckets present on both sides', () => {
    const summary = compareSeries([point(0, 100)], [point(0, 120)])

    expect(summary.rows).toHaveLength(1)
    expect(summary.rows[0]).toMatchObject({ saved: 100, draft: 120, absChange: 20, pctChange: 20 })
    expect(summary.comparedCount).toBe(1)
    expect(summary.netAbsChange).toBe(20)
    expect(summary.netPctChange).toBe(20)
  })

  it('keeps a missing bucket distinct from a zero value', () => {
    const summary = compareSeries([point(0, 0), point(1, 5)], [point(1, 5), point(2, 3)])

    const byHour = new Map(summary.rows.map(row => [row.bucketMs, row]))
    const missingDraft = byHour.get(Date.UTC(2026, 10, 20, 0))
    const missingSaved = byHour.get(Date.UTC(2026, 10, 20, 2))
    expect(missingDraft).toMatchObject({ saved: 0, draft: null, absChange: null, pctChange: null })
    expect(missingSaved).toMatchObject({ saved: null, draft: 3, absChange: null, pctChange: null })
    expect(summary.missingCount).toBe(2)
    expect(summary.comparedCount).toBe(1)
  })

  it('leaves the percentage undefined on a zero saved baseline but still reports the change', () => {
    const summary = compareSeries([point(0, 0)], [point(0, 7)])

    expect(summary.rows[0]).toMatchObject({ absChange: 7, pctChange: null })
    expect(summary.netAbsChange).toBe(7)
    expect(summary.netPctChange).toBeNull()
  })

  it('sums only the buckets both sides share into the net change', () => {
    const summary = compareSeries(
      [point(0, 10), point(1, 10)],
      [point(0, 5), point(1, 15), point(2, 999)],
    )

    expect(summary.savedTotal).toBe(20)
    expect(summary.draftTotal).toBe(20)
    expect(summary.netAbsChange).toBe(0)
    expect(summary.netPctChange).toBe(0)
  })

  it('handles two empty previews without dividing by zero', () => {
    const summary = compareSeries([], [])

    expect(summary.rows).toEqual([])
    expect(summary.netPctChange).toBeNull()
  })
})
