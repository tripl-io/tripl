import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import type { AlertDelivery, AlertDeliveryDetail, AlertDeliveryItem, Role } from '@/types'
import { formatDateTime, shortTimestampParts } from '@/lib/datetime'
import { AlertDeliveryRow } from './AlertDeliveryRow'
import type { RetryWatchOptions } from './retryWatch'
import { type Persona } from '@/test/persona'
import { PersonaProject } from '@/test/PersonaProject'

// Stubbed so a retry's success can be asserted as the sentence a reader sees.
const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))
vi.mock('sonner', () => ({
  toast: { success: toastSuccess, error: toastError },
  Toaster: () => null,
}))

function mockJsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
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
    status: 'failed',
    channel: 'slack',
    is_local: false,
    is_simulated: false,
    matched_count: 1,
    payload_snapshot: null,
    error_message: 'Webhook failed',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    sent_at: null,
    ...overrides,
  }
}

function mockItem(overrides: Partial<AlertDeliveryItem> = {}): AlertDeliveryItem {
  return {
    id: 'item-1',
    delivery_id: 'delivery-1',
    scope_type: 'event',
    scope_ref: 'checkout_completed',
    scope_name: 'checkout_completed',
    event_type_id: null,
    event_id: 'event-1',
    bucket: '2026-01-01T00:00:00Z',
    direction: 'drop',
    actual_count: 12,
    expected_count: 40,
    absolute_delta: 28,
    percent_delta: 70,
    details_path: null,
    monitoring_path: null,
    drift_field: null,
    drift_type: null,
    sample_value: null,
    correlation_group_id: null,
    ...overrides,
  }
}

// The frozen payload the worker writes at send time. The row reads its summary
// from here rather than from the message text, so most of these tests describe
// what this object does to the table cell.
function mockPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    rendered_message:
      '[tripl] 1 alerts\nProject delivery via telegram: Ops\nRule: Spike alerts\nScan: Main scan\n\n- Event checkout_completed: down, actual=12, expected=40',
    items: [
      {
        scope_type: 'event',
        scope_ref: 'checkout_completed',
        scope_name: 'checkout_completed',
        direction: 'drop',
        actual_count: 12,
        expected_count: 40,
        absolute_delta: 28,
        percent_delta: 70,
      },
    ],
    ...overrides,
  }
}

/**
 * A session at one role, for the tests that care.
 *
 * The row reads its role from this context; omitting the provider is the
 * "no role information" case, which keeps the write control (lib/permissions.ts).
 */
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

function renderRow(
  delivery: AlertDelivery,
  focusDeliveryId?: string,
  focusItemKey?: string,
  role: Persona = 'member',
  // Off unless a test is about it: a watch left running would look the
  // delivery up after its test ended, against the next test's fetch stub.
  retryWatch: RetryWatchOptions = { attempts: 0 },
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  // Router, because the row links to the scope an alert fired on with <Link>
  // for in-app navigation — in the app it is always mounted under one.
  return {
    // Returned so the retry tests can assert what landed in the cache: the
    // pinned deep-linked row is a SEPARATE query from the list, and the retry
    // bug was entirely about which of the two got written.
    queryClient,
    ...render(
      <AuthContext.Provider value={authValue(role === 'viewer' ? 'member' : role)}>
        <PersonaProject persona={role}>
          <QueryClientProvider client={queryClient}>
            <MemoryRouter>
              <table>
                <tbody>
                  <AlertDeliveryRow
                    slug="demo"
                    delivery={delivery}
                    focusDeliveryId={focusDeliveryId}
                    focusItemKey={focusItemKey}
                    retryWatch={retryWatch}
                  />
                </tbody>
              </table>
            </MemoryRouter>
          </QueryClientProvider>
        </PersonaProject>
      </AuthContext.Provider>,
    ),
  }
}

// Expanding the row is what fires the detail GET, so every items-table test
// stubs that one endpoint and then clicks the chevron.
function expandRow(detail: AlertDeliveryDetail) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input)
    if (url.endsWith('/alert-deliveries/delivery-1') && (init?.method ?? 'GET') === 'GET') {
      return mockJsonResponse(detail)
    }
    throw new Error(`Unhandled fetch: ${init?.method} ${url}`)
  })
  renderRow(detail)
  fireEvent.click(screen.getByRole('button', { name: 'Expand delivery details' }))
}

afterEach(() => {
  vi.restoreAllMocks()
  toastSuccess.mockClear()
  toastError.mockClear()
})

