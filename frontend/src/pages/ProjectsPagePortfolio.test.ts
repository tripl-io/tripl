import { describe, expect, it } from 'vitest'
import type { Project } from '@/types'
import { reviewQueueHint, summarizePortfolio } from './ProjectsPagePortfolio'

function project(
  slug: string,
  updatedAt: string,
  summary: Partial<Project['summary']> = {},
): Project {
  return {
    id: slug,
    name: slug.toUpperCase(),
    slug,
    description: '',
    app_version_keep_releases: 5,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: updatedAt,
    summary: {
      event_type_count: 0,
      event_count: 0,
      active_event_count: 0,
      implemented_event_count: 0,
      review_pending_event_count: 0,
      archived_event_count: 0,
      variable_count: 0,
      scan_count: 0,
      alert_destination_count: 0,
      alert_rule_count: 0,
      monitoring_signal_count: 0,
      firing_monitor_count: 0,
      open_incident_count: 0,
      failing_scan_config_count: 0,
      latest_scan_job: null,
      latest_signal: null,
      ...summary,
    },
  } as Project
}

describe('summarizePortfolio', () => {
  it('sorts newest first without touching the input', () => {
    const input = [
      project('old', '2026-01-01T00:00:00Z'),
      project('new', '2026-03-01T00:00:00Z'),
    ]
    const { projects } = summarizePortfolio(input)
    expect(projects.map((p) => p.slug)).toEqual(['new', 'old'])
    expect(input.map((p) => p.slug)).toEqual(['old', 'new'])
  })

  it('totals the workspace and counts projects per condition', () => {
    const result = summarizePortfolio([
      project('a', '2026-01-01T00:00:00Z', {
        active_event_count: 10,
        implemented_event_count: 4,
        scan_count: 2,
        monitoring_signal_count: 3,
        failing_scan_config_count: 2,
      }),
      project('b', '2026-01-02T00:00:00Z', {
        active_event_count: 5,
        implemented_event_count: 5,
        review_pending_event_count: 1,
      }),
    ])
    expect(result.totals.projectCount).toBe(2)
    expect(result.totals.activeEventCount).toBe(15)
    expect(result.totals.implementedEventCount).toBe(9)
    expect(result.totals.reviewPendingEventCount).toBe(1)
    expect(result.projectsWithScans).toBe(1)
    expect(result.projectsWithSignals).toBe(1)
    expect(result.projectsWithFailedScan).toBe(1)
    expect(result.failingScanConfigCount).toBe(2)
  })

  it('returns zeroes for an empty workspace', () => {
    const result = summarizePortfolio([])
    expect(result.projects).toEqual([])
    expect(result.totals.projectCount).toBe(0)
    expect(result.failingScanConfigCount).toBe(0)
  })
})

describe('reviewQueueHint', () => {
  it('names the biggest queues first and summarises the rest', () => {
    const hint = reviewQueueHint([
      project('a', '2026-01-01T00:00:00Z', { review_pending_event_count: 1 }),
      project('b', '2026-01-01T00:00:00Z', { review_pending_event_count: 9 }),
      project('c', '2026-01-01T00:00:00Z', { review_pending_event_count: 4 }),
      project('d', '2026-01-01T00:00:00Z', { review_pending_event_count: 2 }),
    ])
    expect(hint).toBe('9 in B · 4 in C · 2 in D · +1 more')
  })

  it('says when nothing is pending', () => {
    expect(reviewQueueHint([project('a', '2026-01-01T00:00:00Z')])).toBe(
      'No pending event reviews',
    )
  })
})
