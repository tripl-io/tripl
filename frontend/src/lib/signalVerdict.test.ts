import { describe, expect, it } from 'vitest'

import type { SignalVerdictInfo } from '@/types'
import {
  needsVerdict,
  signalCommentDraft,
  verdictAttribution,
  verdictLabel,
} from './signalVerdict'

const VERDICT: SignalVerdictInfo = {
  verdict: 'expected',
  expected_reason: 'campaign',
  note: 'Spring sale',
  author_name: 'Ann Lee',
  created_at: '2026-09-25T19:00:00Z',
  source: 'signal',
}

describe('verdictLabel', () => {
  it('names an expected verdict with its reason', () => {
    expect(verdictLabel(VERDICT)).toBe('Expected · campaign')
  })

  it('names the other verdicts alone', () => {
    expect(verdictLabel({ verdict: 'tracking_bug', expected_reason: null })).toBe('Tracking bug')
    expect(verdictLabel({ verdict: 'false_positive', expected_reason: null })).toBe('False positive')
    expect(verdictLabel({ verdict: 'real_issue', expected_reason: null })).toBe('Real issue')
  })
})

describe('verdictAttribution', () => {
  it('names the author', () => {
    expect(verdictAttribution(VERDICT)).toMatch(/^by Ann Lee, /)
  })

  it('says an incident-sourced verdict came from the incident', () => {
    expect(verdictAttribution({ ...VERDICT, source: 'incident', author_name: null })).toMatch(
      /^From the incident · /,
    )
  })

  it('leaves the time out when it is not known', () => {
    expect(verdictAttribution({ ...VERDICT, created_at: null })).toBe('by Ann Lee')
    expect(
      verdictAttribution({ ...VERDICT, created_at: null, author_name: null, source: 'incident' }),
    ).toBe('From the incident')
  })
})

describe('needsVerdict', () => {
  it('is true only without a verdict', () => {
    expect(needsVerdict({ verdict: null })).toBe(true)
    expect(needsVerdict({})).toBe(true)
    expect(needsVerdict({ verdict: VERDICT })).toBe(false)
  })
})

describe('signalCommentDraft', () => {
  const signal = {
    bucket: '2026-09-25T18:00:00Z',
    direction: 'drop' as const,
    actual_count: 10,
    expected_count: 40,
    z_score: -6,
    unit: null,
    relative_effect: 0.75,
  }

  it('summarises the move and appends the note', () => {
    const draft = signalCommentDraft(signal, '  Tracker dropped the param ')
    expect(draft).toMatch(/^Tracking bug: drop at /)
    expect(draft).toContain('10 vs 40 expected')
    expect(draft.endsWith('\n\nTracker dropped the param')).toBe(true)
  })

  it('leaves the note out when there is none', () => {
    expect(signalCommentDraft(signal, '  ')).not.toContain('\n')
  })
})