describe('AlertDeliveryRow retry', () => {
  it('renders a Retry control only for failed deliveries', () => {
    renderRow(mockDelivery({ status: 'sent', error_message: null }))
    expect(screen.queryByRole('button', { name: 'Retry delivery' })).toBeNull()
  })

  it('shows the Retry control for failed deliveries', () => {
    renderRow(mockDelivery({ status: 'failed' }))
    expect(screen.getByRole('button', { name: 'Retry delivery' })).toBeInTheDocument()
  })

  it('withholds it from a viewer, whose retry is a 403', () => {
    // Re-queuing puts a real message back in somebody's channel, so the route
    // is editor-only. The expander stays: reading what was sent is not a write.
    renderRow(mockDelivery({ status: 'failed' }), undefined, undefined, 'viewer')

    expect(screen.queryByRole('button', { name: 'Retry delivery' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Expand delivery details' })).toBeEnabled()
  })

  it('re-queues a failed delivery via the retry endpoint', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/alert-deliveries/delivery-1/retry') && init?.method === 'POST') {
        return mockJsonResponse({ ...mockDelivery({ status: 'pending', error_message: null }), items: [] })
      }
      throw new Error(`Unhandled fetch: ${init?.method} ${url}`)
    })

    renderRow(mockDelivery({ status: 'failed' }))

    fireEvent.click(screen.getByRole('button', { name: 'Retry delivery' }))

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        '/api/v1/projects/demo/alert-deliveries/delivery-1/retry',
        expect.objectContaining({ method: 'POST' }),
      )
    })
  })

  it('flips the row to pending without waiting for the list to refetch', async () => {
    // The second click was the actual bug: the row still read `failed`, so the
    // reader retried an already-queued delivery and the 409 rendered
    // "Only failed deliveries can be retried" for a retry that had worked.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/alert-deliveries/delivery-1/retry') && init?.method === 'POST') {
        return mockJsonResponse({
          ...mockDelivery({
            status: 'pending',
            error_message: null,
            updated_at: '2026-01-02T00:00:00Z',
          }),
          items: [],
        })
      }
      throw new Error(`Unhandled fetch: ${init?.method} ${url}`)
    })

    renderRow(mockDelivery({ status: 'failed' }))

    fireEvent.click(screen.getByRole('button', { name: 'Retry delivery' }))

    await waitFor(() => {
      expect(screen.getByText('pending')).toBeInTheDocument()
    })
    expect(screen.queryByRole('button', { name: 'Retry delivery' })).toBeNull()
  })

  it('writes the retried delivery into the pinned row cache, not only the list', async () => {
    // The deep-linked row the page pins lives under ['alertDelivery', slug, id];
    // invalidating ['alertDeliveries', slug] never touched it.
    const retried = {
      ...mockDelivery({ status: 'pending', error_message: null, updated_at: '2026-01-02T00:00:00Z' }),
      items: [],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/alert-deliveries/delivery-1/retry') && init?.method === 'POST') {
        return mockJsonResponse(retried)
      }
      throw new Error(`Unhandled fetch: ${init?.method} ${url}`)
    })

    const { queryClient } = renderRow(mockDelivery({ status: 'failed' }))

    fireEvent.click(screen.getByRole('button', { name: 'Retry delivery' }))

    await waitFor(() => {
      expect(
        queryClient.getQueryData<AlertDeliveryDetail>(['alertDelivery', 'demo', 'delivery-1']),
      ).toMatchObject({ status: 'pending', error_message: null })
    })
  })

  it('invalidates the incident queue this delivery is counted in', async () => {
    // A retry moves a delivery from `failed` to `pending`, and the Inbox card
    // that groups it prints that status in its delivery count. Invalidating
    // ['alertDeliveries', slug] alone left the incident reporting the failure.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/alert-deliveries/delivery-1/retry') && init?.method === 'POST') {
        return mockJsonResponse({
          ...mockDelivery({ status: 'pending', error_message: null }),
          items: [],
        })
      }
      throw new Error(`Unhandled fetch: ${init?.method} ${url}`)
    })

    const { queryClient } = renderRow(mockDelivery({ status: 'failed' }))
    // Nothing observes the inbox in a row-only render, so what is asserted is
    // the mark that makes the next visit to the Inbox refetch rather than serve
    // this entry for another minute of its 60s staleTime.
    queryClient.setQueryData(['alertInbox', 'demo', ''], { pages: [], pageParams: [] })

    fireEvent.click(screen.getByRole('button', { name: 'Retry delivery' }))

    await waitFor(() =>
      expect(queryClient.getQueryState(['alertInbox', 'demo', ''])?.isInvalidated).toBe(true),
    )
    // The row's own detail write survives it: ['alertDelivery', slug, id] is a
    // different key, not a child of ['alertDeliveries', slug].
    expect(
      queryClient.getQueryData<AlertDeliveryDetail>(['alertDelivery', 'demo', 'delivery-1']),
    ).toMatchObject({ status: 'pending' })
  })

  it('surfaces a retry failure inline via role=alert', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/alert-deliveries/delivery-1/retry') && init?.method === 'POST') {
        return mockJsonResponse({ detail: 'Delivery is not in failed status' }, 409)
      }
      throw new Error(`Unhandled fetch: ${init?.method} ${url}`)
    })

    renderRow(mockDelivery({ status: 'failed' }))

    fireEvent.click(screen.getByRole('button', { name: 'Retry delivery' }))

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Retry failed: Delivery is not in failed status',
      )
    })
  })
})

