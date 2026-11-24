import { demoTest, expect, waitForDemoHistory } from './fixtures'

demoTest('an incident captures a reproducible assertion on its note draft', async ({ page, demo }) => {
  await waitForDemoHistory(page, demo)
  let savedNote = 'Initial investigation'
  // A deterministic event-scoped incident while preserving the demo's real page and permissions.
  await page.route(url => url.pathname.endsWith(`/projects/${demo}/alert-inbox`), async route => {
    const response = await route.fetch()
    const body = await response.json()
    expect(body.items.length).toBeGreaterThan(0)
    const group = { ...body.items[0], scope_type: 'event', scope_types: ['event'],
      scope_names: ['purchase'], note: savedNote }
    await route.fulfill({ response, json: { ...body, items: [group], total: 1, next_cursor: null } })
  })
  await page.route(url => url.pathname.includes(`/projects/${demo}/alert-inbox/`) && url.pathname.endsWith('/actions'), async route => {
    expect(route.request().method()).toBe('POST')
    const body = route.request().postDataJSON()
    expect(body.action).toBe('note')
    savedNote = body.note
    const response = await page.request.get(route.request().url().replace(/\/actions$/, ''))
    expect(response.ok()).toBe(true)
    await route.fulfill({ json: { group: { ...await response.json(), note: savedNote }, action: 'note' } })
  })
  await page.goto(`/p/${demo}/alerting?section=inbox`)
  await page.getByText('Reproduce tracking regression', { exact: true }).click({ timeout: 60_000 })
  await expect(page.getByLabel('Tracked event')).toHaveValue('purchase')
  await page.getByLabel('healthy artifact page').fill('https://ci.example/artifacts/healthy')
  await page.getByLabel('suspect artifact page').fill('https://ci.example/artifacts/suspect')
  await page.getByLabel('CLI report JSON').fill(JSON.stringify({
    version: 1, event: 'purchase', journey: 'checkout', fixture: 'order1', verdict: 'reproduced',
    healthy: { build: 'build1', count: 1 }, suspect: { build: 'build2', count: 0 }, candidate: null,
  }))
  await page.getByRole('button', { name: 'Add evidence to note draft' }).click()
  const draft = page.getByPlaceholder('Replace the note…')
  await expect(draft).toHaveValue(/Initial investigation[\s\S]*Reproduction: reproduced/)
  await expect(draft).toHaveValue(/tripl reproduce/)
  await page.getByRole('button', { name: 'Save note' }).click()
  await expect.poll(() => savedNote).toContain('suspect.json: https://ci.example/artifacts/suspect')
  await page.reload()
  await expect(page.locator('p').filter({ hasText: /Initial investigation.*Reproduction: reproduced/s })).toBeVisible()
})
