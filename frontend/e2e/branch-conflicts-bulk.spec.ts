import { randomUUID } from 'node:crypto'
import type { Page } from '@playwright/test'

import { expect, test } from './fixtures'

/**
 * An event authored on a branch while a scan made its twin on main: one click
 * settles every field ("Keep whichever is filled in"), in one request, and the
 * catalog position is never asked about — the update takes main's.
 */

interface Seeded {
  slug: string
  branchId: string
  branchEventId: string
  mainOrder: number
}

async function seed(page: Page): Promise<Seeded> {
  const slug = `e2e-bulk-${randomUUID().slice(0, 8)}`
  const project = await page.request.post('/api/v1/projects', {
    data: { name: 'Bulk conflicts e2e', slug },
  })
  expect(project.status(), await project.text()).toBe(201)
  const type = await page.request.post(`/api/v1/projects/${slug}/event-types`, {
    data: { name: 'pv', display_name: 'Page view' },
  })
  expect(type.status(), await type.text()).toBe(201)
  const typeId = (await type.json()).id as string
  const opened = await page.request.post(`/api/v1/projects/${slug}/events`, {
    data: { event_type_id: typeId, name: 'opened' },
  })
  expect(opened.status(), await opened.text()).toBe(201)

  const branch = await page.request.post(`/api/v1/projects/${slug}/branches`, {
    data: { name: 'WND-bulk' },
  })
  expect(branch.status(), await branch.text()).toBe(201)
  const branchId = (await branch.json()).id as string

  // Authored on the branch: a title and a tag.
  const authored = await page.request.post(`/api/v1/projects/${slug}/events?branch=${branchId}`, {
    data: {
      event_type_id: await branchTypeId(page, slug, branchId),
      name: 'starting_place',
      title: 'Starting place',
      tags: ['onboarding'],
    },
  })
  expect(authored.status(), await authored.text()).toBe(201)

  // Its twin on main, as a scan would leave it: a description and a later
  // catalog position (one more event ahead of it).
  const filler = await page.request.post(`/api/v1/projects/${slug}/events`, {
    data: { event_type_id: typeId, name: 'filler' },
  })
  expect(filler.status(), await filler.text()).toBe(201)
  const twin = await page.request.post(`/api/v1/projects/${slug}/events`, {
    data: {
      event_type_id: typeId,
      name: 'starting_place',
      description: 'Seen in the onboarding flow',
    },
  })
  expect(twin.status(), await twin.text()).toBe(201)
  const mainOrder = (await twin.json()).order as number
  const branchEvent = await authored.json()
  expect(branchEvent.order).not.toBe(mainOrder)
  return { slug, branchId, branchEventId: branchEvent.id as string, mainOrder }
}

async function branchTypeId(page: Page, slug: string, branchId: string): Promise<string> {
  const types = await page.request.get(`/api/v1/projects/${slug}/event-types?branch=${branchId}`)
  expect(types.status(), await types.text()).toBe(200)
  const items = (await types.json()) as Array<{ id: string; name: string }>
  const pv = items.find((item) => item.name === 'pv')
  expect(pv).toBeTruthy()
  return pv!.id
}

test('one click settles a scanned twin, and the update keeps main’s position', async ({
  page,
  account,
}) => {
  expect(account.email).toBeTruthy()
  const { slug, branchId, branchEventId, mainOrder } = await seed(page)

  await page.goto(`/p/${slug}/branches/${branchId}`)
  const actions = page.getByRole('group', { name: 'All fields of starting_place', exact: true })
  await expect(actions).toBeVisible({ timeout: 60_000 })
  const card = page.locator('.rounded-card').filter({ has: actions })
  await expect(card).toContainText('Added on both sides')
  // Position is main's without asking: no row for it.
  await expect(card.getByText('order', { exact: true })).toHaveCount(0)
  await expect(page.getByText('3 unresolved', { exact: true })).toBeVisible()

  // Take main for everything first, then change the mind: each is one request.
  const batches: string[] = []
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().endsWith('/resolutions/batch')) {
      batches.push(request.url())
    }
  })
  const first = page.waitForResponse((r) => r.url().endsWith('/resolutions/batch'))
  await actions.getByRole('button', { name: 'Take main for all', exact: true }).click()
  expect((await first).status()).toBe(201)
  await expect(page.getByText('0 unresolved', { exact: true })).toBeVisible()
  await expect(card.getByText("Resolved: main's value", { exact: true })).toHaveCount(3)

  const second = page.waitForResponse((r) => r.url().endsWith('/resolutions/batch'))
  await actions.getByRole('button', { name: 'Keep whichever is filled in', exact: true }).click()
  expect((await second).status()).toBe(201)
  // Only the description is main's: the branch left it empty.
  await expect(card.getByText("Resolved: main's value", { exact: true })).toHaveCount(1)
  await expect(card.getByText("Resolved: this branch's value", { exact: true })).toHaveCount(2)
  await expect(page.getByText('0 unresolved', { exact: true })).toBeVisible()
  expect(batches).toHaveLength(2)

  await page.getByRole('button', { name: 'Update from main', exact: true }).first().click()
  const dialog = page.getByRole('dialog')
  const update = dialog.getByRole('button', { name: 'Update branch', exact: true })
  await expect(update).toBeEnabled({ timeout: 60_000 })
  await update.click()
  await expect(dialog).toBeHidden({ timeout: 60_000 })

  const event = await page.request.get(
    `/api/v1/projects/${slug}/events/${branchEventId}?branch=${branchId}`,
  )
  expect(event.status(), await event.text()).toBe(200)
  const body = await event.json()
  expect(body.order).toBe(mainOrder)
  expect(body.title).toBe('Starting place')
  expect(body.description).toBe('Seen in the onboarding flow')
})
