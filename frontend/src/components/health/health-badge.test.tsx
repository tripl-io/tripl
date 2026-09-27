// @vitest-environment jsdom
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { HealthBadge } from './health-badge'

describe('HealthBadge', () => {
  it('names itself "Health N of 100" and shows the bare number', () => {
    render(<HealthBadge score={72} grade="warning" />)
    const badge = screen.getByRole('img', { name: 'Health 72 of 100' })
    expect(badge).toHaveTextContent('72')
    expect(badge).toHaveAttribute('data-grade', 'warning')
    expect(badge).toHaveAttribute('data-tone', 'warning')
  })

  it('tones each grade', () => {
    const { rerender } = render(<HealthBadge score={91} grade="healthy" />)
    expect(screen.getByRole('img')).toHaveAttribute('data-tone', 'success')
    rerender(<HealthBadge score={12} grade="unhealthy" />)
    expect(screen.getByRole('img')).toHaveAttribute('data-tone', 'danger')
  })

  it('derives the grade from the score when the server sent none', () => {
    render(<HealthBadge score={80} />)
    expect(screen.getByRole('img', { name: 'Health 80 of 100' })).toHaveAttribute('data-grade', 'healthy')
  })

  it('rounds a fractional score', () => {
    render(<HealthBadge score={49.6} />)
    expect(screen.getByRole('img', { name: 'Health 50 of 100' })).toHaveAttribute('data-grade', 'warning')
  })
})
