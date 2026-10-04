import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Chip } from './chip'
import { CodeToken } from './code-token'
import { CountBadge } from './count-badge'

describe('Chip taxonomy', () => {
  it('draws a status as a soft tone pill', () => {
    render(<Chip tone="success">Live</Chip>)
    const chip = screen.getByText('Live')
    expect(chip).toHaveClass('rounded-full', 'bg-success-soft', 'text-success', 'h-5')
    expect(chip).toHaveAttribute('data-tone', 'success')
  })

  it('draws a kind tag as a neutral outline with no fill', () => {
    render(<Chip variant="outline">SQL</Chip>)
    expect(screen.getByText('SQL')).toHaveClass('border-border', 'bg-transparent', 'text-fg-muted')
  })

  it('passes span props through', () => {
    render(<Chip title="Why" aria-label="Status: live">Live</Chip>)
    expect(screen.getByLabelText('Status: live')).toHaveAttribute('title', 'Why')
  })
})

describe('CountBadge', () => {
  it('is neutral unless urgent, and caps at max', () => {
    render(
      <>
        <CountBadge count={3} data-testid="calm" />
        <CountBadge count={12} max={9} urgent data-testid="loud" />
      </>,
    )
    expect(screen.getByTestId('calm')).toHaveTextContent('3')
    expect(screen.getByTestId('calm')).toHaveClass('bg-surface-active')
    expect(screen.getByTestId('loud')).toHaveTextContent('9+')
    expect(screen.getByTestId('loud')).toHaveClass('bg-destructive')
  })
})

describe('CodeToken', () => {
  it('renders an identifier as mono code, not a pill', () => {
    render(<CodeToken>prod_monthly</CodeToken>)
    const token = screen.getByText('prod_monthly')
    expect(token.tagName).toBe('CODE')
    expect(token).toHaveClass('mono', 'rounded-sm')
    expect(token).not.toHaveClass('rounded-full', 'font-semibold')
  })
})
