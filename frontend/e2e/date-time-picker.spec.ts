import { randomUUID } from 'node:crypto'

import { deleteDemo, expect, generateDemo, signInAsOwner, test } from './fixtures'

interface PlannedEventRow {
  id: string
  label: string
  starts_at: string
  ends_at: string
  scope_type: string | null
  scope_ref: string | null
}

/**
 * The date + time picker is one control: a button showing both, which opens a
 * calendar with the time field under it. Driven here through the Expected
 * windows form on a demo metric chart, then read back from the server, so the local
 * time a user picks is the instant that is stored.
 */
test('an expected window takes its span from the date and time pickers', async ({ page, account }) => {
  void account
  const slug = await generateDemo(page)
  const projectBase = page.url().replace(/\/overview$/, '')

  const seeded = await page.request.get(`/api/v1/projects/${slug}/planned-events`)
  expect(seeded.status(), await seeded.text()).toBe(200)
  const metricId = ((await seeded.json()) as PlannedEventRow[]).find(row => row.scope_type === 'metric')!.scope_ref!

  await page.goto(`${projectBase}/monitoring/metric/${metricId}`)
  const card = page.locator('#planned-events')
  // A route's first visit compiles it on the dev server: give it time.
  await expect(card.getByRole('heading', { name: 'Expected windows' })).toBeVisible({ timeout: 60_000 })
  // Captions say which picker is which.
  await expect(card.getByText('Starts', { exact: true })).toBeVisible()
  await expect(card.getByText('Ends', { exact: true })).toBeVisible()

  // Starts: next month's 10th at 9:15, confirmed with Enter in the time field.
  await card.getByRole('button', { name: /^Starts: / }).click()
  const starts = page.getByRole('dialog', { name: 'Starts: choose a date and time' })
  await starts.getByRole('button', { name: 'Next month' }).click()
  await starts.getByRole('button', { name: / 10, \d{4}$/ }).click()
  // Picking a day moves straight on to the time.
  const startTime = starts.getByLabel('Starts, time')
  await expect(startTime).toBeFocused()
  await startTime.fill('09:15')
  await startTime.press('Enter')
  await expect(starts).toBeHidden()
  await expect(card.getByRole('button', { name: /^Starts: / })).toHaveText(/ 10, \d{4}, 9:15\sAM$/)

  // Ends: the 12th at 18:30, confirmed with Done.
  await card.getByRole('button', { name: /^Ends: / }).click()
  const ends = page.getByRole('dialog', { name: 'Ends: choose a date and time' })
  await ends.getByRole('button', { name: 'Next month' }).click()
  await ends.getByRole('button', { name: / 12, \d{4}$/ }).click()
  await ends.getByLabel('Ends, time').fill('18:30')
  await ends.getByRole('button', { name: 'Done' }).click()
  await expect(ends).toBeHidden()
  await expect(card.getByRole('button', { name: /^Ends: / })).toHaveText(/ 12, \d{4}, 6:30\sPM$/)

  await card.getByLabel('Expected window label').fill('E2E picked window')
  await card.getByRole('button', { name: 'Add expected window' }).click()
  await expect(card.getByText('E2E picked window')).toBeVisible()

  // Stored as the instants picked in the browser's own zone.
  const list = await page.request.get(`/api/v1/projects/${slug}/planned-events`)
  const row = ((await list.json()) as PlannedEventRow[]).find(item => item.label === 'E2E picked window')!
  const local = (iso: string) =>
    page.evaluate(value => {
      const date = new Date(value)
      return [date.getDate(), date.getHours(), date.getMinutes()]
    }, iso)
  expect(await local(row.starts_at)).toEqual([10, 9, 15])
  expect(await local(row.ends_at)).toEqual([12, 18, 30])

  await card.getByRole('button', { name: 'Delete expected window E2E picked window' }).click()
  await page.getByRole('alertdialog').getByRole('button', { name: 'Delete' }).click()
  await expect(card.getByText('E2E picked window')).toHaveCount(0)

  await page.goto(`${projectBase}/overview`)
  await deleteDemo(page)
})

/**
 * The audit log filters by day with the same calendar (it was a native
 * mm/dd/yyyy field): From narrows the list and bounds To, Clear lifts it. The
 * log is the owner's, so this runs as the owner on a project of its own.
 */
test('the audit log filters by day with the in-app calendar', async ({ page }) => {
  await signInAsOwner(page)
  const slug = `e2e-audit-${randomUUID().slice(0, 8)}`
  const created = await page.request.post('/api/v1/projects', { data: { name: 'E2E audit', slug } })
  expect(created.status(), await created.text()).toBe(201)

  await page.goto(`/p/${slug}/audit`)
  // A route's first visit compiles it on the dev server: give it time.
  const from = page.getByRole('button', { name: /^From: / })
  await expect(from).toBeVisible({ timeout: 60_000 })
  await expect(page.locator('input[type="date"]')).toHaveCount(0)

  // From = today: the project was created just now, so its entry stays listed.
  await from.click()
  const calendar = page.getByRole('dialog', { name: 'From: choose a date' })
  await calendar.locator('button[aria-current="date"]').click()
  await expect(calendar).toBeHidden()
  expect(await from.getAttribute('data-value')).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  // Visible only: the Action filter's hidden <option> has the same text.
  await expect(page.getByText('Created project').filter({ visible: true })).toBeVisible()

  // To offers From's day, and nothing before it.
  await page.getByRole('button', { name: /^To: / }).click()
  const toCalendar = page.getByRole('dialog', { name: 'To: choose a date' })
  await expect(toCalendar.locator('button[aria-current="date"]')).toBeEnabled()
  await page.keyboard.press('Escape')

  await from.click()
  await calendar.getByRole('button', { name: 'Clear' }).click()
  await expect(from).toHaveAttribute('data-value', '')

  await page.request.delete(`/api/v1/projects/${slug}`)
})
