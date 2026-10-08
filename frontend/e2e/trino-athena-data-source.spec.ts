import type { Page, Route } from '@playwright/test'

import { expect, signInAsOwner, test } from './fixtures'

/**
 * Trino keeps the host, port and user fields (the database is the catalog, the
 * password optional) and offers its scheme and schema settings; Athena replaces
 * host and port with the AWS region, the user and password with an access key,
 * and offers the workgroup, result location and catalog.
 *
 * The page and every other call are the real stack. The create request alone is
 * answered here: the connection itself needs a live coordinator or AWS account.
 * What the server does with the body is covered by the backend suite
 * (test_trino_adapter.py, test_athena_adapter.py, test_data_sources.py), and
 * Trino's SQL by the Trino conformance job.
 */
async function captureCreate(page: Page, dbType: string): Promise<Record<string, unknown>[]> {
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
        id: '00000000-0000-4000-8000-000000000003',
        project_id: null,
        name: body.name,
        db_type: dbType,
        host: body.host,
        port: body.port,
        database_name: body.database_name,
        username: body.username,
        password_set: Boolean(body.password),
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
  return posted
}

async function openNewSource(page: Page, dbType: string) {
  await page.goto('/settings/data-sources')
  // A route's first visit compiles it on the dev server: give it time.
  await page.getByRole('button', { name: 'Add connection' }).first().click({ timeout: 60_000 })
  const dialog = page.getByRole('dialog', { name: /New data source/ })
  await dialog.getByLabel('Type', { exact: true }).selectOption(dbType)
  return dialog
}

test('a Trino connection names a catalog and may go without a password', async ({ page }) => {
  await signInAsOwner(page)
  const posted = await captureCreate(page, 'trino')
  const dialog = await openNewSource(page, 'trino')

  for (const label of ['Host', 'Port', 'Catalog', 'Username', 'Password', 'Scheme', 'Default schema']) {
    await expect(dialog.getByLabel(label, { exact: true })).toBeVisible()
  }
  await expect(dialog.getByLabel('Port', { exact: true })).toHaveValue('443')

  await dialog.getByLabel('Name', { exact: true }).fill('Lake')
  await dialog.getByLabel('Host', { exact: true }).fill('trino.internal')
  await dialog.getByLabel('Port', { exact: true }).fill('8080')
  await dialog.getByLabel('Catalog', { exact: true }).fill('hive')
  await dialog.getByLabel('Username', { exact: true }).fill('tripl')
  await dialog.getByLabel('Scheme', { exact: true }).selectOption('http')
  await dialog.getByLabel('Default schema', { exact: true }).fill('events')
  await dialog.getByRole('button', { name: 'Create' }).click()
  await expect(dialog).toBeHidden()

  expect(posted).toEqual([
    expect.objectContaining({
      db_type: 'trino',
      host: 'trino.internal',
      port: 8080,
      database_name: 'hive',
      username: 'tripl',
      password: '',
      connection_settings: { http_scheme: 'http', schema_name: 'events', schema_allowlist: null },
    }),
  ])
})

test('an Athena connection takes a region, an access key and a workgroup', async ({ page }) => {
  await signInAsOwner(page)
  const posted = await captureCreate(page, 'athena')
  const dialog = await openNewSource(page, 'athena')

  for (const label of [
    'AWS region',
    'Database',
    'Access key ID',
    'Secret access key',
    'Workgroup',
    'Query result location',
  ]) {
    await expect(dialog.getByLabel(label, { exact: true })).toBeVisible()
  }
  await expect(dialog.getByLabel('Port', { exact: true })).toHaveCount(0)

  await dialog.getByLabel('Name', { exact: true }).fill('Athena')
  await dialog.getByLabel('AWS region', { exact: true }).fill('eu-west-1')
  await dialog.getByLabel('Database', { exact: true }).fill('analytics')
  await dialog.getByLabel('Access key ID', { exact: true }).fill('AKIAEXAMPLE')
  await dialog.getByLabel('Secret access key', { exact: true }).fill('e2e-secret')
  await dialog.getByLabel('Workgroup', { exact: true }).fill('analytics')
  await dialog.getByLabel('Query result location', { exact: true }).fill('s3://my-bucket/athena/')
  await dialog.getByRole('button', { name: 'Create' }).click()
  await expect(dialog).toBeHidden()

  expect(posted).toEqual([
    expect.objectContaining({
      db_type: 'athena',
      host: 'eu-west-1',
      port: 443,
      database_name: 'analytics',
      username: 'AKIAEXAMPLE',
      password: 'e2e-secret',
      connection_settings: {
        work_group: 'analytics',
        s3_output_location: 's3://my-bucket/athena/',
        catalog_name: null,
        schema_allowlist: null,
      },
    }),
  ])
})
