import { randomUUID } from 'node:crypto'
import { test as base, expect, type BrowserContext, type Page } from '@playwright/test'

export interface Account {
  email: string
  password: string
  name: string
}

type SessionState = Awaited<ReturnType<BrowserContext['storageState']>>

/** The demo a worker builds once, and the signed-in session of its owner. */
export interface SharedDemo {
  slug: string
  session: SessionState
}

/**
 * How long a demo build may take before a spec gives up on it: the page's own
 * five-minute wait (DEMO_PROVISION_TIMEOUT_MS) plus the navigation after it.
 * The worker writes the plan, then scans weeks of history out of the demo
 * warehouse, so a cold CI stack takes minutes, not seconds.
 */
export const DEMO_BUILD_TIMEOUT_MS = 320_000

/**
 * How long a demo's history fill may take before a spec gives up on it: about
 * five minutes on a CI runner on its own, longer while another demo's fill
 * takes turns with it on the worker.
 */
export const DEMO_HISTORY_TIMEOUT_MS = 480_000

/** Sign up a fresh account through the API; the session lands in the page's context. */
async function signUp(page: Page, name: string): Promise<Account> {
  const account: Account = {
    email: `e2e-${randomUUID().slice(0, 8)}@example.com`,
    password: `E2e-${randomUUID()}`,
    name,
  }
  const resp = await page.request.post('/api/v1/auth/register', { data: account })
  expect(resp.status(), await resp.text()).toBe(201)
  return account
}

/**
 * Delete the demos the signed-in account generated. A demo left behind goes on
 * filling its history on the worker, which the next test's build waits for.
 */
async function deleteOwnDemos(page: Page): Promise<void> {
  const me = await page.request.get('/api/v1/auth/me')
  if (!me.ok()) return
  const { id } = (await me.json()) as { id: string }
  const projects = (await (await page.request.get('/api/v1/projects')).json()) as {
    slug: string
    is_demo?: boolean
    created_by_user_id?: string | null
  }[]
  for (const project of projects) {
    if (project.is_demo && project.created_by_user_id === id) {
      await page.request.delete(`/api/v1/projects/demo/${project.slug}`)
    }
  }
}

/**
 * Every test signs up an account of its own, so tests share nothing but the
 * stack — except the tests on the worker's shared demo (`demoTest`). Sign-up
 * goes through the API (the sign-in page has tests of its own); the session
 * cookie lands in the page's browser context. On a fresh self-hosted instance
 * the first account becomes its owner; later ones join the default
 * organization as members, who may still generate a demo.
 */
export const test = base.extend<{ account: Account }, { sharedDemo: SharedDemo }>({
  account: async ({ page }, provide) => {
    await provide(await signUp(page, 'E2E Visitor'))
    await deleteOwnDemos(page)
  },
  // Built on first use and kept for the worker's life. Playwright replaces a
  // worker after any failed test, so a demo a failure left half-changed is
  // never handed to the next test.
  sharedDemo: [
    async ({ browser }, provide, workerInfo) => {
      const context = await browser.newContext({ baseURL: workerInfo.project.use.baseURL })
      const page = await context.newPage()
      await signUp(page, 'E2E Shared Demo')
      const slug = await buildDemo(page)
      await provide({ slug, session: await context.storageState() })
      // A local stack outlives the run: take the demo away again.
      await page.request.delete(`/api/v1/projects/demo/${slug}`)
      await context.close()
    },
    { scope: 'worker', timeout: DEMO_BUILD_TIMEOUT_MS + 60_000 },
  ],
})

/**
 * A test on the worker's shared demo, signed in as the account that owns it;
 * `demo` is the demo's slug. A build takes minutes, so specs that only read
 * the demo, or put back everything they write before they pass, share one.
 * A spec that leaves a change behind, or tests generation or deletion itself,
 * builds its own with `generateDemo`.
 */
export const demoTest = test.extend<{ demo: string }>({
  storageState: async ({ sharedDemo }, provide) => {
    await provide(sharedDemo.session)
  },
  demo: async ({ sharedDemo }, provide) => {
    await provide(sharedDemo.slug)
  },
})

/**
 * Sign the page in as the organization owner global setup made (or was given
 * through E2E_OWNER_EMAIL / E2E_OWNER_PASSWORD), for organization settings.
 * Skips the test when there is none: a local stack whose owner is someone else.
 */
export async function signInAsOwner(page: Page): Promise<void> {
  const email = process.env.E2E_OWNER_EMAIL
  const password = process.env.E2E_OWNER_PASSWORD
  test.skip(!email || !password, 'no organization owner: set E2E_OWNER_EMAIL and E2E_OWNER_PASSWORD')
  const resp = await page.request.post('/api/v1/auth/login', { data: { email, password } })
  expect(resp.status(), await resp.text()).toBe(200)
}

/**
 * Give the running test — or the hook, in a beforeAll — room for one demo
 * build on top of whatever else it does.
 */
export function allowDemoBuild(): void {
  test.setTimeout(test.info().timeout + DEMO_BUILD_TIMEOUT_MS)
}

/** Generate the demo from the empty workspace and wait for its overview. */
async function buildDemo(page: Page): Promise<string> {
  await page.goto('/')
  await page.getByRole('button', { name: 'Generate demo project' }).click()
  await expect(page.getByRole('heading', { name: 'Generating demo project' })).toBeVisible()
  await page.waitForURL(/\/p\/demo-[a-z0-9-]+\/overview$/, { timeout: DEMO_BUILD_TIMEOUT_MS })
  const slug = /\/p\/(demo-[a-z0-9-]+)\//.exec(page.url())?.[1]
  expect(slug, page.url()).toBeTruthy()
  return slug!
}

/**
 * Generate a demo of the test's own from the empty workspace and wait for its
 * overview. Returns the demo project's slug. Generation runs the real worker
 * against the demo warehouse, so it takes a while; the caller's timeout is
 * extended for it.
 */
export async function generateDemo(page: Page): Promise<string> {
  allowDemoBuild()
  return buildDemo(page)
}

/**
 * Wait for a demo's history fill (it opens on its last two days and fills the
 * rest on the worker). Only a spec that needs the whole story waits — the
 * weekly promo verdicts, the alerts the full baseline finds.
 */
export async function waitForDemoHistory(page: Page, slug: string): Promise<void> {
  test.setTimeout(test.info().timeout + DEMO_HISTORY_TIMEOUT_MS)
  await expect
    .poll(
      async () => {
        const response = await page.request.get(`/api/v1/projects/${slug}`)
        return ((await response.json()) as { demo_history_status?: string | null }).demo_history_status
      },
      { timeout: DEMO_HISTORY_TIMEOUT_MS, intervals: [2_000, 5_000] },
    )
    .not.toBe('filling')
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
