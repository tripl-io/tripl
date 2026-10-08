import type { Route } from '@playwright/test'

import { expect, signInAsOwner, test } from './fixtures'

/**
 * Redshift and Greenplum connect over the PostgreSQL protocol: picking either
 * type keeps the host, port, database and user fields, offers the PostgreSQL
 * TLS and search-path settings, and moves the port to the engine's default
 * (5439 for Redshift, 5432 for Greenplum).
 *
 * The page and every other call are the real stack. The create request alone is
 * answered here: the connection itself needs a live warehouse, which CI does not
 * have for Redshift. What the server does with the body is covered by the
 * backend suite (test_greenplum_redshift_adapters.py, test_data_sources.py), and
 * Greenplum's SQL by the Greenplum conformance job.
 */
for (const engine of [
  { dbType: 'redshift', port: 5439, host: 'wg.123456789012.eu-west-1.redshift-serverless.amazonaws.com' },
  { dbType: 'greenplum', port: 5432, host: 'gp-coordinator.example.com' },
] as const) {
  test(`a ${engine.dbType} connection takes the PostgreSQL settings on port ${engine.port}`, async ({ page }) => {
    await signInAsOwner(page)

    const posted: Record<string, unknown>[] = []
    const isCreate = (url: URL) => url.pathname.endsWith('/data-sources')
    await page.route(isCreate, async (route: Route) => {
      const request = route.request()
      if (request.method() !== 'POST') return route.fallback()
      const body = request.postDataJSON() as Record<string, unknown>
      posted.push(body)
      return route.fulfill({
        status: 201,
        json: {
          id: '00000000-0000-4000-8000-000000000002',
          project_id: null,
          name: body.name,
          db_type: engine.dbType,
          host: body.host,
          port: body.port,
          database_name: body.database_name,
          username: body.username,
          password_set: true,
          timeout_seconds: null,
          json_path_discovery: null,
          connection_settings: body.connection_settings,
          last_test_at: null,
          last_test_status: null,
          last_test_message: null,
          created_at: '2026-10-08T00:00:00Z',
          updated_at: '2026-10-08T00:00:00Z',
        },
      })
    })

    await page.goto('/settings/data-sources')
    // A route's first visit compiles it on the dev server: give it time.
    await page.getByRole('button', { name: 'Add connection' }).first().click({ timeout: 60_000 })
    const dialog = page.getByRole('dialog', { name: /New data source/ })
    await dialog.getByLabel('Type', { exact: true }).selectOption(engine.dbType)

    for (const label of ['Host', 'Port', 'Database', 'Username', 'Password', 'SSL mode', 'Search path']) {
      await expect(dialog.getByLabel(label, { exact: true })).toBeVisible()
    }
    await expect(dialog.getByLabel('Port', { exact: true })).toHaveValue(String(engine.port))

    await dialog.getByLabel('Name', { exact: true }).fill('Warehouse')
    await dialog.getByLabel('Host', { exact: true }).fill(engine.host)
    await dialog.getByLabel('Database', { exact: true }).fill('dev')
    await dialog.getByLabel('Username', { exact: true }).fill('tripl_reader')
    await dialog.getByLabel('Password', { exact: true }).fill('e2e-password')
    await dialog.getByLabel('Search path', { exact: true }).fill('analytics')
    await dialog.getByRole('button', { name: 'Create' }).click()
    await expect(dialog).toBeHidden()

    expect(posted).toEqual([
      expect.objectContaining({
        db_type: engine.dbType,
        host: engine.host,
        port: engine.port,
        database_name: 'dev',
        username: 'tripl_reader',
        password: 'e2e-password',
        connection_settings: expect.objectContaining({ search_path: 'analytics' }),
      }),
    ])
  })
}
