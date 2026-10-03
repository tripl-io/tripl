import { randomUUID } from 'node:crypto'
import { test as base, expect } from '@playwright/test'

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

export { expect }
