import { act, fireEvent, render, screen } from '@testing-library/react'
import { Trash2 } from 'lucide-react'
import { describe, expect, it, vi } from 'vitest'
import { IconButton } from './icon-button'

describe('IconButton', () => {
  it('is named by its label and works without an app-level TooltipProvider', () => {
    const onClick = vi.fn()
    render(
      <IconButton label="Remove filter" onClick={onClick}>
        <Trash2 aria-hidden="true" />
      </IconButton>,
    )
    const button = screen.getByRole('button', { name: 'Remove filter' })
    expect(button).toHaveAttribute('type', 'button')
    fireEvent.click(button)
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('shows its label as a tooltip on keyboard focus', async () => {
    render(
      <IconButton label="Close preview">
        <Trash2 aria-hidden="true" />
      </IconButton>,
    )
    const button = screen.getByRole('button', { name: 'Close preview' })
    act(() => button.focus())
    expect(await screen.findByRole('tooltip')).toHaveTextContent('Close preview')
  })
})
