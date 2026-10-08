import { describe, expect, it } from 'vitest'

import { incidentDeltaBadge, incidentHeadline } from './inboxCardLabels'

describe('incidentHeadline', () => {
  it('leads with the first scope and counts the rest', () => {
    expect(incidentHeadline({ scope_names: ['checkout', 'cart', 'pay'] })).toEqual({
      primary: 'checkout',
      more: 2,
    })
  })
})

describe('incidentDeltaBadge', () => {
  it('signs the change by direction', () => {
    expect(incidentDeltaBadge({ direction: 'spike', actual_count: 5767, expected_count: 3174 })).toEqual({
      label: '+82%',
      tone: 'warning',
    })
    expect(incidentDeltaBadge({ direction: 'drop', actual_count: 412, expected_count: 1010 })).toEqual({
      label: '−59%',
      tone: 'danger',
    })
  })

  it('has no percentage without a baseline, but still flags a drop to zero', () => {
    expect(incidentDeltaBadge({ direction: 'spike', actual_count: 5, expected_count: 0 })).toBeNull()
    expect(incidentDeltaBadge({ direction: 'drop', actual_count: 0, expected_count: 40 })?.label).toBe(
      'dropped to zero',
    )
  })

  it('gives a drift no badge: its counts are rows the scan compared, not a size', () => {
    // A distribution drift's row says "spike" and carries the two windows' rows;
    // the badge read "+0%".
    expect(
      incidentDeltaBadge({
        direction: 'spike',
        actual_count: 48000,
        expected_count: 48000,
        scope_types: ['distribution'],
      }),
    ).toBeNull()
  })
})
