import { randomUUID } from 'node:crypto'
import { request, type FullConfig } from '@playwright/test'

/**
 * An organization owner for the tests that need one (organization settings).
 *
 * On a fresh instance the first account to sign up becomes its owner, so this
 * signs one up before any test runs — in CI the stack is brand new. Against a
 * stack that already has an owner, pass one in with E2E_OWNER_EMAIL and
 * E2E_OWNER_PASSWORD instead. The credentials reach the tests through the
 * environment, which Playwright hands from global setup to every worker.
 */
export default async function globalSetup(config: FullConfig): Promise<void> {
  if (process.env.E2E_OWNER_EMAIL && process.env.E2E_OWNER_PASSWORD) return
  const baseURL = config.projects[0]?.use.baseURL
  const context = await request.newContext({ baseURL })
  const email = `e2e-owner-${randomUUID().slice(0, 8)}@example.com`
  const password = `E2e-${randomUUID()}`
  const resp = await context.post('/api/v1/auth/register', {
    data: { email, password, name: 'E2E Owner' },
  })
  if (resp.status() !== 201) {
    throw new Error(`could not sign up the e2e owner: ${resp.status()} ${await resp.text()}`)
  }
  const me = (await resp.json()) as { role?: string | null }
  await context.dispose()
  if (me.role === 'owner') {
    process.env.E2E_OWNER_EMAIL = email
    process.env.E2E_OWNER_PASSWORD = password
  } else if (process.env.CI) {
    // CI brings up a fresh stack, so this account must be its first.
    throw new Error(`the e2e owner signed up as ${me.role ?? 'no role'}, not owner: is the stack fresh?`)
  }
}
