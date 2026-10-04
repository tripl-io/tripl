import { describe, expect, it } from 'vitest'

import {
  MIN_GROUP_SIZE,
  eventNamePrefix,
  groupEventNames,
  isDeepPrefix,
  type NameGroupInput,
} from './eventNameGroups'
import { at } from '@/test/at'

function ev(id: string, name: string): NameGroupInput {
  return { id, name }
}

describe('eventNamePrefix', () => {
  it('strips the final underscore-separated segment', () => {
    expect(eventNamePrefix('checkout_button_click')).toBe('checkout_button')
  })

  it('strips only the last segment for a deep name', () => {
    expect(
      eventNamePrefix('page_value_question_sail_navigation_yes_selected'),
    ).toBe('page_value_question_sail_navigation_yes')
  })

  it('splits on colon delimiters', () => {
    expect(eventNamePrefix('spot:services:start')).toBe('spot:services')
  })

  it('splits on slash delimiters', () => {
    expect(eventNamePrefix('web/checkout/submit')).toBe('web/checkout')
  })

  it('treats a run of delimiters as a single split point', () => {
    expect(eventNamePrefix('spot::services')).toBe('spot')
  })

  it('returns null for a single segment with no delimiter', () => {
    expect(eventNamePrefix('pageview')).toBeNull()
  })

  it('returns null for an empty string', () => {
    expect(eventNamePrefix('')).toBeNull()
  })

  it('returns null when only a leading delimiter is present', () => {
    expect(eventNamePrefix('_foo')).toBeNull()
  })

  it('ignores a trailing delimiter run (no suffix to strip)', () => {
    // The only delimiter is trailing, so there is no interior split point.
    expect(eventNamePrefix('foo_')).toBeNull()
  })

  it('ignores trailing delimiters but still splits on the interior run', () => {
    expect(eventNamePrefix('foo_bar_')).toBe('foo')
  })

  it('handles mixed delimiter types, splitting on the last one', () => {
    expect(eventNamePrefix('a_b:c/d')).toBe('a_b:c')
  })
})

describe('groupEventNames', () => {
  it('forms a cluster when at least MIN_GROUP_SIZE events share a prefix', () => {
    const events = [
      ev('1', 'checkout_button_click'),
      ev('2', 'checkout_button_view'),
      ev('3', 'checkout_button_hover'),
    ]

    const { groups, ungrouped } = groupEventNames(events)

    expect(groups).toEqual([
      { prefix: 'checkout_button', eventIds: ['1', '2', '3'], count: 3 },
    ])
    expect(ungrouped).toEqual([])
  })

  it('does not cluster a prefix shared by fewer than MIN_GROUP_SIZE events', () => {
    const events = [
      ev('1', 'checkout_button_click'),
      ev('2', 'checkout_button_view'),
    ]

    const { groups, ungrouped } = groupEventNames(events)

    expect(groups).toEqual([])
    expect(ungrouped).toEqual(events)
  })

  it('leaves single-segment (prefix-less) names ungrouped', () => {
    const events = [ev('1', 'pageview'), ev('2', 'signup'), ev('3', 'login')]

    const { groups, ungrouped } = groupEventNames(events)

    expect(groups).toEqual([])
    expect(ungrouped).toEqual(events)
  })

  it('preserves the original input order in ungrouped', () => {
    const events = [
      ev('a', 'checkout_button_click'),
      ev('b', 'standalone_one'),
      ev('c', 'checkout_button_view'),
      ev('d', 'standalone_two'),
      ev('e', 'checkout_button_hover'),
    ]

    const { groups, ungrouped } = groupEventNames(events)

    expect(groups).toEqual([
      { prefix: 'checkout_button', eventIds: ['a', 'c', 'e'], count: 3 },
    ])
    // `standalone_*` share no 3-member prefix, so they stay in original order.
    expect(ungrouped.map((e) => e.id)).toEqual(['b', 'd'])
  })

  it('keeps eventIds in input order within a cluster', () => {
    const events = [
      ev('z', 'nav_menu_open'),
      ev('m', 'nav_menu_close'),
      ev('a', 'nav_menu_toggle'),
    ]

    const { groups } = groupEventNames(events)

    expect(at(groups, 0).eventIds).toEqual(['z', 'm', 'a'])
  })

  it('sorts groups by count descending then prefix ascending', () => {
    const events = [
      // beta: 4 events
      ev('b1', 'beta_a'),
      ev('b2', 'beta_b'),
      ev('b3', 'beta_c'),
      ev('b4', 'beta_d'),
      // alpha: 3 events
      ev('a1', 'alpha_a'),
      ev('a2', 'alpha_b'),
      ev('a3', 'alpha_c'),
      // gamma: 3 events (ties alpha on count, loses on prefix order)
      ev('g1', 'gamma_a'),
      ev('g2', 'gamma_b'),
      ev('g3', 'gamma_c'),
    ]

    const { groups } = groupEventNames(events)

    expect(groups.map((g) => g.prefix)).toEqual(['beta', 'alpha', 'gamma'])
    expect(groups.map((g) => g.count)).toEqual([4, 3, 3])
  })

  it('respects a custom minGroupSize', () => {
    const events = [
      ev('1', 'checkout_button_click'),
      ev('2', 'checkout_button_view'),
    ]

    const { groups } = groupEventNames(events, 2)

    expect(groups).toEqual([
      { prefix: 'checkout_button', eventIds: ['1', '2'], count: 2 },
    ])
  })

  it('returns empty results for empty input', () => {
    expect(groupEventNames([])).toEqual({ groups: [], ungrouped: [] })
  })

  it('separates distinct prefixes into distinct clusters', () => {
    const events = [
      ev('1', 'auth_login_success'),
      ev('2', 'auth_login_failure'),
      ev('3', 'auth_login_retry'),
      ev('4', 'cart_item_add'),
      ev('5', 'cart_item_remove'),
      ev('6', 'cart_item_update'),
    ]

    const { groups, ungrouped } = groupEventNames(events)

    expect(groups).toHaveLength(2)
    expect(new Set(groups.map((g) => g.prefix))).toEqual(
      new Set(['auth_login', 'cart_item']),
    )
    expect(ungrouped).toEqual([])
  })

  it('exposes MIN_GROUP_SIZE as the default threshold', () => {
    const events = Array.from({ length: MIN_GROUP_SIZE }, (_, i) =>
      ev(String(i), `shared_prefix_variant${i}`),
    )

    const { groups } = groupEventNames(events)

    expect(groups).toHaveLength(1)
    expect(at(groups, 0).count).toBe(MIN_GROUP_SIZE)
  })
})

describe('isDeepPrefix', () => {
  it('accepts prefixes of two or more segments only', () => {
    expect(isDeepPrefix('checkout')).toBe(false)
    expect(isDeepPrefix('checkout_step')).toBe(true)
    expect(isDeepPrefix('page:view')).toBe(true)
    expect(isDeepPrefix('a/b')).toBe(true)
  })
})
