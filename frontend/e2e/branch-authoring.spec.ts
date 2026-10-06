import { randomUUID } from 'node:crypto'
import type { Page } from '@playwright/test'

import { expect, test } from './fixtures'

/**
 * Authoring on a plan branch, as analysts do it: the branch strip offers a new
 * event from any page, and the events search box takes typing anywhere in its
 * text and keeps focus when emptied — it used to drop characters, throw the
 * caret to the end, and lose focus so the next "c" opened a new event.
 */

async function seedProject(page: Page): Promise<{ slug: string; branchId: string }> {
  const slug = `e2e-branch-${randomUUID().slice(0, 8)}`
  const project = await page.request.post('/api/v1/projects', { data: { name: 'Branch authoring e2e', slug } })
  expect(project.status(), await project.text()).toBe(201)
  const type = await page.request.post(`/api/v1/projects/${slug}/event-types`, {
    data: { name: 'track', display_name: 'Track' },
  })
  expect(type.status(), await type.text()).toBe(201)
  const typeId = (await type.json()).id as string
  for (const name of ['checkout_started', 'signup_completed']) {
    const event = await page.request.post(`/api/v1/projects/${slug}/events`, {
      data: { event_type_id: typeId, name },
    })
    expect(event.status(), await event.text()).toBe(201)
  }
  const branch = await page.request.post(`/api/v1/projects/${slug}/branches`, { data: { name: 'WND-1' } })
  expect(branch.status(), await branch.text()).toBe(201)
  return { slug, branchId: (await branch.json()).id as string }
}

test('the branch strip opens a new event on the branch', async ({ page, account }) => {
  expect(account.email).toBeTruthy()
  const { slug, branchId } = await seedProject(page)

  await page.goto(`/p/${slug}/events?branch=${branchId}`)
  const strip = page.getByRole('region', { name: 'Plan branch' })
  await expect(strip).toContainText('WND-1', { timeout: 60_000 })
  await strip.getByRole('link', { name: 'New event on this branch' }).click()

  await expect(page).toHaveURL(new RegExp(`/p/${slug}/events/all/new\\?branch=${branchId}$`))
  await expect(page.getByRole('button', { name: 'Create event' })).toBeVisible({ timeout: 60_000 })
  await expect(page.getByRole('button', { name: 'Save and add another' })).toBeVisible()
})

test('the events search takes typing in the middle and keeps focus when emptied', async ({ page, account }) => {
  expect(account.email).toBeTruthy()
  const { slug } = await seedProject(page)

  await page.goto(`/p/${slug}/events`)
  const search = page.getByRole('searchbox', { name: 'Search events' })
  await expect(search).toBeVisible({ timeout: 60_000 })
  await search.click()
  await search.pressSequentially('chckout', { delay: 20 })
  await expect(search).toHaveValue('chckout')

  // Back to after "ch" and type the missing letter there.
  for (let i = 0; i < 'ckout'.length; i++) await search.press('ArrowLeft')
  await search.pressSequentially('e', { delay: 20 })
  await expect(search).toHaveValue('checkout')
  await expect(page.getByText('checkout_started').first()).toBeVisible()

  // A search that matches nothing, then emptied: the toolbar stays.
  await search.fill('zzz-nothing')
  await expect(page).toHaveURL(/q=zzz-nothing/)
  await search.press('ControlOrMeta+a')
  await search.press('Backspace')
  await expect(page.getByText('signup_completed').first()).toBeVisible()
  await expect(search).toBeFocused()
  await page.keyboard.press('c')
  await expect(search).toHaveValue('c')
  await expect(page).not.toHaveURL(/\/new/)
})
