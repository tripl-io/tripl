import { describe, expect, it } from 'vitest'

import { historyFieldLabel, historyValueLabel } from './eventHistory'

describe('historyFieldLabel', () => {
  it('names the known fields', () => {
    expect(historyFieldLabel('created')).toBe('Created')
    expect(historyFieldLabel('field:screen')).toBe('Field · screen')
    expect(historyFieldLabel('meta:owner')).toBe('owner')
  })

  it('names a signal verdict (#254)', () => {
    expect(historyFieldLabel('signal_verdict')).toBe('Signal verdict')
  })
})

describe('historyValueLabel', () => {
  it('passes other fields through', () => {
    expect(historyValueLabel('title', 'Signup')).toBe('Signup')
    expect(historyValueLabel('title', null)).toBeNull()
  })

  it('reads a verdict with its reason and note', () => {
    expect(historyValueLabel('signal_verdict', 'tracking_bug')).toBe('Tracking bug')
    expect(historyValueLabel('signal_verdict', 'expected:campaign')).toBe('Expected · campaign')
    expect(historyValueLabel('signal_verdict', 'real_issue — Checkout fails on Android')).toBe(
      'Real issue — Checkout fails on Android',
    )
    expect(historyValueLabel('signal_verdict', 'expected:release — v5.2 — hotfix')).toBe(
      'Expected · release — v5.2 — hotfix',
    )
  })

  it('says a cleared verdict was cleared', () => {
    expect(historyValueLabel('signal_verdict', null)).toBe('cleared')
  })

  it('shows a value it does not recognise as recorded', () => {
    expect(historyValueLabel('signal_verdict', 'something_else')).toBe('something_else')
  })
})
