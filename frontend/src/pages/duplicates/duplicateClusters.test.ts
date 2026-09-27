import { describe, expect, it } from 'vitest'
import type { DuplicateCluster } from '@/types'
import { clusterKey, proposedKeeper, uniqueClusters, volumeLabel } from './duplicateClusters'

const cluster = (events: DuplicateCluster['events']): DuplicateCluster => ({ events, score: 0.93 })

describe('duplicate clusters (F12, #265)', () => {
  it('keys a cluster by its members in any order', () => {
    const a = cluster([
      { id: 'b', name: 'paywall_view', status: 'live', volume_7d: 1, event_type_id: 'et-1' },
      { id: 'a', name: 'paywall_screen_view', status: 'draft', volume_7d: 0, event_type_id: 'et-1' },
    ])
    const b = cluster([...a.events].reverse())
    expect(clusterKey(a)).toBe(clusterKey(b))
  })

  it('proposes the member with the most traffic', () => {
    expect(
      proposedKeeper(
        cluster([
          { id: 'a', name: 'paywall_screen_view', status: 'live', volume_7d: 12, event_type_id: 'et-1' },
          { id: 'b', name: 'paywall_view', status: 'live', volume_7d: 900, event_type_id: 'et-1' },
        ]),
      )?.id,
    ).toBe('b')
  })

  it('breaks a traffic tie by status, then by order', () => {
    expect(
      proposedKeeper(
        cluster([
          { id: 'a', name: 'x', status: 'draft', volume_7d: 0, event_type_id: 'et-1' },
          { id: 'b', name: 'y', status: 'implemented', volume_7d: 0, event_type_id: 'et-1' },
          { id: 'c', name: 'z', status: 'implemented', volume_7d: 0, event_type_id: 'et-1' },
        ]),
      )?.id,
    ).toBe('b')
  })

  it('keeps each cluster once, the first occurrence, in order', () => {
    const ab = cluster([
      { id: 'a', name: 'x', status: 'live', volume_7d: 0, event_type_id: 'et-1' },
      { id: 'b', name: 'y', status: 'live', volume_7d: 0, event_type_id: 'et-1' },
    ])
    const cd = cluster([
      { id: 'c', name: 'z', status: 'live', volume_7d: 0, event_type_id: 'et-1' },
      { id: 'd', name: 'w', status: 'live', volume_7d: 0, event_type_id: 'et-1' },
    ])
    const baAgain = { ...cluster([...ab.events].reverse()), score: 0.5 }
    const out = uniqueClusters([ab, cd, baAgain])
    expect(out).toEqual([ab, cd])
    expect(out[0]).toBe(ab)
  })

  it('says when nothing arrived', () => {
    expect(volumeLabel(0)).toBe('no data in 7 days')
    expect(volumeLabel(1234)).toMatch(/1.?234 in 7 days/)
  })
})