// The widest column on the audit table used to show the first 87 characters of
// `rendered_message` — and all 100 production messages open with the same
// four-line template header repeating the five cells to its left, so the column
// carried nothing at all.
describe('AlertDeliveryRow what fired', () => {
  const sent = (payload: Record<string, unknown> | null) =>
    mockDelivery({ status: 'sent', error_message: null, payload_snapshot: payload })

  it('summarises what fired instead of repeating the message template header', () => {
    renderRow(sent(mockPayload()))

    expect(screen.getByText('↓ checkout_completed 70.0%')).toBeInTheDocument()
    expect(screen.queryByText(/Project delivery via telegram/)).toBeNull()
  })

  it('counts the scopes that did not fit', () => {
    renderRow(
      sent(
        mockPayload({
          items: [
            { scope_name: 'checkout_completed', direction: 'drop', percent_delta: 70, expected_count: 40 },
            { scope_name: 'signup_started', direction: 'spike', percent_delta: 104.6, expected_count: 70 },
            { scope_name: 'map_model_selected', direction: 'spike', percent_delta: 100.4, expected_count: 165 },
          ],
        }),
      ),
    )

    expect(screen.getByText('↓ checkout_completed 70.0%')).toBeInTheDocument()
    expect(screen.getByText('+2 more')).toBeInTheDocument()
  })

  it('shows the error instead of the summary on a failed delivery', () => {
    // The reason a delivery failed outranks what it would have said: this is the
    // one row on the page someone is looking for.
    renderRow(mockDelivery({ status: 'failed', payload_snapshot: mockPayload() }))

    expect(screen.getByText('Webhook failed')).toBeInTheDocument()
    expect(screen.queryByText('↓ checkout_completed 70.0%')).toBeNull()
  })

  it('says there was no baseline rather than claiming a 0% move', () => {
    // Same rule as the items table: a zero baseline has no percentage, and
    // printing 0.0% reports the largest possible relative move as the smallest.
    renderRow(
      sent(
        mockPayload({
          items: [
            { scope_name: 'first_launch', direction: 'spike', percent_delta: 0, expected_count: 0 },
          ],
        }),
      ),
    )

    expect(screen.getByText('↑ first_launch no baseline')).toBeInTheDocument()
  })

  it('falls back to a dash for a delivery whose payload froze no items', () => {
    // A payload written by an older worker must degrade to "no summary", not
    // crash the page someone opened because something was already wrong.
    renderRow(sent(null))

    expect(screen.queryByText(/[↑↓]/)).toBeNull()
  })
})

describe('AlertDeliveryRow timestamp', () => {
  it('splits the timestamp over two lines and keeps the full value on the cell', () => {
    // One string wrapped over FOUR lines in the nine-column table and inflated
    // every row to ~100px. Asserted through `formatDateTime` rather than a
    // literal so the test does not depend on the runner's locale or zone.
    const delivery = mockDelivery({ status: 'sent', error_message: null })
    renderRow(delivery)

    const full = formatDateTime(delivery.created_at)
    expect(screen.getByTitle(full)).toBeInTheDocument()
    expect(screen.queryByText(full)).toBeNull()
  })

  it('prints the two lines in the app locale and clock, like every other time', () => {
    // It used the browser's locale, so a German reader saw "1. Jan." and a
    // 24-hour clock here beside English dates on the rest of the page.
    const delivery = mockDelivery({ status: 'sent', error_message: null })
    renderRow(delivery)

    const parts = shortTimestampParts(delivery.created_at)
    if (!parts) throw new Error('the fixture has a readable created_at')
    const cell = screen.getByTitle(formatDateTime(delivery.created_at))
    expect(within(cell).getByText(parts.date)).toBeInTheDocument()
    // getByText collapses whitespace (ICU may emit a narrow no-break space).
    expect(within(cell).getByText(parts.time.replace(/\s+/g, ' '))).toBeInTheDocument()
    expect(parts.time).toMatch(/^\d{1,2}:\d{2}\s[AP]M$/)
  })
})

// `payload_snapshot.ai_explanation` is generated by an outbound LLM call and was
// populated on 100 of 100 production deliveries while being rendered nowhere at
// all — it reached the reader only inside the truncated preview.
describe('AlertDeliveryRow expanded payload', () => {
  it('renders the AI explanation as a block of its own', async () => {
    expandRow({
      ...mockDelivery({
        status: 'sent',
        error_message: null,
        payload_snapshot: mockPayload({ ai_explanation: 'Both map settings events roughly doubled in one hour.' }),
      }),
      items: [mockItem()],
    })

    expect(await screen.findByText('AI explanation')).toBeInTheDocument()
    expect(
      screen.getByText('Both map settings events roughly doubled in one hour.'),
    ).toBeInTheDocument()
  })

  it('omits the AI block when the rule generated no explanation', async () => {
    expandRow({
      ...mockDelivery({ status: 'sent', error_message: null, payload_snapshot: mockPayload() }),
      items: [mockItem()],
    })

    expect(await screen.findByText('checkout_completed')).toBeInTheDocument()
    expect(screen.queryByText('AI explanation')).toBeNull()
  })

  it('keeps the sent message in the expanded row rather than the table cell', async () => {
    expandRow({
      ...mockDelivery({ status: 'sent', error_message: null, payload_snapshot: mockPayload() }),
      items: [mockItem()],
    })

    expect(await screen.findByText('Message as sent')).toBeInTheDocument()
    expect(screen.getByText(/Project delivery via telegram: Ops/)).toBeInTheDocument()
  })
})

