import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { FilterEditor } from './FilterEditor'
import type { RuleFilterDraft } from './constants'

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

// A production-sized catalog: the page is capped, `total` is not. The picker has
// to stay honest about the gap between the two.
const TOTAL_EVENTS = 2466

function mockEventsFetch(): string[] {
  const calls: string[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input)
    calls.push(url)
    if (/\/events\/evt-1(\?|$)/.test(url)) {
      return jsonResponse({ id: 'evt-1', name: 'checkout_started' })
    }
    if (url.includes('/events')) {
      const search = new URL(url, 'http://localhost').searchParams.get('search')
      const items = [
        { id: 'evt-1', name: 'checkout_started' },
        { id: 'evt-2', name: 'checkout_completed' },
      ].filter((event) => !search || event.name.includes(search))
      return jsonResponse({ items, total: search ? items.length : TOTAL_EVENTS })
    }
    throw new Error(`Unhandled fetch: ${url}`)
  })
  return calls
}

// acme-ios holds exactly one event whose stored `name` is the empty string, and
// both label sources have to survive it: the per-id read that labels an already
// selected value, and the searched page that labels the rows you pick from.
function mockBlankEventFetch() {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input)
    if (/\/events\/evt-blank(\?|$)/.test(url)) {
      return jsonResponse({ id: 'evt-blank', name: '' })
    }
    if (url.includes('/events')) {
      return jsonResponse({ items: [{ id: 'evt-blank', name: '' }], total: 1 })
    }
    throw new Error(`Unhandled fetch: ${url}`)
  })
}

function listCalls(calls: string[]) {
  return calls.filter((url) => /\/events\?/.test(url))
}

const DEFAULT_FILTERS: RuleFilterDraft[] = [
  { uid: 'filter-1', field: 'event', operator: 'in', values: ['evt-1'] },
]

function renderEventFilter(filters: RuleFilterDraft[] = DEFAULT_FILTERS) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <FilterEditor filters={filters} eventTypes={[]} slug="demo" onChange={() => {}} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('FilterEditor — event scope picker', () => {
  it('names an already-selected event without listing the catalog', async () => {
    // The tab used to pull `GET /events?limit=10000` on mount just to label the
    // ids a saved rule already carries. Now the label comes from one read per
    // selected id, and the list stays untouched until someone opens the picker.
    const calls = mockEventsFetch()
    renderEventFilter()

    expect(await screen.findByText('checkout_started')).toBeInTheDocument()
    expect(listCalls(calls)).toEqual([])
    expect(calls.some((url) => url.includes('limit=10000'))).toBe(false)
  })

  it('queries a capped page when the picker opens, and says how much is hidden', async () => {
    const calls = mockEventsFetch()
    renderEventFilter()
    await screen.findByText('checkout_started')

    fireEvent.click(screen.getByRole('button', { name: /1 selected/ }))

    await waitFor(() => expect(listCalls(calls)).toHaveLength(1))
    expect(listCalls(calls)[0]).toContain('limit=50')
    // No search term yet, so the first page is the plain head of the catalog.
    expect(listCalls(calls)[0]).not.toContain('search=')

    // 2 of 2,466 rendered — the remaining 2,464 are reachable by typing, and the
    // footer says so instead of letting the list look complete.
    expect(await screen.findByText(/2464 more match/)).toBeInTheDocument()
  })

  it('re-queries the server as the operator types', async () => {
    const calls = mockEventsFetch()
    renderEventFilter()
    await screen.findByText('checkout_started')

    fireEvent.click(screen.getByRole('button', { name: /1 selected/ }))
    await waitFor(() => expect(listCalls(calls)).toHaveLength(1))

    fireEvent.change(screen.getByLabelText('Search values'), {
      target: { value: 'completed' },
    })

    // Debounced, so the request follows the keystroke rather than racing it.
    await waitFor(() =>
      expect(listCalls(calls).some((url) => url.includes('search=completed'))).toBe(true),
    )
    // Server-side filtering: the browser never sees the rows it did not ask for.
    expect(await screen.findByText('checkout_completed')).toBeInTheDocument()
  })

  it('names an event whose stored name is blank, on the trigger and in the list', async () => {
    // `??` cannot catch this: `labelByValue.get()` returns '' as a HIT, so the
    // `?? value` fallback never fires and the collapsed trigger painted nothing
    // but a chevron — no text, and so no accessible name at all. Single-value
    // mode is where it bites hardest, because `eq`/`ne` render no chip row: the
    // trigger is the only place the selection is named.
    mockBlankEventFetch()
    renderEventFilter([
      { uid: 'filter-1', field: 'event', operator: 'eq', values: ['evt-blank'] },
    ])

    // Source 1 — the per-id read behind `selectedLabels`, which is what labels a
    // saved id the current page does not contain.
    const trigger = await screen.findByRole('button', { name: '(unnamed event)' })

    // Source 2 — the searched page behind `options`, which is what labels the
    // row you would click to change the selection. Two buttons carry the name
    // once the picker is open: the trigger and that row.
    fireEvent.click(trigger)
    await waitFor(() =>
      expect(screen.getAllByRole('button', { name: '(unnamed event)' })).toHaveLength(2),
    )
  })
})

