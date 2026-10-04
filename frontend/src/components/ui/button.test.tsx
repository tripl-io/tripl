import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Button } from './button'

// jsdom resolves no Tailwind, so the disabled look can only be read off the
// classes the variant emits; theme-contrast.test.ts holds the inks it names
// to AA on the surfaces it names.
describe('Button disabled look', () => {
  it('turns a disabled primary button neutral instead of fading it', () => {
    render(<Button disabled>Save</Button>)

    const button = screen.getByRole('button', { name: 'Save' })
    expect(button).toBeDisabled()
    expect(button.className).not.toContain('opacity-50')
    expect(button.className).toContain('disabled:bg-[var(--surface-hover)]')
    expect(button.className).toContain('disabled:text-[var(--fg-muted)]')
  })

  it('keeps a disabled ghost button unfilled, with legible faint text', () => {
    render(<Button variant="ghost" disabled>Cancel</Button>)

    const button = screen.getByRole('button', { name: 'Cancel' })
    expect(button.className).not.toContain('opacity-50')
    expect(button.className).not.toContain('disabled:bg-')
    expect(button.className).toContain('disabled:text-[var(--fg-faint)]')
  })
})

// The app's control scale: read off the classes too.
describe('Button sizes', () => {
  it('defaults to a 32px, 12.5px control on the shared control radius', () => {
    render(<Button>Save</Button>)
    const button = screen.getByRole('button', { name: 'Save' })
    expect(button).toHaveClass('h-8', 'text-body-sm', 'rounded-control')
    expect(button).not.toHaveClass('h-9')
    expect(button).not.toHaveClass('text-sm')
  })

  it('draws sm at 28px with 14px icons', () => {
    render(<Button size="sm">Filter</Button>)
    const button = screen.getByRole('button', { name: 'Filter' })
    expect(button).toHaveClass('h-7', "[&_svg:not([class*='size-'])]:size-3.5")
    expect(button).not.toHaveClass("[&_svg:not([class*='size-'])]:size-4")
  })
})
