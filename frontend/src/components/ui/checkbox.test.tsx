import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Checkbox } from './checkbox'

// jsdom resolves no Tailwind, so which glyph shows is read off the classes:
// each glyph is `hidden` until the Root's data-state matches it.
const SHOW_MINUS = 'group-data-[state=indeterminate]/checkbox:block'
const SHOW_CHECK = 'group-data-[state=checked]/checkbox:block'

describe('Checkbox indeterminate state', () => {
  it('announces "mixed" and draws a minus on the filled box', () => {
    render(<Checkbox aria-label="Select all rows" checked="indeterminate" />)

    const box = screen.getByRole('checkbox', { name: 'Select all rows' })
    expect(box).toHaveAttribute('aria-checked', 'mixed')
    expect(box).toHaveAttribute('data-state', 'indeterminate')
    expect(box).toHaveClass('group/checkbox')
    expect(box.className).toContain('data-[state=indeterminate]:bg-accent-solid')
    expect(box.querySelector('.lucide-minus')).toHaveClass('hidden', SHOW_MINUS)
    expect(box.querySelector('.lucide-check')).toHaveClass('hidden', SHOW_CHECK)
  })

  it('keys the glyph off data-state, so an uncontrolled indeterminate box shows the minus', () => {
    render(<Checkbox aria-label="Select some rows" defaultChecked="indeterminate" />)

    const box = screen.getByRole('checkbox', { name: 'Select some rows' })
    expect(box).toHaveAttribute('data-state', 'indeterminate')
    expect(box.querySelector('.lucide-minus')).toHaveClass(SHOW_MINUS)
  })

  it('draws a tick when checked', () => {
    render(<Checkbox aria-label="Select row" checked />)

    const box = screen.getByRole('checkbox', { name: 'Select row' })
    expect(box).toHaveAttribute('aria-checked', 'true')
    expect(box).toHaveAttribute('data-state', 'checked')
    expect(box.querySelector('.lucide-check')).toHaveClass(SHOW_CHECK)
  })
})
