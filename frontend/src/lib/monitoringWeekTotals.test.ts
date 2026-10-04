import { describe, expect, it } from 'vitest'

import { formatWeekSummary } from './monitoringWeekTotals'

describe('formatWeekSummary', () => {
  it('reads the week and its change against the week before', () => {
    expect(formatWeekSummary({ week_total: 612_000, prior_week_total: 588_000 })).toBe(
      '612k in 7d · +4% vs prior week',
    )
  })

  it('leaves the comparison off when the prior week is empty or absent', () => {
    expect(formatWeekSummary({ week_total: 1_200, prior_week_total: 0 })).toBe('1.2k in 7d')
    expect(formatWeekSummary({ week_total: 40, prior_week_total: null })).toBe('40 in 7d')
  })

  it('says nothing for a response without weekly totals', () => {
    expect(formatWeekSummary(undefined)).toBeNull()
    expect(formatWeekSummary({ week_total: null, prior_week_total: 5 })).toBeNull()
  })
})
