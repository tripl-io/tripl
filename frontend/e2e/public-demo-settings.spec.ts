import { test, expect } from '@playwright/test'
import type { AuthUser } from '../src/types'
import { mockApi } from './mock-api'

// The CI stack uses self-hosted mode. This browser contract mocks the public
// demo API; the backend tests cover the 403s these settings get there.
test('a public demo visitor is not offered the settings the demo refuses', async ({ page }) => {
  const timestamp = '2026-10-08T12:00:00Z'
  const owner: AuthUser = {
    id: 'owner', email: 'owner@example.com', name: 'Owner', role: 'owner',
    is_platform_admin: false, email_verified: true,
    orgs: [{ slug: 'demo-visitor', name: 'Demo workspace', role: 'owner' }],
    created_at: timestamp, updated_at: timestamp,
  }
  const { assertAllMocked } = await mockApi(page, {
    'GET /auth/status': () => ({ json: {
      public_demo: true, deployment_mode: 'hosted', has_users: true,
      google_sign_in: true, registration_enabled: false, email_verification_required: true,
    } }),
    'GET /auth/me': () => ({ json: owner }),
    'GET /projects': () => ({ json: [] }),
    'GET /users/invitations': () => ({ json: [] }),
  })

  await page.goto('/settings/invitations?org=demo-visitor')
  const rail = page.getByRole('navigation', { name: 'Settings' })
  await expect(rail.getByRole('link', { name: 'Invitations', exact: true })).toBeVisible()
  // The organization's own mail, AI, search, photos, trackers and limits: the
  // server answers every change on them with 403 here, so the rail leaves them out.
  for (const label of ['Email', 'AI', 'Semantic search', 'Photos', 'Trackers', 'Limits']) {
    await expect(rail.getByRole('link', { name: label, exact: true })).toHaveCount(0)
  }
  // What the demo does take stays.
  for (const label of ['Details', 'Members', 'Data sources', 'API keys', 'Profile']) {
    await expect(rail.getByRole('link', { name: label, exact: true })).toBeVisible()
  }

  // A bookmark to one of them says the demo does not offer it, instead of a
  // form whose every save is refused.
  await page.goto('/settings/organization/email?org=demo-visitor')
  await expect(page.getByRole('heading', { level: 1, name: 'Email' })).toBeVisible()
  await expect(page.getByRole('note')).toContainText('This public demo runs with these settings fixed')
  await expect(
    page.getByRole('link', { name: 'Run tripl yourself (opens in a new tab)' }),
  ).toHaveAttribute('href', 'https://docs.tripl.io/quick-start')
  assertAllMocked()
})
