import { deleteDemo, expect, generateDemo, test } from './fixtures'

/**
 * Suggested recurring windows (#271): the demo's weekly promo was marked
 * expected three weeks running, so the Annotations page suggests planning its
 * next sends; planning them creates the windows and retires the suggestion.
 */
test('a suggested recurring window is planned from the annotations page', async ({
  page,
  account,
}) => {
  void account
  await generateDemo(page)
  const overview = page.url()

  await page.goto(overview.replace(/\/overview$/, '/annotations'))
  // A route's first visit compiles it on the dev server: give it time.
  const suggestions = page.getByTestId('planned-window-suggestions')
  const promo = suggestions.getByRole('listitem').filter({ hasText: 'Weekly promo email' })
  await expect(promo).toBeVisible({ timeout: 60_000 })
  await expect(promo.getByText('Paywall View')).toBeVisible()

  await promo.getByRole('button', { name: /^Plan the next 4 windows on Paywall View$/ }).click()
  await expect(page.getByText('Planned the next 4 windows')).toBeVisible()
  await expect(suggestions).toHaveCount(0)
  await expect(page.getByTestId('planned-events-list').getByText('Weekly promo email')).toHaveCount(4)

  await page.goto(overview)
  await deleteDemo(page)
})
