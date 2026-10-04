import { useEffect, useState } from 'react'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { formatDate } from '@/lib/datetime'
import type { AlertDelivery, AlertDeliveryListResponse, Role } from '@/types'

import { AlertAuditPanel } from './AlertAuditPanel'
import type { DeliveryFilters } from './AlertAuditPanel'
import { type Persona } from '@/test/persona'
import { PersonaProject } from '@/test/PersonaProject'

const NO_FILTERS: DeliveryFilters = {
  status: '',
  channel: '',
  destination_id: '',
  rule_id: '',
  scan_config_id: '',
  date_from: '',
  date_to: '',
}

function mockDelivery(overrides: Partial<AlertDelivery> = {}): AlertDelivery {
  return {
    id: 'delivery-1',
    project_id: 'project-1',
    scan_config_id: 'scan-1',
    scan_job_id: null,
    destination_id: 'destination-1',
    rule_id: 'rule-1',
    destination_name: 'Ops',
    rule_name: 'Spike alerts',
    scan_name: 'Main scan',
    status: 'sent',
    channel: 'telegram',
    matched_count: 1,
    payload_snapshot: null,
    error_message: null,
    is_local: false,
    is_simulated: false,
    created_at: '2026-08-12T12:02:46Z',
    updated_at: '2026-08-12T12:09:13Z',
    sent_at: '2026-08-12T12:09:13Z',
    ...overrides,
  }
}

function page(count: number, total: number): AlertDeliveryListResponse {
  return {
    items: Array.from({ length: count }, (_, index) =>
      mockDelivery({ id: `delivery-${index + 1}` }),
    ),
    total,
    // The Delivery log pages by offset; the cursor is not read here.
    next_cursor: null,
  }
}

interface HarnessProps {
  deliveries?: AlertDeliveryListResponse
  isLoading?: boolean
  isError?: boolean
  initialFilters?: DeliveryFilters
  initialOffset?: number
  onFilters?: (filters: DeliveryFilters) => void
  onOffset?: (offset: number) => void
}

/**
 * The panel with the state the page owns, held for real.
 *
 * Both halves of this section are about state the panel does not own — the
 * offset lives on ProjectAlertingTab and comes back down as a prop — so a
 * harness that actually holds it is the only way to assert what a Newer/Older
 * click does to the sentence above it.
 */
function Harness({
  deliveries,
  isLoading = false,
  isError = false,
  initialFilters = NO_FILTERS,
  initialOffset = 0,
  onFilters,
  onOffset,
}: HarnessProps) {
  const [filters, setFilters] = useState<DeliveryFilters>(initialFilters)
  const [offset, setOffset] = useState(initialOffset)

  useEffect(() => {
    onFilters?.(filters)
  }, [filters, onFilters])
  useEffect(() => {
    onOffset?.(offset)
  }, [offset, onOffset])

  return (
    <AlertAuditPanel
      slug="demo"
      deliveries={deliveries}
      isLoading={isLoading}
      isError={isError}
      pinnedDelivery={null}
      deliveryFilters={filters}
      // What the page does: one write that sets the filters and drops the
      // offset together.
      onDeliveryFiltersChange={next => {
        setFilters(next)
        setOffset(0)
      }}
      deliveryOffset={offset}
      onDeliveryOffsetChange={setOffset}
      deliveryLimit={2}
      destinations={[]}
      allRules={[]}
      scans={[]}
    />
  )
}

/** A session at one role — the panel and its rows read it from this context. */
function authValue(role: Role): AuthContextValue {
  return {
    user: {
      id: 'user-1',
      email: 'someone@example.com',
      name: 'Someone',
      role,
      is_platform_admin: false,
      orgs: [],
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    status: 'authenticated',
    error: null,
    isLoggingOut: false,
    logout: async () => {},
    refresh: () => {},
  }
}

function renderPanel(props: HarnessProps = {}, role: Persona = 'member') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // Router and query client because each rendered row is an AlertDeliveryRow,
  // which links to the scope it fired on and lazily fetches its own detail.
  return render(
    <AuthContext.Provider value={authValue(role === 'viewer' ? 'member' : role)}>
      <PersonaProject persona={role}>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter>
            <Harness {...props} />
          </MemoryRouter>
        </QueryClientProvider>
      </PersonaProject>
    </AuthContext.Provider>,
  )
}

/** The range is one "Sent" chip now; its two inputs live in a popover. */
function openDates() {
  fireEvent.click(screen.getByRole('button', { name: /^Sent filter:/ }))
}

/**
 * Picks the 12th of the month the calendar opens on (AL-19: the app's
 * DatePicker, not a native date input). With nothing picked it opens on the
 * current month, so the day is always there; returns the day as `YYYY-MM-DD`.
 */
