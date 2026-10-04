import { describe, expect, it } from 'vitest'
import { disambiguate } from './successorLabels'

const base = (item: { name: string }) => `${item.name} · Screen`

describe('disambiguate', () => {
  it('leaves labels that are already unique alone', () => {
    expect(disambiguate(
      [
        { id: 'a', name: 'checkout', status: 'live', created_at: '2026-01-02T10:00:00Z' },
        { id: 'b', name: 'cart', status: 'live', created_at: '2026-01-02T10:00:00Z' },
      ],
      base,
    )).toEqual([
      { id: 'a', name: 'checkout · Screen' },
      { id: 'b', name: 'cart · Screen' },
    ])
  })

  it('tells two namesakes of one type apart by status and the day they were added', () => {
    expect(disambiguate(
      [
        { id: 'a', name: 'checkout', status: 'deprecated', created_at: '2025-11-02T10:00:00Z' },
        { id: 'b', name: 'checkout', status: 'live', created_at: '2026-01-02T10:00:00Z' },
      ],
      base,
    ).map(option => option.name)).toEqual([
      'checkout · Screen · deprecated · added 2025-11-02',
      'checkout · Screen · live · added 2026-01-02',
    ])
  })

  it('falls back to the id when status and day match too', () => {
    const names = disambiguate(
      [
        { id: 'aaaaaaaa-1', name: 'checkout', status: 'live', created_at: '2026-01-02T10:00:00Z' },
        { id: 'bbbbbbbb-2', name: 'checkout', status: 'live', created_at: '2026-01-02T11:00:00Z' },
      ],
      base,
    ).map(option => option.name)
    expect(names).toEqual([
      'checkout · Screen · live · added 2026-01-02 · #aaaaaaaa',
      'checkout · Screen · live · added 2026-01-02 · #bbbbbbbb',
    ])
  })
})
