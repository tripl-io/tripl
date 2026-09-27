import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { EventName } from './event-name'

describe('EventName', () => {
  // tripl-wkwv.5: acme-ios holds one event whose stored name is the empty
  // string, and this component returned it verbatim — so the enclosing <Link>
  // had no text, no accessible name and no clickable area.
  it('prints a placeholder for a name that would paint nothing', () => {
    const { container } = render(<EventName name="" />)

    expect(screen.getByText('(unnamed event)')).toBeInTheDocument()
    // Real text, not decoration: the whole point is that the surrounding link
    // gets an accessible name from it.
    expect(container.querySelector('[aria-hidden]')).toBeNull()
  })

  it('treats a whitespace-only name the same way', () => {
    render(<EventName name="   " />)

    expect(screen.getByText('(unnamed event)')).toBeInTheDocument()
  })

  // The other defect this component covers, and the one it was written for.
  // Both live here, and neither may eat the other.
  it('still marks empty segments in a name that has them', () => {
    render(<EventName name="spot::services" />)

    expect(screen.getByTitle('empty segment')).toBeInTheDocument()
    expect(screen.queryByText('(unnamed event)')).toBeNull()
  })

  it('renders an ordinary name unchanged', () => {
    render(<EventName name="checkout_completed" />)

    expect(screen.getByText('checkout_completed')).toBeInTheDocument()
    expect(screen.queryByTitle('empty segment')).toBeNull()
  })
})
