import { test, expect } from '@playwright/test'
import { mockApi } from './mock-api'

// The CI stack configures no OpenID Connect provider, so this browser contract
// mocks the instance's status; backend tests (test_oidc_sign_in.py) drive the
// flow against a fake provider.
test('the sign-in page offers the instance OpenID Connect provider and starts it', async ({ page }) => {
  const { assertAllMocked } = await mockApi(page, {
    'GET /auth/status': () => ({ json: {
      has_users: true, registration_enabled: true, deployment_mode: 'self_hosted',
      google_sign_in: false, oidc_sign_in: true, oidc_button_label: 'Sign in with Okta',
    } }),
    'GET /auth/me': () => ({ status: 401, json: { detail: 'Not authenticated' } }),
    // Stands in for the provider's sign-in page the server would redirect to.
    'GET /auth/oidc/start': () => ({ contentType: 'text/html', body: '<h1>Provider sign-in</h1>' }),
  })

  await page.goto('/auth')
  const button = page.getByRole('link', { name: 'Sign in with Okta' })
  // A route's first visit compiles it on the dev server: give it time.
  await expect(button).toBeVisible({ timeout: 60_000 })
  await expect(page.getByRole('link', { name: 'Continue with Google' })).toHaveCount(0)
  // The password form stays for accounts that have one.
  await expect(page.getByLabel('Password', { exact: true })).toBeVisible()

  await button.click()
  await expect(page).toHaveURL(/\/api\/v1\/auth\/oidc\/start$/)
  await expect(page.getByRole('heading', { name: 'Provider sign-in' })).toBeVisible()
  assertAllMocked()
})
