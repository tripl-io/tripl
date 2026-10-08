import { randomUUID } from 'node:crypto'

import { expect, generateDemo, test } from './fixtures'

/**
 * The smoke suite: the paths a new team walks on day one, through the built
 * frontend, the real API and the real worker, with nothing mocked.
 *
 * Every other spec signs up through the API (`fixtures.account`) and tests one
 * feature. This one is the cross-layer tie the rest assume: the sign-up and
 * sign-in FORMS, creating a project by hand, a scan RUN by the worker against
 * the synthetic warehouse, and an anomaly in the alert inbox. A data source
 * cannot be connected credential-free here except through the demo, so the scan
 * and the inbox are the demo's.
 */

test('sign up and sign in through the forms, then create a project', async ({ page }) => {
  const email = `e2e-smoke-${randomUUID().slice(0, 8)}@example.com`
  const password = `E2e-${randomUUID()}`

  await page.goto('/auth')
  await page.getByRole('button', { name: 'Create account' }).click()
  await page.getByLabel('Name').fill('Smoke Test')
  await page.getByLabel('Email').fill(email)
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Create your account' }).click()
  // Signed in: the workspace replaces the form.
  await expect(page.getByRole('button', { name: /^Account menu/ }).first()).toBeVisible({ timeout: 60_000 })

  // Sign out and back in with the same credentials.
  await page.getByRole('button', { name: /^Account menu/ }).first().click()
  await page.getByRole('menuitem', { name: 'Sign out' }).click()
  await expect(page).toHaveURL(/\/auth/, { timeout: 30_000 })
  await page.getByLabel('Email').fill(email)
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  await expect(page.getByRole('button', { name: /^Account menu/ }).first()).toBeVisible({ timeout: 60_000 })

  // A project by hand, from the workspace's New project dialog.
  await page.goto('/workspace?new=1')
  const dialog = page.getByRole('dialog', { name: 'New project' })
  await expect(dialog).toBeVisible({ timeout: 60_000 })
  const name = `Smoke ${randomUUID().slice(0, 6)}`
  await dialog.getByLabel('Project name').fill(name)
  await dialog.getByRole('button', { name: 'Create', exact: true }).click()
  await page.waitForURL(/\/p\/[a-z0-9-]+\/overview$/, { timeout: 60_000 })
  const slug = /\/p\/([a-z0-9-]+)\//.exec(page.url())?.[1]
  const created = await page.request.get(`/api/v1/projects/${slug}`)
  expect(created.status()).toBe(200)
  expect(((await created.json()) as { name: string }).name).toBe(name)
})

test('a scan runs on the worker and the anomaly reaches the alert inbox', async ({ page, account }) => {
  void account
  const slug = await generateDemo(page)

  // Run the demo's scan the way a user does, from the Scans page.
  const scans = (await (await page.request.get(`/api/v1/projects/${slug}/scans`)).json()) as {
    id: string
    name: string
  }[]
  const scan = scans[0]
  expect(scan, 'the demo seeds a scan').toBeDefined()
  if (!scan) return
  const jobsUrl = `/api/v1/projects/${slug}/scans/${scan.id}/jobs`
  const before = (await (await page.request.get(jobsUrl)).json()) as { id: string }[]

  await page.goto(`/p/${slug}/scans`)
  // A route's first visit compiles it on the dev server: give it time.
  await page.getByRole('button', { name: `Run ${scan.name} now` }).click({ timeout: 60_000 })

  // The worker picks the job up and finishes it against the synthetic warehouse.
  await expect
    .poll(
      async () => {
        const jobs = (await (await page.request.get(jobsUrl)).json()) as { id: string; status: string }[]
        const fresh = jobs.filter((job) => !before.some((old) => old.id === job.id))
        return fresh[0]?.status ?? 'none'
      },
      { timeout: 180_000, intervals: [2_000] },
    )
    .toBe('completed')

  // The inbox holds the firing incident, and its card offers triage.
  const inbox = (await (await page.request.get(`/api/v1/projects/${slug}/alert-inbox`)).json()) as {
    total: number
  }
  expect(inbox.total).toBeGreaterThan(0)
  await page.goto(`/p/${slug}/alerting`)
  await expect(page.getByRole('button', { name: /^Acknowledge / }).first()).toBeVisible({
    timeout: 60_000,
  })
})
