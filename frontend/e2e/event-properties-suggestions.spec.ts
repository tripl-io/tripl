import { randomUUID } from 'node:crypto'
import type { Page } from '@playwright/test'

import { expect, test } from './fixtures'

/**
 * The event's Properties panel offers the properties its field values name
 * through `${…}` (tripl-4zzc.6). It used to say "No properties on this event
 * yet." under a JSON value that plainly used three, so an analyst re-entered
 * each one through "Add property". Walked on a plan branch, which holds its own
 * copy of every event under a new id — the copy is the one opened and written.
 */

const EVENT = 'map:close:point'
const VALUE = '{"spot_id":"${property.spot_id}","type":"${type}","details_rank":"${details_rank}","x":"${nope}"}'

async function seedProject(page: Page): Promise<{ slug: string; branchId: string; mainEventId: string }> {
  const slug = `e2e-props-${randomUUID().slice(0, 8)}`
  const project = await page.request.post('/api/v1/projects', { data: { name: 'Property suggestions e2e', slug } })
  expect(project.status(), await project.text()).toBe(201)
  const type = await page.request.post(`/api/v1/projects/${slug}/event-types`, {
    data: { name: 'track', display_name: 'Track' },
  })
  expect(type.status(), await type.text()).toBe(201)
  const typeId = (await type.json()).id as string
  const field = await page.request.post(`/api/v1/projects/${slug}/event-types/${typeId}/fields`, {
    data: { name: 'payload', display_name: 'Payload', field_type: 'json' },
  })
  expect(field.status(), await field.text()).toBe(201)
  // spot_id answers to the scan's `property.spot_id` through a binding.
  for (const data of [
    { name: 'spot_id', bindings: ['property.spot_id'] },
    { name: 'type' },
    { name: 'details_rank' },
  ]) {
    const variable = await page.request.post(`/api/v1/projects/${slug}/variables`, { data })
    expect(variable.status(), await variable.text()).toBe(201)
  }
  const event = await page.request.post(`/api/v1/projects/${slug}/events`, {
    data: {
      event_type_id: typeId,
      name: EVENT,
      field_values: [{ field_definition_id: (await field.json()).id, value: VALUE }],
    },
  })
  expect(event.status(), await event.text()).toBe(201)
  const branch = await page.request.post(`/api/v1/projects/${slug}/branches`, { data: { name: 'WND-2' } })
  expect(branch.status(), await branch.text()).toBe(201)
  return { slug, branchId: (await branch.json()).id as string, mainEventId: (await event.json()).id as string }
}

async function branchEventId(page: Page, slug: string, branchId: string): Promise<string> {
  const resp = await page.request.get(`/api/v1/projects/${slug}/events?branch=${branchId}`)
  expect(resp.status(), await resp.text()).toBe(200)
  const items = (await resp.json()).items as { id: string; name: string }[]
  const copy = items.find(e => e.name === EVENT)
  expect(copy, JSON.stringify(items)).toBeTruthy()
  return copy!.id
}

async function propertyCount(page: Page, slug: string, eventId: string, branchId?: string): Promise<number> {
  const resp = await page.request.get(
    `/api/v1/projects/${slug}/events/${eventId}/properties${branchId ? `?branch=${branchId}` : ''}`,
  )
  expect(resp.status(), await resp.text()).toBe(200)
  return ((await resp.json()) as unknown[]).length
}

test('the properties panel adds what the field values use, on the branch', async ({ page, account }) => {
  expect(account.email).toBeTruthy()
  const { slug, branchId, mainEventId } = await seedProject(page)
  const eventId = await branchEventId(page, slug, branchId)
  expect(eventId).not.toBe(mainEventId)

  await page.goto(`/p/${slug}/events/all/${eventId}/edit?branch=${branchId}`)
  await expect(page.getByText('None listed yet · field values use 3', { exact: true })).toBeVisible({ timeout: 60_000 })
  const used = page.getByRole('list', { name: 'Properties the field values use', exact: true })
  for (const name of ['spot_id', 'type', 'details_rank']) {
    await expect(used.getByRole('link', { name, exact: true })).toBeVisible()
  }
  await expect(used).not.toContainText('nope')

  // Add all: optional, since its Required switch is left off.
  await page.getByRole('button', { name: 'Add all (3)', exact: true }).click()
  await expect(used).toHaveCount(0)
  await expect(page.getByText('3 on this event · 0 required', { exact: true })).toBeVisible()
  for (const name of ['spot_id', 'type', 'details_rank']) {
    await expect(page.getByRole('switch', { name: `${name} is required`, exact: true })).not.toBeChecked()
  }
  expect(await propertyCount(page, slug, eventId, branchId)).toBe(3)
  // Written on the branch only: main's event still lists none.
  expect(await propertyCount(page, slug, mainEventId)).toBe(0)

  // Undo one: removed, it is offered again.
  await page.getByRole('button', { name: 'Remove type from this event', exact: true }).click()
  await page.getByRole('alertdialog').getByRole('button', { name: 'Remove', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Add type to this event', exact: true })).toBeVisible()
  await expect(
    page.getByText('2 on this event · 0 required · 1 more used in field values', { exact: true }),
  ).toBeVisible()
  expect(await propertyCount(page, slug, eventId, branchId)).toBe(2)
})