describe('AlertDeliveryRow items table', () => {
  it('explains an empty item list instead of rendering a bare header row', async () => {
    // The seeded demo failure has exactly this shape — matched_count copied from
    // the incident, zero items of its own — and used to render eight headers over
    // nothing beside a "4" in the Matched column.
    expandRow({ ...mockDelivery({ matched_count: 4 }), items: [] })

    expect(
      await screen.findByText(
        'No per-scope rows were stored with this attempt — its 4 matched scopes are recorded on the attempt that carried the same incident.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Scope' })).toBeNull()
  })

  it('says nothing about a sibling attempt when the delivery matched nothing', async () => {
    expandRow({ ...mockDelivery({ matched_count: 0 }), items: [] })

    expect(
      await screen.findByText('This delivery matched nothing, so it has no per-scope rows.'),
    ).toBeInTheDocument()
  })

  it('renders one row per item when the delivery owns its item list', async () => {
    expandRow({
      ...mockDelivery({ status: 'sent', error_message: null, matched_count: 2 }),
      items: [
        mockItem(),
        mockItem({ id: 'item-2', scope_ref: 'signup_started', scope_name: 'signup_started', direction: 'spike' }),
      ],
    })

    expect(await screen.findByText('checkout_completed')).toBeInTheDocument()
    expect(screen.getByText('signup_started')).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Scope' })).toBeInTheDocument()
    expect(screen.queryByText(/No per-scope rows were stored/)).toBeNull()
  })

  it('links each item to the scope it fired on', async () => {
    expandRow({ ...mockDelivery({ status: 'sent', error_message: null }), items: [mockItem()] })

    // Derived from the item's own scope rather than read from `monitoring_path`,
    // which alerts stopped filling once they began pointing at the incident.
    // Without this the card showed the alert and no way to look at the thing
    // that caused it.
    const link = await screen.findByRole('link', { name: 'Open checkout_completed' })
    expect(link).toHaveAttribute('href', '/p/demo/monitoring/event/checkout_completed')
  })

  it('labels the link by what it opens, not always "event"', async () => {
    expandRow({
      ...mockDelivery({ status: 'sent', error_message: null }),
      items: [
        mockItem({
          scope_type: 'event_type',
          scope_ref: 'type-1',
          scope_name: 'checkout',
          event_id: null,
        }),
      ],
    })

    const link = await screen.findByRole('link', { name: 'Open checkout' })
    expect(link).toHaveTextContent('event type')
    expect(link).toHaveAttribute('href', '/p/demo/monitoring/event-type/type-1')
  })

  it('offers one link, not the derived route and its stored twin', async () => {
    expandRow({
      ...mockDelivery({ status: 'sent', error_message: null }),
      items: [
        mockItem({
          // The stored path is the same destination the derived route builds, so
          // rendering both would put two links to one page in one cell.
          monitoring_path: 'https://tripl.example.com/p/demo/monitoring/event/checkout_completed',
        }),
      ],
    })

    await screen.findByRole('link', { name: 'Open checkout_completed' })
    expect(screen.queryByRole('link', { name: /^Monitoring for/ })).toBeNull()
  })

  it('falls back to the stored monitoring path when nothing can be derived', async () => {
    expandRow({
      ...mockDelivery({ status: 'sent', error_message: null }),
      items: [
        mockItem({
          scope_type: 'schema',
          scope_name: 'missing_field',
          event_id: null,
          monitoring_path: 'https://tripl.example.com/p/demo/monitoring/event/legacy',
        }),
      ],
    })

    expect(
      await screen.findByRole('link', { name: 'Monitoring for missing_field' }),
    ).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /^Open / })).toBeNull()
  })

  it.each([
    'https://tripl.example.com/p/demo/alerting/delivery-1?item=event:x',
    // Stored before the page moved out of /settings (#238).
    'https://tripl.example.com/p/demo/settings/alerting/delivery-1?item=event:x',
  ])('does not offer a link back to the page the reader is already on (%s)', async (detailsPath) => {
    expandRow({
      ...mockDelivery({ status: 'sent', error_message: null }),
      items: [
        mockItem({
          details_path: detailsPath,
        }),
      ],
    })

    await screen.findByRole('link', { name: 'Open checkout_completed' })
    // `details_path` is the incident deep link an alert message carries. It is
    // useful in telegram and pointless in a cell on the page it names.
    expect(screen.queryByRole('link', { name: /^Details for/ })).toBeNull()
  })

  it('falls back to a dash when the scope has nowhere to link', async () => {
    expandRow({
      ...mockDelivery({ status: 'sent', error_message: null }),
      items: [
        // A schema drift: no monitoring route of its own, and no event behind it.
        mockItem({ scope_type: 'schema', scope_name: 'missing_field', event_id: null }),
      ],
    })

    expect(await screen.findByText('missing_field')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /^Open / })).toBeNull()
  })

  it('shows the percentage for an item that had a baseline', async () => {
    expandRow({ ...mockDelivery({ status: 'sent', error_message: null }), items: [mockItem()] })

    expect(await screen.findByText('70.0%')).toBeInTheDocument()
  })

  it('says there was no baseline instead of claiming a 0% change', async () => {
    // The percent gate deliberately admits zero-baseline anomalies
    // and the stored percent_delta is 0.0 for them because the
    // ratio is undefined — the same value the alert message renders, so the two
    // must read the same.
    expandRow({
      ...mockDelivery({ status: 'sent', error_message: null }),
      items: [
        mockItem({
          direction: 'spike',
          actual_count: 137,
          expected_count: 0,
          absolute_delta: 137,
          percent_delta: 0,
        }),
      ],
    })

    expect(await screen.findByText('no baseline')).toBeInTheDocument()
    expect(screen.queryByText('0.0%')).toBeNull()
  })
})

