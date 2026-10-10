import { randomUUID } from 'node:crypto'
import type { Page } from '@playwright/test'

import { expect, test } from './fixtures'

/**
 * Duplicate (tripl-4zzc.7): an analyst writing a run of similar events starts
 * the next one from an existing event instead of retyping its type, values,
 * tags and description. The copy is a new Draft on the same branch, and its
 * name has to change before it can be created.
 */

interface Seeded {
  slug: string
  branchId: string
  eventId: string
}

async function seed(page: Page): Promise<Seeded> {
  const slug = `e2e-duplicate-${randomUUID().slice(0, 8)}`
  const project = await page.request.post('/api/v1/projects', { data: { name: 'Duplicate event e2e', slug } })
  expect(project.status(), await project.text()).toBe(201)
  const type = await page.request.post(`/api/v1/projects/${slug}/event-types`, {
    data: { name: 'track', display_name: 'Track' },
  })
  expect(type.status(), await type.text()).toBe(201)
  const typeId = (await type.json()).id as string
  const field = await page.request.post(`/api/v1/projects/${slug}/event-types/${typeId}/fields`, {
    data: { name: 'screen', display_name: 'Screen', field_type: 'string' },
  })
  expect(field.status(), await field.text()).toBe(201)
  const fieldId = (await field.json()).id as string
  const branch = await page.request.post(`/api/v1/projects/${slug}/branches`, { data: { name: 'WND-2' } })
  expect(branch.status(), await branch.text()).toBe(201)
  const branchId = (await branch.json()).id as string
  // The type's branch copy has its own ids: read them on the branch.
  const branchTypes = await page.request.get(`/api/v1/projects/${slug}/event-types?branch=${branchId}`)
  expect(branchTypes.status(), await branchTypes.text()).toBe(200)
  const branchType = ((await branchTypes.json()) as Array<{ id: string; name: string; field_definitions: Array<{ id: string; name: string }> }>)
    .find(t => t.name === 'track')!
  const branchFieldId = branchType.field_definitions.find(f => f.name === 'screen')?.id ?? fieldId
  const event = await page.request.post(`/api/v1/projects/${slug}/events?branch=${branchId}`, {
    data: {
      event_type_id: branchType.id,
      name: 'checkout_started',
      description: 'Fires when checkout opens',
      status: 'live',
      tags: ['checkout'],
      field_values: [{ field_definition_id: branchFieldId, value: 'cart' }],
    },
  })
  expect(event.status(), await event.text()).toBe(201)
  return { slug, branchId, eventId: (await event.json()).id as string }
}

test('Duplicate starts a new Draft from an existing event, on the same branch', async ({ page, account }) => {
  expect(account.email).toBeTruthy()
  const { slug, branchId, eventId } = await seed(page)

  await page.goto(`/p/${slug}/events/all/${eventId}/edit?branch=${branchId}`)
  await expect(page.getByRole('heading', { name: 'Edit · checkout_started', exact: true })).toBeVisible({ timeout: 60_000 })
  await page.getByRole('button', { name: 'Duplicate', exact: true }).click()

  await expect(page).toHaveURL(new RegExp(`/p/${slug}/events/all/new\\?branch=${branchId}&from=${eventId}$`))
  const assertPrefilled = async () => {
    await expect(page.getByRole('heading', { name: 'New event', exact: true })).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText(/Duplicating/)).toContainText('checkout_started')
    await expect(page.getByLabel(/^Screen/)).toHaveValue('cart')
    await expect(page.getByLabel(/^Description/)).toHaveValue('Fires when checkout opens')
    await expect(page.getByLabel(/^Status/)).toHaveValue('draft')
    await expect(page.getByLabel(/^Name/)).toHaveValue('checkout_started')
    // The source's own name: a byte-identical second event.
    await expect(page.getByRole('button', { name: 'Create event', exact: true })).toBeDisabled()
  }
  await assertPrefilled()

  // A link, not router state: a reload rebuilds the same prefill.
  await page.reload()
  await assertPrefilled()

  await page.getByLabel(/^Name/).fill('checkout_completed')
  await expect(page.getByRole('button', { name: 'Create event', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: 'Create event', exact: true }).click()
  await expect(page.getByText('Added checkout_completed to branch WND-2')).toBeVisible({ timeout: 30_000 })

  const list = await page.request.get(
    `/api/v1/projects/${slug}/events?branch=${branchId}&search=checkout_completed`,
  )
  expect(list.status(), await list.text()).toBe(200)
  const created = ((await list.json()).items as Array<{
    id: string
    name: string
    status: string
    description: string
    tags: Array<{ name: string }>
  }>).find(item => item.name === 'checkout_completed')
  expect(created).toBeTruthy()
  expect(created!.status).toBe('draft')
  expect(created!.description).toBe('Fires when checkout opens')
  expect(created!.tags.map(t => t.name)).toEqual(['checkout'])

  // Undo: the copy is an ordinary branch event, and deleting it leaves the
  // source untouched.
  const removed = await page.request.delete(`/api/v1/projects/${slug}/events/${created!.id}?branch=${branchId}`)
  expect(removed.ok(), await removed.text()).toBe(true)
  const after = await page.request.get(`/api/v1/projects/${slug}/events/${eventId}?branch=${branchId}`)
  expect(after.status()).toBe(200)
  expect((await after.json()).status).toBe('live')
})
