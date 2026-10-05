import { randomUUID } from 'node:crypto'

import type { Route } from '@playwright/test'

import type { AlertDestination } from '../src/types'
import { expect, signInAsOwner, test } from './fixtures'

/**
 * PagerDuty and Microsoft Teams destinations, added through the destination
 * dialog the way a person does it: PagerDuty from the guided setup's channel
 * tiles, Teams from "Add destination" once a destination exists.
 *
 * The project, the page and every other alerting call are the real stack. The
 * destinations collection alone is answered here: saving a Teams URL resolves
 * its host for the SSRF guard, and a CI runner cannot be relied on to resolve a
 * Microsoft webhook domain. The backend suite covers what the server does with
 * the body (test_pagerduty_teams_destinations.py); this proves the dialog sends
 * the right one and the page lists what comes back.
 */
test('a PagerDuty and a Microsoft Teams destination are added through the dialog', async ({ page }) => {
  await signInAsOwner(page)
  const slug = `e2e-pd-teams-${randomUUID().slice(0, 8)}`
  const created = await page.request.post('/api/v1/projects', {
    data: { name: 'PagerDuty and Teams', slug, description: '' },
  })
  expect(created.status(), await created.text()).toBe(201)
  const projectId = ((await created.json()) as { id: string }).id

  const destinations: AlertDestination[] = []
  const posted: Record<string, unknown>[] = []
  const isCollection = (url: URL) => url.pathname.endsWith(`/projects/${slug}/alert-destinations`)
  await page.route(isCollection, async (route: Route) => {
    const request = route.request()
    if (request.method() === 'GET') return route.fulfill({ json: destinations })
    if (request.method() !== 'POST') return route.fallback()
    const body = request.postDataJSON() as Record<string, unknown>
    posted.push(body)
    const destination = toDestination(projectId, body)
    destinations.unshift(destination)
    return route.fulfill({ status: 201, json: destination })
  })

  await page.goto(`/p/${slug}/alerting`)
  // A route's first visit compiles it on the dev server: give it time.
  await page.getByRole('button', { name: 'PagerDuty' }).click({ timeout: 60_000 })
  const pagerDialog = page.getByRole('dialog', { name: /New PagerDuty destination/ })
  await pagerDialog.getByLabel('Name').fill('On-call pager')
  await pagerDialog.getByLabel('Integration key').fill('R0uT1nGkEy0123456789abcdefABCDEF')
  await pagerDialog.getByLabel('Severity').selectOption('critical')
  await pagerDialog.getByRole('button', { name: 'Create' }).click()
  await expect(pagerDialog).toBeHidden()

  // The guided flow hands the new destination to a prefilled rule; this test
  // is about destinations, so that dialog is dismissed untouched.
  const ruleDialog = page.getByRole('dialog')
  if (await ruleDialog.isVisible()) await page.keyboard.press('Escape')

  await page.goto(`/p/${slug}/alerting?section=destinations`)
  await page.getByRole('button', { name: 'Add destination' }).click({ timeout: 60_000 })
  await page.getByRole('menuitem', { name: 'Microsoft Teams' }).click()
  const teamsDialog = page.getByRole('dialog', { name: /New Microsoft Teams destination/ })
  await teamsDialog.getByLabel('Name').fill('Ops channel')
  await teamsDialog.getByLabel('Webhook URL').fill('https://contoso.webhook.office.com/webhookb2/e2e')
  await teamsDialog.getByRole('button', { name: 'Create' }).click()
  await expect(teamsDialog).toBeHidden()

  await expect(page.getByText('On-call pager')).toBeVisible()
  await expect(page.getByText('Ops channel')).toBeVisible()
  expect(posted).toEqual([
    expect.objectContaining({
      type: 'pagerduty',
      pagerduty_routing_key: 'R0uT1nGkEy0123456789abcdefABCDEF',
      pagerduty_severity: 'critical',
    }),
    expect.objectContaining({
      type: 'teams',
      teams_webhook_url: 'https://contoso.webhook.office.com/webhookb2/e2e',
    }),
  ])
  // Secrets never come back in the list the page renders.
  await expect(page.getByText('R0uT1nGkEy0123456789abcdefABCDEF')).toHaveCount(0)

  await page.request.delete(`/api/v1/projects/${slug}`)
})

/** What the API answers a create with: secrets reported as set, never echoed. */
function toDestination(projectId: string, body: Record<string, unknown>): AlertDestination {
  const now = new Date().toISOString()
  return {
    id: randomUUID(),
    project_id: projectId,
    type: body.type as AlertDestination['type'],
    name: String(body.name),
    enabled: true,
    webhook_set: false,
    bot_token_set: false,
    chat_id: null,
    target_url_set: false,
    webhook_header_name: null,
    email_recipients: null,
    email_from_address: null,
    email_subject_template: null,
    jira_base_url: null,
    jira_auth_email: null,
    jira_api_token_set: false,
    jira_project_key: null,
    jira_issue_type: null,
    linear_api_key_set: false,
    linear_team_id: null,
    linear_state_id: null,
    linear_label_ids: null,
    pagerduty_routing_key_set: body.type === 'pagerduty',
    pagerduty_severity: body.type === 'pagerduty' ? String(body.pagerduty_severity ?? 'error') : null,
    teams_webhook_set: body.type === 'teams',
    delivery_schedule_cron: null,
    project_timezone: 'UTC',
    last_digest_at: null,
    next_digest_at: null,
    held_count: 0,
    is_local: false,
    delivery_count: 0,
    incident_count: 0,
    rules: [],
    created_at: now,
    updated_at: now,
  }
}
