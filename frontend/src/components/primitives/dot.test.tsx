import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Dot } from './dot'

describe('Dot', () => {
  it('is pure decoration when nothing names it', () => {
    const { container } = render(<Dot tone="success" />)

    expect(container.textContent).toBe('')
    expect(container.firstElementChild).toHaveAttribute('aria-hidden', 'true')
  })

  it('names its meaning when it stands alone, so colour is not the only cue', () => {
    render(<Dot tone="success" label="Status: Live" />)

    expect(screen.getByText('Status: Live')).toBeInTheDocument()
    expect(screen.getByTitle('Status: Live')).toHaveAttribute('aria-hidden', 'true')
  })
})
