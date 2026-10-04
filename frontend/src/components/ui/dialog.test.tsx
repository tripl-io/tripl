import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from './dialog'
import { focusFirstInvalid } from '@/components/forms/validation'

describe('Dialog layout', () => {
  it('lets only the body scroll, with the header and footer pinned', () => {
    render(
      <Dialog open>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New rule</DialogTitle>
            <DialogDescription>Alert when a metric moves.</DialogDescription>
          </DialogHeader>
          <DialogBody data-testid="body">Long form</DialogBody>
          <DialogFooter>
            <button type="button">Create</button>
          </DialogFooter>
        </DialogContent>
      </Dialog>,
    )

    const dialog = screen.getByRole('dialog', { name: 'New rule' })
    // jsdom resolves no Tailwind, so the layout is read off the classes.
    expect(dialog).toHaveClass('flex', 'flex-col', 'bg-popover', 'rounded-card')
    expect(dialog).not.toHaveClass('bg-background')
    expect(screen.getByTestId('body')).toHaveClass('min-h-0', 'flex-1', 'overflow-y-auto')
    expect(screen.getByRole('button', { name: 'Create' }).parentElement).toHaveClass('shrink-0')
  })
})

// The helper every dialog form calls (forms/validation), not a copy of it.
describe('focusFirstInvalid', () => {
  it('focuses the first invalid field and reports whether it found one', () => {
    render(
      <>
        <form data-testid="form">
          <input aria-label="Name" />
          <input aria-label="Threshold" aria-invalid="true" />
          <input aria-label="Window" aria-invalid="true" />
        </form>
        <form data-testid="valid-form">
          <input aria-label="Label" />
        </form>
      </>,
    )

    expect(focusFirstInvalid(screen.getByTestId('form'))).toBe(true)
    expect(screen.getByRole('textbox', { name: 'Threshold' })).toHaveFocus()
    expect(focusFirstInvalid(screen.getByTestId('valid-form'))).toBe(false)
  })
})