describe('FilterEditor — icon buttons carry names (DS-13 / ALR-23)', () => {
  it('names each row remove button by position and field, and each chip by its value', async () => {
    mockEventsFetch()
    renderEventFilter([
      { uid: 'filter-1', field: 'event', operator: 'in', values: ['evt-1'] },
      { uid: 'filter-2', field: 'direction', operator: 'eq', values: [] },
    ])

    expect(screen.getByRole('button', { name: 'Remove filter 1: Event' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove filter 2: Direction' })).toBeInTheDocument()
    // The chip's X says which value it drops, not a bare "Remove value".
    expect(await screen.findByRole('button', { name: 'Remove checkout_started' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove value' })).toBeNull()
  })
})

// A catalog metric filter (JR-15): values are MetricDefinition ids, so the row
// must name them from the metrics catalog — not fall back to the direction
// picker and print raw uuids.
function mockMetricsFetch(): string[] {
  const calls: string[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input)
    calls.push(url)
    if (/\/metrics\/met-1(\?|$)/.test(url)) {
      return jsonResponse({ id: 'met-1', name: 'checkout_rate', display_name: 'Checkout rate' })
    }
    if (/\/metrics\?/.test(url)) {
      return jsonResponse({
        items: [
          { id: 'met-1', name: 'checkout_rate', display_name: 'Checkout rate' },
          { id: 'met-2', name: 'refund_count', display_name: '' },
        ],
        total: 2,
      })
    }
    throw new Error(`Unhandled fetch: ${url}`)
  })
  return calls
}

describe('FilterEditor — metric filter (JR-15)', () => {
  it('names a saved metric from the catalog, under the Metric field', async () => {
    mockMetricsFetch()
    renderEventFilter([
      { uid: 'filter-1', field: 'metric', operator: 'in', values: ['met-1'] },
    ])

    expect(await screen.findByText('Checkout rate')).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Filter field' })).toHaveTextContent('Metric')
    expect(screen.getByRole('button', { name: 'Remove filter 1: Metric' })).toBeInTheDocument()
    expect(screen.queryByText('met-1')).toBeNull()
  })

  it('lists catalog metrics, not directions, when the picker opens', async () => {
    const calls = mockMetricsFetch()
    renderEventFilter([
      { uid: 'filter-1', field: 'metric', operator: 'in', values: [] },
    ])

    fireEvent.click(screen.getByRole('button', { name: 'Choose metrics…' }))

    await waitFor(() =>
      expect(calls.some((url) => /\/metrics\?/.test(url) && url.includes('limit=50'))).toBe(true),
    )
    expect(await screen.findByText('Checkout rate')).toBeInTheDocument()
    // A blank display name falls back to the machine name.
    expect(screen.getByText('refund_count')).toBeInTheDocument()
    expect(screen.queryByText('Spike (up)')).toBeNull()
    expect(screen.getByLabelText('Search values')).toHaveAttribute('placeholder', 'Search metrics…')
  })
})
