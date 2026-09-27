import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { DuplicateCheckResult } from '@/types'
import { DuplicateHints, DuplicateLiveRegion } from './DuplicateHints'

const result: DuplicateCheckResult = {
  duplicates: [{ event_id: 'ev-1', name: 'Paywall View', event_type_id: 'et-1', status: 'live', score: 0.94, reasons: ['similar name'] }],
  lint: [{ code: 'case', message: 'Screen events use snake_case.', suggestion: 'paywall_screen_view' }],
  suggestion: 'paywall_screen_view',
}

function renderHints(props: Partial<Parameters<typeof DuplicateHints>[0]> = {}) {
  return render(
    <MemoryRouter>
      <DuplicateHints slug="demo" name="Paywall Screen View" result={result} {...props} />
    </MemoryRouter>,
  )
}

describe('DuplicateHints (F12, #265)', () => {
  it('renders the match rows as plain text, not as live regions', () => {
    renderHints()
    const row = screen.getByText(/Looks like/)
    expect(row.closest('[role="status"], [aria-live]')).toBeNull()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('keeps the server’s order of matches', () => {
    renderHints({
      result: {
        duplicates: [
          { event_id: 'ev-1', name: 'Paywall View', event_type_id: 'et-1', status: 'live', score: 0.9, reasons: [] },
          { event_id: 'ev-2', name: 'Paywall Viewed', event_type_id: 'et-2', status: 'live', score: 0.99, reasons: [] },
        ],
        lint: [],
      },
    })
    expect(screen.getAllByText(/Looks like/).map(node => node.textContent)).toEqual([
      expect.stringContaining('Paywall View (90%)'),
      expect.stringContaining('Paywall Viewed (99%)'),
    ])
  })

  it('reads "Looks like <Name> (94%) — Open · Mark as replacement"', () => {
    const onMark = vi.fn()
    renderHints({ onMarkReplacement: onMark })

    expect(screen.getByText(/Looks like/)).toHaveTextContent('Looks like Paywall View (94%) — Open · Mark as replacement')
    expect(screen.getByRole('link', { name: 'Open Paywall View' })).toHaveAttribute('href', '/p/demo/monitoring/event/ev-1')
    fireEvent.click(screen.getByRole('button', { name: 'Mark as replacement' }))
    expect(onMark).toHaveBeenCalledWith(result.duplicates[0])
  })

  it('offers the suggested name only where the name can change', () => {
    const onUse = vi.fn()
    const { unmount } = renderHints({ onUseSuggestion: onUse })
    fireEvent.click(screen.getByRole('button', { name: 'Use suggested name' }))
    expect(onUse).toHaveBeenCalledWith('paywall_screen_view')
    unmount()

    renderHints()
    expect(screen.getByText('paywall_screen_view')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Use suggested name' })).not.toBeInTheDocument()
  })

  it('replaces the warning with what will happen once a match is marked', () => {
    const onClear = vi.fn()
    renderHints({
      onMarkReplacement: vi.fn(),
      replacement: { event_id: 'ev-1', name: 'Paywall View' },
      onClearReplacement: onClear,
    })
    expect(screen.queryByText(/Looks like/)).not.toBeInTheDocument()
    expect(screen.getByText(/Replaces “Paywall View”/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    expect(onClear).toHaveBeenCalled()
  })

  it('renders nothing when the catalog has nothing to say', () => {
    const { container } = renderHints({ result: { duplicates: [], lint: [] } })
    expect(container).toBeEmptyDOMElement()
  })

  it('compact: the best match and the first hint, no replacement action', () => {
    renderHints({ compact: true, onMarkReplacement: vi.fn() })
    expect(screen.getByText(/Looks like/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Mark as replacement' })).not.toBeInTheDocument()
  })
})

describe('DuplicateLiveRegion (F12, #265)', () => {
  it('is one polite region whose text is the count', () => {
    const { rerender } = render(<DuplicateLiveRegion count={0} />)
    const region = screen.getByRole('status')
    expect(region).toHaveAttribute('aria-live', 'polite')
    expect(region).toHaveTextContent('')

    rerender(<DuplicateLiveRegion count={2} />)
    expect(screen.getByRole('status')).toBe(region)
    expect(region).toHaveTextContent('2 possible duplicates')
  })
})
