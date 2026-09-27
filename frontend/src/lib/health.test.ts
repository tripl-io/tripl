import { describe, expect, it } from 'vitest'

import {
  HEALTH_COMPONENT_ORDER,
  chunkIds,
  formatHealthDelta,
  formatHealthPoints,
  formatHealthValue,
  formatHealthWeight,
  gradeForScore,
  healthAriaLabel,
  healthDeltaTone,
  orderedAverages,
  orderedComponents,
  renormalizedFootnote,
} from './health'
import type { HealthComponent } from '@/types/health'

function component(key: HealthComponent['key']): HealthComponent {
  return {
    key,
    label: key,
    weight: 10,
    applies: true,
    excluded_reason: null,
    value: 1,
    effective_weight: 10,
    points: 10,
    detail: '',
    counts: {},
  }
}

describe('gradeForScore', () => {
  it('uses the fixed thresholds: healthy >= 80, warning >= 50', () => {
    expect(gradeForScore(100)).toBe('healthy')
    expect(gradeForScore(80)).toBe('healthy')
    expect(gradeForScore(79)).toBe('warning')
    expect(gradeForScore(50)).toBe('warning')
    expect(gradeForScore(49)).toBe('unhealthy')
    expect(gradeForScore(0)).toBe('unhealthy')
  })
})

describe('health formatting', () => {
  it('names a badge for a screen reader', () => {
    expect(healthAriaLabel(72)).toBe('Health 72 of 100')
    expect(healthAriaLabel(71.6)).toBe('Health 72 of 100')
  })

  it('prints a value as a whole percent and an excluded one as a dash', () => {
    expect(formatHealthValue(0.75)).toBe('75%')
    expect(formatHealthValue(1)).toBe('100%')
    expect(formatHealthValue(1.4)).toBe('100%')
    expect(formatHealthValue(null)).toBe('—')
  })

  it('prints weights and points with at most one decimal', () => {
    expect(formatHealthWeight(20)).toBe('20%')
    expect(formatHealthWeight(23.53)).toBe('23.5%')
    expect(formatHealthWeight(null)).toBe('—')
    expect(formatHealthPoints(18.46)).toBe('18.5')
    expect(formatHealthPoints(0)).toBe('0')
    expect(formatHealthPoints(null)).toBe('—')
  })

  it('signs the delta with a real minus and omits it without a previous score', () => {
    expect(formatHealthDelta(72, 75)).toBe('−3')
    expect(formatHealthDelta(75, 72)).toBe('+3')
    expect(formatHealthDelta(72, 72)).toBe('±0')
    expect(formatHealthDelta(72, null)).toBeNull()
    expect(formatHealthDelta(null, 72)).toBeNull()
  })

  it('tones a drop as danger and a rise as success', () => {
    expect(healthDeltaTone(70, 75)).toBe('danger')
    expect(healthDeltaTone(75, 70)).toBe('success')
    expect(healthDeltaTone(70, 70)).toBe('neutral')
    expect(healthDeltaTone(70, null)).toBe('neutral')
  })

  it('counts the applicable components in the footnote', () => {
    expect(renormalizedFootnote(4)).toBe('Weights renormalized over 4 applicable components')
    expect(renormalizedFootnote(1)).toBe('Weights renormalized over 1 applicable component')
  })
})

describe('ordering', () => {
  it('puts components in the fixed order whatever order they arrive in', () => {
    const shuffled = [...HEALTH_COMPONENT_ORDER].reverse().map(component)
    expect(orderedComponents({ components: shuffled }).map((c) => c.key)).toEqual([
      ...HEALTH_COMPONENT_ORDER,
    ])
  })

  it('orders component averages the same way', () => {
    const averages = [
      { key: 'documentation' as const, value: 0.5, applies_count: 3 },
      { key: 'implemented_seen' as const, value: 1, applies_count: 2 },
    ]
    expect(orderedAverages(averages).map((a) => a.key)).toEqual(['implemented_seen', 'documentation'])
    expect(orderedAverages(undefined)).toEqual([])
  })
})

describe('chunkIds', () => {
  it('splits ids into index-aligned chunks of the batch size', () => {
    const ids = Array.from({ length: 1201 }, (_, i) => `e${i}`)
    const chunks = chunkIds(ids, 500)
    expect(chunks.map((c) => c.length)).toEqual([500, 500, 201])
    expect(chunks[1]?.[0]).toBe('e500')
    expect(chunkIds([], 500)).toEqual([])
  })

  it('rejects a chunk size below one', () => {
    expect(() => chunkIds(['a'], 0)).toThrow()
  })
})
