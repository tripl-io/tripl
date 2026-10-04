import { expect, test } from './fixtures'

/**
 * Demo generation is queued on the worker: the create answers 202 with a
 * hidden `seeding` shell at once, the browser re-reads the shell until the
 * worker marks it ready, and a cancel sent while it seeds discards it. The
 * stack runs the real worker, so this is the queue, not a mock of it.
 */

const DEMO_SLUG = /^demo-[a-z0-9-]+$/

test('the create returns a seeding shell and the page waits for the worker', async ({ page, account }) => {
  void account
  await page.goto('/')

  const created = page.waitForResponse(
    (resp) => resp.request().method() === 'POST' && resp.url().endsWith('/projects/demo'),
  )
  await page.getByRole('button', { name: 'Generate demo project' }).click()
  const createResp = await created
  expect(createResp.status()).toBe(202)
  const shell = (await createResp.json()) as { slug: string; generation_status: string }
  expect(shell.slug).toMatch(DEMO_SLUG)
  // The request only reserves the workspace; the seed has not run yet.
  expect(shell.generation_status).toBe('seeding')

  // The dialog narrates while the page re-reads the shell, and lands on the
  // ready demo's overview.
  await expect(page.getByRole('heading', { name: 'Generating demo workspace' })).toBeVisible()
  await page.waitForURL(new RegExp(`/p/${shell.slug}/overview$`), { timeout: 150_000 })
  await expect(page.locator('[data-demo-banner]').getByText('Demo workspace', { exact: true })).toBeVisible()

  const ready = await page.request.get(`/api/v1/projects/${shell.slug}`)
  expect(ready.status()).toBe(200)
  expect(((await ready.json()) as { generation_status: string }).generation_status).toBe('ready')
})

test('cancelling while the worker seeds leaves no demo behind', async ({ page, account }) => {
  void account
  await page.goto('/')

  const created = page.waitForResponse(
    (resp) => resp.request().method() === 'POST' && resp.url().endsWith('/projects/demo'),
  )
  await page.getByRole('button', { name: 'Generate demo project' }).click()
  const shell = (await (await created).json()) as { slug: string }

  // The seed takes seconds of CPU on the worker; the cancel lands well inside it.
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel' }).click()
  await expect(page.getByRole('heading', { name: 'Demo generation cancelled' })).toBeVisible({
    timeout: 30_000,
  })

  // The worker finishes its seed, sees the cancel and discards the shell.
  await expect
    .poll(async () => (await page.request.get(`/api/v1/projects/${shell.slug}`)).status(), {
      timeout: 120_000,
    })
    .toBe(404)
  const list = (await (await page.request.get('/api/v1/projects')).json()) as { slug: string }[]
  expect(list.map((project) => project.slug)).not.toContain(shell.slug)
})
