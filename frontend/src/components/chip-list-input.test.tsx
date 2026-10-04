import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { ChipListInput } from './chip-list-input'

function Harness({ initial = [] as string[] }: { initial?: string[] }) {
  const [values, setValues] = useState(initial)
  return (
    <ChipListInput
      values={values}
      onChange={setValues}
      placeholder="Add a key"
      ariaLabel="Jira keys"
      validate={value => /^[A-Z]+-\d+$/.test(value)}
      invalidMessage="Use a key like PROJ-4770."
    />
  )
}

describe('ChipListInput', () => {
  it('announces a rejected value and ties it to the input', () => {
    render(<Harness />)
    const input = screen.getByRole('textbox', { name: 'Jira keys' })
    fireEvent.change(input, { target: { value: 'nope' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('Use a key like PROJ-4770.')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAccessibleDescription('Use a key like PROJ-4770.')

    // Typing again clears the complaint.
    fireEvent.change(input, { target: { value: 'PROJ-1' } })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(input).not.toHaveAttribute('aria-invalid')
  })

  it('says a duplicate is already there instead of silently dropping it', () => {
    render(<Harness initial={['PROJ-1']} />)
    const input = screen.getByRole('textbox', { name: 'Jira keys' })
    fireEvent.change(input, { target: { value: 'PROJ-1' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(screen.getByRole('alert')).toHaveTextContent('Already added.')
    expect(screen.getAllByRole('button', { name: 'Remove PROJ-1' })).toHaveLength(1)
    expect(input).toHaveValue('PROJ-1')
  })

  it('stays quiet when focus moves to one of its own chip remove buttons', () => {
    render(<Harness initial={['PROJ-1']} />)
    const input = screen.getByRole('textbox', { name: 'Jira keys' })
    const remove = screen.getByRole('button', { name: 'Remove PROJ-1' })
    fireEvent.change(input, { target: { value: 'PROJ' } })
    fireEvent.blur(input, { relatedTarget: remove })

    expect(screen.queryByRole('alert')).toBeNull()
    expect(input).toHaveValue('PROJ')
  })

  it('says why a rejected draft was not added when focus leaves the control', () => {
    render(
      <>
        <Harness />
        <button type="button">Save</button>
      </>,
    )
    const input = screen.getByRole('textbox', { name: 'Jira keys' })
    fireEvent.change(input, { target: { value: 'PROJ' } })
    fireEvent.blur(input, { relatedTarget: screen.getByRole('button', { name: 'Save' }) })

    expect(screen.getByRole('alert')).toHaveTextContent('Use a key like PROJ-4770.')
    expect(input).toHaveValue('PROJ')
  })

  it('says a duplicate was not added when focus leaves the control', () => {
    render(<Harness initial={['PROJ-1']} />)
    const input = screen.getByRole('textbox', { name: 'Jira keys' })
    fireEvent.change(input, { target: { value: 'PROJ-1' } })
    fireEvent.blur(input)

    expect(screen.getByRole('alert')).toHaveTextContent('Already added.')
  })

  it('still turns a valid draft into a chip on blur', () => {
    render(<Harness />)
    const input = screen.getByRole('textbox', { name: 'Jira keys' })
    fireEvent.change(input, { target: { value: 'PROJ-7' } })
    fireEvent.blur(input)

    expect(screen.getByRole('button', { name: 'Remove PROJ-7' })).toBeInTheDocument()
    expect(input).toHaveValue('')
  })

  it('takes back the last chip on Backspace in an empty box', () => {
    render(<Harness initial={['PROJ-1', 'PROJ-2']} />)
    const input = screen.getByRole('textbox', { name: 'Jira keys' })
    fireEvent.keyDown(input, { key: 'Backspace' })

    expect(screen.queryByRole('button', { name: 'Remove PROJ-2' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Remove PROJ-1' })).toBeInTheDocument()
  })
})
