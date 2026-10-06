import { randomUUID } from 'node:crypto'
import type { Page } from '@playwright/test'

import { expect, test } from './fixtures'

/**
 * Splitting a task's branch: tick one row, move it to a new branch, and what
 * it needs (its new field, its new event type) goes along. Moved back, the
 * first branch has it all again; copying a change the other branch made
 * differently is refused with the field named.
 */

const API = '/api/v1/projects'

interface Seeded {
  slug: string
  branchId: string
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- API bodies, read field by field
async function ok(response: Awaited<ReturnType<Page['request']['get']>>, status = 200): Promise<any> {
  expect(response.status(), await response.text()).toBe(status)
  return response.json()
}

async function variableId(page: Page, slug: string, branchId: string, name: string) {
  const body = await ok(await page.request.get(`${API}/${slug}/variables?branch=${branchId}`))
  const items = (Array.isArray(body) ? body : body.items) as Array<{ id: string; name: string }>
  const found = items.find((item) => item.name === name)
  expect(found).toBeTruthy()
  return found!.id
}

async function seed(page: Page): Promise<Seeded> {
  const slug = `e2e-transfer-${randomUUID().slice(0, 8)}`
  await ok(await page.request.post(API, { data: { name: 'Branch transfer e2e', slug } }), 201)
  await ok(
    await page.request.post(`${API}/${slug}/variables`, {
      data: { name: 'currency', description: '' },
    }),
    201,
  )
  const branch = await ok(
    await page.request.post(`${API}/${slug}/branches`, { data: { name: 'TASK-1' } }),
    201,
  )
  const branchId = branch.id as string

  // On TASK-1: a new event type with a new field, an event valued for it,
  // and an edit of an existing variable.
  const type = await ok(
    await page.request.post(`${API}/${slug}/event-types?branch=${branchId}`, {
      data: { name: 'screen', display_name: 'Screen' },
    }),
    201,
  )
  const field = await ok(
    await page.request.post(`${API}/${slug}/event-types/${type.id}/fields?branch=${branchId}`, {
      data: { name: 'title', display_name: 'Title', field_type: 'string' },
    }),
    201,
  )
  await ok(
    await page.request.post(`${API}/${slug}/events?branch=${branchId}`, {
      data: {
        event_type_id: type.id,
        name: 'open',
        field_values: [{ field_definition_id: field.id, value: 'Home' }],
      },
    }),
    201,
  )
  const currency = await variableId(page, slug, branchId, 'currency')
  await ok(
    await page.request.patch(`${API}/${slug}/variables/${currency}?branch=${branchId}`, {
      data: { description: 'ISO 4217 code' },
    }),
  )
  return { slug, branchId }
}

async function diffNames(page: Page, slug: string, branchId: string): Promise<string[]> {
  const body = await ok(await page.request.get(`${API}/${slug}/branches/${branchId}/diff`))
  return (body.entries as Array<{ name: string }>).map((entry) => entry.name).sort()
}

async function branchIdByName(page: Page, slug: string, name: string): Promise<string> {
  const body = await ok(await page.request.get(`${API}/${slug}/branches`))
  const found = (body.items as Array<{ id: string; name: string }>).find((b) => b.name === name)
  expect(found).toBeTruthy()
  return found!.id
}

async function moveTo(page: Page, target: string) {
  await page.getByRole('button', { name: 'Move to branch…', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('radio', { name: target, exact: true }).check()
  const confirm = dialog.getByRole('button', { name: 'Move changes', exact: true })
  await expect(confirm).toBeEnabled({ timeout: 60_000 })
  await confirm.click()
  await expect(dialog).toBeHidden({ timeout: 60_000 })
}

test('move a row and what it needs to a new branch, and move it back', async ({
  page,
  account,
}) => {
  expect(account.email).toBeTruthy()
  const { slug, branchId } = await seed(page)

  await page.goto(`/p/${slug}/branches/${branchId}`)
  const pick = page.getByRole('checkbox', { name: 'Select open', exact: true })
  await expect(pick).toBeVisible({ timeout: 60_000 })
  await pick.click()
  await expect(page.getByText('1 selected', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: 'Move to branch…', exact: true }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByRole('radio', { name: 'New branch…', exact: true }).check()
  await dialog.getByLabel('Branch name', { exact: true }).fill('TASK-2')
  const carried = dialog.getByRole('region', { name: 'Also carried', exact: true })
  await expect(carried).toContainText('Field title (needed by open)', { timeout: 60_000 })
  await expect(carried).toContainText('Event type screen (needed by open)')
  const confirm = dialog.getByRole('button', { name: 'Move changes', exact: true })
  await expect(confirm).toBeEnabled()
  await confirm.click()
  await expect(dialog).toBeHidden({ timeout: 60_000 })

  // TASK-1 keeps only the variable edit; TASK-2 holds the three additions.
  const task2 = await branchIdByName(page, slug, 'TASK-2')
  await expect.poll(() => diffNames(page, slug, branchId)).toEqual(['currency'])
  expect(await diffNames(page, slug, task2)).toEqual(['open', 'screen', 'title'])
  await expect(page.getByRole('checkbox', { name: 'Select open', exact: true })).toHaveCount(0)

  // Undo: move the three back.
  await page.goto(`/p/${slug}/branches/${task2}`)
  for (const name of ['open', 'title', 'screen']) {
    const box = page.getByRole('checkbox', { name: `Select ${name}`, exact: true })
    await expect(box).toBeVisible({ timeout: 60_000 })
    await box.click()
  }
  await moveTo(page, 'TASK-1')
  await expect.poll(() => diffNames(page, slug, task2)).toEqual([])
  expect(await diffNames(page, slug, branchId)).toEqual(['currency', 'open', 'screen', 'title'])

  // A branch that edited the same field: the copy is refused, field named.
  const other = await ok(
    await page.request.post(`${API}/${slug}/branches`, { data: { name: 'TASK-3' } }),
    201,
  )
  const theirs = await variableId(page, slug, other.id, 'currency')
  await ok(
    await page.request.patch(`${API}/${slug}/variables/${theirs}?branch=${other.id}`, {
      data: { description: 'Three-letter code' },
    }),
  )
  await page.goto(`/p/${slug}/branches/${branchId}`)
  const currency = page.getByRole('checkbox', { name: 'Select currency', exact: true })
  await expect(currency).toBeVisible({ timeout: 60_000 })
  await currency.click()
  await page.getByRole('button', { name: 'Copy to branch…', exact: true }).click()
  const copyDialog = page.getByRole('dialog')
  await copyDialog.getByRole('radio', { name: 'TASK-3', exact: true }).check()
  const conflicts = copyDialog.getByTestId('transfer-conflicts')
  await expect(conflicts).toContainText('currency', { timeout: 60_000 })
  await expect(conflicts).toContainText('description')
  await expect(copyDialog.getByRole('button', { name: 'Copy changes', exact: true })).toBeDisabled()
})
