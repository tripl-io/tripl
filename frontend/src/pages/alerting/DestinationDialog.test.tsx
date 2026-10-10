import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'

import { alertingApi } from '@/api/alerting'
import type { AlertDestination } from '@/types'

import { DestinationDialog, type DestinationDialogTarget } from './DestinationDialog'

let testDestinationDraft: MockInstance<typeof alertingApi.testDestinationDraft>

function makeSlack(overrides: Partial<AlertDestination> = {}): AlertDestination {
  return {
    id: 'dest-1',
    project_id: 'proj-1',
    type: 'slack',
    name: 'Ops Slack',
    held_count: 0,
    project_timezone: 'UTC',
    enabled: true,
    webhook_set: true,
    bot_token_set: false,
    chat_id: null,
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
    delivery_count: 0,
    incident_count: 0,
    rules: [],
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  }
}

function renderDialog(target: DestinationDialogTarget) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <DestinationDialog
        slug="demo"
        target={target}
        project={{ timezone: 'UTC' }}
        isDemo={false}
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />
    </QueryClientProvider>,
  )
}

// restoreMocks (vite.config) undoes the spy after every test.
beforeEach(() => {
  testDestinationDraft = vi.spyOn(alertingApi, 'testDestinationDraft')
})

// Setting up Slack took Create, close, find the card, then Test.
describe('DestinationDialog — Send test before saving', () => {
  it('tests the unsaved settings and says the channel took the message', async () => {
    testDestinationDraft.mockResolvedValue({ ok: true, error: null, sent_at: null })
    renderDialog({ mode: 'create', type: 'slack', handOffToRule: false })

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Ops Slack' } })
    fireEvent.change(screen.getByLabelText('Webhook URL'), {
      target: { value: 'https://hooks.slack.com/services/T/B/X' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Send test' }))

    expect(await screen.findByText('Test message reached the channel.')).toBeInTheDocument()
    expect(testDestinationDraft).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({
        destination_id: null,
        type: 'slack',
        name: 'Ops Slack',
        webhook_url: 'https://hooks.slack.com/services/T/B/X',
      }),
    )
  })

  it('names the saved destination on edit, leaving a blank secret to the stored one', async () => {
    testDestinationDraft.mockResolvedValue({ ok: true, error: null, sent_at: null })
    renderDialog({ mode: 'edit', destination: makeSlack() })

    fireEvent.click(screen.getByRole('button', { name: 'Send test' }))

    await screen.findByText('Test message reached the channel.')
    const body = testDestinationDraft.mock.lastCall?.[1]
    expect(body).toBeDefined()
    expect(body?.destination_id).toBe('dest-1')
    expect(body?.type).toBe('slack')
    // Absent, not '': the server reads a missing secret as "the one on file".
    expect(body?.webhook_url).toBeUndefined()
  })

  it('reads a refusal in plain words, with the raw error behind Details', async () => {
    testDestinationDraft.mockResolvedValue({
      ok: false,
      error: '<urlopen error Tunnel connection failed: 403 Forbidden>',
      sent_at: null,
      error_kind: 'network',
      http_status: null,
    })
    renderDialog({ mode: 'edit', destination: makeSlack() })

    fireEvent.click(screen.getByRole('button', { name: 'Send test' }))

    expect(
      await screen.findByText(/Test message not delivered\. Couldn't reach the URL/),
    ).toBeInTheDocument()
    expect(screen.getByText('Details')).toBeInTheDocument()
  })

  it('drops the result once the settings it was sent with change', async () => {
    testDestinationDraft.mockResolvedValue({ ok: true, error: null, sent_at: null })
    renderDialog({ mode: 'edit', destination: makeSlack() })

    fireEvent.click(screen.getByRole('button', { name: 'Send test' }))
    await screen.findByText('Test message reached the channel.')

    // A result about the old webhook under a new one would be a false answer.
    fireEvent.change(screen.getByLabelText('Webhook URL'), {
      target: { value: 'https://hooks.slack.com/services/T/B/Y' },
    })
    expect(screen.queryByText('Test message reached the channel.')).toBeNull()
  })
})

describe('DestinationDialog — PagerDuty and Microsoft Teams', () => {
  it('collects a PagerDuty integration key and severity', async () => {
    testDestinationDraft.mockResolvedValue({ ok: true, error: null, sent_at: null })
    renderDialog({ mode: 'create', type: 'pagerduty', handOffToRule: false })

    expect(screen.getByRole('heading', { name: /New PagerDuty destination/ })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'On-call' } })
    fireEvent.change(screen.getByLabelText('Integration key'), { target: { value: 'R0UTINGKEY' } })
    const severity = screen.getByLabelText('Severity')
    expect(severity).toHaveValue('error')
    fireEvent.change(severity, { target: { value: 'critical' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send test' }))

    await screen.findByText('Test message reached the channel.')
    expect(testDestinationDraft).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({
        type: 'pagerduty',
        pagerduty_routing_key: 'R0UTINGKEY',
        pagerduty_severity: 'critical',
      }),
    )
  })

  it('collects a Teams webhook URL', async () => {
    testDestinationDraft.mockResolvedValue({ ok: true, error: null, sent_at: null })
    renderDialog({ mode: 'create', type: 'teams', handOffToRule: false })

    expect(screen.getByRole('heading', { name: /New Microsoft Teams destination/ })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Webhook URL'), {
      target: { value: 'https://contoso.webhook.office.com/x' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Send test' }))

    await screen.findByText('Test message reached the channel.')
    expect(testDestinationDraft).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({ type: 'teams', teams_webhook_url: 'https://contoso.webhook.office.com/x' }),
    )
  })
})
