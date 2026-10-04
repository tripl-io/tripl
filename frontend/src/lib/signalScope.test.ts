import { describe, expect, it } from 'vitest'

import { signalScopeLabel, signalScopeRefLabel, unnamedScopeLabel } from './signalScope'
import type { MonitoringSignal } from '@/types'

const REF = '3f2a1b9c-0000-4000-8000-000000000000'

function signal(overrides: Partial<MonitoringSignal> = {}): MonitoringSignal {
  return {
    scope_type: 'event',
    scope_ref: REF,
    scope_name: 'checkout_started',
    direction: 'drop',
    actual_count: 10,
    expected_count: 100,
    detected_at: '2026-07-29T10:00:00Z',
    ...overrides,
  } as MonitoringSignal
}

describe('signalScopeLabel — scope kinds', () => {
  it('names project total without a ref', () => {
    expect(signalScopeLabel(signal({ scope_type: 'project_total' }))).toBe('Project total')
  })

  it.each([
    ['metric', 'Metric · Checkout conversion'],
    ['schema_drift', 'Schema drift · Checkout conversion'],
    ['distribution_drift', 'Distribution drift · Checkout conversion'],
    ['variable_value_drift', 'Value drift · Checkout conversion'],
    ['release_regression', 'Release regression · Checkout conversion'],
  ])('does not call a %s an event', (scopeType, expected) => {
    // The top bar's copy fell through to `event ${ref}` for every scope it did
    // not handle, so these five were all announced as events once the bell
    // started reading the expanded list.
    expect(
      signalScopeLabel(
        signal({
          scope_type: scopeType as MonitoringSignal['scope_type'],
          scope_name: 'Checkout conversion',
        }),
      ),
    ).toBe(expected)
  })

  it('falls back to the raw scope type for a kind it has never seen', () => {
    expect(
      signalScopeLabel(signal({ scope_type: 'brand_new' as MonitoringSignal['scope_type'] })),
    ).toBe('brand_new · checkout_started')
  })
})

describe('signalScopeLabel — display names', () => {
  it('reads the name the server resolved, off the signal', () => {
    expect(signalScopeLabel(signal())).toBe('Event · checkout_started')
  })

  it.each([
    ['null — the entity was deleted', null],
    ['undefined — a locally synthesised signal', undefined],
    ['an empty string', ''],
  ])('returns null rather than a ref when the name is %s', (_case, scopeName) => {
    // Terminal, not pending: the entity was deleted, or the kind has no entity
    // to name. Either way the caller has to say so in words — a hex prefix reads
    // as a name and puts two names on one incident.
    expect(signalScopeLabel(signal({ scope_name: scopeName }))).toBeNull()
  })
})

describe('unnamedScopeLabel', () => {
  it.each([
    ['event', 'deleted event'],
    ['event_type', 'deleted event type'],
    ['metric', 'deleted metric'],
  ])('calls an unresolved %s deleted, because only these three are looked up', (scopeType, word) => {
    expect(unnamedScopeLabel(signal({ scope_type: scopeType as MonitoringSignal['scope_type'] })))
      .toBe(word)
  })

  it('stays neutral for a kind that has no entity behind it', () => {
    // A release regression is never named at all, so "deleted" would be a claim
    // about an entity that never existed.
    expect(unnamedScopeLabel(signal({ scope_type: 'release_regression' }))).toBe('unnamed scope')
  })
})

describe('signalScopeRefLabel', () => {
  it('keeps the scope kind and 8 hex characters, for an aria-label or a title', () => {
    expect(signalScopeRefLabel(signal({ scope_type: 'metric' }))).toBe('Metric 3f2a1b9c')
  })

  it('names project total rather than slicing a ref out of it', () => {
    expect(signalScopeRefLabel(signal({ scope_type: 'project_total' }))).toBe('Project total')
  })

  it('does not call a never-named kind an event either', () => {
    // The kinds with no entity behind them reach the operator through this
    // function alone, so tripl-jfm3.120's wrong fallback would survive here.
    expect(signalScopeRefLabel(signal({ scope_type: 'release_regression' }))).toBe(
      'Release regression 3f2a1b9c',
    )
  })
})
