import { fireEvent, screen, within } from '@testing-library/react'
import { APP_LOCALE } from '@/lib/format'

/**
 * Picks a day through a `DatePicker` / `DateTimePicker` the way a user does:
 * open it by its `<Label>`, page to the month, click the day. `isoDate` is
 * `YYYY-MM-DD`; with `time` (`HH:mm`, which the time field reads like a typed
 * "9:30 PM") the time field is set too and the popover closed with Done. The
 * calendar names months and days in the app locale, so the lookups here do too.
 *
 * Queries pass `hidden: true`: inside a Dialog, jsdom cannot position the
 * nested popover, and Testing Library then counts it as inaccessible.
 */
export async function pickDate(label: string, isoDate: string, time?: string): Promise<void> {
  const [year, month, day] = isoDate.split('-').map(Number) as [number, number, number]
  const target = new Date(year, month - 1, day)
  const monthName = target.toLocaleDateString(APP_LOCALE, { month: 'long', year: 'numeric' })

  fireEvent.click(screen.getByLabelText(label))
  let grid = await screen.findByRole('grid', { hidden: true })
  for (let step = 0; step < 240 && grid.getAttribute('aria-label') !== monthName; step += 1) {
    // A day's name, less its weekday ("January 14, 2026"), parses everywhere.
    const someDay = within(grid).getAllByRole('button', { hidden: true })[0]?.getAttribute('aria-label') ?? ''
    const shown = new Date(someDay.replace(/^[^,]+, /, ''))
    fireEvent.click(screen.getByRole('button', { name: shown < target ? 'Next month' : 'Previous month', hidden: true }))
    grid = await screen.findByRole('grid', { hidden: true })
  }
  const dayName = target.toLocaleDateString(APP_LOCALE, {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
    year: 'numeric',
  })
  fireEvent.click(within(grid).getByRole('button', { name: dayName, hidden: true }))
  if (time !== undefined) {
    // Picking a day focuses the time field; in a Dialog, jsdom reads that as
    // focus leaving the dialog and closes the popover (a browser does not).
    if (!screen.queryByLabelText(/, time$/)) fireEvent.click(screen.getByLabelText(label))
    fireEvent.change(await screen.findByLabelText(/, time$/), { target: { value: time } })
    fireEvent.click(screen.getByRole('button', { name: 'Done', hidden: true }))
  }
}
