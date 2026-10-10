import { describe, expect, it } from 'vitest'
import {
  PLAN_COVERAGE_HELP,
  dataMatchHelp,
  formatNotImplementedBreakdown,
  formatPlanCoverage,
  planCoverageRatio,
  planCoverageTone,
} from './coverage'

describe('formatPlanCoverage', () => {
  it('formats a partial plan to one decimal place', () => {
    expect(formatPlanCoverage(320, 323)).toBe('99.1%')
  })

  it('shows a clean "100%" only when every active event is implemented', () => {
    expect(formatPlanCoverage(323, 323)).toBe('100%')
  })

  it('shows "100%" when implemented exceeds active', () => {
    expect(formatPlanCoverage(330, 323)).toBe('100%')
  })

  // Nothing to cover is no score, not a failing one.
  it('returns "—" when there are no active events', () => {
    expect(formatPlanCoverage(0, 0)).toBe('—')
    expect(formatPlanCoverage(0, -1)).toBe('—')
  })

  it('never reports a partial plan as "100%"', () => {
    expect(formatPlanCoverage(322, 323)).not.toBe('100%')
    expect(formatPlanCoverage(322, 323)).toBe('99.7%')
  })

  it('clamps a value that would round up to 100.0 down to "99.9%"', () => {
    // 9995 / 10000 = 99.95%, which toFixed(1) rounds to "100.0".
    expect(formatPlanCoverage(9995, 10000)).toBe('99.9%')
  })
})

describe('planCoverageRatio', () => {
  it('returns the implemented/active ratio for a partial plan', () => {
    expect(planCoverageRatio(320, 323)).toBeCloseTo(0.990712, 5)
  })

  it('returns 1 for a fully implemented plan', () => {
    expect(planCoverageRatio(323, 323)).toBe(1)
  })

  it('returns 0 when there are no active events', () => {
    expect(planCoverageRatio(0, 0)).toBe(0)
  })
})

// The Overview's tile links to the Coverage page's; both read this one rule,
// so one figure never wears two severities a click apart.
describe('planCoverageTone', () => {
  it('marks a plan under the good bar as a warning, never an alarm', () => {
    // 58.8%: amber on the Overview and red on Coverage before.
    expect(planCoverageTone(588, 1000)).toBe('warning')
    expect(planCoverageTone(0, 10)).toBe('warning')
  })

  it('leaves a good plan, and a plan with nothing active, neutral', () => {
    expect(planCoverageTone(90, 100)).toBe('neutral')
    expect(planCoverageTone(323, 323)).toBe('neutral')
    expect(planCoverageTone(0, 0)).toBe('neutral')
  })
})

describe('formatNotImplementedBreakdown', () => {
  it('names the rest of the remainder as one group, not a status of its own', () => {
    expect(formatNotImplementedBreakdown(6, 7)).toBe(
      'Not implemented: 6 in review, 1 in another status (draft, ready for dev or deprecated).',
    )
  })

  it('leaves the group out when everything left is in review', () => {
    expect(formatNotImplementedBreakdown(6, 6)).toBe('Not implemented: 6 in review.')
    // The review count can run ahead of the remainder; it is capped to it.
    expect(formatNotImplementedBreakdown(9, 6)).toBe('Not implemented: 6 in review.')
  })

  it('says "none in review" rather than "0 in review"', () => {
    expect(formatNotImplementedBreakdown(0, 7)).toBe(
      'Not implemented: none in review, 7 in another status (draft, ready for dev or deprecated).',
    )
  })
})

// Coverage and Reconciliation each name the other's measure, in its own unit.
describe('governance help', () => {
  it('describes data match as warehouse occurrences on the Coverage page', () => {
    expect(PLAN_COVERAGE_HELP).toContain('occurrences (warehouse rows)')
    expect(PLAN_COVERAGE_HELP).not.toContain('how many planned events are actually seen')
  })

  it('names the window and the unit of data match', () => {
    const help = dataMatchHelp(14)
    expect(help).toContain('over the last 14 days')
    expect(help).toContain('Counts occurrences (warehouse rows), not catalog entries.')
    expect(help).toContain('plan coverage')
  })
})
