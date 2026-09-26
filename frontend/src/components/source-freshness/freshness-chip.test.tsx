import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { SourceFreshness } from '@/types'
import { FreshnessChip } from './freshness-chip'

const late: SourceFreshness = {
  status: 'late',
  lag_seconds: 7 * 3600,
  last_event_at: '2026-09-26T03:00:00Z',
  last_collection_at: '2026-09-26T09:55:00Z',
  expected_by: '2026-09-26T06:00:00Z',
}

describe('FreshnessChip (F16, #269)', () => {
  it('draws a late source as a warning pill with its lag', () => {
    render(<FreshnessChip freshness={late} />)
    const chip = screen.getByText('Data late · 7h')
    expect(chip).toHaveAttribute('data-tone', 'warning')
    expect(chip).toHaveAttribute('data-freshness', 'late')
    expect(chip.getAttribute('title')).toContain('expected within 3h')
  })

  it('draws an overdue scan as a danger pill', () => {
    render(<FreshnessChip freshness={{ ...late, status: 'overdue' }} />)
    expect(screen.getByText('Scan overdue')).toHaveAttribute('data-tone', 'danger')
  })

  it('names the scan in its accessible label when given one', () => {
    render(<FreshnessChip freshness={late} name="Orders" />)
    expect(screen.getByLabelText(/^Data late · 7h\. Orders: Newest event 7h ago/)).toBeInTheDocument()
  })

  it.each(['fresh', 'unknown'] as const)('renders nothing for a %s source', (status) => {
    const { container } = render(<FreshnessChip freshness={{ ...late, status }} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing without a reading', () => {
    const { container } = render(<FreshnessChip freshness={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })
})
