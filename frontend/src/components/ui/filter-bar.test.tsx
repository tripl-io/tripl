import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { FilterBar, FilterSearch, FilterSelect } from './filter-bar'

const STATUS_OPTIONS = [
  { value: 'live', label: 'live' },
  { value: 'draft', label: 'draft' },
]

function renderBar({ status = 'any', onClear = vi.fn() } = {}) {
  render(
    <FilterBar count="42 events" active={status !== 'any'} onClear={onClear}>
      <FilterSearch things="events" value="" onValueChange={() => {}} />
      <FilterSelect label="Status" value={status} onValueChange={() => {}} options={STATUS_OPTIONS} />
    </FilterBar>,
  )
  return { onClear }
}

describe('FilterBar', () => {
  it('names the search box after its things and focuses it on "/"', () => {
    renderBar()
    const search = screen.getByRole('searchbox', { name: 'Search events' })
    expect(search).toHaveAttribute('placeholder', 'Search events…')

    fireEvent.keyDown(document.body, { key: '/' })
    expect(search).toHaveFocus()
  })

  it('shows an unset filter as a quiet "Label: any" chip and no clear link', () => {
    renderBar()
    const chip = screen.getByRole('combobox', { name: 'Status filter: any' })
    expect(chip).toHaveTextContent('Status:any')
    expect(chip).toHaveClass('border-dashed')
    expect(screen.queryByRole('button', { name: 'Clear filters' })).toBeNull()
    expect(screen.getByText('42 events')).toBeInTheDocument()
  })

  it('marks a set filter and offers "Clear filters"', () => {
    const { onClear } = renderBar({ status: 'live' })
    const chip = screen.getByRole('combobox', { name: 'Status filter: live' })
    expect(chip).toHaveTextContent('Status:live')
    expect(chip).toHaveClass('bg-accent-soft')
    expect(chip).not.toHaveClass('border-dashed')

    fireEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(onClear).toHaveBeenCalledTimes(1)
  })

  it('keeps the count live region mounted before there is a count', () => {
    const { rerender } = render(
      <FilterBar>
        <FilterSearch things="events" value="" onValueChange={() => {}} />
      </FilterBar>,
    )
    const region = document.querySelector('[data-slot="filter-bar"] [aria-live="polite"]')
    expect(region).toBeInTheDocument()
    expect(region).toBeEmptyDOMElement()

    rerender(
      <FilterBar count="3 events">
        <FilterSearch things="events" value="" onValueChange={() => {}} />
      </FilterBar>,
    )
    expect(document.querySelector('[data-slot="filter-bar"] [aria-live="polite"]')).toBe(region)
    expect(region).toHaveTextContent('3 events')
  })
})

describe('FilterBar below 640px', () => {
  const realMatchMedia = window.matchMedia

  function installNarrow() {
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      writable: true,
      value: (query: string) => ({
        matches: query.includes('max-width: 639'),
        media: query,
        onchange: null,
        addEventListener: () => {},
        removeEventListener: () => {},
        addListener: () => {},
        removeListener: () => {},
        dispatchEvent: () => false,
      }),
    })
  }

  afterEach(() => {
    Object.defineProperty(window, 'matchMedia', { configurable: true, writable: true, value: realMatchMedia })
  })

  it('folds the chips into a "Filters (n)" sheet and keeps the search in the row', () => {
    installNarrow()
    const onClear = vi.fn()
    render(
      <FilterBar count="42 events" active onClear={onClear}>
        <FilterSearch things="events" value="" onValueChange={() => {}} />
        <FilterSelect label="Status" value="live" onValueChange={() => {}} options={STATUS_OPTIONS} />
        <FilterSelect label="Kind" value="any" onValueChange={() => {}} options={STATUS_OPTIONS} />
      </FilterBar>,
    )

    expect(screen.getByRole('searchbox', { name: 'Search events' })).toBeInTheDocument()
    expect(screen.queryByRole('combobox')).toBeNull()
    // Only the set chip counts.
    const trigger = screen.getByRole('button', { name: 'Filters (1)' })

    fireEvent.click(trigger)
    const sheet = screen.getByRole('dialog', { name: 'Filters' })
    expect(within(sheet).getByRole('combobox', { name: 'Status filter: live' })).toBeInTheDocument()
    expect(within(sheet).getByRole('combobox', { name: 'Kind filter: any' })).toBeInTheDocument()
    expect(within(sheet).getByText('42 events')).toBeInTheDocument()

    fireEvent.click(within(sheet).getByRole('button', { name: 'Clear filters' }))
    expect(onClear).toHaveBeenCalledTimes(1)
    fireEvent.click(within(sheet).getByRole('button', { name: 'Done' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('shows no Filters button when there is nothing to fold', () => {
    installNarrow()
    render(
      <FilterBar>
        <FilterSearch things="events" value="" onValueChange={() => {}} />
      </FilterBar>,
    )
    expect(screen.queryByRole('button', { name: /^Filters/ })).toBeNull()
  })

  it('renders the chips in place on a wide screen', () => {
    renderBar({ status: 'live' })
    expect(screen.getByRole('combobox', { name: 'Status filter: live' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Filters/ })).toBeNull()
  })
})
