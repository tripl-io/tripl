import { fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { VariableInput } from './VariableInput'
import type { VariableSuggestion } from './variableSuggestions'
import { MAX_VARIABLE_SUGGESTIONS } from './variableSuggestions'

const MANY: VariableSuggestion[] = Array.from({ length: 200 }, (_, i) => ({ name: `var_${i}` }))

function Controlled({ variables, type }: { variables: VariableSuggestion[]; type?: string }) {
  const [value, setValue] = useState('')
  return <VariableInput id="v" value={value} onChange={setValue} variables={variables} type={type} />
}

describe('VariableInput suggestions (EVT-24)', () => {
  it('caps a long property list instead of running past the viewport', () => {
    render(<Controlled variables={MANY} />)
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '$' } })

    const listbox = screen.getByRole('listbox')
    expect(within(listbox).getAllByRole('option')).toHaveLength(MAX_VARIABLE_SUGGESTIONS)
  })

  it('narrows to what is typed, so the tail is one keystroke away', () => {
    render(<Controlled variables={MANY} />)
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '${var_19' } })

    const options = within(screen.getByRole('listbox')).getAllByRole('option')
    // var_19 and var_190 … var_199.
    expect(options).toHaveLength(11)
  })

  it('scrolls the highlighted option into view as the arrows move it', () => {
    const scrollIntoView = vi.fn()
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: scrollIntoView,
    })
    try {
      render(<Controlled variables={MANY} />)
      const input = screen.getByRole('combobox')
      fireEvent.change(input, { target: { value: '$' } })
      scrollIntoView.mockClear()

      fireEvent.keyDown(input, { key: 'ArrowDown' })
      expect(input).toHaveAttribute('aria-activedescendant', expect.stringMatching(/-opt-1$/))
      expect(scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' })
    } finally {
      // jsdom has no scrollIntoView of its own; take the stub away again.
      delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView
    }
  })

  it('inserts the chosen property', () => {
    render(<Controlled variables={[{ name: 'price' }]} />)
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: '${pr' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(input).toHaveValue('${price}')
  })

  it('is no combobox on a date input, which cannot take a token', () => {
    render(<Controlled variables={MANY} type="date" />)
    expect(screen.queryByRole('combobox')).toBeNull()
  })
})

describe('VariableInput suggestion list placement (DS-35)', () => {
  it('portals the list out of the field, so a clipping card cannot cut it off', () => {
    const { container } = render(<Controlled variables={MANY} />)
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: '$' } })

    const listbox = screen.getByRole('listbox')
    expect(container.contains(listbox)).toBe(false)
    expect(input).toHaveAttribute('aria-controls', listbox.id)
    // Focus never moves into the list; typing continues in the field.
    expect(input).toHaveAttribute('aria-expanded', 'true')
  })

  it('picks an option from the portalled list and closes it', () => {
    render(<Controlled variables={MANY} />)
    const input = screen.getByRole('combobox')
    fireEvent.change(input, { target: { value: '${var_19' } })

    fireEvent.mouseDown(screen.getByRole('option', { name: /\$\{var_19\}/ }))

    expect(input).toHaveValue('${var_19}')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })
})
