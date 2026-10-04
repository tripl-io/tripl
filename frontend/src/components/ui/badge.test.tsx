import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Badge } from './badge'

describe('Badge danger tone', () => {
  it('draws a red state on the soft danger fill, not the solid count fill', () => {
    render(
      <>
        <Badge variant="danger">Failed</Badge>
        <Badge variant="destructive">3</Badge>
      </>,
    )

    // jsdom paints no Tailwind, so the variant is read off its token classes.
    expect(screen.getByText('Failed')).toHaveClass('bg-danger-soft', 'text-danger')
    expect(screen.getByText('Failed')).not.toHaveClass('bg-destructive')
    expect(screen.getByText('3')).toHaveClass('bg-destructive')
  })
})

describe('Badge as a Chip alias', () => {
  it('renders a neutral pill, not a solid brand block, when no variant is given', () => {
    render(<Badge>v1.2.0</Badge>)

    const badge = screen.getByText('v1.2.0')
    expect(badge).toHaveClass('rounded-full', 'bg-surface-hover', 'text-fg-muted')
    expect(badge).not.toHaveClass('bg-primary')
    expect(badge).toHaveAttribute('data-tone', 'neutral')
  })

  it('keeps the brand fill behind an explicit solid variant', () => {
    render(<Badge variant="solid">New</Badge>)
    expect(screen.getByText('New')).toHaveClass('bg-accent-solid', 'text-accent-solid-fg')
  })

  it('lets a call-site class override one property of the variant', () => {
    render(
      <Badge variant="outline" className="bg-warning-soft text-warning">
        Pre-release
      </Badge>,
    )
    const badge = screen.getByText('Pre-release')
    expect(badge).toHaveClass('bg-warning-soft', 'text-warning')
    expect(badge).not.toHaveClass('text-fg-muted')
  })
})
