import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { initialsOf } from './initials'
import { UserAvatar } from './user-avatar'

describe('initialsOf', () => {
  it('takes the first letters of the first two words', () => {
    expect(initialsOf('John Smith Doe')).toBe('JS')
    expect(initialsOf('  ada   lovelace ')).toBe('AL')
  })

  it('takes two letters of a single word or an email', () => {
    expect(initialsOf('alice')).toBe('AL')
    expect(initialsOf('bob@example.com')).toBe('BO')
  })

  it('falls back to a dot for nothing', () => {
    expect(initialsOf('')).toBe('•')
    expect(initialsOf(null)).toBe('•')
    expect(initialsOf(undefined)).toBe('•')
  })
})

describe('UserAvatar', () => {
  it('always paints the AA-pinned --avatar-bg token', () => {
    const { container } = render(<UserAvatar name="Ada Lovelace" size={40} />)
    const avatar = container.firstElementChild as HTMLElement
    expect(avatar).toHaveTextContent('AL')
    expect(avatar).toHaveStyle({ background: 'var(--avatar-bg)' })
  })

  it('is decorative unless given a label', () => {
    const { container, rerender } = render(<UserAvatar name="Ada Lovelace" />)
    expect(container.firstElementChild).toHaveAttribute('aria-hidden', 'true')
    expect(screen.queryByRole('img')).toBeNull()

    rerender(<UserAvatar name="Ada Lovelace" label="Ada Lovelace" />)
    expect(screen.getByRole('img', { name: 'Ada Lovelace' })).toBeInTheDocument()
  })
})