async function pickTwelfth(label: 'From' | 'To'): Promise<string> {
  fireEvent.click(screen.getByLabelText(label))
  const grid = await screen.findByRole('grid')
  fireEvent.click(within(grid).getByRole('button', { name: / 12, \d{4}$/ }))
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-12`
}

// Loading and empty shared one branch, so a request that had not answered
// rendered the same sentence as one that answered "nothing" — and the sentence
// asserted the second. IncidentDeliveries.tsx gets this right
// and says why at :46-47.
describe('AlertAuditPanel states', () => {
  it('says it is still loading rather than that nothing was ever sent', () => {
    renderPanel({ isLoading: true })

    expect(screen.getByText('Loading deliveries…')).toBeInTheDocument()
    expect(screen.queryByText('No deliveries yet.')).toBeNull()
    // Nor "0 deliveries" in the header before the first answer.
    expect(screen.queryByText('0 deliveries')).toBeNull()
  })

  it('says the request failed rather than that nothing was ever sent', () => {
    renderPanel({ isError: true })

    expect(screen.getByRole('alert')).toHaveTextContent('Could not load the delivery log')
    expect(screen.queryByText('No deliveries yet.')).toBeNull()
  })

  it('says nothing has been sent only when nothing has been sent', () => {
    renderPanel({ deliveries: page(0, 0) })

    expect(screen.getByText('No deliveries yet.')).toBeInTheDocument()
  })

  it('blames the filter, not the project, when a filter matched nothing', () => {
    // The exact production shape: Status=Failed on a healthy project reported
    // "No deliveries yet." over a project that had delivered 115 times.
    renderPanel({
      deliveries: page(0, 0),
      initialFilters: { ...NO_FILTERS, status: 'failed' },
    })

    expect(
      screen.getByText('No deliveries match these filters. Clear them to see the full log.'),
    ).toBeInTheDocument()
    expect(screen.queryByText('No deliveries yet.')).toBeNull()
  })

  it('keeps the loaded page on screen while the next one is in flight', () => {
    // With keepPreviousData the reader should keep reading, not watch the table
    // blink to "Loading…" on every Older click.
    renderPanel({ deliveries: page(2, 5), isLoading: true })

    expect(screen.queryByText('Loading deliveries…')).toBeNull()
    expect(screen.getAllByRole('button', { name: 'Expand delivery details' })).toHaveLength(2)
  })
})

// The panel said "115 deliveries", rendered 50 and never mentioned the other 65.
// The oldest row on screen was four days back, so a reader who scrolled to the
// bottom concluded their alert had never been sent.
describe('AlertAuditPanel paging', () => {
  it('states that the list is truncated instead of ending silently', () => {
    renderPanel({ deliveries: page(2, 5) })

    expect(
      screen.getByText(
        'Showing the most recent 2 of 5 deliveries — use Older to reach the rest, or narrow the filter.',
      ),
    ).toBeInTheDocument()
  })

  it('reaches the older deliveries and says where the reader now is', () => {
    renderPanel({ deliveries: page(2, 5) })

    fireEvent.click(screen.getByRole('button', { name: 'Older' }))

    expect(screen.getByText('Showing 3–4 of 5 deliveries.')).toBeInTheDocument()
  })

  it('steps back by exactly one page and never past the first', () => {
    // Clamped at 0 rather than allowed to go negative: a negative offset is a
    // 422 from the endpoint, which would read as "the log broke".
    const onOffset = vi.fn()
    renderPanel({ deliveries: page(2, 5), initialOffset: 2, onOffset })

    fireEvent.click(screen.getByRole('button', { name: 'Newer' }))

    expect(onOffset).toHaveBeenLastCalledWith(0)
    expect(screen.getByRole('button', { name: 'Newer' })).toBeDisabled()
  })

  it('offers no paging when the whole log is on screen', () => {
    renderPanel({ deliveries: page(2, 2) })

    expect(screen.queryByRole('button', { name: 'Older' })).toBeNull()
    expect(screen.queryByText(/Showing/)).toBeNull()
  })
})

// The backend has accepted date_from/date_to all along and the page passed
// neither, so the only way to reach a delivery from last Tuesday was to page
// past everything in between.
describe('AlertAuditPanel date range', () => {
  it('pins To to the END of its day, so the day asked for is included', async () => {
    const onFilters = vi.fn()
    renderPanel({ deliveries: page(2, 5), onFilters })

    openDates()
    await pickTwelfth('To')

    const written = onFilters.mock.lastCall?.[0] as DeliveryFilters
    const bound = new Date(written.date_to)
    // Local parts, because the input speaks the reader's calendar day and a
    // midnight bound would drop every delivery sent on the day they asked for.
    expect(bound.getDate()).toBe(12)
    expect(bound.getHours()).toBe(23)
  })

  it('pins From to the START of its day', async () => {
    const onFilters = vi.fn()
    renderPanel({ deliveries: page(2, 5), onFilters })

    openDates()
    await pickTwelfth('From')

    const written = onFilters.mock.lastCall?.[0] as DeliveryFilters
    const bound = new Date(written.date_from)
    expect(bound.getDate()).toBe(12)
    expect(bound.getHours()).toBe(0)
  })

  it('shows the chosen day back on the picker it came from', async () => {
    renderPanel({ deliveries: page(2, 5) })

    openDates()
    const picked = await pickTwelfth('From')

    expect(screen.getByLabelText('From')).toHaveAccessibleName(`From: ${formatDate(picked)}`)
  })
})

describe('AlertAuditPanel filters', () => {
  it('clears every filter at once', () => {
    const onFilters = vi.fn()
    renderPanel({
      deliveries: page(0, 0),
      initialFilters: { ...NO_FILTERS, status: 'failed', date_to: '2026-08-12T23:59:59.999Z' },
      onFilters,
    })

    fireEvent.click(screen.getByRole('button', { name: /Clear filters/ }))

    expect(onFilters).toHaveBeenLastCalledWith(NO_FILTERS)
    expect(screen.queryByRole('button', { name: /Clear filters/ })).toBeNull()
  })

  it('sends the reader back to the first page when the filter changes', async () => {
    // The offset is an index INTO the filtered set: narrowing 115 rows to 4
    // while parked on page 3 lands on a blank page that reads as "nothing
    // matches".
    const onOffset = vi.fn()
    renderPanel({ deliveries: page(2, 5), initialOffset: 2, onOffset })
    expect(onOffset).toHaveBeenLastCalledWith(2)

    // From is the app's DatePicker now, not a native date input, so
    // pick a day through its calendar rather than typing into it.
    openDates()
    await pickTwelfth('From')

    expect(onOffset).toHaveBeenLastCalledWith(0)
  })

  it('says how much the active filter left', () => {
    renderPanel({ deliveries: page(2, 5), initialFilters: { ...NO_FILTERS, status: 'failed' } })

    expect(screen.getByText('5 deliveries match the filter')).toBeInTheDocument()
  })
})

describe('AlertAuditPanel naming', () => {
  it('is a delivery log, not a second Audit', () => {
    // The sidebar already has an "Audit log" — the who-changed-what trail, a
    // different thing entirely — and nothing on this panel said what it was.
    // The `audit` section key stays as it is: every alert ever
    // sent carries a deep link built on it.
    renderPanel({ deliveries: page(2, 5) })

    expect(screen.getByText('Delivery log')).toBeInTheDocument()
    expect(
      screen.getByText(
        /Every alert this project actually sent — the deliveries behind the incidents in the Inbox\./,
      ),
    ).toBeInTheDocument()
  })
})

describe('AlertAuditPanel viewer gating', () => {
  const failedPage: AlertDeliveryListResponse = {
    items: [mockDelivery({ status: 'failed', error_message: 'Forbidden' })],
    total: 1,
    next_cursor: null,
  }

  it('leaves the log itself readable — filters and paging are not writes', () => {
    renderPanel({ deliveries: failedPage }, 'viewer')

    expect(screen.getByRole('combobox', { name: /^Status filter/ })).toBeEnabled()
    expect(screen.getByRole('button', { name: /^Sent filter:/ })).toBeEnabled()
    expect(screen.getByText('Ops')).toBeInTheDocument()
  })

  it('drops the one write it has, and says why once', () => {
    renderPanel({ deliveries: failedPage }, 'viewer')

    expect(screen.queryByRole('button', { name: 'Retry delivery' })).toBeNull()
    expect(screen.getAllByText(/you have the viewer role in this project/i)).toHaveLength(1)
  })

  it('says nothing of the sort to an editor, who gets the Retry', () => {
    renderPanel({ deliveries: failedPage }, 'member')

    expect(screen.getByRole('button', { name: 'Retry delivery' })).toBeEnabled()
    expect(screen.queryByText(/you have the viewer role in this project/i)).toBeNull()
  })
})

// A retry moving a row out of Status=Failed, or a destination deleted
// elsewhere, can shrink the log under the offset — page 3 then answers zero
// rows while `total` is still positive.
describe('AlertAuditPanel — a page emptied under the reader', () => {
  it('says the page is empty now, instead of that nothing was ever sent', () => {
    renderPanel({ deliveries: page(0, 3), initialOffset: 4 })

    expect(screen.getByText(/This page is now empty/)).toBeInTheDocument()
    expect(screen.queryByText('No deliveries yet.')).toBeNull()
  })

  it('keeps Newer on screen, and it lands on the last page that has rows', () => {
    const onOffset = vi.fn()
    renderPanel({ deliveries: page(0, 3), initialOffset: 4, onOffset })

    const newer = screen.getByRole('button', { name: 'Newer' })
    expect(newer).toBeEnabled()
    fireEvent.click(newer)

    // Limit 2, total 3: the last page with rows starts at 2.
    expect(onOffset).toHaveBeenLastCalledWith(2)
  })

  it('jumps past several empty pages in one step', () => {
    const onOffset = vi.fn()
    renderPanel({ deliveries: page(0, 3), initialOffset: 10, onOffset })

    fireEvent.click(screen.getByRole('button', { name: 'Newer' }))

    // One page back would be 8, still past the end; the last page is at 2.
    expect(onOffset).toHaveBeenLastCalledWith(2)
  })
})
