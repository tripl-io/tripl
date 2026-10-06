import { randomUUID } from 'node:crypto'

import { expect, signInAsOwner, test } from './fixtures'

/**
 * Project settings › Plan rules lists the gates a plan change passes in the
 * project and, in Community, says organization rules are Tripl Enterprise's;
 * Organization settings carry the Plan governance teaser where its page would
 * be. All real stack: nothing here is mocked.
 */
test('Plan rules lists the project gates and the organization rules are an Enterprise teaser', async ({ page }) => {
  await signInAsOwner(page)
  const slug = `e2e-plan-rules-${randomUUID().slice(0, 8)}`
  const created = await page.request.post('/api/v1/projects', {
    data: { name: 'Plan rules e2e', slug, description: '' },
  })
  expect(created.status(), await created.text()).toBe(201)

  await page.goto(`/settings/project/plan-rules?project=${slug}`)
  // A route's first visit compiles it on the dev server: give it time.
  await expect(page.getByRole('heading', { name: 'Plan rules', exact: true })).toBeVisible({ timeout: 60_000 })
  await expect(page.getByRole('heading', { name: 'In this project', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Plan branches › Merge policy', exact: true })).toHaveAttribute(
    'href',
    new RegExp(`/p/${slug}/branches$`),
  )
  await expect(page.getByText(/needs one of its owners’ approval/)).toBeVisible()

  await expect(page.getByRole('heading', { name: 'Across the organization', exact: true })).toBeVisible()
  await expect(page.getByText('Organization rules are part of Tripl Enterprise.', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Compare editions', exact: true })).toHaveAttribute(
    'href',
    'https://tripl-io.github.io/tripl/editions',
  )
  await expect(page.getByText('Not built yet', { exact: true })).toHaveCount(0)
  await expect(page.getByText('Coming later', { exact: true })).toHaveCount(0)
  // The rail entry is no longer tagged "Soon".
  const railItem = page.getByRole('link', { name: 'Plan rules', exact: true })
  await expect(railItem).toBeVisible()
  await expect(railItem).not.toContainText('Soon')

  await page.goto('/settings/organization/governance')
  await expect(
    page.getByRole('heading', { name: 'Plan governance is part of Tripl Enterprise', exact: true }),
  ).toBeVisible({ timeout: 60_000 })
  await expect(page.getByRole('link', { name: 'Compare editions', exact: true })).toBeVisible()
})
