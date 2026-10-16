import { demoTest, expect } from './fixtures'

/**
 * An event's page offers the query that selects its rows from the warehouse,
 * built from the scan that reads it. The demo's scan is a real config over the
 * demo warehouse, so the query is the derived one, not a mock.
 */
demoTest('an event page shows and copies the warehouse SQL', async ({ page, context, demo: slug }) => {
  await context.grantPermissions(['clipboard-write'])

  const list = await page.request.get(`/api/v1/projects/${slug}/events?limit=50`)
  expect(list.status(), await list.text()).toBe(200)
  // Every demo event falls under a demo scan, so any of them has a query.
  const items = (await list.json()).items as { id: string }[]
  expect(items.length).toBeGreaterThan(0)

  await page.goto(`/p/${slug}/monitoring/event/${items[0]!.id}`)
  const panel = page.getByTestId('event-warehouse-sql')
  await expect(panel).toBeVisible({ timeout: 60_000 })
  await panel.getByText('Warehouse SQL', { exact: true }).click()

  await expect(panel.getByText(/^From the scan /)).toBeVisible({ timeout: 30_000 })
  // The scan's own query, wrapped and limited (the editor loads lazily).
  await expect(panel).toContainText('AS scan', { timeout: 30_000 })
  await expect(panel).toContainText('LIMIT 100')
  await panel.getByRole('button', { name: 'Copy SQL' }).click()
  await expect(page.getByText('SQL copied')).toBeVisible()
})
