import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { RangeSegmentedControl } from './range-segmented-control'

describe('RangeSegmentedControl', () => {
  it('is a labelled group of pressed-state buttons on the shared segmented look', () => {
    const onChange = vi.fn()
    render(<RangeSegmentedControl value={30} onChange={onChange} />)

    const group = screen.getByRole('group', { name: 'Time range' })
    expect(group).toHaveClass('bg-bg-sunken')
    const selected = within(group).getByRole('button', { name: '30d' })
    expect(selected).toHaveAttribute('aria-pressed', 'true')
    // A raised option, not the solid accent fill, and a 32px-class tap target.
    expect(selected).not.toHaveClass('bg-primary')
    expect(selected).toHaveClass('max-sm:h-8')

    fireEvent.click(within(group).getByRole('button', { name: '7d' }))
    expect(onChange).toHaveBeenCalledWith(7)
  })

  it('takes the compact size', () => {
    render(<RangeSegmentedControl value={7} onChange={() => {}} size="sm" />)
    expect(screen.getByRole('button', { name: '7d' })).toHaveClass('h-[22px]')
  })
})
