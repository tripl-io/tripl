import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { SignalVerdictDialog } from './SignalVerdictDialog'

function renderDialog(onClose: () => void, onConfirm = vi.fn()) {
  return render(
    <MemoryRouter>
      <SignalVerdictDialog
        verdict="real_issue"
        bucket="2026-09-25T18:00:00Z"
        scopeLabel="signup_completed"
        routed={false}
        pending={false}
        onConfirm={onConfirm}
        onClose={onClose}
      />
    </MemoryRouter>,
  )
}

describe('SignalVerdictDialog — unsaved note', () => {
  it('closes an untouched dialog at once', () => {
    const onClose = vi.fn()
    renderDialog(onClose)

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('asks before Escape drops a typed note', async () => {
    const onClose = vi.fn()
    renderDialog(onClose)

    fireEvent.change(screen.getByLabelText(/Note/), { target: { value: 'Checkout outage, 18:00-18:40' } })
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })

    expect(await screen.findByRole('alertdialog')).toHaveTextContent('Leave without saving?')
    expect(onClose).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('confirms the verdict without asking', () => {
    const onClose = vi.fn()
    const onConfirm = vi.fn()
    renderDialog(onClose, onConfirm)

    fireEvent.change(screen.getByLabelText(/Note/), { target: { value: 'Checkout outage' } })
    fireEvent.click(screen.getByRole('button', { name: 'Mark as a real issue' }))

    expect(onConfirm).toHaveBeenCalledWith({ expectedReason: null, note: 'Checkout outage' })
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })
})