// The destination of the deep link an alert message carries for release
// regressions. Landing here has to SHOW the numbers the message quoted —
// otherwise the link is no better than the event page it replaced.
describe('AlertDeliveryRow deep link', () => {
  function stubDetail(detail: AlertDeliveryDetail) {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      if (String(input).endsWith('/alert-deliveries/delivery-1')) {
        return mockJsonResponse(detail)
      }
      throw new Error(`Unhandled fetch: ${String(input)}`)
    })
  }

  const releaseRegressionItem = mockItem({
    scope_type: 'release_regression',
    scope_name: 'spot:open:wind:',
    actual_count: 345,
    expected_count: 715.7,
    absolute_delta: 370.7,
    percent_delta: 51.8,
    drift_field: '15.7.5',
    drift_type: 'volume_drop',
    sample_value: '15.7.4',
  })

  it('opens the linked delivery without a click', async () => {
    // The link exists to show one delivery's per-scope rows. A collapsed row
    // hides exactly those, so arriving collapsed defeats the link.
    stubDetail({ ...mockDelivery({ status: 'sent', error_message: null }), items: [releaseRegressionItem] })
    renderRow(mockDelivery({ status: 'sent', error_message: null }), 'delivery-1')

    expect(await screen.findByText('spot:open:wind:')).toBeInTheDocument()
    expect(screen.getByText('345')).toBeInTheDocument()
    // Rounded for reading — these are occurrence counts, and the
    // stored float stays reachable in the cell's title.
    expect(screen.getByText('716')).toBeInTheDocument()
    expect(screen.getByTitle('715.7')).toBeInTheDocument()
  })

  it('stays collapsed when the link names a different delivery', async () => {
    renderRow(mockDelivery({ status: 'sent', error_message: null }), 'delivery-999')

    expect(
      screen.getByRole('button', { name: 'Expand delivery details' }),
    ).toHaveAttribute('aria-expanded', 'false')
  })

  it('says what the expected count is, so the page cannot re-teach the count reading', async () => {
    // "actual 345 / expected 715.7" side by side reads as two counts of one
    // thing, which is the misreading the alert message was changed to prevent.
    // The page the message links to must not undo that.
    stubDetail({ ...mockDelivery({ status: 'sent', error_message: null }), items: [releaseRegressionItem] })
    renderRow(mockDelivery({ status: 'sent', error_message: null }), 'delivery-1')

    expect(
      await screen.findByText(/Adoption-adjusted: 15\.7\.4's share of this scope at 15\.7\.5's own volume/),
    ).toBeInTheDocument()
    expect(screen.getByText(/share-for-share, not a raw count drop/)).toBeInTheDocument()
  })

  it('still offers no link in the Link column, however good the event_id looks', async () => {
    // The asymmetry with the Inbox card is deliberate, and this is where it is
    // recorded rather than in a comment nobody reads. The card's new "view
    // event volume" link sits on an identity line, where the reader is being
    // told WHAT fired; this cell sits immediately right of Actual / Expected /
    // Abs Δ / % Δ, where the column's whole meaning is "the page that shows
    // this item" — so a link labelled "event" beside the release numbers is
    // exactly the second, contradicting link the deny-set exists to suppress.
    // "—" is the honest answer on this surface, and the reader who wants the
    // event reaches it from the incident card the message already links to.
    stubDetail({
      ...mockDelivery({ status: 'sent', error_message: null }),
      items: [{ ...releaseRegressionItem, event_id: 'evt-1' }],
    })
    renderRow(mockDelivery({ status: 'sent', error_message: null }), 'delivery-1')

    const itemRow = (await screen.findByText('spot:open:wind:')).closest('tr')!
    expect(itemRow.querySelector('a')).toBeNull()
    expect(itemRow).toHaveTextContent('—')
  })

  it('leaves the expected count of every other scope unannotated', async () => {
    stubDetail({ ...mockDelivery({ status: 'sent', error_message: null }), items: [mockItem()] })
    renderRow(mockDelivery({ status: 'sent', error_message: null }), 'delivery-1')

    expect(await screen.findByText('checkout_completed')).toBeInTheDocument()
    expect(screen.queryByText(/Adoption-adjusted/)).toBeNull()
  })
})

