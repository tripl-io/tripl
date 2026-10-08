import { deleteDemo, expect, generateDemo, test } from './fixtures'

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
 * calendar with the time field under it. Driven here through the planned-event
 * form on a demo metric chart, then read back from the server, so the local
 * time a user picks is the instant that is stored.
 */
test('a planned event takes its window from the date and time pickers', async ({ page, account }) => {
  void account
  const slug = await generateDemo(page)
  const projectBase = page.url().replace(/\/overview$/, '')

  const seeded = await page.request.get(`/api/v1/projects/${slug}/planned-events`)
  expect(seeded.status(), await seeded.text()).toBe(200)
  const metricId = ((await seeded.json()) as PlannedEventRow[]).find(row => row.scope_type === 'metric')!.scope_ref!

  await page.goto(`${projectBase}/monitoring/metric/${metricId}`)
  const card = page.locator('#planned-events')
  // A route's first visit compiles it on the dev server: give it time.
  await expect(card.getByRole('heading', { name: 'Planned events' })).toBeVisible({ timeout: 60_000 })
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

  await card.getByLabel('Planned event label').fill('E2E picked window')
  await card.getByRole('button', { name: 'Add planned event' }).click()
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

  await card.getByRole('button', { name: 'Delete planned event E2E picked window' }).click()
  await page.getByRole('alertdialog').getByRole('button', { name: 'Delete' }).click()
  await expect(card.getByText('E2E picked window')).toHaveCount(0)

  await page.goto(`${projectBase}/overview`)
  await deleteDemo(page)
})
