import { describe, expect, it } from 'vitest'

import type { AlertDestination } from '@/types'

import { defaultDestinationForm, type DestinationChannel, type DestinationFormState } from './constants'
import {
  destinationFormProblems,
  destinationFormToPayload,
  destinationFormToTestBody,
  destinationToForm,
} from './destinationForm'

function makeDestination(overrides: Partial<AlertDestination> = {}): AlertDestination {
  return {
    id: 'dest-1',
    project_id: 'proj-1',
    type: 'webhook',
    name: 'Hook',
    held_count: 0,
    project_timezone: 'UTC',
    enabled: true,
    webhook_set: false,
    bot_token_set: false,
    chat_id: null,
    target_url_set: true,
    webhook_header_name: 'Authorization',
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

function filled(type: DestinationChannel, patch: Partial<DestinationFormState> = {}): DestinationFormState {
  return { ...defaultDestinationForm(type), name: `My ${type}`, ...patch }
}

describe('destinationFormToPayload — create', () => {
  // Every channel but Telegram failed with "Telegram chat_id is required",
  // because create spread the whole form and `chat_id: ''` went with it.
  const cases: [DestinationChannel, Partial<DestinationFormState>, Record<string, unknown>][] = [
    ['slack', { webhook_url: 'https://hooks.slack.com/services/T/B/X' }, {
      webhook_url: 'https://hooks.slack.com/services/T/B/X',
    }],
    ['webhook', { target_url: 'https://example.com/hook' }, { target_url: 'https://example.com/hook' }],
    ['email', { email_recipients: 'a@example.com' }, { email_recipients: 'a@example.com' }],
    ['jira', {
      jira_base_url: 'https://acme.atlassian.net',
      jira_auth_email: 'a@example.com',
      jira_api_token: 'tok',
      jira_project_key: 'ENG',
    }, {
      jira_base_url: 'https://acme.atlassian.net',
      jira_auth_email: 'a@example.com',
      jira_api_token: 'tok',
      jira_project_key: 'ENG',
      jira_issue_type: 'Task',
    }],
    ['linear', { linear_api_key: 'lin_api_x', linear_team_id: 'TEAM' }, {
      linear_api_key: 'lin_api_x',
      linear_team_id: 'TEAM',
    }],
    // A severity is always sent, so a new destination never relies on the
    // server's default without saying which one it got.
    ['pagerduty', { pagerduty_routing_key: 'R0UTINGKEY' }, {
      pagerduty_routing_key: 'R0UTINGKEY',
      pagerduty_severity: 'error',
    }],
    ['teams', { teams_webhook_url: 'https://contoso.webhook.office.com/x' }, {
      teams_webhook_url: 'https://contoso.webhook.office.com/x',
    }],
  ]

  it.each(cases)('sends only the %s fields, and no chat_id', (type, patch, expected) => {
    const body = destinationFormToPayload(filled(type, patch), null)
    // Drop the keys the payload carries as `undefined` — JSON.stringify drops
    // them too, so this is what reaches the wire.
    const wire = JSON.parse(JSON.stringify(body)) as Record<string, unknown>

    expect(wire).toEqual({
      type,
      name: `My ${type}`,
      enabled: true,
      delivery_schedule_cron: null,
      ...expected,
    })
    expect(wire).not.toHaveProperty('chat_id')
  })

  it('still sends the chat id for Telegram', () => {
    const body = destinationFormToPayload(filled('telegram', { bot_token: '1:ABC', chat_id: '-100' }), null)

    expect(body).toMatchObject({ type: 'telegram', bot_token: '1:ABC', chat_id: '-100' })
  })

  it('sends the webhook header only as a pair the user typed', () => {
    const body = destinationFormToPayload(
      filled('webhook', {
        target_url: 'https://example.com',
        webhook_header_name: 'X-Key',
        webhook_header_value: 's3cret',
      }),
      null,
    )
    expect(body).toMatchObject({ webhook_header_name: 'X-Key', webhook_header_value: 's3cret' })
  })

  it('refuses to create the demo-only local sink', () => {
    expect(() => destinationFormToPayload({ ...filled('slack'), type: 'demo_sink' }, null)).toThrow()
  })
})

describe('destinationFormToPayload — edit', () => {
  it('keeps a stored PagerDuty key and Teams URL when the secret is left blank', () => {
    const pagerduty = makeDestination({
      type: 'pagerduty',
      pagerduty_routing_key_set: true,
      pagerduty_severity: 'warning',
    })
    const form = destinationToForm(pagerduty)
    expect(form.pagerduty_severity).toBe('warning')
    expect(JSON.parse(JSON.stringify(destinationFormToPayload(form, pagerduty)))).toEqual({
      name: pagerduty.name,
      enabled: true,
      delivery_schedule_cron: null,
      pagerduty_severity: 'warning',
    })

    const teams = makeDestination({ type: 'teams', teams_webhook_set: true })
    const teamsBody = destinationFormToPayload(destinationToForm(teams), teams)
    expect(JSON.parse(JSON.stringify(teamsBody))).not.toHaveProperty('teams_webhook_url')
  })

  it('omits an empty secret, which keeps the stored one', () => {
    const existing = makeDestination({ type: 'slack', webhook_set: true })
    const body = destinationFormToPayload(destinationToForm(existing), existing)

    expect(JSON.parse(JSON.stringify(body))).not.toHaveProperty('webhook_url')
  })

  it('clears an optional column that was emptied', () => {
    const existing = makeDestination({ type: 'linear', linear_team_id: 'T', linear_state_id: 'S' })
    const body = destinationFormToPayload({ ...destinationToForm(existing), linear_state_id: '' }, existing)

    expect(body).toMatchObject({ linear_team_id: 'T', linear_state_id: null })
  })

  it('sends a required field as typed rather than dropping an emptied one', () => {
    const existing = makeDestination({
      type: 'jira',
      jira_base_url: 'https://acme.atlassian.net',
      jira_auth_email: 'a@example.com',
      jira_project_key: 'ENG',
    })
    const body = destinationFormToPayload({ ...destinationToForm(existing), jira_project_key: 'OPS' }, existing)

    expect(body).toMatchObject({ jira_project_key: 'OPS', jira_base_url: 'https://acme.atlassian.net' })
  })

  it('resets an emptied Jira issue type to Task instead of dropping it', () => {
    const existing = makeDestination({
      type: 'jira',
      jira_base_url: 'https://acme.atlassian.net',
      jira_auth_email: 'a@example.com',
      jira_project_key: 'ENG',
      jira_issue_type: 'Bug',
    })
    const body = destinationFormToPayload({ ...destinationToForm(existing), jira_issue_type: '' }, existing)

    expect(JSON.parse(JSON.stringify(body))).toMatchObject({ jira_issue_type: 'Task' })
  })

  it('removes the webhook secret header as a pair of nulls', () => {
    const existing = makeDestination()
    const body = destinationFormToPayload(destinationToForm(existing), existing, { removeWebhookHeader: true })

    expect(body).toMatchObject({ webhook_header_name: null, webhook_header_value: null })
  })

  it('sends a local sink its name, switch and schedule only', () => {
    const existing = makeDestination({ type: 'demo_sink', is_local: true, webhook_header_name: null })
    const body = destinationFormToPayload(
      { ...destinationToForm(existing), name: 'Renamed', enabled: false },
      existing,
    )

    expect(JSON.parse(JSON.stringify(body))).toEqual({
      name: 'Renamed',
      enabled: false,
      delivery_schedule_cron: null,
    })
  })
})

describe('destinationFormProblems — the webhook header is a pair', () => {
  it('asks for a name when only a value is typed', () => {
    expect(destinationFormProblems(filled('webhook', { webhook_header_value: 'v' }), null))
      .toHaveProperty('webhook_header_name')
  })

  it('asks for a value when a new name has nothing stored behind it', () => {
    expect(destinationFormProblems(filled('webhook', { webhook_header_name: 'X-Key' }), null))
      .toHaveProperty('webhook_header_value')
  })

  it('accepts an unchanged name whose value is stored', () => {
    const existing = makeDestination()
    expect(destinationFormProblems(destinationToForm(existing), existing)).toEqual({})
  })

  it('does not let clearing the name orphan a stored value', () => {
    const existing = makeDestination()
    expect(destinationFormProblems({ ...destinationToForm(existing), webhook_header_name: '' }, existing))
      .toHaveProperty('webhook_header_name')
    expect(
      destinationFormProblems(
        { ...destinationToForm(existing), webhook_header_name: '' },
        existing,
        { removeWebhookHeader: true },
      ),
    ).toEqual({})
  })
})

// The dialog's "Send test": the channel fields the save would send,
// plus the channel and the destination whose stored secrets fill the blanks.
describe('destinationFormToTestBody', () => {
  it('sends an unsaved destination as the create body would, with no id', () => {
    const body = destinationFormToTestBody(
      filled('slack', { webhook_url: 'https://hooks.slack.com/services/T/B/X' }),
      null,
    )

    expect(body).toMatchObject({ destination_id: null, type: 'slack', name: 'My slack' })
    expect(body.webhook_url).toBe('https://hooks.slack.com/services/T/B/X')
  })

  it('names the saved destination and leaves an untouched secret out', () => {
    const existing = makeDestination()
    const body = destinationFormToTestBody(destinationToForm(existing), existing)

    expect(body.destination_id).toBe(existing.id)
    expect(body.type).toBe('webhook')
    // Absent means "the stored one" to the server, exactly as on Save.
    expect(body.target_url).toBeUndefined()
    expect(body.webhook_header_value).toBeUndefined()
    expect(body.webhook_header_name).toBe('Authorization')
  })

  it('sends a removed header as removed', () => {
    const existing = makeDestination()
    const body = destinationFormToTestBody(destinationToForm(existing), existing, {
      removeWebhookHeader: true,
    })

    expect(body.webhook_header_name).toBeNull()
    expect(body.webhook_header_value).toBeNull()
  })
})
