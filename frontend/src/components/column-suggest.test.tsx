import { act, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { ColumnSuggestInput } from './column-suggest'

function renderInput(label: string, disabled: boolean) {
  render(
    <ColumnSuggestInput
      value=""
      onChange={() => {}}
      suggestions={['event_time']}
      aria-label={label}
      disabled={disabled}
    />,
  )
  return screen.getByLabelText(label)
}

describe('ColumnSuggestInput disabled treatment', () => {
  /**
   * The metric form's column boxes carried their own `opacity: 0.6` knock-down,
   * the same cue that on the dark theme left a dead field 3/255 of fill and
   * 7/255 of border from a live one — measurably no cue at all.
   * The guard is deliberately not a restatement of the shared primitive's
   * values: it pins that a disabled box does not look like a live one, and that
   * it is not dimming to say so.
   */
  it('does not answer "disabled" with an invisible dim', () => {
    const live = renderInput('Timestamp column', false)
    const dead = renderInput('Value column', true)

    expect(dead).toBeDisabled()
    expect(dead.getAttribute('style')).not.toBe(live.getAttribute('style'))
    expect(dead.style.opacity).toBe('')
  })
})

function Controlled({ onPick }: { onPick: (value: string) => void }) {
  const [value, setValue] = useState('')
  return (
    // Stands in for the clipping SCard the list used to be cut off by.
    <div data-testid="card" style={{ overflow: 'hidden' }}>
      <ColumnSuggestInput
        value={value}
        onChange={next => {
          setValue(next)
          onPick(next)
        }}
        suggestions={['event_time', 'event_name', 'platform']}
        aria-label="Value column"
      />
    </div>
  )
}

describe('ColumnSuggestInput suggestion list (DS-3)', () => {
  it('renders the listbox outside the clipping card, keeping focus in the input', () => {
    render(<Controlled onPick={() => {}} />)
    const input = screen.getByRole('combobox', { name: 'Value column' })
    // Focusing opens the list, a state update of its own.
    act(() => input.focus())

    const listbox = screen.getByRole('listbox', { name: 'Column suggestions' })
    expect(input).toHaveAttribute('aria-expanded', 'true')
    expect(input).toHaveAttribute('aria-controls', listbox.id)
    expect(screen.getByTestId('card')).not.toContainElement(listbox)
    expect(input).toHaveFocus()
  })

  it('picks with the keyboard and by click', () => {
    const onPick = vi.fn()
    render(<Controlled onPick={onPick} />)
    const input = screen.getByRole('combobox', { name: 'Value column' })
    fireEvent.focus(input)

    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(input).toHaveAttribute(
      'aria-activedescendant',
      screen.getByRole('option', { name: 'event_name' }).id,
    )
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onPick).toHaveBeenLastCalledWith('event_name')
    expect(screen.queryByRole('listbox')).toBeNull()

    fireEvent.change(input, { target: { value: 'plat' } })
    fireEvent.click(screen.getByRole('option', { name: 'platform' }))
    expect(onPick).toHaveBeenLastCalledWith('platform')
  })

  it('closes on Escape', () => {
    render(<Controlled onPick={() => {}} />)
    const input = screen.getByRole('combobox', { name: 'Value column' })
    fireEvent.focus(input)
    expect(screen.getByRole('listbox')).toBeInTheDocument()
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(input).toHaveAttribute('aria-expanded', 'false')
  })
})
