import { deleteDemo, expect, generateDemo, test } from './fixtures'

/**
 * The holiday calendar (F18): choosing a country in Detection settings turns
 * its public holidays into read-only, project-wide expected windows, listed on
 * the Annotations page; choosing None takes them away again.
 */
test('a holiday calendar adds the country holidays as expected windows', async ({
  page,
  account,
}) => {
  void account
  await generateDemo(page)
  const overview = page.url()

  // A route's first visit compiles it on the dev server: give it time.
  await page.goto(overview.replace(/\/overview$/, '/settings/monitoring'))
  const country = page.getByLabel('Country', { exact: true })
  await expect(country).toBeEnabled({ timeout: 60_000 })
  await country.selectOption({ label: 'Germany (DE)' })
  await expect(page.getByText('Holidays of Germany (DE) added as expected windows')).toBeVisible()

  await page.goto(overview.replace(/\/overview$/, '/annotations'))
  const planned = page.getByTestId('planned-events-list')
  const unity = planned.getByRole('listitem').filter({ hasText: 'German Unity Day' }).first()
  await expect(unity).toBeVisible({ timeout: 60_000 })
  await expect(unity.getByText('Holiday', { exact: true })).toBeVisible()
  // The calendar owns the row: no delete beside it.
  await expect(unity.getByRole('button', { name: /^Delete expected window/ })).toHaveCount(0)

  await page.goto(overview.replace(/\/overview$/, '/settings/monitoring'))
  await expect(country).toHaveValue('DE', { timeout: 60_000 })
  await country.selectOption({ label: 'None' })
  await expect(page.getByText('Holiday calendar removed')).toBeVisible()

  await page.goto(overview.replace(/\/overview$/, '/annotations'))
  await expect(page.getByTestId('planned-events-list').getByText('Spring promo (planned)')).toBeVisible({
    timeout: 60_000,
  })
  await expect(page.getByText('German Unity Day')).toHaveCount(0)

  await page.goto(overview)
  await deleteDemo(page)
})
