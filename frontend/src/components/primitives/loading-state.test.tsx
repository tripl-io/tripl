import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { LoadingState } from './loading-state'

// loading text was not announced on most surfaces.
describe('LoadingState', () => {
  it('announces the default label as a status', () => {
    render(<LoadingState />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading…')
  })

  it('keeps the label for assistive tech when drawing skeleton rows', () => {
    const { container } = render(<LoadingState rows={3} label="Loading metrics…" />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading metrics…')
    expect(container.querySelectorAll('[data-slot="skeleton"]')).toHaveLength(3)
  })

  it('renders inline when asked', () => {
    render(<LoadingState as="span" />)
    expect(screen.getByRole('status').tagName).toBe('SPAN')
  })
})
