import { deleteDemo, expect, generateDemo, test } from './fixtures'

/**
 * The project-wide Annotations page: reached from the sidebar, it lists the
 * demo's seeded markers and planned promo; a marker posted to one event shows
 * up there named after its chart, and can be deleted from the page.
 */
test('the annotations page lists every marker and planned event, and deletes one', async ({
  page,
  account,
}) => {
  void account
  const slug = await generateDemo(page)

  // A marker on one event, posted the way a deploy pipeline would.
  const events = await page.request.get(`/api/v1/projects/${slug}/events`)
  expect(events.status(), await events.text()).toBe(200)
  const event = ((await events.json()) as { items: { id: string; name: string }[] }).items[0]!
  const created = await page.request.post(`/api/v1/projects/${slug}/annotations`, {
    data: {
      bucket: new Date().toISOString(),
      label: 'E2E deploy',
      scope_type: 'event',
      scope_ref: event.id,
    },
  })
  expect(created.status(), await created.text()).toBe(201)

  await page.getByRole('link', { name: 'Annotations', exact: true }).click()
  await expect(page).toHaveURL(/\/annotations$/)
  // A route's first visit compiles it on the dev server: give it time.
  const annotations = page.getByTestId('annotations-list')
  await expect(annotations.getByText('E2E deploy')).toBeVisible({ timeout: 60_000 })
  // Its row names the event it is on, linking to that chart (the event may
  // carry seeded markers of its own, so look inside this row only).
  const row = annotations.getByRole('listitem').filter({ hasText: 'E2E deploy' })
  await expect(row.getByRole('link', { name: event.name })).toBeVisible()
  await expect(annotations.getByText('Injected demo spike')).toBeVisible()
  await expect(page.getByTestId('planned-events-list').getByText('Spring promo (planned)')).toBeVisible()

  // The source filter narrows to the worker's release markers.
  await page.getByRole('button', { name: 'Releases', exact: true }).click()
  await expect(annotations.getByText('E2E deploy')).toHaveCount(0)
  await page.getByRole('button', { name: 'All', exact: true }).click()

  await annotations.getByRole('button', { name: 'Delete annotation E2E deploy' }).click()
  await page.getByRole('alertdialog').getByRole('button', { name: 'Delete' }).click()
  await expect(annotations.getByText('E2E deploy')).toHaveCount(0)

  await page.goto(page.url().replace(/\/annotations$/, '/overview'))
  await deleteDemo(page)
})