// A telegram delivery carries up to 8 items (_MAX_ITEMS_PER_DELIVERY), and every
// one of those lines printed the same delivery-keyed URL. Landing on the row was
// therefore landing on 8 siblings with nothing marking the one the message
// quoted. `?item=<scope_type>:<scope_ref>` names it.
describe('AlertDeliveryRow per-item anchor', () => {
  const sent = () => mockDelivery({ status: 'sent', error_message: null, matched_count: 3 })

  // Three items, two of which share a scope_ref: a release regression's
  // scope_ref IS its event id, and one rule can include events AND release
  // regressions, so this shape is reachable, not contrived.
  const items: AlertDeliveryItem[] = [
    mockItem({ id: 'item-1', scope_type: 'event', scope_ref: 'evt-a', scope_name: 'Login' }),
    mockItem({
      id: 'item-2',
      scope_type: 'release_regression',
      scope_ref: 'evt-a',
      scope_name: 'spot:open:wind:',
    }),
    mockItem({
      id: 'item-3',
      scope_type: 'release_regression',
      scope_ref: 'evt-b',
      scope_name: 'spot:open:swell:',
    }),
  ]

  function stubItems() {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      if (String(input).endsWith('/alert-deliveries/delivery-1')) {
        return mockJsonResponse({ ...sent(), items })
      }
      throw new Error(`Unhandled fetch: ${String(input)}`)
    })
  }

  function rowFor(scopeName: string): HTMLElement {
    const cell = screen.getByText(scopeName).closest('tr')
    if (!cell) throw new Error(`No row for ${scopeName}`)
    return cell
  }

  it('marks the one row the alert line was about', async () => {
    stubItems()
    renderRow(sent(), 'delivery-1', 'release_regression:evt-b')

    expect(await screen.findByText('spot:open:swell:')).toBeInTheDocument()
    expect(rowFor('spot:open:swell:')).toHaveAttribute('aria-current', 'true')
    expect(rowFor('spot:open:wind:')).not.toHaveAttribute('aria-current')
    expect(rowFor('Login')).not.toHaveAttribute('aria-current')
  })

  it('names the marked row in text, not only as a tint', async () => {
    // A background tint among 8 rows is easy to miss and says nothing at all to
    // a screen reader, so the row states why it is marked.
    stubItems()
    renderRow(sent(), 'delivery-1', 'release_regression:evt-b')

    expect(await screen.findByText('from your alert')).toBeInTheDocument()
    expect(rowFor('spot:open:swell:')).toHaveTextContent('from your alert')
  })

  it('does not mark a sibling that merely shares the scope_ref', async () => {
    // The exact reason the anchor carries scope_type: `evt-a` belongs to both
    // the event item and the release regression in this delivery.
    stubItems()
    renderRow(sent(), 'delivery-1', 'release_regression:evt-a')

    expect(await screen.findByText('spot:open:wind:')).toBeInTheDocument()
    expect(rowFor('spot:open:wind:')).toHaveAttribute('aria-current', 'true')
    expect(rowFor('Login')).not.toHaveAttribute('aria-current')
  })

  it('marks nothing when the anchor names an item this delivery does not have', async () => {
    // Stale links must degrade to the delivery-level behaviour they had before
    // anchors existed, not mark an arbitrary row.
    stubItems()
    renderRow(sent(), 'delivery-1', 'release_regression:evt-gone')

    expect(await screen.findByText('spot:open:wind:')).toBeInTheDocument()
    expect(screen.queryByText('from your alert')).toBeNull()
  })

  it('ignores an anchor meant for a different delivery', async () => {
    stubItems()
    renderRow(sent(), 'delivery-999', 'release_regression:evt-b')

    expect(
      screen.getByRole('button', { name: 'Expand delivery details' }),
    ).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('from your alert')).toBeNull()
  })
})

