import { describe, expect, it } from 'vitest'
import type { Project } from '@/types'
import {
  demoGenerationBlockedReason,
  demoGenerationWarning,
  ownedDemoCount,
} from './demoGenerationGuard'
import { MAX_DEMOS_PER_CREATOR } from './useDemoProvisioning'

function makeProject(overrides: Partial<Project>): Project {
  return {
    id: 'p-1',
    name: 'Demo Project',
    slug: 'demo-1',
    description: '',
    app_version_keep_releases: 5,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    is_demo: true,
    generation_status: 'ready',
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
      failing_scan_config_count: 0,
      latest_scan_job: null,
      latest_signal: null,
    },
    ...overrides,
  } as Project
}

describe('ownedDemoCount', () => {
  it('counts only the demos this user created', () => {
    const projects = [
      makeProject({ id: 'a', created_by_user_id: 'u-1' }),
      makeProject({ id: 'b', created_by_user_id: 'u-2' }),
      makeProject({ id: 'c', created_by_user_id: 'u-1', is_demo: false }),
    ]

    expect(ownedDemoCount(projects, 'u-1')).toBe(1)
  })

  it('is zero for an unauthenticated caller', () => {
    expect(ownedDemoCount([makeProject({ created_by_user_id: 'u-1' })], undefined)).toBe(0)
  })
})

describe('demoGenerationWarning', () => {
  it('adds no friction to the first demo', () => {
    expect(demoGenerationWarning(0)).toBeNull()
  })

  it('asks before minting a second identical-looking workspace', () => {
    const warning = demoGenerationWarning(1)

    expect(warning?.canProceed).toBe(true)
    expect(warning?.message).toMatch(/Resetting an existing demo/i)
  })

  it('refuses once the per-creator cap is reached', () => {
    const warning = demoGenerationWarning(MAX_DEMOS_PER_CREATOR)

    expect(warning?.canProceed).toBe(false)
    expect(warning?.title).toBe('Demo limit reached')
  })
})

describe('demoGenerationBlockedReason (DEMO-27)', () => {
  it('leaves the button enabled below the cap', () => {
    expect(demoGenerationBlockedReason(0)).toBeNull()
    expect(demoGenerationBlockedReason(MAX_DEMOS_PER_CREATOR - 1)).toBeNull()
  })

  it('names the cap and the way out once it is reached', () => {
    const reason = demoGenerationBlockedReason(MAX_DEMOS_PER_CREATOR)
    expect(reason).toContain(`${MAX_DEMOS_PER_CREATOR} of ${MAX_DEMOS_PER_CREATOR} demos`)
    expect(reason).toMatch(/reset or delete one/i)
  })
})
