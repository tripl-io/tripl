import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import type { SourceFreshnessItem } from '@/types'
import { SignalsHeldNotice } from './signals-held-notice'

function heldItem(id: string, status: 'late' | 'overdue', lagHours = 7): SourceFreshnessItem {
  return {
    id,
    name: `Scan ${id}`,
    data_source_id: 'ds-1',
    freshness: {
      status,
      lag_seconds: lagHours * 3600,
      last_event_at: '2026-09-26T03:00:00Z',
      last_collection_at: '2026-09-26T09:55:00Z',
      expected_by: '2026-09-26T06:00:00Z',
    },
  }
}

function renderNotice(items: SourceFreshnessItem[]) {
  return render(
    <MemoryRouter>
      <SignalsHeldNotice slug="demo" items={items} />
    </MemoryRouter>,
  )
}

describe('SignalsHeldNotice (F16, #269)', () => {
  it('says drop signals are held and links each late scan', () => {
    renderNotice([heldItem('a', 'late')])
    const notice = screen.getByRole('status')
    expect(notice).toHaveTextContent('Data late — drop signals held.')
    expect(notice).toHaveTextContent('(newest event 7h ago)')
    expect(screen.getByRole('link', { name: 'Scan a' })).toHaveAttribute('href', '/p/demo/scans/a')
  })

  it('leads with the overdue wording when every held scan is overdue', () => {
    renderNotice([heldItem('a', 'overdue'), heldItem('b', 'overdue')])
    expect(screen.getByRole('status')).toHaveTextContent('Scan overdue — drop signals held.')
    expect(screen.getByRole('status')).toHaveTextContent('Scan a (scan overdue) and Scan b (scan overdue)')
  })

  it('renders nothing when no scan is holding', () => {
    const { container } = renderNotice([])
    expect(container).toBeEmptyDOMElement()
  })
})
