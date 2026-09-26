import { describe, expect, it } from 'vitest'
import type { SourceFreshness, SourceFreshnessItem } from '@/types'
import { dataSourceFreshness } from './data-source-freshness-model'

function item(id: string, dataSourceId: string, freshness: Partial<SourceFreshness>): SourceFreshnessItem {
  return {
    id,
    name: `Scan ${id}`,
    data_source_id: dataSourceId,
    freshness: {
      status: 'fresh',
      lag_seconds: 600,
      last_event_at: null,
      last_collection_at: null,
      expected_by: null,
      ...freshness,
    },
  }
}

describe('dataSourceFreshness (F16, #269)', () => {
  it("speaks for the source with its worst scan's reading", () => {
    const items = [
      item('a', 'ds-1', { status: 'fresh' }),
      item('b', 'ds-1', { status: 'late', lag_seconds: 7 * 3600 }),
      item('c', 'ds-2', { status: 'overdue' }),
    ]
    expect(dataSourceFreshness('ds-1', items)?.id).toBe('b')
    expect(dataSourceFreshness('ds-2', items)?.id).toBe('c')
  })

  it('is null for a source no scan reads', () => {
    expect(dataSourceFreshness('ds-9', [item('a', 'ds-1', {})])).toBeNull()
  })
})
