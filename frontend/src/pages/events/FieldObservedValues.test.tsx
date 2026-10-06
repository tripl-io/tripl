import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { EventFieldObservedValues } from '@/types'
import { FieldObservedValues } from './FieldObservedValues'

const TWO: EventFieldObservedValues = {
  distinct_count: 2,
  total_count: 100,
  values: [
    { value: 'map/main', count: 62, share: 0.62 },
    { value: 'spot/main', count: 38, share: 0.38 },
  ],
  other_count: 0,
  observed_at: '2026-10-05T09:00:00Z',
  scan_config_id: null,
}

function many(n: number, kept = n): EventFieldObservedValues {
  return {
    distinct_count: n,
    total_count: n * 10,
    values: Array.from({ length: kept }, (_, i) => ({
      value: `screen/${i}`,
      count: 10,
      share: 1 / n,
    })),
    other_count: (n - kept) * 10,
    observed_at: '2026-10-05T09:00:00Z',
    scan_config_id: null,
  }
}

describe('FieldObservedValues', () => {
  it('names the values with their shares and the scan date', () => {
    render(<FieldObservedValues observed={TWO} />)
    const line = screen.getByTestId('field-observed-values')
    expect(line).toHaveTextContent(
      /^Seen with 2 values in the scan of .*2026: map\/main \(62%\), spot\/main \(38%\)$/,
    )
  })

  it('marks the value the scan stored', () => {
    render(<FieldObservedValues observed={TWO} stored={{ value: 'map/main', isAuthored: false }} />)
    expect(screen.getByTestId('field-observed-values')).toHaveTextContent('map/main (62%, stored)')
  })

  it('does not mark a hand-set value as the scan’s choice', () => {
    render(<FieldObservedValues observed={TWO} stored={{ value: 'map/main', isAuthored: true }} />)
    expect(screen.getByTestId('field-observed-values')).not.toHaveTextContent('stored')
  })

  it('says the distribution is main’s on a branch', () => {
    render(<FieldObservedValues observed={TWO} onMain />)
    expect(screen.getByTestId('field-observed-values')).toHaveTextContent(
      'Seen with 2 values on main in the scan of',
    )
  })

  it('renders nothing for a single value or no observation', () => {
    const { container, rerender } = render(<FieldObservedValues observed={null} />)
    expect(container).toBeEmptyDOMElement()
    rerender(<FieldObservedValues observed={{ ...TWO, distinct_count: 1, values: TWO.values.slice(0, 1) }} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows three values and expands to the kept ones', () => {
    render(<FieldObservedValues observed={many(25, 20)} />)
    const line = screen.getByTestId('field-observed-values')
    expect(line).toHaveTextContent('screen/2')
    expect(line).not.toHaveTextContent('screen/3')
    // The button counts what it opens; the values past the cap are counted
    // apart, because no click can show them.
    expect(line).toHaveTextContent(/\+17 more · and 5 more not kept$/)
    fireEvent.click(screen.getByRole('button', { name: '+17 more' }))
    expect(line).toHaveTextContent('screen/19')
    // Past the 20 kept, the line can count the rest but not name them.
    expect(line).toHaveTextContent(/and 5 more$/)
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('offers no "not kept" count when every value was kept', () => {
    render(<FieldObservedValues observed={many(5)} />)
    const line = screen.getByTestId('field-observed-values')
    expect(screen.getByRole('button', { name: '+2 more' })).toBeInTheDocument()
    expect(line).not.toHaveTextContent('not kept')
  })

  it('names an empty value instead of leaving a blank', () => {
    const observed: EventFieldObservedValues = {
      ...TWO,
      values: [
        { value: 'map/main', count: 62, share: 0.62 },
        { value: '', count: 38, share: 0.38 },
      ],
    }
    const { rerender } = render(<FieldObservedValues observed={observed} />)
    expect(screen.getByTestId('field-observed-values')).toHaveTextContent(
      'map/main (62%), (empty) (38%)',
    )
    rerender(<FieldObservedValues observed={observed} compact />)
    expect(screen.getByTestId('field-observed-values')).toHaveTextContent(
      'Seen with 2 values: map/main 62% · (empty) 38%',
    )
  })

  it('leaves the share out when the counts are unknown', () => {
    render(
      <FieldObservedValues
        observed={{
          ...TWO,
          total_count: null,
          other_count: null,
          values: TWO.values.map(v => ({ ...v, count: null, share: null })),
        }}
      />,
    )
    const line = screen.getByTestId('field-observed-values')
    expect(line).toHaveTextContent('map/main, spot/main')
    expect(line).not.toHaveTextContent('%')
  })

  it('has a compact caption for the read view', () => {
    render(<FieldObservedValues observed={many(5)} compact />)
    expect(screen.getByTestId('field-observed-values')).toHaveTextContent(
      'Seen with 5 values: screen/0 20% · screen/1 20% · screen/2 20% · +2 more',
    )
    expect(screen.queryByRole('button')).toBeNull()
  })
})
