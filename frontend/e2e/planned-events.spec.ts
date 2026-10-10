import { deleteDemo, expect, generateDemo, test } from './fixtures'

interface PlannedEventRow {
  id: string
  label: string
  scope_type: string | null
  scope_ref: string | null
}

/**
 * Expected windows (F18, #271; planned events in the API), on the demo: its
 * seeded promo covers the spike on a catalog metric, so that metric's chart
 * shades the window and lists it; a window added from the card shows up there
 * and can be deleted again.
 */
test('the demo promo shades its metric chart, and an expected window can be added and removed', async ({
  page,
  account,
}) => {
  void account
  const slug = await generateDemo(page)
  const projectBase = page.url().replace(/\/overview$/, '')

  // The seeded promo, and the catalog metric it covers.
  const resp = await page.request.get(`/api/v1/projects/${slug}/planned-events`)
  expect(resp.status(), await resp.text()).toBe(200)
  const seeded = ((await resp.json()) as PlannedEventRow[]).find(row => row.label === 'Spring promo (planned)')
  expect(seeded?.scope_type).toBe('metric')
  const metricId = seeded!.scope_ref!

  await page.goto(`${projectBase}/monitoring/metric/${metricId}`)
  const card = page.locator('#planned-events')
  // A route's first visit compiles it on the dev server: give it time.
  await expect(card.getByRole('heading', { name: 'Expected windows' })).toBeVisible({ timeout: 60_000 })
  await expect(card.getByText('Spring promo (planned)')).toBeVisible()
  // The chart names the shaded window for screen readers.
  await expect(page.getByTestId('planned-window').first()).toContainText('Spring promo (planned)')

  // Add one from the card (default window: now to a day from now)...
  await card.getByLabel('Expected window label').fill('E2E launch')
  await card.getByRole('button', { name: 'Add expected window' }).click()
  await expect(card.getByText('E2E launch')).toBeVisible()

  // ...and delete it again.
  await card.getByRole('button', { name: 'Delete expected window E2E launch' }).click()
  await page.getByRole('alertdialog').getByRole('button', { name: 'Delete' }).click()
  await expect(card.getByText('E2E launch')).toHaveCount(0)
  await expect(card.getByText('Spring promo (planned)')).toBeVisible()

  await page.goto(`${projectBase}/overview`)
  await deleteDemo(page)
})
