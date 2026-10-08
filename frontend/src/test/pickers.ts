import { fireEvent, screen, within } from '@testing-library/react'

/**
 * Picks a day through a `DatePicker` / `DateTimePicker` the way a user does:
 * open it by its `<Label>`, page to the month, click the day. `isoDate` is
 * `YYYY-MM-DD`; with `time` (`HH:mm`) the time field is set too and the
 * popover closed with Done.
 */
export async function pickDate(label: string, isoDate: string, time?: string): Promise<void> {
  const [year, month, day] = isoDate.split('-').map(Number) as [number, number, number]
  const target = new Date(year, month - 1, day)
  const monthName = target.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })

  fireEvent.click(screen.getByLabelText(label))
  let grid = await screen.findByRole('grid')
  for (let step = 0; step < 240 && grid.getAttribute('aria-label') !== monthName; step += 1) {
    // A day's name, less its weekday ("January 14, 2026"), parses everywhere.
    const someDay = within(grid).getAllByRole('button')[0]?.getAttribute('aria-label') ?? ''
    const shown = new Date(someDay.replace(/^[^,]+, /, ''))
    fireEvent.click(screen.getByRole('button', { name: shown < target ? 'Next month' : 'Previous month' }))
    grid = await screen.findByRole('grid')
  }
  const dayName = target.toLocaleDateString(undefined, {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
    year: 'numeric',
  })
  fireEvent.click(within(grid).getByRole('button', { name: dayName }))
  if (time !== undefined) {
    fireEvent.change(screen.getByLabelText(/, time$/), { target: { value: time } })
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
  }
}
