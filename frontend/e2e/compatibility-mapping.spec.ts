import { demoTest, expect } from './fixtures'

demoTest('author a compatibility mapping and find the model command', async ({ page, demo: slug }) => {
  const project = `/api/v1/projects/${slug}`
  const field = await page.request.post(`${project}/meta-fields`, {
    data: { name: 'compatibility_mapping', display_name: 'Compatibility mapping', field_type: 'string' },
  })
  expect(field.ok(), await field.text()).toBeTruthy()
  const listing = await page.request.get(`${project}/events?limit=1`)
  const { items } = await listing.json()
  await page.goto(`/p/${slug}/events/all/${items[0].id}/edit`)
  await page.getByRole('button', { name: 'Insert example' }).click()
  await expect(page.getByLabel('Compatibility mapping')).toHaveValue(/checkout_success/)
  await expect(page.getByText(/tripl compatibility model/)).toContainText(items[0].id)
})
