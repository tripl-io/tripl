import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { propertyDriftsApi, type PropertyDrift } from '@/api/propertyDrifts'
import { branchPropertyEntriesKey } from '@/lib/queryKeys'
import { PropertyDriftList } from './PropertyDriftList'

vi.mock('@/api/propertyDrifts', () => ({
  propertyDriftsApi: {
    list: vi.fn(),
    act: vi.fn(),
  },
}))

const list = vi.mocked(propertyDriftsApi.list)
const act = vi.mocked(propertyDriftsApi.act)

function drift(overrides: Partial<PropertyDrift>): PropertyDrift {
  return {
    id: 'd-1',
    variable_id: 'v-plan',
    variable_name: 'plan',
    event_id: 'ev-1',
    event_name: 'signup',
    scan_config_id: 'sc-1',
    kind: 'new_property',
    detail: { presence_rate: 0.2 },
    status: 'open',
    resolution_note: null,
    snoozed_until: null,
    resolved_at: null,
    resolved_by: null,
    detected_at: '2026-10-01T09:00:00Z',
    ...overrides,
  }
}

function renderList(
  props: Partial<Parameters<typeof PropertyDriftList>[0]> = {},
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <PropertyDriftList slug="demo" eventId="ev-1" {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('PropertyDriftList (F23)', () => {
  it('renders nothing when the event has no open drift', async () => {
    list.mockResolvedValue({ items: [], total: 0 })
    const { container } = renderList()
    await waitFor(() => expect(list).toHaveBeenCalledWith('demo', { eventId: 'ev-1', activeOnly: true }))
    expect(container).toBeEmptyDOMElement()
  })

  it("lists the event's drifts, most serious first, with what the scan saw", async () => {
    list.mockResolvedValue({
      items: [
        drift({ id: 'd-new', variable_name: 'coupon', kind: 'new_property', detail: { presence_rate: 0.125 } }),
        drift({
          id: 'd-missing',
          variable_name: 'plan',
          kind: 'missing_required',
          detail: { presence_rate: 0.4, threshold: 0.95 },
        }),
      ],
      total: 2,
    })
    renderList()
    const rows = await screen.findAllByRole('listitem')
    expect(rows).toHaveLength(2)
    const [first, second] = rows as [HTMLElement, HTMLElement]
    expect(within(first).getByText('${plan}')).toBeInTheDocument()
    expect(within(first).getByText('Missing required property')).toBeInTheDocument()
    expect(within(first).getByText('Required, but carried on 40% of rows (threshold 95%).')).toBeInTheDocument()
    expect(within(first).getByRole('button', { name: 'Make optional' })).toBeInTheDocument()
    expect(within(second).getByText('${coupon}')).toBeInTheDocument()
    expect(within(second).getByText("Carried on 12.5% of rows, but not on the event's property list.")).toBeInTheDocument()
    expect(within(second).getByRole('button', { name: 'Add to list' })).toBeInTheDocument()
    // The event page names no event per row: they are all this one.
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('accepts, snoozes and dismisses through the triage endpoint', async () => {
    list.mockResolvedValue({ items: [drift({ id: 'd-1' })], total: 1 })
    act.mockResolvedValue(drift({ id: 'd-1', status: 'accepted' }))
    renderList()

    fireEvent.click(await screen.findByRole('button', { name: 'Add to list' }))
    await waitFor(() => expect(act).toHaveBeenCalledWith('demo', 'd-1', { action: 'accept' }))

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(act).toHaveBeenCalledWith('demo', 'd-1', { action: 'false_positive' }))

    fireEvent.click(screen.getByRole('button', { name: 'Snooze 7d' }))
    await waitFor(() => expect(act).toHaveBeenCalledTimes(3))
    const [, , body] = act.mock.calls[2]!
    expect(body.action).toBe('snooze')
    const until = Date.parse((body as { snoozed_until: string }).snoozed_until)
    expect(until - Date.now()).toBeGreaterThan(6.9 * 24 * 60 * 60 * 1000)
  })

  it("refetches main's property list after Accept, so the grid drops the accepted property", async () => {
    list.mockResolvedValue({ items: [drift({ id: 'd-1' })], total: 1 })
    act.mockResolvedValue(drift({ id: 'd-1', status: 'accepted' }))
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    renderList({}, queryClient)

    fireEvent.click(await screen.findByRole('button', { name: 'Add to list' }))
    await waitFor(() => expect(act).toHaveBeenCalledWith('demo', 'd-1', { action: 'accept' }))
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: branchPropertyEntriesKey('demo', null) }),
    )
  })

  it('shows the error the server gave', async () => {
    list.mockResolvedValue({ items: [drift({ kind: 'type_change', event_id: null, detail: {} })], total: 1 })
    act.mockRejectedValue(new Error('Observed type is not a property type'))
    renderList({ eventId: undefined })
    fireEvent.click(await screen.findByRole('button', { name: 'Retype' }))
    expect(await screen.findByText('Observed type is not a property type')).toBeInTheDocument()
  })

  it('adds the type changes of the listed properties when given them', async () => {
    list.mockImplementation(async (_slug, filters) =>
      filters?.kind === 'type_change'
        ? {
            items: [
              drift({
                id: 't-price',
                variable_id: 'v-price',
                variable_name: 'price',
                event_id: null,
                event_name: null,
                kind: 'type_change',
                detail: { expected_type: 'string', observed_type: 'number' },
              }),
              drift({ id: 't-other', variable_id: 'v-other', variable_name: 'other', event_id: null, kind: 'type_change' }),
            ],
            total: 2,
          }
        : { items: [], total: 0 },
    )
    renderList({ variableIds: ['v-price'] })
    expect(await screen.findByText('${price}')).toBeInTheDocument()
    expect(screen.getByText('Typed string, but the scan saw number values.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retype to number' })).toBeInTheDocument()
    expect(screen.queryByText('${other}')).toBeNull()
    expect(list).toHaveBeenCalledWith('demo', { kind: 'type_change', activeOnly: true })
  })

  it('project-wide, names and links each drift to its event page', async () => {
    list.mockResolvedValue({
      items: [
        drift({ id: 'd-1' }),
        drift({ id: 't-1', event_id: null, event_name: null, kind: 'type_change', variable_name: 'price' }),
      ],
      total: 2,
    })
    renderList({ eventId: undefined })
    const link = await screen.findByRole('link', { name: 'signup' })
    expect(link.getAttribute('href')).toMatch(/\/p\/demo\/monitoring\/event\/ev-1$/)
    expect(screen.getByText('All events')).toBeInTheDocument()
    expect(list).toHaveBeenCalledWith('demo', { eventId: undefined, activeOnly: true })
  })

  it('leaves out a snooze that has not lapsed, and the actions for a reader', async () => {
    list.mockResolvedValue({
      items: [
        drift({ id: 'd-1' }),
        drift({
          id: 'd-2',
          variable_name: 'later',
          status: 'snoozed',
          snoozed_until: new Date(Date.now() + 60_000).toISOString(),
        }),
      ],
      total: 2,
    })
    renderList({ readOnly: true })
    expect(await screen.findByText('${plan}')).toBeInTheDocument()
    expect(screen.queryByText('${later}')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
  })
})
