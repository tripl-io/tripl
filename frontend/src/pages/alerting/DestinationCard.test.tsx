import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

/** The toaster, stubbed: a refused switch says why in one. */
const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }))
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: toastError },
  Toaster: () => null,
}))

import { alertingApi } from '@/api/alerting'
import type { AlertDestination, AlertRule } from '@/types'

import { DestinationCard } from './DestinationCard'

function makeRule(overrides: Partial<AlertRule> = {}): AlertRule {
  return {
    id: 'rule-1',
    destination_id: 'dest-1',
    scan_config_id: null,
    name: 'Prod drops',
    enabled: true,
    include_project_total: true,
    include_event_types: false,
    include_events: false,
    include_schema_drifts: false,
    include_distribution_drifts: false,
    include_release_regressions: false,
    include_variable_value_drifts: false,
    include_metrics: false,
    include_source_freshness: false,
    include_lifecycle: false,
    include_property_drifts: false,
    notify_owners: false,
    notify_on_spike: false,
    notify_on_drop: true,
    ai_explanation_enabled: false,
    min_percent_delta: 100,
    min_absolute_delta: 0,
    min_expected_count: 0,
    cooldown_minutes: 360,
    message_template: null,
    items_template: null,
    message_format: 'plain',
    filters: [],
    muted: false,
    muted_until: null,
    total_deliveries: 115,
    incident_count: 57,
    last_delivery_at: '2026-08-12T09:00:00Z',
    last_delivery_status: 'sent',
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  }
}

function makeDestination(overrides: Partial<AlertDestination> = {}): AlertDestination {
  return {
    id: 'dest-1',
    project_id: 'proj-1',
    type: 'telegram',
    name: 'TG',
    // Zero, not omitted: the API always sends this field, and a double that can
    // leave it out is a claim that a destination with no held count is normal.
    held_count: 0,
    project_timezone: 'UTC',
    enabled: true,
    webhook_set: false,
    bot_token_set: true,
    chat_id: '-1002233445566',
    target_url_set: false,
    webhook_header_name: null,
    email_recipients: null,
    email_from_address: null,
    email_subject_template: null,
    jira_base_url: null,
    jira_auth_email: null,
    jira_api_token_set: false,
    jira_project_key: null,
    jira_issue_type: null,
    linear_api_key_set: false,
    linear_team_id: null,
    linear_state_id: null,
    linear_label_ids: null,
    pagerduty_routing_key_set: false,
    pagerduty_severity: null,
    teams_webhook_set: false,
    is_local: false,
    delivery_count: 115,
    incident_count: 57,
    rules: [makeRule()],
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  }
}

