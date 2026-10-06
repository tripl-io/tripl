import { randomUUID } from 'node:crypto'
import type { Page } from '@playwright/test'

import { expect, test } from './fixtures'

/**
 * "As merged" (tripl-4zzc.3): one event as main will hold it after the merge,
 * attributes and properties together, main's later edits included. Opened from
 * an event row, and from a property-only change on a variable row, where the
 * event has no row of its own. Read-only: there is no write to undo.
 */

interface Seeded {
  slug: string
  branchId: string
}

async function seed(page: Page): Promise<Seeded> {
  const slug = `e2e-merged-${randomUUID().slice(0, 8)}`
  const project = await page.request.post('/api/v1/projects', {
    data: { name: 'Merge preview e2e', slug },
  })
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

  const mainIds: Record<string, string> = {}
  for (const [name, title] of [
    ['checkout_started', 'Checkout started'],
    ['checkout_done', 'Checkout done'],
  ] as const) {
    const event = await page.request.post(`/api/v1/projects/${slug}/events`, {
      data: {
        event_type_id: typeId,
        name,
        title,
        field_values: [{ field_definition_id: fieldId, value: 'cart' }],
      },
    })
    expect(event.status(), await event.text()).toBe(201)
    mainIds[name] = (await event.json()).id as string
  }
  const variable = await page.request.post(`/api/v1/projects/${slug}/variables`, {
    data: { name: 'plan', allowed_values: ['free'] },
  })
  expect(variable.status(), await variable.text()).toBe(201)
  const variableId = (await variable.json()).id as string
  for (const name of ['checkout_started', 'checkout_done']) {
    const entry = await page.request.put(
      `/api/v1/projects/${slug}/variables/${variableId}/event-overrides/${mainIds[name]}`,
      { data: { values: ['free'] } },
    )
    expect(entry.status(), await entry.text()).toBe(200)
  }

  const branch = await page.request.post(`/api/v1/projects/${slug}/branches`, {
    data: { name: 'WND-2' },
  })
  expect(branch.status(), await branch.text()).toBe(201)
  const branchId = (await branch.json()).id as string

  const branchEvents = await page.request.get(`/api/v1/projects/${slug}/events?branch=${branchId}`)
  const onBranch = Object.fromEntries(
    ((await branchEvents.json()).items as { id: string; name: string }[]).map((e) => [e.name, e.id]),
  )
  const branchVariables = await page.request.get(
    `/api/v1/projects/${slug}/variables?branch=${branchId}`,
  )
  const branchVariableId = (
    (await branchVariables.json()).items as { id: string; name: string }[]
  ).find((v) => v.name === 'plan')!.id

  // On the branch: a new title on checkout_started, and `pro` documented for
  // both events — checkout_done's only change is that property.
  const retitle = await page.request.patch(
    `/api/v1/projects/${slug}/events/${onBranch.checkout_started}?branch=${branchId}`,
    { data: { title: 'Start checkout' } },
  )
  expect(retitle.status(), await retitle.text()).toBe(200)
  for (const name of ['checkout_started', 'checkout_done']) {
    const entry = await page.request.put(
      `/api/v1/projects/${slug}/variables/${branchVariableId}/event-overrides/${onBranch[name]}?branch=${branchId}`,
      { data: { values: ['free', 'pro'] } },
    )
    expect(entry.status(), await entry.text()).toBe(200)
  }
  // On main, after the cut: a field the branch left alone.
  const mainEdit = await page.request.patch(
    `/api/v1/projects/${slug}/events/${mainIds.checkout_started}`,
    { data: { description: 'Written on main later' } },
  )
  expect(mainEdit.status(), await mainEdit.text()).toBe(200)
  return { slug, branchId }
}

test('an event and a property-only change, each as merged', async ({ page, account }) => {
  expect(account.email).toBeTruthy()
  const { slug, branchId } = await seed(page)

  await page.goto(`/p/${slug}/branches/${branchId}`)
  const openStarted = page.getByRole('button', { name: 'As merged: checkout_started', exact: true })
  await expect(openStarted).toBeVisible({ timeout: 60_000 })
  await openStarted.click()

  const started = page.getByRole('dialog', {
    name: 'Event after merge: checkout_started',
    exact: true,
  })
  await expect(started).toBeVisible()
  const hide = started.getByRole('checkbox', { name: /Hide unchanged/ })
  if ((await hide.count()) > 0 && (await hide.isChecked())) await hide.uncheck()
  const attributes = started.getByRole('region', { name: 'Attributes', exact: true })
  // The title the branch wrote, main's old one struck beside it.
  await expect(attributes.getByText('Start checkout', { exact: true })).toBeVisible()
  await expect(attributes.locator('del', { hasText: 'Checkout started' })).toBeVisible()
  await expect(attributes.getByText('Changed', { exact: true })).toBeVisible()
  // Main's later description, marked as main's.
  await expect(attributes.getByText('Written on main later', { exact: true })).toBeVisible()
  await expect(attributes.getByText('changed on main since this branch', { exact: true })).toBeVisible()
  // The field value nobody touched.
  const fieldValues = started.getByRole('region', { name: 'Field values', exact: true })
  await expect(fieldValues.getByText('cart', { exact: true })).toBeVisible()
  await expect(fieldValues.getByText('Unchanged', { exact: true })).toBeAttached()
  // The property: `pro` added under plan.
  const planValues = started.getByRole('list', { name: 'plan values', exact: true })
  await expect(planValues.getByRole('listitem').filter({ hasText: 'pro' })).toContainText('Added')

  await expect(page).toHaveURL(/[?&]merged=/)
  await page.goBack()
  await expect(started).toBeHidden()
  await expect(page).not.toHaveURL(/[?&]merged/)

  // checkout_done changed only as a property: reached from the variable row.
  await page.locator('button[aria-expanded]', { hasText: /^~\s*plan/ }).first().click()
  await page.getByRole('button', { name: 'As merged: track.checkout_done', exact: true }).click()
  const done = page.getByRole('dialog', { name: 'Event after merge: checkout_done', exact: true })
  await expect(done).toBeVisible()
  const doneValues = done.getByRole('list', { name: 'plan values', exact: true })
  await expect(doneValues.getByRole('listitem').filter({ hasText: 'pro' })).toContainText('Added')
  const doneHide = done.getByRole('checkbox', { name: /Hide unchanged/ })
  if ((await doneHide.count()) > 0 && (await doneHide.isChecked())) await doneHide.uncheck()
  await expect(
    done.getByRole('region', { name: 'Attributes', exact: true }).getByText('Checkout done', {
      exact: true,
    }),
  ).toBeVisible()
  // Opened by type and name, then named by id.
  await expect(page).toHaveURL(/[?&]merged=[0-9a-f-]{36}/)
  await page.goBack()
  await expect(done).toBeHidden()

  // A landed branch offers no projection: main already holds the answer.
  const close = await page.request.post(`/api/v1/projects/${slug}/branches/${branchId}/transition`, {
    data: { action: 'close' },
  })
  expect(close.status(), await close.text()).toBe(200)
  await page.reload()
  await expect(page.getByText('checkout_started').first()).toBeVisible({ timeout: 60_000 })
  await expect(page.getByRole('button', { name: /^As merged/ })).toHaveCount(0)
})
