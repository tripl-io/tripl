import { randomUUID } from 'node:crypto'
import type { Page } from '@playwright/test'

import { expect, test } from './fixtures'

/**
 * One structured event fires on several screens; the scan stores the busiest
 * row's `page` and keeps the rest as the field's observed values. The edit form
 * says so under the box — before "Split volume by this field" — and the event
 * page's Fields table says it under the value.
 *
 * Observations are written only by a scan, and this stack has no warehouse for
 * a seeded project, so the event's own detail response is augmented with the
 * `observed_values` a scan would have attached. Everything else — the project,
 * the type, the event and every other call the pages make — is the real stack.
 */

const OBSERVED = {
  distinct_count: 2,
  total_count: 100,
  values: [
    { value: 'map/main', count: 62, share: 0.62 },
    { value: 'spot/main', count: 38, share: 0.38 },
  ],
  other_count: 0,
  observed_at: '2026-10-05T09:00:00Z',
  scan_config_id: null,
}

async function seedEvent(page: Page): Promise<{ slug: string; eventId: string; pageFieldId: string }> {
  const slug = `e2e-observed-${randomUUID().slice(0, 8)}`
  const project = await page.request.post('/api/v1/projects', { data: { name: 'Observed values e2e', slug } })
  expect(project.status(), await project.text()).toBe(201)
  const type = await page.request.post(`/api/v1/projects/${slug}/event-types`, {
    data: { name: 'se', display_name: 'Structured event' },
  })
  expect(type.status(), await type.text()).toBe(201)
  const typeId = (await type.json()).id as string
  const fieldIds: Record<string, string> = {}
  for (const name of ['page', 'action']) {
    const field = await page.request.post(`/api/v1/projects/${slug}/event-types/${typeId}/fields`, {
      data: { name, display_name: name === 'page' ? 'Page' : 'Action', field_type: 'string' },
    })
    expect(field.status(), await field.text()).toBe(201)
    fieldIds[name] = (await field.json()).id as string
  }
  const event = await page.request.post(`/api/v1/projects/${slug}/events`, {
    data: {
      event_type_id: typeId,
      name: 'windbar_tap',
      field_values: [
        { field_definition_id: fieldIds.page, value: 'map/main' },
        { field_definition_id: fieldIds.action, value: 'tap' },
      ],
    },
  })
  expect(event.status(), await event.text()).toBe(201)
  return { slug, eventId: (await event.json()).id as string, pageFieldId: fieldIds.page! }
}

/**
 * Attach the observation to the `page` field of the event's detail reads.
 *
 * The event was created through the API, so its values are authored; the
 * value is turned into a scan-written one here, the only kind an observation
 * sits next to, so the line can mark it "stored".
 */
async function observeOnDetail(page: Page, slug: string, eventId: string, pageFieldId: string) {
  await page.route(
    url => url.pathname.endsWith(`/projects/${slug}/events/${eventId}`),
    async route => {
      const response = await route.fetch()
      const body = await response.json()
      body.field_values = body.field_values.map((fv: { field_definition_id: string }) =>
        fv.field_definition_id === pageFieldId ? { ...fv, is_authored: false, observed_values: OBSERVED } : fv,
      )
      await route.fulfill({ response, json: body })
    },
  )
}

test('an event that fires on two screens says so on its form and its page', async ({ page, account }) => {
  expect(account.email).toBeTruthy()
  const { slug, eventId, pageFieldId } = await seedEvent(page)
  await observeOnDetail(page, slug, eventId, pageFieldId)

  await page.goto(`/p/${slug}/events/all/${eventId}/edit`)
  const line = page.getByTestId('field-observed-values')
  await expect(line).toHaveCount(1, { timeout: 60_000 })
  await expect(line).toContainText('Seen with 2 values in the scan of')
  await expect(line).toContainText('map/main (62%, stored), spot/main (38%)')
  // "It varies", then where to see how it varies over time.
  await expect(line.locator('xpath=following-sibling::*[1]')).toHaveText('Split volume by this field')

  await page.goto(`/p/${slug}/monitoring/event/${eventId}`)
  const table = page.getByRole('table', { name: 'Fields', exact: true })
  await expect(table).toBeVisible({ timeout: 60_000 })
  const pageRow = table.getByRole('row').filter({ hasText: 'map/main' })
  await expect(pageRow.getByTestId('field-observed-values')).toHaveText(
    'Seen with 2 values: map/main 62% · spot/main 38%',
  )
  // The single-valued field carries no line.
  const actionRow = table.getByRole('row').filter({ hasText: 'tap' })
  await expect(actionRow.getByTestId('field-observed-values')).toHaveCount(0)
  await expect(table.getByTestId('field-observed-values')).toHaveCount(1)
})