function renderCard(destination: AlertDestination = makeDestination(), canWrite = true) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const tree = (current: AlertDestination) => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <DestinationCard
          slug="acme-ios"
          destination={current}
          canWrite={canWrite}
          onEditDestination={() => {}}
        />
      </MemoryRouter>
    </QueryClientProvider>
  )
  const result = render(tree(destination))
  return {
    ...result,
    /** Re-render the same card with the destination as a refetch would return it. */
    update: (next: AlertDestination) => result.rerender(tree(next)),
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('DestinationCard test send', () => {
  it('reports a delivered probe, because a stored token is not a working one', async () => {
    const test = vi.spyOn(alertingApi, 'testDestination').mockResolvedValue({
      ok: true,
      error: null,
      sent_at: '2026-08-12T12:00:00Z',
    })
    renderCard()

    fireEvent.click(screen.getByRole('button', { name: 'Send a test message through TG' }))

    expect(await screen.findByText(/Test message reached the channel at/)).toBeInTheDocument()
    expect(test).toHaveBeenCalledWith('acme-ios', 'dest-1')
  })

  it('renders a channel refusal as the answer, not as a crash', async () => {
    // The route answers 200 with `ok: false` — a revoked bot token IS what the
    // button was pressed to find out, so it must not be styled as a server
    // fault the operator can do nothing about.
    vi.spyOn(alertingApi, 'testDestination').mockResolvedValue({
      ok: false,
      error: 'Forbidden: bot was blocked by the user',
      sent_at: null,
    })
    renderCard()

    fireEvent.click(screen.getByRole('button', { name: 'Send a test message through TG' }))

    const refusal = await screen.findByText(/bot was blocked by the user/)
    expect(refusal).toHaveAttribute('role', 'status')
  })

  it('names the transport failure when the request never reached the server', async () => {
    vi.spyOn(alertingApi, 'testDestination').mockRejectedValue(new Error('Network down'))
    renderCard()

    fireEvent.click(screen.getByRole('button', { name: 'Send a test message through TG' }))

    const failure = await screen.findByRole('alert')
    expect(failure).toHaveTextContent('Test send failed: Network down')
  })
})

describe('DestinationCard traffic', () => {
  it('reports the channel traffic, including how much routes to it', () => {
    renderCard()

    // The rule COUNT stays after the rules themselves moved to the Monitors
    // section: "wired up and nothing routes here" is a fact about
    // the channel, and it is the one thing the card would otherwise not say.
    expect(screen.getByText('1 rule · 115 deliveries · 57 incidents')).toBeInTheDocument()
  })

  it('says a channel has carried nothing rather than looking identical to a working one', () => {
    renderCard(makeDestination({ rules: [], delivery_count: 0, incident_count: 0 }))

    expect(screen.getByText('0 rules · 0 deliveries · 0 incidents')).toBeInTheDocument()
  })

  it('keeps the chat id readable instead of clipping the value that identifies the chat', () => {
    renderCard()

    expect(screen.getByText('chat -1002233445566')).toBeInTheDocument()
  })
})

describe('DestinationCard viewer gating', () => {
  // Both are editor-only endpoints (deps.py `require_editor`), and each used to
  // render fully enabled for a viewer whose click came straight back as a 403.
  // The rule controls that used to be in this list moved to the Monitors
  // section along with the rules themselves.
  const WRITE_BUTTONS = ['Send a test message through TG', 'Edit destination TG']

  it('offers an editor every write control', () => {
    renderCard()

    for (const name of WRITE_BUTTONS) {
      expect(screen.getByRole('button', { name })).toBeEnabled()
    }
    expect(screen.getByRole('switch', { name: 'Toggle TG' })).toBeEnabled()
  })

  it('offers a viewer none of them', () => {
    renderCard(makeDestination(), false)

    for (const name of WRITE_BUTTONS) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
    expect(screen.queryByRole('switch', { name: 'Toggle TG' })).toBeNull()
  })

  it('keeps every fact the card was reporting', () => {
    renderCard(makeDestination(), false)

    expect(screen.getByText('1 rule · 115 deliveries · 57 incidents')).toBeInTheDocument()
    expect(screen.getByText('TG')).toBeInTheDocument()
    expect(screen.getByText('chat -1002233445566')).toBeInTheDocument()
  })
})

describe('DestinationCard after an edit', () => {
  it('drops a refusal that described the credentials stored before the edit', async () => {
    vi.spyOn(alertingApi, 'testDestination').mockResolvedValue({
      ok: false,
      error: 'Unauthorized',
      sent_at: null,
    })
    const { update } = renderCard()

    fireEvent.click(screen.getByRole('button', { name: 'Send a test message through TG' }))
    expect(await screen.findByText(/The channel refused the test message: Unauthorized/)).toBeInTheDocument()

    update(makeDestination({ chat_id: '-100999', updated_at: '2026-08-13T00:00:00Z' }))

    expect(screen.queryByText(/The channel refused the test message/)).toBeNull()
  })

  it('drops the result when the editor is opened, since it can replace a secret', async () => {
    vi.spyOn(alertingApi, 'testDestination').mockResolvedValue({ ok: false, error: 'Unauthorized', sent_at: null })
    renderCard()

    fireEvent.click(screen.getByRole('button', { name: 'Send a test message through TG' }))
    expect(await screen.findByText(/The channel refused the test message: Unauthorized/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Edit destination TG' }))

    expect(screen.queryByText(/The channel refused the test message/)).toBeNull()
  })

  it('keeps the result across a write that does not touch the channel settings', async () => {
    vi.spyOn(alertingApi, 'testDestination').mockResolvedValue({ ok: false, error: 'Unauthorized', sent_at: null })
    const { update } = renderCard()

    fireEvent.click(screen.getByRole('button', { name: 'Send a test message through TG' }))
    expect(await screen.findByText(/The channel refused the test message: Unauthorized/)).toBeInTheDocument()

    // The digest flusher's `last_flushed_at` tick and the Enabled switch both
    // move `updated_at` without changing what the test was a test of.
    update(makeDestination({ enabled: false, updated_at: '2026-08-13T00:00:00Z' }))

    expect(screen.getByText(/The channel refused the test message: Unauthorized/)).toBeInTheDocument()
  })

  it('can be dismissed', async () => {
    vi.spyOn(alertingApi, 'testDestination').mockResolvedValue({ ok: true, error: null, sent_at: null })
    renderCard()

    fireEvent.click(screen.getByRole('button', { name: 'Send a test message through TG' }))
    expect(await screen.findByText('Test message reached the channel.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss the test result for TG' }))

    expect(screen.queryByText('Test message reached the channel.')).toBeNull()
  })
})

describe('DestinationCard enable switch', () => {
  it('says why when the server refuses the write', async () => {
    vi.spyOn(alertingApi, 'updateDestination').mockRejectedValue(
      new Error('A demo destination cannot be enabled'),
    )
    renderCard()

    fireEvent.click(screen.getByRole('switch', { name: 'Toggle TG' }))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('A demo destination cannot be enabled', expect.anything()),
    )
  })
})


describe('DestinationCard test failures in words', () => {
  it('says what a transport error means and keeps the raw text under Details', async () => {
    vi.spyOn(alertingApi, 'testDestination').mockResolvedValue({
      ok: false,
      error: '<urlopen error Tunnel connection failed: 403 Forbidden>',
      sent_at: null,
    })
    renderCard()

    fireEvent.click(screen.getByRole('button', { name: 'Send a test message through TG' }))

    expect(
      await screen.findByText(
        "Test message not delivered. Couldn't reach the URL: a network proxy blocked the request.",
      ),
    ).toHaveAttribute('role', 'status')
    expect(screen.getByText('Details')).toBeInTheDocument()
    expect(screen.getByText('<urlopen error Tunnel connection failed: 403 Forbidden>')).toBeInTheDocument()
  })
})

describe('DestinationCard facts', () => {
  it('names the channel instead of printing its enum, and folds stored secrets into one word', () => {
    renderCard()

    expect(screen.getByText('Telegram')).toBeInTheDocument()
    expect(screen.queryByText('telegram')).toBeNull()
    expect(screen.getByText('Configured')).toBeInTheDocument()
    expect(screen.queryByText('bot token set')).toBeNull()
    // The switch is the state; no pill repeats it while enabled.
    expect(screen.queryByText('enabled')).toBeNull()
  })

  it('marks a disabled destination', () => {
    renderCard(makeDestination({ enabled: false }))

    expect(screen.getByText('Disabled')).toBeInTheDocument()
  })
})

describe('DestinationCard delete', () => {
  it('puts Delete inside the card, named by the destination', () => {
    const onDelete = vi.fn()
    const queryClient = new QueryClient()
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <DestinationCard
            slug="acme-ios"
            destination={makeDestination()}
            canWrite
            onEditDestination={() => {}}
            onDeleteDestination={onDelete}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Delete destination TG' }))

    expect(onDelete).toHaveBeenCalledWith(expect.objectContaining({ id: 'dest-1' }))
  })

  it('offers no delete where the section passes none', () => {
    renderCard()

    expect(screen.queryByRole('button', { name: /^Delete destination/ })).toBeNull()
  })
})
