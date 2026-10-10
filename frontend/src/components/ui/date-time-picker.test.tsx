import { useState } from 'react'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { expectNoAxeViolations } from '@/test/axe'
import { DatePicker, DateTimePicker } from './date-time-picker'

function renderPicker(initial = '2026-01-14T09:30') {
  const onChange = vi.fn()
  function Harness() {
    const [value, setValue] = useState(initial)
    return (
      <DateTimePicker
        label="Date and time"
        value={value}
        onChange={next => {
          onChange(next)
          setValue(next)
        }}
      />
    )
  }
  render(<Harness />)
  return { onChange }
}

function openCalendar() {
  fireEvent.click(screen.getByRole('button', { name: /^Date and time: / }))
  return screen.findByRole('grid', { name: 'January 2026' })
}

describe('DateTimePicker', () => {
  it('shows the date and the time on one button, and the time field in the popover', async () => {
    renderPicker()

    const button = screen.getByRole('button', { name: 'Date and time: Jan 14, 2026, 9:30 AM' })
    expect(button).toHaveTextContent('Jan 14, 2026, 9:30 AM')
    expect(screen.queryByLabelText('Date and time, time')).toBeNull()

    await openCalendar()
    // In the trigger's own clock, not the browser's (`\s`: ICU may put a
    // narrow no-break space before AM).
    expect((screen.getByLabelText('Date and time, time') as HTMLInputElement).value).toMatch(/^9:30\sAM$/)
  })

  it('opens on the chosen day, focused and selected', async () => {
    renderPicker()

    const grid = await openCalendar()
    const day = within(grid).getByRole('button', { name: 'Wednesday, January 14, 2026' })
    await waitFor(() => expect(day).toHaveFocus())
    // The <td> is a gridcell by virtue of its role="grid" table, so it carries
    // no explicit role. Testing Library does not derive that implicit role, so
    // reach the cell through the DOM; the axe test below checks the semantics.
    expect(day.closest('td')).toHaveAttribute('aria-selected', 'true')
    expect(day).toHaveAttribute('tabindex', '0')
  })

  it('picks a day with the mouse, keeps the time, and moves on to the time field', async () => {
    const { onChange } = renderPicker()

    const grid = await openCalendar()
    fireEvent.click(within(grid).getByRole('button', { name: 'Tuesday, January 20, 2026' }))

    expect(onChange).toHaveBeenLastCalledWith('2026-01-20T09:30')
    expect(screen.getByLabelText('Date and time, time')).toHaveFocus()
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(screen.queryByRole('grid')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: /^Date and time: / })).toHaveTextContent('Jan 20, 2026, 9:30 AM')
  })

  it('moves by day and week with the arrow keys, and by month with Page Down', async () => {
    renderPicker()

    const grid = await openCalendar()
    await waitFor(() =>
      expect(within(grid).getByRole('button', { name: 'Wednesday, January 14, 2026' })).toHaveFocus(),
    )

    fireEvent.keyDown(grid, { key: 'ArrowRight' })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Thursday, January 15, 2026' })).toHaveFocus(),
    )
    fireEvent.keyDown(grid, { key: 'ArrowDown' })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Thursday, January 22, 2026' })).toHaveFocus(),
    )
    fireEvent.keyDown(grid, { key: 'Home' })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Monday, January 19, 2026' })).toHaveFocus(),
    )

    fireEvent.keyDown(grid, { key: 'PageDown' })
    const february = await screen.findByRole('grid', { name: 'February 2026' })
    await waitFor(() =>
      expect(within(february).getByRole('button', { name: 'Thursday, February 19, 2026' })).toHaveFocus(),
    )
  })

  it('changes month with the previous / next buttons', async () => {
    renderPicker()

    await openCalendar()
    fireEvent.click(screen.getByRole('button', { name: 'Next month' }))
    expect(await screen.findByRole('grid', { name: 'February 2026' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Previous month' }))
    fireEvent.click(screen.getByRole('button', { name: 'Previous month' }))
    expect(await screen.findByRole('grid', { name: 'December 2025' })).toBeInTheDocument()
  })

  it('changes the time and keeps the day; Enter closes the popover', async () => {
    const { onChange } = renderPicker()

    await openCalendar()
    const time = screen.getByLabelText('Date and time, time')
    fireEvent.change(time, { target: { value: '17:05' } })
    expect(onChange).toHaveBeenLastCalledWith('2026-01-14T17:05')

    fireEvent.keyDown(time, { key: 'Enter' })
    await waitFor(() => expect(screen.queryByRole('grid')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Date and time: Jan 14, 2026, 5:05 PM' })).toBeInTheDocument()
  })

  it('takes a time typed in the 12-hour clock and shows it back in that clock', async () => {
    const { onChange } = renderPicker()

    await openCalendar()
    const time = screen.getByLabelText('Date and time, time')
    fireEvent.change(time, { target: { value: '9:3' } })
    // Half-typed: nothing is sent, and the field says so.
    expect(onChange).not.toHaveBeenCalled()
    expect(time).toHaveAttribute('aria-invalid', 'true')

    fireEvent.change(time, { target: { value: '9:45 pm' } })
    expect(onChange).toHaveBeenLastCalledWith('2026-01-14T21:45')
    fireEvent.blur(time)
    expect((time as HTMLInputElement).value).toMatch(/^9:45\sPM$/)
    expect(time).not.toHaveAttribute('aria-invalid')
  })

  it('puts back the last time that read when the field is left half-typed', async () => {
    const { onChange } = renderPicker()

    await openCalendar()
    const time = screen.getByLabelText('Date and time, time')
    fireEvent.change(time, { target: { value: '' } })
    fireEvent.blur(time)
    expect(onChange).not.toHaveBeenCalled()
    expect((time as HTMLInputElement).value).toMatch(/^9:30\sAM$/)
  })

  it('sets the current moment with Now', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date(2026, 0, 14, 16, 7))
    try {
      const { onChange } = renderPicker('')
      fireEvent.click(screen.getByRole('button', { name: /^Date and time: / }))
      fireEvent.click(await screen.findByRole('button', { name: 'Now' }))
      expect(onChange).toHaveBeenLastCalledWith('2026-01-14T16:07')
    } finally {
      vi.useRealTimers()
    }
  })

  it('asks for a date and time when it has none, and starts a picked day at 09:00', async () => {
    const { onChange } = renderPicker('')

    const button = screen.getByRole('button', { name: 'Date and time: none picked' })
    expect(button).toHaveTextContent('Pick date and time')
    fireEvent.click(button)
    const grid = await screen.findByRole('grid')
    const first = within(grid).getAllByRole('button')[0]
    if (!first) throw new Error('the grid has days')
    fireEvent.click(first)
    expect(onChange).toHaveBeenLastCalledWith(expect.stringMatching(/^\d{4}-\d{2}-01T09:00$/))
  })

  it('offers Clear only when asked to, and sends an empty value', async () => {
    const onChange = vi.fn()
    const { rerender } = render(
      <DateTimePicker label="Sunset" value="2026-01-14T09:30" onChange={onChange} />,
    )
    fireEvent.click(screen.getByRole('button', { name: /^Sunset: / }))
    await screen.findByRole('grid')
    expect(screen.queryByRole('button', { name: 'Clear' })).toBeNull()

    rerender(<DateTimePicker label="Sunset" value="2026-01-14T09:30" onChange={onChange} clearable />)
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(onChange).toHaveBeenLastCalledWith('')
  })

  it('has no axe violations, open or closed', async () => {
    renderPicker()
    await expectNoAxeViolations(document.body)

    await openCalendar()
    await expectNoAxeViolations(document.body)
  })
})

describe('DatePicker (date only)', () => {
  function renderDatePicker(initial = '2026-01-14', bounds: { min?: string; max?: string } = {}) {
    const onChange = vi.fn()
    function Harness() {
      const [value, setValue] = useState(initial)
      return (
        <DatePicker
          label="From"
          value={value}
          {...bounds}
          onChange={next => {
            onChange(next)
            setValue(next)
          }}
        />
      )
    }
    render(<Harness />)
    return { onChange }
  }

  it('has no time field and writes YYYY-MM-DD', async () => {
    const { onChange } = renderDatePicker()

    expect(screen.queryByLabelText(/time/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'From: Jan 14, 2026' }))
    const grid = await screen.findByRole('grid', { name: 'January 2026' })
    fireEvent.click(within(grid).getByRole('button', { name: 'Tuesday, January 20, 2026' }))

    expect(onChange).toHaveBeenLastCalledWith('2026-01-20')
    await waitFor(() => expect(screen.queryByRole('grid')).toBeNull())
    expect(screen.getByRole('button', { name: 'From: Jan 20, 2026' })).toBeInTheDocument()
  })

  it('lifts the filter with Clear when clearable', async () => {
    const onChange = vi.fn()
    render(<DatePicker label="From" value="2026-01-14" onChange={onChange} clearable />)

    fireEvent.click(screen.getByRole('button', { name: 'From: Jan 14, 2026' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Clear' }))
    expect(onChange).toHaveBeenLastCalledWith('')
  })

  it('disables days outside min and max, and keeps the keyboard inside them', async () => {
    const { onChange } = renderDatePicker('2026-01-14', { min: '2026-01-10', max: '2026-01-15' })

    fireEvent.click(screen.getByRole('button', { name: 'From: Jan 14, 2026' }))
    const grid = await screen.findByRole('grid', { name: 'January 2026' })
    expect(within(grid).getByRole('button', { name: 'Friday, January 9, 2026' })).toBeDisabled()
    expect(within(grid).getByRole('button', { name: 'Friday, January 16, 2026' })).toBeDisabled()
    expect(within(grid).getByRole('button', { name: 'Saturday, January 10, 2026' })).toBeEnabled()

    // A week forward would pass max: focus stops on the last allowed day.
    fireEvent.keyDown(grid, { key: 'ArrowDown' })
    await waitFor(() =>
      expect(within(grid).getByRole('button', { name: 'Thursday, January 15, 2026' })).toHaveFocus(),
    )
    expect(onChange).not.toHaveBeenCalled()
  })
})
