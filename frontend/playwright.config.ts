import { defineConfig, devices } from '@playwright/test'

/**
 * End-to-end tests (tripl-fj5g.1): a real browser against a running stack,
 * `compose.dev.yaml` by default (the Vite dev server on :5173 proxying the
 * API). Point E2E_BASE_URL elsewhere to walk another instance. The stack is
 * not started from here: CI brings it up first, and locally it is usually up
 * already.
 *
 * E2E_CHROMIUM uses an installed Chromium instead of Playwright's download,
 * for machines Playwright ships no browser for.
 */
const baseURL = process.env.E2E_BASE_URL ?? 'http://127.0.0.1:5173'
const executablePath = process.env.E2E_CHROMIUM || undefined

export default defineConfig({
  testDir: './e2e',
  // A demo takes a while to generate, and the stack is shared: one at a time.
  fullyParallel: false,
  workers: 1,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], launchOptions: { executablePath } },
    },
  ],
})
