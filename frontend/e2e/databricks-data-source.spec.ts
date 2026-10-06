import type { Route } from '@playwright/test'

import { expect, signInAsOwner, test } from './fixtures'

/**
 * The Databricks connection form: picking the type swaps in its own fields, the
 * HTTP path is required before anything is sent, and the body that is sent
 * carries the settings and port 443.
 *
 * The page and every other call are the real stack. The create request alone is
 * answered here: a real create would only store the row, and the connection
 * itself needs a live SQL warehouse, which CI does not have. The backend suite
 * covers what the server does with the body (test_databricks_adapter.py,
 * test_data_sources.py).
 */
test('a Databricks connection shows its own fields and requires the HTTP path', async ({ page }) => {
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
        id: '00000000-0000-4000-8000-000000000001',
        project_id: null,
        name: body.name,
        db_type: 'databricks',
        host: body.host,
        port: 443,
        database_name: body.database_name,
        username: '',
        password_set: true,
        timeout_seconds: null,
        json_path_discovery: null,
        connection_settings: body.connection_settings,
        last_test_at: null,
        last_test_status: null,
        last_test_message: null,
        created_at: '2026-10-07T00:00:00Z',
        updated_at: '2026-10-07T00:00:00Z',
      },
    })
  })

  await page.goto('/settings/data-sources')
  // A route's first visit compiles it on the dev server: give it time.
  await page.getByRole('button', { name: 'Add connection' }).first().click({ timeout: 60_000 })
  const dialog = page.getByRole('dialog', { name: /New data source/ })
  await dialog.getByLabel('Type', { exact: true }).selectOption('databricks')

  // Its own fields, and none of the generic ones it has no use for.
  for (const label of [
    'Server hostname',
    'Catalog',
    'OAuth client ID',
    'Access token or OAuth secret',
    'HTTP path',
    'Authentication',
    'Default schema',
    'Schema allowlist',
  ]) {
    await expect(dialog.getByLabel(label, { exact: true })).toBeVisible()
  }
  await expect(dialog.getByLabel('Port', { exact: true })).toHaveCount(0)
  await expect(dialog.getByLabel('Database', { exact: true })).toHaveCount(0)

  await dialog.getByLabel('Name', { exact: true }).fill('Lakehouse')
  await dialog.getByLabel('Server hostname', { exact: true }).fill('dbc-a1b2c3d4-e5f6.cloud.databricks.com')
  await dialog.getByLabel('Catalog', { exact: true }).fill('main')
  await dialog.getByLabel('Access token or OAuth secret', { exact: true }).fill('dapi-e2e-token')

  // No HTTP path: flagged inline, nothing sent.
  const httpPath = dialog.getByLabel('HTTP path', { exact: true })
  await dialog.getByRole('button', { name: 'Create' }).click()
  await expect(httpPath).toHaveAttribute('aria-invalid', 'true')
  await expect(dialog.getByText('Required', { exact: true })).toBeVisible()
  expect(posted).toEqual([])

  // A whole URL is not a path.
  await httpPath.fill('https://dbc-a1b2c3d4-e5f6.cloud.databricks.com/sql/1.0/warehouses/abc123')
  await dialog.getByRole('button', { name: 'Create' }).click()
  await expect(dialog.getByText(/Paste only the path/)).toBeVisible()
  expect(posted).toEqual([])

  await httpPath.fill('/sql/1.0/warehouses/abc123')
  await dialog.getByLabel('Schema allowlist', { exact: true }).fill('analytics, marts')
  await dialog.getByRole('button', { name: 'Create' }).click()
  await expect(dialog).toBeHidden()

  expect(posted).toEqual([
    expect.objectContaining({
      db_type: 'databricks',
      host: 'dbc-a1b2c3d4-e5f6.cloud.databricks.com',
      port: 443,
      database_name: 'main',
      password: 'dapi-e2e-token',
      connection_settings: {
        http_path: '/sql/1.0/warehouses/abc123',
        auth_type: 'pat',
        schema_name: null,
        schema_allowlist: ['analytics', 'marts'],
      },
    }),
  ])
})
