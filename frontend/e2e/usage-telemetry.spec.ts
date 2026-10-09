import { expect, signInAsOwner, test } from './fixtures'

/**
 * Usage telemetry is on by default in Community, but the dev stack CI runs
 * (compose.dev.yaml) turns it off; the platform admin reads that — and where it
 * would go — under Runtime.
 */
test('a platform admin sees usage telemetry is off and where it would go', async ({ page }) => {
  await signInAsOwner(page)
  const me = (await (await page.request.get('/api/v1/auth/me')).json()) as { is_platform_admin?: boolean }
  test.skip(!me.is_platform_admin, 'the owner given is not a platform admin')

  const status = await page.request.get('/api/v1/platform/settings/telemetry')
  expect(status.status()).toBe(200)
  expect(await status.json()).toMatchObject({ enabled: false, reason: 'disabled', last_payload: null })

  await page.goto('/settings/instance/runtime')
  // A route's first visit compiles it on the dev server: give it time.
  await expect(page.getByText('Usage telemetry')).toBeVisible({ timeout: 60_000 })
  await expect(page.getByText('Off (TELEMETRY_ENABLED=false)')).toBeVisible()
  await expect(page.getByText('https://telemetry.tripl.io/v1/ping')).toBeVisible()
})
