import { describe, expect, it } from 'vitest'

import {
  INBOX_SCOPE_NAME_LIMIT,
  incidentDeltaBadge,
  incidentHeadline,
  incidentItemsLabel,
  incidentMoreScopesLabel,
} from './inboxCardLabels'

describe('incidentHeadline', () => {
  it('leads with the first scope and counts the rest', () => {
    expect(incidentHeadline({ scope_names: ['checkout', 'cart', 'pay'] })).toEqual({
      primary: 'checkout',
      more: 2,
      capped: false,
    })
    expect(incidentMoreScopesLabel(incidentHeadline({ scope_names: ['checkout', 'cart', 'pay'] }))).toBe(
      'and 2 more',
    )
  })

  it('does not claim an exact count once the server cut the list', () => {
    const names = Array.from({ length: INBOX_SCOPE_NAME_LIMIT }, (_, index) => `scope_${index}`)
    const headline = incidentHeadline({ scope_names: names })
    expect(headline).toEqual({ primary: 'scope_0', more: INBOX_SCOPE_NAME_LIMIT - 1, capped: true })
    expect(incidentMoreScopesLabel(headline)).toBe(`and ${INBOX_SCOPE_NAME_LIMIT - 1}+ more`)
  })
})

describe('incidentItemsLabel', () => {
  it('counts the items, and the scopes when there is more than one', () => {
    // It read "8 items (4 distinct scope names shown)" with one name visible.
    expect(incidentItemsLabel({ item_count: 8, scope_names: ['a', 'b', 'c', 'd'] })).toBe(
      '8 items across 4 scopes',
    )
    expect(incidentItemsLabel({ item_count: 3, scope_names: ['a'] })).toBe('3 items')
    expect(incidentItemsLabel({ item_count: 1, scope_names: ['a'] })).toBe('1 item')
  })

  it('says 8+ scopes when the list is full', () => {
    const names = Array.from({ length: INBOX_SCOPE_NAME_LIMIT }, (_, index) => `scope_${index}`)
    expect(incidentItemsLabel({ item_count: 20, scope_names: names })).toBe(
      `20 items across ${INBOX_SCOPE_NAME_LIMIT}+ scopes`,
    )
  })
})

describe('incidentDeltaBadge', () => {
  it('signs the change by direction, in the colours every signal surface uses', () => {
    // Spike red, drop amber (statusLexicon's SIGNAL_DIRECTION). The card had
    // them the other way round, so one spike was red on Anomalies and amber here.
    expect(incidentDeltaBadge({ direction: 'spike', actual_count: 5767, expected_count: 3174 })).toEqual({
      label: '+82%',
      tone: 'danger',
    })
    expect(incidentDeltaBadge({ direction: 'drop', actual_count: 412, expected_count: 1010 })).toEqual({
      label: '−59%',
      tone: 'warning',
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
