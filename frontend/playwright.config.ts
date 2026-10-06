import { defineConfig, devices } from '@playwright/test'

/**
 * End-to-end tests: a real browser against a running stack,
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
  // Signs up the organization owner the settings tests act as.
  globalSetup: './e2e/global-setup.ts',
  // A demo takes a while to generate, and the stack is shared: one at a time.
  fullyParallel: false,
  workers: 1,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  // A screenshot with no baseline fails in CI rather than becoming one (the
  // retry would then pass against it). Baselines are Linux Chromium's, made in
  // CI on purpose: E2E_UPDATE_SNAPSHOTS (the `update-snapshots` pull request
  // label) writes the missing and changed ones, and the job uploads them.
  updateSnapshots: process.env.E2E_UPDATE_SNAPSHOTS === 'true' ? 'changed' : process.env.CI ? 'none' : 'missing',
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
