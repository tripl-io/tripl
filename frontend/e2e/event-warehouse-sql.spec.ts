import { deleteDemo, expect, generateDemo, test } from './fixtures'

/**
 * An event's page offers the query that selects its rows from the warehouse,
 * built from the scan that reads it. The demo's events come from a real scan
 * (over the synthetic warehouse), so the query is the derived one, not a mock.
 */
test('an event page shows and copies the warehouse SQL', async ({ page, context, account }) => {
  void account
  await context.grantPermissions(['clipboard-write'])
  const slug = await generateDemo(page)

  const list = await page.request.get(`/api/v1/projects/${slug}/events?limit=50`)
  expect(list.status(), await list.text()).toBe(200)
  const items = (await list.json()).items as { id: string; source_name: string | null }[]
  const scanned = items.find(item => item.source_name)
  expect(scanned, 'the demo has a scanned event').toBeTruthy()

  await page.goto(`/p/${slug}/monitoring/event/${scanned!.id}`)
  const panel = page.getByTestId('event-warehouse-sql')
  await expect(panel).toBeVisible({ timeout: 60_000 })
  await panel.getByText('Warehouse SQL', { exact: true }).click()

  await expect(panel.getByText(/^From the scan /)).toBeVisible({ timeout: 30_000 })
  // The scan's own query, wrapped and limited (the editor loads lazily).
  await expect(panel).toContainText('AS scan', { timeout: 30_000 })
  await expect(panel).toContainText('LIMIT 100')
  await panel.getByRole('button', { name: 'Copy SQL' }).click()
  await expect(page.getByText('SQL copied')).toBeVisible()

  await deleteDemo(page)
})
