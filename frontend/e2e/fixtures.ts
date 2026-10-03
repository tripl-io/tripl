import { randomUUID } from 'node:crypto'
import { test as base, expect, type Page } from '@playwright/test'

export interface Account {
  email: string
  password: string
  name: string
}

/**
 * Every test signs up an account of its own, so tests share nothing but the
 * stack. Sign-up goes through the API (the sign-in page has tests of its own);
 * the session cookie lands in the page's browser context. On a fresh
 * self-hosted instance the first account becomes its owner; later ones join
 * the default organization as members, who may still generate a demo.
 */
export const test = base.extend<{ account: Account }>({
  account: async ({ page }, provide) => {
    const account: Account = {
      email: `e2e-${randomUUID().slice(0, 8)}@example.com`,
      password: `E2e-${randomUUID()}`,
      name: 'E2E Visitor',
    }
    const resp = await page.request.post('/api/v1/auth/register', { data: account })
    expect(resp.status(), await resp.text()).toBe(201)
    await provide(account)
  },
})

/**
 * Generate the demo from the empty workspace and wait for its overview.
 * Returns the demo project's slug. Generation runs the real worker against the
 * synthetic warehouse, so it takes a while.
 */
export async function generateDemo(page: Page): Promise<string> {
  await page.goto('/')
  await page.getByRole('button', { name: 'Generate demo project' }).click()
  await expect(page.getByRole('heading', { name: 'Generating demo workspace' })).toBeVisible()
  await page.waitForURL(/\/p\/demo-[a-z0-9-]+\/overview$/, { timeout: 150_000 })
  const slug = /\/p\/(demo-[a-z0-9-]+)\//.exec(page.url())?.[1]
  expect(slug, page.url()).toBeTruthy()
  return slug!
}

/** Delete the demo from its banner; the workspace offers a new one again. */
export async function deleteDemo(page: Page): Promise<void> {
  const banner = page.locator('[data-demo-banner]')
  await banner.getByRole('button', { name: /^Manage demo/ }).click()
  await page.getByRole('menuitem', { name: 'Delete…' }).click()
  await page.getByRole('alertdialog').getByRole('button', { name: 'Delete demo' }).click()
  await expect(page.getByRole('button', { name: 'Generate demo project' })).toBeVisible()
}

export { expect }
