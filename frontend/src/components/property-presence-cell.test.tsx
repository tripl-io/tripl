import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PropertyPresenceCell } from './property-presence-cell'

describe('PropertyPresenceCell', () => {
  it('flags a required property the event carries below the threshold', () => {
    render(<PropertyPresenceCell presenceRate={0.5} required suggestedRequired={false} threshold={0.9} />)
    const cell = screen.getByText('50%')
    expect(cell).toHaveAttribute(
      'title',
      "Carried by 50% of the event's rows at the last scan; the required threshold is 90%.",
    )
    expect(screen.getByText('below threshold')).toBeInTheDocument()
    expect(screen.queryByText('looks required')).not.toBeInTheDocument()
  })

  it('suggests Required for an optional property the event always carries, at the default threshold', () => {
    render(<PropertyPresenceCell presenceRate={0.99} required={false} suggestedRequired threshold={null} />)
    expect(screen.getByText('99%')).toHaveAttribute(
      'title',
      "Carried by 99% of the event's rows at the last scan; the required threshold is 95%.",
    )
    expect(screen.getByText('looks required')).toBeInTheDocument()
    expect(screen.queryByText('below threshold')).not.toBeInTheDocument()
  })

  it('says nothing was measured before a scan has run', () => {
    render(<PropertyPresenceCell presenceRate={null} required suggestedRequired={null} threshold={0.95} />)
    expect(screen.getByText('—')).toHaveAttribute('title', 'No scan has measured this yet.')
    expect(screen.queryByText('below threshold')).not.toBeInTheDocument()
    expect(screen.queryByText('looks required')).not.toBeInTheDocument()
  })
})