// The full error lived only in a `title` on the truncated cell — invisible on
// touch and to most screen readers — and the expanded row never showed it, so
// on a phone nobody could read why a delivery failed before pressing Retry.
describe('AlertDeliveryRow — why it failed, in full', () => {
  it('puts the whole error message first in the expanded row', async () => {
    const message = 'Slack returned 404 channel_not_found: the webhook points at an archived channel'
    expandRow({ ...mockDelivery({ status: 'failed', error_message: message }), items: [] })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Why it failed')
    expect(alert).toHaveTextContent(message)
  })

  it('says nothing of the sort on a delivery that did not fail', async () => {
    expandRow({ ...mockDelivery({ status: 'sent', error_message: null }), items: [] })

    await screen.findByText(/This delivery matched nothing|No per-scope rows/)
    expect(screen.queryByText('Why it failed')).toBeNull()
  })
})

// `{open && detail && …}` rendered nothing while the detail loaded and nothing
// forever when it failed — the chevron turned over an empty row.
describe('AlertDeliveryRow — the expanded row while its detail is not there', () => {
  it('says it is loading', () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise<Response>(() => {}))
    renderRow(mockDelivery({ status: 'sent', error_message: null }))

    fireEvent.click(screen.getByRole('button', { name: 'Expand delivery details' }))

    expect(screen.getByRole('status')).toHaveTextContent('Loading delivery details…')
  })

  it('says the request failed, and offers to try again', async () => {
    let calls = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/alert-deliveries/delivery-1')) {
        calls += 1
        if (calls === 1) return mockJsonResponse({ detail: 'boom' }, 500)
        return mockJsonResponse({
          ...mockDelivery({ status: 'sent', error_message: null, matched_count: 0 }),
          items: [],
        })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderRow(mockDelivery({ status: 'sent', error_message: null }))

    fireEvent.click(screen.getByRole('button', { name: 'Expand delivery details' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/Could not load this delivery's details/)
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(
      await screen.findByText('This delivery matched nothing, so it has no per-scope rows.'),
    ).toBeInTheDocument()
  })
})

// One click re-posted to Slack, Jira or Linear — for the trackers, a new issue —
// and the only feedback was the badge changing, on a row that could then move
// off a Status=Failed page entirely.
describe('AlertDeliveryRow — a retry says what it did', () => {
  function mockRetry() {
    return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/alert-deliveries/delivery-1/retry') && init?.method === 'POST') {
        return mockJsonResponse({
          ...mockDelivery({ status: 'pending', error_message: null, updated_at: '2026-01-02T00:00:00Z' }),
          items: [],
        })
      }
      throw new Error(`Unhandled fetch: ${init?.method} ${url}`)
    })
  }

  it('confirms that a retry was queued, naming the destination', async () => {
    mockRetry()
    renderRow(mockDelivery({ status: 'failed', destination_name: 'Ops Slack' }))

    fireEvent.click(screen.getByRole('button', { name: 'Retry delivery' }))

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        'Retry queued — Ops Slack will send this alert again.',
      ),
    )
  })

  it('asks first where a retry opens a second ticket', async () => {
    const fetchSpy = mockRetry()
    renderRow(mockDelivery({ status: 'failed', channel: 'jira', destination_name: 'Ops Jira' }))

    fireEvent.click(screen.getByRole('button', { name: 'Retry delivery' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Jira opens a new issue for it')
    expect(fetchSpy).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  // The retry endpoint only re-queues, so the outcome is looked up once the
  // worker has had a go.
  function mockRetryThen(outcomes: AlertDeliveryDetail[]) {
    let looks = 0
    return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url.endsWith('/alert-deliveries/delivery-1/retry') && method === 'POST') {
        return mockJsonResponse({
          ...mockDelivery({ status: 'pending', error_message: null, updated_at: '2026-01-02T00:00:00Z' }),
          items: [],
        })
      }
      if (url.endsWith('/alert-deliveries/delivery-1') && method === 'GET') {
        const outcome = outcomes[Math.min(looks, outcomes.length - 1)]
        looks += 1
        return mockJsonResponse(outcome)
      }
      if (method === 'GET') return mockJsonResponse({ items: [], total: 0 })
      throw new Error(`Unhandled fetch: ${method} ${url}`)
    })
  }

  it('says "Still failing" with the reason when the retried send fails again', async () => {
    mockRetryThen([
      { ...mockDelivery({ status: 'pending', error_message: null }), items: [] },
      {
        ...mockDelivery({ status: 'failed', error_message: 'Slack answered 404: channel_not_found' }),
        items: [],
      },
    ])
    renderRow(
      mockDelivery({ status: 'failed', destination_name: 'Ops Slack' }),
      undefined,
      undefined,
      'member',
      { intervalMs: 5, attempts: 5 },
    )

    fireEvent.click(screen.getByRole('button', { name: 'Retry delivery' }))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('Still failing: Slack answered 404: channel_not_found'),
    )
  })

  it('says it was delivered once the retried send goes through', async () => {
    mockRetryThen([{ ...mockDelivery({ status: 'sent', error_message: null }), items: [] }])
    renderRow(
      mockDelivery({ status: 'failed', destination_name: 'Ops Slack' }),
      undefined,
      undefined,
      'member',
      { intervalMs: 5, attempts: 5 },
    )

    fireEvent.click(screen.getByRole('button', { name: 'Retry delivery' }))

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        'Delivered — Ops Slack accepted the retried alert.',
      ),
    )
    expect(toastError).not.toHaveBeenCalled()
  })

  it('sends the retry once the ticket retry is confirmed', async () => {
    const fetchSpy = mockRetry()
    renderRow(mockDelivery({ status: 'failed', channel: 'linear', destination_name: 'Ops Linear' }))

    fireEvent.click(screen.getByRole('button', { name: 'Retry delivery' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))

    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        '/api/v1/projects/demo/alert-deliveries/delivery-1/retry',
        expect.objectContaining({ method: 'POST' }),
      ),
    )
  })
})

