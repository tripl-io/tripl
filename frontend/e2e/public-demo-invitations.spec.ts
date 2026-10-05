import { test, expect } from '@playwright/test'
import type { AuthUser, Project } from '../src/types'

// The CI stack uses self-hosted mode. This browser contract mocks the public
// demo API; backend tests cover the actual verified-account and viewer grants.
test('a demo owner copies a colleague link and the colleague accepts into the invited workspace', async ({ page }) => {
  const timestamp = '2026-10-04T12:00:00Z'
  const demoOrg = { slug: 'shared-demo', name: 'Shared demo', role: 'owner' as const }
  const owner: AuthUser = {
    id: 'owner', email: 'owner@example.com', name: 'Owner', role: 'owner',
    is_platform_admin: false, email_verified: true, orgs: [demoOrg],
    created_at: timestamp, updated_at: timestamp,
  }
  const colleague: AuthUser = {
    ...owner, id: 'colleague', email: 'colleague@example.com', name: 'Colleague',
    orgs: [{ slug: 'personal', name: 'Personal workspace', role: 'owner' }],
  }
  const project: Project = {
    id: 'demo', slug: 'demo-shared', name: 'Shared analytics demo', description: '',
    created_at: timestamp, updated_at: timestamp, app_version_keep_releases: 10,
    is_demo: true, generation_status: 'ready', my_role: 'viewer', can_mutate: false,
    summary: {
      event_type_count: 1, event_count: 1, active_event_count: 1, implemented_event_count: 1,
      review_pending_event_count: 0, archived_event_count: 0, variable_count: 0, scan_count: 0,
      alert_destination_count: 0, alert_rule_count: 0, monitoring_signal_count: 0,
      firing_monitor_count: 0, open_incident_count: 0, failing_scan_config_count: 0,
      latest_scan_job: null, latest_signal: null,
    },
  }
  let session = owner
  let accepted = false
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: async (value: string) => { sessionStorage.setItem('copied-invite', value) } },
    })
  })
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/auth/status')) {
      return route.fulfill({ json: {
        public_demo: true, deployment_mode: 'hosted', has_users: true,
        google_sign_in: true, registration_enabled: false, email_verification_required: true,
      } })
    }
    if (path.endsWith('/auth/me')) return route.fulfill({ json: session })
    if (path.endsWith('/users/invitations') && request.method() === 'POST') {
      expect(request.postDataJSON()).toEqual({ email: colleague.email, role: 'member' })
      return route.fulfill({ json: {
        invitation: {
          id: 'invite', email: colleague.email, role: 'member', is_expired: false,
          invited_by_user_id: owner.id, created_at: timestamp, expires_at: timestamp,
        },
        accept_path: '/invite/demo-token', expires_at: timestamp,
      } })
    }
    if (path.endsWith('/auth/invitations/demo-token/accept')) {
      expect(request.postDataJSON()).toEqual({})
      session = { ...colleague, orgs: [...colleague.orgs, { ...demoOrg, role: 'member' }] }
      accepted = true
      return route.fulfill({ json: session })
    }
    if (path.endsWith('/auth/invitations/demo-token')) {
      return route.fulfill({ json: { email: colleague.email, role: 'member', expires_at: timestamp } })
    }
    if (path.endsWith('/projects')) {
      return route.fulfill({ json: session.id === owner.id || accepted ? [project] : [] })
    }
    if (path.endsWith('/projects/demo-shared')) return route.fulfill({ json: project })
    if (path.includes('/metrics/total')) {
      return route.fulfill({ json: { data: [] } })
    }
    if (path.endsWith('/overview/kpi-series')) {
      return route.fulfill({ json: { new_events: [] } })
    }
    // The Overview's plan-health panel renders an object; the catch-all `[]`
    // below would crash it.
    if (path.endsWith('/projects/demo-shared/health')) {
      return route.fulfill({ json: {
        score: null, grade: null, scored_events: 0, healthy_count: 0, warning_count: 0,
        unhealthy_count: 0, component_averages: [], worst: [], trend: [], previous_score: null,
        computed_at: timestamp,
      } })
    }
    return route.fulfill({ json: [] })
  })

  await page.goto('/settings/invitations?org=shared-demo')
  await expect(page.getByText(/No email is sent/)).toBeVisible()
  await page.getByRole('textbox', { name: 'Email', exact: true }).fill(colleague.email)
  await page.getByRole('button', { name: 'Create invite link' }).click()
  const invite = await page.getByRole('textbox', { name: 'Invite link' }).inputValue()
  await page.getByRole('button', { name: 'Copy', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Copied', exact: true })).toBeVisible()
  expect(await page.evaluate(() => sessionStorage.getItem('copied-invite'))).toBe(invite)

  session = colleague
  await page.goto(invite)
  await expect(page.getByText(/viewer access to existing demo projects/)).toBeVisible()
  await page.getByRole('button', { name: 'Accept with this account' }).click()
  await expect(page).toHaveURL(/\/o\/shared-demo$/)
  await expect(page.getByText(project.name, { exact: true }).first()).toBeVisible()
  await page.goto('/o/shared-demo/p/demo-shared/overview')
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible()
  await expect(page.locator('[data-demo-banner]').getByText('Demo workspace', { exact: true })).toBeVisible()
})
