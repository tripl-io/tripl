import { describe, expect, it } from 'vitest'

import { rowCapHint } from './rowCapHints'

describe('rowCapHint (B15)', () => {
  it("quotes the organization's own caps once they are known", () => {
    const defaults = { scan_row_limit_default: 20_000, metrics_row_limit_default: 250_000 }
    expect(rowCapHint('catalog', defaults)).toBe(
      'Most rows one catalog run reads. Empty uses the organization default: 20,000.',
    )
    expect(rowCapHint('metrics', defaults)).toBe(
      'Most rows one metrics run reads. Empty uses the organization default: 250,000.',
    )
  })

  it('falls back to the shipped defaults, saying they may have changed', () => {
    expect(rowCapHint('catalog', undefined)).toBe(
      'Most rows one catalog run reads. Empty uses the organization default: 50,000 unless changed in its settings.',
    )
  })
})
