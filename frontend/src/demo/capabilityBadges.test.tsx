import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { DemoDataBadge, SyntheticSourceBadge } from './capabilityBadges'

describe('capability badges', () => {
  it('labels a synthetic source distinctly from a real connection', () => {
    render(<SyntheticSourceBadge />)

    const badge = screen.getByText('Synthetic')
    expect(badge).toBeInTheDocument()
    expect(badge).toHaveAttribute('title', expect.stringContaining('not a real connection'))
  })

  it('marks the demo project as local synthetic data', () => {
    render(<DemoDataBadge />)
    expect(screen.getByText('Local synthetic data')).toBeInTheDocument()
  })
})