// The expanded detail printed `variable_value_drift`, a channel in capitals,
// and "1 items".
describe('AlertDeliveryRow — words, not enums', () => {
  it('names the channel the way the destination picker does', () => {
    renderRow(mockDelivery({ channel: 'slack' }))

    expect(screen.getByText('Slack')).toBeInTheDocument()
  })

  it('calls the demo sink a local sink', () => {
    renderRow(mockDelivery({ channel: 'demo_sink', is_local: true }))

    expect(screen.getByText('Local sink')).toBeInTheDocument()
  })

  it('labels the item kind and counts items in the singular', async () => {
    expandRow({
      ...mockDelivery({ status: 'sent', error_message: null, payload_snapshot: mockPayload() }),
      items: [mockItem({ scope_type: 'variable_value_drift' })],
    })

    expect(await screen.findByText('1 item')).toBeInTheDocument()
    expect(screen.queryByText('variable_value_drift')).toBeNull()
  })
})

// The status cell stacked "sent" over a "Local · simulated" pill, which put
// the row's baseline off its neighbours'.
describe('AlertDeliveryRow — the status cell holds only the status', () => {
  it('marks a simulated send under the destination, not beside the status', () => {
    renderRow(mockDelivery({ status: 'sent', error_message: null, is_local: true, is_simulated: true }))

    const suffix = screen.getByText('Local · simulated')
    expect(suffix.closest('td')).not.toBe(screen.getByText('sent').closest('td'))
    expect(suffix.closest('td')).toHaveTextContent(/Ops/)
  })

  // Moved here with the unused LocalDeliveryBadge: this suffix is the only
  // place a demo-sink delivery is marked, so it carries the honesty checks.
  it('says nothing was sent outside, and never in the success tone', () => {
    renderRow(mockDelivery({ status: 'sent', error_message: null, is_local: true, is_simulated: false }))

    const suffix = screen.getByText('Local')
    expect(suffix).toHaveAttribute('title', expect.stringContaining('nothing was sent to an external channel'))
    expect(suffix.className).toMatch(/text-warning/)
    expect(suffix.className).not.toMatch(/emerald|success/)
  })
})

describe('AlertDeliveryRow owner notifications (F07, #260)', () => {
  it('lists the owners the delivery emailed, with each outcome', async () => {
    expandRow({
      ...mockDelivery({ status: 'sent', error_message: null }),
      items: [mockItem()],
      owner_notifications: [
        { user_id: 'u-1', name: 'Anna', email: 'anna@example.com', status: 'sent' },
        { user_id: 'u-2', name: 'Oleg', email: 'oleg@example.com', status: 'failed', error: 'SMTP refused' },
      ],
    })

    const block = await screen.findByTestId('owner-notifications')
    expect(within(block).getByText('Owners notified by email')).toBeInTheDocument()
    expect(within(block).getByText('Anna')).toBeInTheDocument()
    expect(within(block).getByText('anna@example.com')).toBeInTheDocument()
    expect(within(block).getByText('emailed')).toBeInTheDocument()
    expect(within(block).getByText('failed')).toBeInTheDocument()
    expect(within(block).getByText('SMTP refused')).toBeInTheDocument()
  })

  it('says nothing about owners when none were notified', async () => {
    expandRow({ ...mockDelivery({ status: 'sent', error_message: null }), items: [mockItem()] })

    expect(await screen.findByText('checkout_completed')).toBeInTheDocument()
    expect(screen.queryByTestId('owner-notifications')).toBeNull()
  })

  it('shows a row still being sent as sending, without an error', async () => {
    expandRow({
      ...mockDelivery({ status: 'sent', error_message: null }),
      items: [mockItem()],
      owner_notifications: [
        { user_id: 'u-1', name: 'Anna', email: 'anna@example.com', status: 'pending', error: 'stale', sent_at: null },
        { user_id: 'u-2', name: 'Oleg', email: 'oleg@example.com', status: 'skipped', error: 'not a project member' },
      ],
    })

    const block = await screen.findByTestId('owner-notifications')
    expect(within(block).getByText('sending')).toBeInTheDocument()
    expect(within(block).queryByText('stale')).toBeNull()
    expect(within(block).getByText('not a project member')).toBeInTheDocument()
  })
})
