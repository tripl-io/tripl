import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import { DeliveryScheduleField } from './DeliveryScheduleField'

/**
 * The field edits ONE value — the cron expression that ships to the API — so
 * the harness holds exactly that and asserts on it. Anything else would test a
 * shape the wire never sees.
 *
 * Mode switching goes through a Radix Select, which jsdom cannot drive without
 * a pointer-events shim; the mode transitions themselves are covered
 * exhaustively by ./deliverySchedule.test.ts against the same pure functions
 * this component calls. What is asserted here is the wiring: what a stored
 * expression renders as, and what editing the inputs writes back.
 */
function Harness({ initial = '' }: { initial?: string }) {
  const [cron, setCron] = useState(initial)
  const [valid, setValid] = useState(true)
  return (
    <>
      <DeliveryScheduleField
        value={cron}
        onChange={setCron}
        onValidityChange={setValid}
        projectTimezone="Europe/Moscow"
        nextDigestAt={null}
      />
      <output data-testid="cron">{cron === '' ? '(immediate)' : cron}</output>
      <output data-testid="valid">{valid ? 'valid' : 'invalid'}</output>
    </>
  )
}

describe('DeliveryScheduleField', () => {
  it('starts on immediate and says so, because that is the unchanged default', () => {
    render(<Harness />)

    expect(screen.getByTestId('cron')).toHaveTextContent('(immediate)')
    expect(screen.getByText(/sent as soon as a collection finds something/i)).toBeInTheDocument()
  })

  it('shows a stored daily expression as a time, and names the project timezone', () => {
    render(<Harness initial="0 9 * * *" />)

    expect(screen.getByLabelText('Time of day')).toHaveValue('09:00')
    // The zone has to be on screen: "09:00" is meaningless without it, and
    // reading it as local time is the mistake this copy exists to prevent.
    expect(screen.getByText(/Europe\/Moscow/)).toBeInTheDocument()
    expect(screen.getByText(/Daily at 09:00/)).toBeInTheDocument()
  })

  it('shows a multi-time expression as the list that produced it', () => {
    render(<Harness initial="0 9,18 * * *" />)

    expect(screen.getByLabelText('Times of day')).toHaveValue('09:00, 18:00')
    expect(screen.getByText(/Every day at 09:00, 18:00/)).toBeInTheDocument()
  })

  it('keeps an expression the presets cannot express in the cron box, verbatim', () => {
    render(<Harness initial="*/5 9-17 * * 1-5" />)

    // Silently rewriting a hand-written cron into a near-miss preset would
    // change when someone is paged without telling them.
    expect(screen.getByLabelText('Cron expression')).toHaveValue('*/5 9-17 * * 1-5')
    expect(screen.getByTestId('cron')).toHaveTextContent('*/5 9-17 * * 1-5')
  })

  it('writes an edited time straight back as a cron expression', () => {
    render(<Harness initial="0 9 * * *" />)

    fireEvent.change(screen.getByLabelText('Time of day'), { target: { value: '18:30' } })

    expect(screen.getByTestId('cron')).toHaveTextContent('30 18 * * *')
  })

  it('flags a malformed time instead of writing a schedule nobody asked for', () => {
    render(<Harness initial="0 9 * * *" />)

    fireEvent.change(screen.getByLabelText('Time of day'), { target: { value: '9am' } })

    expect(screen.getByText(/Enter a time as HH:MM/)).toBeInTheDocument()
  })

  it('flags a cron expression that is not five fields', () => {
    render(<Harness initial="*/5 9-17 * * 1-5" />)

    fireEvent.change(screen.getByLabelText('Cron expression'), { target: { value: '@daily' } })

    expect(screen.getByText(/5 fields/)).toBeInTheDocument()
  })

  it('tells the form the draft on screen cannot be saved, and when it can again', () => {
    render(<Harness initial="*/5 9-17 * * 1-5" />)

    fireEvent.change(screen.getByLabelText('Cron expression'), { target: { value: '' } })

    // The last good expression is still what the form holds — so the owner
    // has to be told, or Save would store it under a visible error.
    expect(screen.getByTestId('cron')).toHaveTextContent('*/5 9-17 * * 1-5')
    expect(screen.getByTestId('valid')).toHaveTextContent('invalid')

    fireEvent.change(screen.getByLabelText('Cron expression'), { target: { value: '0 8 * * *' } })

    expect(screen.getByTestId('valid')).toHaveTextContent('valid')
    expect(screen.getByTestId('cron')).toHaveTextContent('0 8 * * *')
  })

  it('offers the native time picker for a single time, and ties the error to it', () => {
    render(<Harness initial="0 9 * * *" />)

    const time = screen.getByLabelText('Time of day')
    expect(time).toHaveAttribute('type', 'time')

    fireEvent.change(time, { target: { value: '' } })

    expect(time).toHaveAttribute('aria-invalid', 'true')
    expect(time).toHaveAccessibleDescription(/Enter a time as HH:MM/)
  })

  it('attaches a server-refused cadence to the input on screen', () => {
    render(
      <DeliveryScheduleField
        value="0 9 * * *"
        onChange={() => {}}
        projectTimezone="UTC"
        serverError="Invalid cron expression"
      />,
    )

    const input = screen.getByLabelText('Time of day')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAccessibleDescription('Invalid cron expression')
  })

  it('points the mode picker at a server error when it is the only control', () => {
    render(
      <DeliveryScheduleField
        value=""
        onChange={() => {}}
        projectTimezone="UTC"
        serverError="Invalid cron expression"
      />,
    )

    const picker = screen.getByRole('combobox')
    expect(picker).toHaveAttribute('aria-invalid', 'true')
    expect(picker).toHaveAccessibleDescription('Invalid cron expression')
  })
})
