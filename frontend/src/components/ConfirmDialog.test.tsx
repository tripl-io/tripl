import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import ConfirmDialog from './ConfirmDialog'

describe('ConfirmDialog', () => {
  it('labels the safe answer "Cancel" by default', () => {
    render(
      <ConfirmDialog open title="Delete scan?" message="It cannot be undone." onConfirm={() => {}} onCancel={() => {}} />,
    )
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument()
  })

  it('names the safe answer for what it does when asked', () => {
    const onCancel = vi.fn()
    render(
      <ConfirmDialog
        open
        title="Leave without saving?"
        message="Your changes have not been saved."
        confirmLabel="Discard changes"
        cancelLabel="Keep editing"
        onConfirm={() => {}}
        onCancel={onCancel}
      />,
    )
    expect(screen.getByRole('alertdialog', { name: 'Leave without saving?' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
  })
})
