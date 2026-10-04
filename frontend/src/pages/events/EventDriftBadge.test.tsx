import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import { eventTypesApi } from '@/api/eventTypes'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { EventDriftBadge } from './EventDriftBadge'
import { personaAuth } from '@/test/persona'
import { SessionProject } from '@/test/PersonaProject'

vi.mock('@/api/eventTypes', () => ({
  eventTypesApi: {
    listDrifts: vi.fn(),
    applyDriftAction: vi.fn(),
  },
}))

const DRIFT = {
  id: 'drift-1',
  event_type_id: 'et-1',
  scan_config_id: null,
  field_name: 'action',
  drift_type: 'missing_field' as const,
  observed_type: null,
  declared_type: 'string',
  sample_value: null,
  status: 'open' as const,
  resolution_note: null,
  snoozed_until: null,
  resolved_at: null,
  resolved_by: null,
  detected_at: '2026-07-26T00:00:00Z',
}

// The real 409 from services/schema_drift_service._reject_if_name_format_needs.
// api/client.ts puts a plain-string `detail` straight into ApiError.message, so
// the popover can render it with no parsing.
const CONFLICT_DETAIL =
  "Cannot accept this drift: the field 'action' is used by the event name format " +
  "of 1 scan config(s): 'Old events (iOS)' ({action}). Accepting would delete the " +
  'field and every scan would then fail with \'the event name format references ' +
  "unknown keys'. Edit the scan's Event name format so it no longer references " +
  'this column, then accept the drift.'

function renderBadge() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <EventDriftBadge slug="demo" eventTypeId="et-1" count={1} />
    </QueryClientProvider>,
  )
}

async function openPopoverAndAccept() {
  fireEvent.click(screen.getByRole('button', { name: '1 schema drift on this event type' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Accept' }))
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('EventDriftBadge', () => {
  it('shows a viewer the drifts without the triage buttons', async () => {
    vi.mocked(eventTypesApi.listDrifts).mockResolvedValue({ items: [DRIFT], total: 1 })
    const viewer: AuthContextValue = personaAuth('viewer')
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={viewer}>
          <SessionProject session={viewer}>
            <EventDriftBadge slug="demo" eventTypeId="et-1" count={1} />
          </SessionProject>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: '1 schema drift on this event type' }))
    expect(await screen.findByText(/action/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Accept' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Snooze' })).not.toBeInTheDocument()
  })

  it('labels a contract drift on a property path as a property contract (F23)', async () => {
    vi.mocked(eventTypesApi.listDrifts).mockResolvedValue({
      items: [
        {
          ...DRIFT,
          field_name: 'props.plan',
          drift_type: 'enum_violation' as const,
          observed_type: 'bad_rate=10.00%; max=0.00%; bad=1; total=10',
          declared_type: 'enum',
          sample_value: 'platinum',
        },
      ],
      total: 1,
    })
    renderBadge()
    fireEvent.click(screen.getByRole('button', { name: '1 schema drift on this event type' }))
    expect(await screen.findByText('props.plan')).toBeInTheDocument()
    expect(screen.getByText(/property enum/)).toBeInTheDocument()
  })

  it('shows a paused note, not a red auth failure, when the drift list 401s under the sign-in dialog', async () => {
    vi.mocked(eventTypesApi.listDrifts).mockRejectedValue(new ApiError('Authentication required', 401))

    // The paused note is for a 401 while the session-expired dialog is up.
    const expired: AuthContextValue = {
      user: null,
      status: 'authenticated',
      error: null,
      isLoggingOut: false,
      logout: async () => {},
      refresh: () => {},
      sessionExpired: true,
    }
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={expired}>
          <SessionProject session={expired}>
            <EventDriftBadge slug="demo" eventTypeId="et-1" count={1} />
          </SessionProject>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: '1 schema drift on this event type' }))

    expect(await screen.findByRole('status')).toHaveTextContent('Waiting for you to sign in again')
    expect(screen.queryByText('Authentication required')).not.toBeInTheDocument()
  })

  it('shows a load failure through ErrorState', async () => {
    vi.mocked(eventTypesApi.listDrifts).mockRejectedValue(new ApiError('Server exploded', 500))

    renderBadge()
    fireEvent.click(screen.getByRole('button', { name: '1 schema drift on this event type' }))

    expect(await screen.findByRole('heading', { name: 'Failed to load drifts' })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('Server exploded')
  })

  it('renders the backend 409 verbatim when the accept is blocked', async () => {
    // Without this the guard is invisible: the button just stops pending and the
    // drift stays open with no explanation.
    vi.mocked(eventTypesApi.listDrifts).mockResolvedValue({ items: [DRIFT], total: 1 })
    vi.mocked(eventTypesApi.applyDriftAction).mockRejectedValue(
      new ApiError(CONFLICT_DETAIL, 409),
    )

    renderBadge()
    await openPopoverAndAccept()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('is used by the event name format')
    expect(alert).toHaveTextContent('Event name format')
  })

  it('does not greet the next popover open with the previous 409', async () => {
    // The `onOpenChange` reset in EventDriftBadge is the only thing stopping
    // this: react-query keeps a mutation's error until it is reset or replaced,
    // and Radix remounts the content on every open, so a failure from the last
    // visit renders again next to a drift it may have nothing to do with.
    vi.mocked(eventTypesApi.listDrifts).mockResolvedValue({ items: [DRIFT], total: 1 })
    vi.mocked(eventTypesApi.applyDriftAction).mockRejectedValue(
      new ApiError(CONFLICT_DETAIL, 409),
    )

    renderBadge()
    await openPopoverAndAccept()
    await screen.findByRole('alert')

    const trigger = screen.getByRole('button', { name: '1 schema drift on this event type' })
    fireEvent.click(trigger) // close
    fireEvent.click(trigger) // reopen

    await screen.findByRole('button', { name: 'Accept' })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows no alert when the accept succeeds', async () => {
    vi.mocked(eventTypesApi.listDrifts).mockResolvedValue({ items: [DRIFT], total: 1 })
    vi.mocked(eventTypesApi.applyDriftAction).mockResolvedValue({
      ...DRIFT,
      status: 'accepted',
    })

    renderBadge()
    await openPopoverAndAccept()

    await waitFor(() =>
      expect(eventTypesApi.applyDriftAction).toHaveBeenCalledWith('demo', 'drift-1', {
        action: 'accept',
      }),
    )
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
