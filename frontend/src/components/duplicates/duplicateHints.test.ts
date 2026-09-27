import { describe, expect, it } from 'vitest'
import type { DuplicateCheckResult } from '@/types'
import {
  duplicateSummary,
  formatDuplicateScore,
  lintIssues,
  suggestedName,
  visibleDuplicates,
} from './duplicateHints'

const result: DuplicateCheckResult = {
  duplicates: [
    { event_id: 'b', name: 'paywall_shown', event_type_id: 'et-1', status: 'live', score: 0.9, reasons: [] },
    { event_id: 'a', name: 'paywall_view', event_type_id: 'et-1', status: 'live', score: 0.97, reasons: [] },
    { event_id: 'c', name: 'paywall_open', event_type_id: 'et-1', status: 'live', score: 0.89, reasons: [] },
    { event_id: 'd', name: 'paywall_seen', event_type_id: 'et-1', status: 'live', score: 0.88, reasons: [] },
  ],
  lint: [
    { code: 'case', message: 'Use snake_case.', suggestion: 'paywall_screen_view' },
    { code: 'case', message: 'Repeated.', suggestion: 'paywall_screen_view' },
    { code: 'verb_order', message: 'Verb goes last.', suggestion: 'screen_view_paywall' },
  ],
  suggestion: null,
}

describe('duplicate hint helpers (F12, #265)', () => {
  it('floors the score so a near-miss never reads as the threshold', () => {
    expect(formatDuplicateScore(0.879)).toBe('87%')
    expect(formatDuplicateScore(0.94)).toBe('94%')
    expect(formatDuplicateScore(1.2)).toBe('100%')
  })

  it('keeps the server’s order (same type first), filtering and slicing only', () => {
    // 'b' (0.9) stays ahead of 'a' (0.97): the server ranked it first.
    expect(visibleDuplicates(result).map(m => m.event_id)).toEqual(['b', 'a', 'c'])
    expect(visibleDuplicates(result, new Set(['a'])).map(m => m.event_id)).toEqual(['b', 'c', 'd'])
    expect(visibleDuplicates(result, undefined, 1).map(m => m.event_id)).toEqual(['b'])
    expect(visibleDuplicates(undefined)).toEqual([])
  })

  it('does not reorder a cross-type match above a same-type one', () => {
    const mixed: DuplicateCheckResult = {
      duplicates: [
        { event_id: 'same', name: 'paywall_view', event_type_id: 'et-1', status: 'live', score: 0.9, reasons: [] },
        { event_id: 'other', name: 'paywall_view', event_type_id: 'et-2', status: 'live', score: 0.99, reasons: [] },
      ],
      lint: [],
    }
    expect(visibleDuplicates(mixed).map(m => m.event_id)).toEqual(['same', 'other'])
  })

  it('summarises the count for the live region, or says nothing', () => {
    expect(duplicateSummary(0)).toBe('')
    expect(duplicateSummary(1)).toBe('1 possible duplicate')
    expect(duplicateSummary(2)).toBe('2 possible duplicates')
  })

  it('offers the whole-name suggestion first, else the first issue’s, never the name itself', () => {
    expect(suggestedName({ ...result, suggestion: 'paywall_view_screen' }, 'x')).toBe('paywall_view_screen')
    expect(suggestedName(result, 'x')).toBe('paywall_screen_view')
    expect(suggestedName(result, 'paywall_screen_view')).toBe('screen_view_paywall')
    expect(suggestedName({ duplicates: [], lint: [] }, 'x')).toBeNull()
  })

  it('shows one issue per code', () => {
    expect(lintIssues(result).map(issue => issue.message)).toEqual(['Use snake_case.', 'Verb goes last.'])
  })
})
