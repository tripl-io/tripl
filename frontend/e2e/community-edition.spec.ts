import { expect, signInAsOwner, test } from './fixtures'

/**
 * Community runs one organization and has no platform console: its platform
 * admin (the instance's first account, the owner global setup made) is told
 * both are tripl Enterprise's instead of being offered what the server
 * refuses.
 */
test('a platform admin sees more organizations and the console tagged Enterprise', async ({ page }) => {
  await signInAsOwner(page)
  const me = (await (await page.request.get('/api/v1/auth/me')).json()) as { is_platform_admin?: boolean }
  test.skip(!me.is_platform_admin, 'the owner given is not a platform admin')

  await page.goto('/settings/organization/general')
  // A route's first visit compiles it on the dev server: give it time.
  await expect(
    page.getByRole('heading', { name: 'Creating more organizations: part of tripl Enterprise' }),
  ).toBeVisible({ timeout: 60_000 })
  await expect(page.getByRole('heading', { name: 'Create organization' })).toHaveCount(0)
  const refused = await page.request.post('/api/v1/orgs', { data: { slug: 'e2e-second', name: 'Second' } })
  expect(refused.status()).toBe(403)

  await page.goto('/settings/platform/orgs')
  await expect(
    page.getByRole('heading', { name: 'Organizations: part of tripl Enterprise' }),
  ).toBeVisible({ timeout: 60_000 })
  // Settings links carry the organization they were made in (`?org=`).
  await expect(page.getByRole('link', { name: /User accounts/ })).toHaveAttribute(
    'href',
    /^\/settings\/platform\/users(\?|$)/,
  )
})
