import { deleteDemo, expect, generateDemo, test } from './fixtures'

/**
 * The demo, the first thing a new account does: generate it
 * from the empty workspace, land on its overview, look around, and delete it.
 * Generation runs the real worker against a synthetic warehouse, so this is
 * the whole pipeline, not a mock of it.
 */
test('a new account generates a demo, looks around and deletes it', async ({ page, account }) => {
  void account
  // The empty workspace offers the demo first; generation ends on the demo's
  // overview, under the demo banner.
  await generateDemo(page)
  const banner = page.locator('[data-demo-banner]')
  await expect(banner.getByText('Demo project', { exact: true })).toBeVisible()

  // The plan is seeded: the events page lists events.
  // The sidebar link carries the count ("Events 17").
  await page.getByRole('link', { name: /^Events \d+$/ }).click()
  await expect(page).toHaveURL(/\/events$/)
  // A route's first visit compiles it on the dev server: give it time.
  await expect(page.getByRole('main').getByText('Purchase', { exact: true }).first()).toBeVisible({
    timeout: 60_000,
  })

  // Delete it; the workspace is empty again and offers the demo anew.
  await deleteDemo(page)
})
