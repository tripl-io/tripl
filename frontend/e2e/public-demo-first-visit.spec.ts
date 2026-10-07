import { test, expect } from '@playwright/test'
import type { AuthUser, Project } from '../src/types'
import { mockApi } from './mock-api'

// The CI stack uses self-hosted mode, so the public demo's API is mocked here;
// demo-provisioning.spec.ts covers the real create and its worker.
test('a newcomer to the public demo gets their demo without looking for the button, once', async ({ page }) => {
  const timestamp = '2026-10-08T12:00:00Z'
  const newcomer: AuthUser = {
    id: 'newcomer', email: 'newcomer@example.com', name: 'Newcomer', role: 'owner',
    is_platform_admin: false, email_verified: true,
    orgs: [{ slug: 'newcomer-ws', name: 'Newcomer', role: 'owner' }],
    created_at: timestamp, updated_at: timestamp,
  }
  // The create answers with a shell the worker is still seeding, and every
  // re-read says the same: the dialog stays up for as long as the test looks.
  const shell: Project = {
    id: 'demo', slug: 'demo-newcomer', name: 'Analytics demo', description: '',
    created_at: timestamp, updated_at: timestamp, app_version_keep_releases: 10,
    is_demo: true, generation_status: 'seeding', my_role: 'owner', can_mutate: true,
    created_by_user_id: newcomer.id,
    summary: {
      event_type_count: 0, event_count: 0, active_event_count: 0, implemented_event_count: 0,
      review_pending_event_count: 0, archived_event_count: 0, variable_count: 0, scan_count: 0,
      alert_destination_count: 0, alert_rule_count: 0, monitoring_signal_count: 0,
      firing_monitor_count: 0, open_incident_count: 0, failing_scan_config_count: 0,
      latest_scan_job: null, latest_signal: null,
    },
  }
  let creates = 0
  const { assertAllMocked } = await mockApi(page, {
    'GET /auth/status': () => ({ json: {
      public_demo: true, deployment_mode: 'hosted', has_users: true,
      google_sign_in: true, registration_enabled: false, email_verification_required: true,
    } }),
    'GET /auth/me': () => ({ json: newcomer }),
    // A seeding shell is not listed: the workspace reads as empty throughout.
    'GET /projects': () => ({ json: [] }),
    'GET /data-sources': () => ({ json: [] }),
    'GET /me/notifications/unread-count': () => ({ json: { unread: 0 } }),
    'POST /projects/demo': () => {
      creates += 1
      return { status: 202, json: shell }
    },
    'GET /projects/demo-newcomer': () => ({ json: shell }),
  })

  // Signed in with Google, they land on their empty workspace — and the demo
  // the sign-in card promised is already being made.
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Generating demo workspace' })).toBeVisible()
  expect(creates).toBe(1)

  // Back later, on a workspace still empty (the create was abandoned): the
  // button is theirs to press, and nothing starts by itself again.
  const demoStatusKnown = page.waitForResponse((response) => response.url().endsWith('/auth/status'))
  await page.reload()
  await demoStatusKnown
  await expect(page.getByRole('button', { name: 'Generate demo project' })).toBeVisible()
  // A start would follow the list and the status within a tick; give it room
  // to show, so the absence below is not just a check made too early.
  await page.waitForTimeout(500)
  await expect(page.getByRole('heading', { name: 'Generating demo workspace' })).toHaveCount(0)
  expect(creates).toBe(1)
  assertAllMocked()
})
