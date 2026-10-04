import type { alertingApi } from '@/api/alerting'
import type { AlertDestination, AlertDestinationDraftTestRequest } from '@/types'

import type { DestinationFormState } from './constants'

export type CreateDestinationBody = Parameters<typeof alertingApi.createDestination>[1]
export type UpdateDestinationBody = Parameters<typeof alertingApi.updateDestination>[2]

/**
 * The widths the API enforces (`AlertDestinationCreate` / `…Update` in backend
 * schemas/alerting.py), mirrored onto the inputs as `maxLength` so an over-long
 * value stops at the keyboard instead of failing late as a raw 422.
 */
export const DESTINATION_FIELD_MAX_LENGTH = {
  name: 255,
  chat_id: 255,
  webhook_header_name: 255,
  email_from_address: 255,
  email_subject_template: 500,
  jira_base_url: 255,
  jira_auth_email: 255,
  jira_project_key: 64,
  jira_issue_type: 64,
  linear_team_id: 64,
  linear_state_id: 64,
  linear_label_ids: 1024,
} as const satisfies Partial<Record<keyof DestinationFormState, number>>

/** The form a stored destination opens the edit dialog with. Secrets start empty: they are write-only. */
export function destinationToForm(destination: AlertDestination): DestinationFormState {
  return {
    type: destination.type,
    name: destination.name,
    enabled: destination.enabled,
    webhook_url: '',
    bot_token: '',
    chat_id: destination.chat_id ?? '',
    target_url: '',
    webhook_header_name: destination.webhook_header_name ?? '',
    webhook_header_value: '',
    email_recipients: destination.email_recipients ?? '',
    email_from_address: destination.email_from_address ?? '',
    email_subject_template: destination.email_subject_template ?? '',
    jira_base_url: destination.jira_base_url ?? '',
    jira_auth_email: destination.jira_auth_email ?? '',
    jira_api_token: '',
    jira_project_key: destination.jira_project_key ?? '',
    jira_issue_type: destination.jira_issue_type ?? 'Task',
    linear_api_key: '',
    linear_team_id: destination.linear_team_id ?? '',
    linear_state_id: destination.linear_state_id ?? '',
    linear_label_ids: destination.linear_label_ids ?? '',
    delivery_schedule_cron: destination.delivery_schedule_cron ?? '',
  }
}

/** '' is the form's "nothing typed"; on the wire that is an absent key. */
function typed(value: string): string | undefined {
  return value === '' ? undefined : value
}

/** '' is the form's "clear it"; on the wire an optional column clears with null. */
function orNull(value: string): string | null {
  return value === '' ? null : value
}

/**
 * The request body a destination dialog submits.
 *
 * ONE function for both requests, so create and update cannot disagree about
 * which fields go on the wire. Create used to spread the whole form —
 * every channel's empty strings, `chat_id: ''` among them — and the API's
 * `normalize_chat_id` is not gated on the type, so every channel but Telegram
 * failed with "Telegram chat_id is required". Only the selected
 * channel's keys are sent now.
 *
 * The two modes still differ where the API does:
 *  - a secret left empty on EDIT means "keep the stored one" and is omitted;
 *  - an optional column emptied on edit is sent as null, which clears it;
 *  - a required non-secret field (Jira base URL, Linear team…) is always sent
 *    as typed — the dialog keeps those `required`, so emptying one is refused
 *    at the form rather than silently keeping the old value.
 *
 * `removeWebhookHeader` sends both halves of the webhook's secret header as
 * null.
 */
export function destinationFormToPayload(
  form: DestinationFormState,
  existing: null,
  options?: { removeWebhookHeader?: boolean },
): CreateDestinationBody
export function destinationFormToPayload(
  form: DestinationFormState,
  existing: AlertDestination,
  options?: { removeWebhookHeader?: boolean },
): UpdateDestinationBody
export function destinationFormToPayload(
  form: DestinationFormState,
  existing: AlertDestination | null,
  { removeWebhookHeader = false }: { removeWebhookHeader?: boolean } = {},
): CreateDestinationBody | UpdateDestinationBody {
  const common = {
    name: form.name,
    enabled: form.enabled,
    // '' is the form's way of saying immediate; the API wants null.
    delivery_schedule_cron: form.delivery_schedule_cron || null,
  }
  const channel = channelFields(form, existing, removeWebhookHeader)
  if (existing) return { ...common, ...channel }
  const { type } = form
  // The demo-only ``demo_sink`` is created by the seeder, never here — so the
  // create payload always carries a real channel. Narrowed rather than cast.
  if (type === 'demo_sink') {
    throw new Error('The local demo sink cannot be created from the UI')
  }
  return { ...common, type, ...channel }
}

/**
 * The body of the dialog's "Send test": what Create or Save would send
 * for the channel, plus the channel itself and, when editing, the destination
 * whose stored secrets fill the ones the form leaves blank.
 *
 * The channel fields come from the same `channelFields` the save does, so a
 * test and the save it precedes cannot read the form two ways.
 */
export function destinationFormToTestBody(
  form: DestinationFormState,
  existing: AlertDestination | null,
  { removeWebhookHeader = false }: { removeWebhookHeader?: boolean } = {},
): AlertDestinationDraftTestRequest {
  return {
    destination_id: existing?.id ?? null,
    type: form.type,
    name: form.name.trim() || null,
    ...channelFields(form, existing, removeWebhookHeader),
  }
}

function channelFields(
  form: DestinationFormState,
  existing: AlertDestination | null,
  removeWebhookHeader: boolean,
): Omit<UpdateDestinationBody, 'name' | 'enabled' | 'delivery_schedule_cron'> {
  const editing = existing !== null
  switch (form.type) {
    case 'slack':
      return { webhook_url: typed(form.webhook_url) }
    case 'telegram':
      return { bot_token: typed(form.bot_token), chat_id: form.chat_id }
    case 'webhook':
      if (removeWebhookHeader) {
        return { target_url: typed(form.target_url), webhook_header_name: null, webhook_header_value: null }
      }
      return {
        target_url: typed(form.target_url),
        // Create: absent unless both are typed (the pair check refuses one
        // alone). Edit: the name as shown — an unchanged name with an empty
        // value keeps the stored secret.
        webhook_header_name: editing ? orNull(form.webhook_header_name) : typed(form.webhook_header_name),
        webhook_header_value: typed(form.webhook_header_value),
      }
    case 'email':
      return {
        email_recipients: form.email_recipients,
        email_from_address: editing ? orNull(form.email_from_address) : typed(form.email_from_address),
        email_subject_template: editing
          ? orNull(form.email_subject_template)
          : typed(form.email_subject_template),
      }
    case 'jira':
      return {
        jira_base_url: form.jira_base_url,
        jira_auth_email: form.jira_auth_email,
        jira_api_token: typed(form.jira_api_token),
        jira_project_key: form.jira_project_key,
        // Emptied means "the default the placeholder shows", never "absent":
        // an absent key is skipped by update, so clearing a stored "Bug" and
        // pressing Save used to close the dialog and keep "Bug".
        jira_issue_type: form.jira_issue_type || 'Task',
      }
    case 'linear':
      return {
        linear_api_key: typed(form.linear_api_key),
        linear_team_id: form.linear_team_id,
        linear_state_id: editing ? orNull(form.linear_state_id) : typed(form.linear_state_id),
        linear_label_ids: editing ? orNull(form.linear_label_ids) : typed(form.linear_label_ids),
      }
    case 'demo_sink':
      // A local sink carries no channel configuration at all, and the API
      // refuses any it is sent: name, switch and schedule only.
      return {}
  }
}

/**
 * Client-side checks the inputs' own `required` cannot express, keyed by the
 * payload field they belong beside.
 *
 * The webhook header is a PAIR — the API refuses a name without a value on
 * create — and nothing on the form said so: a name typed alone failed as a raw
 * 422, and on edit a new name with no value and nothing stored was sent and
 * silently produced a header with no secret.
 */
export function destinationFormProblems(
  form: DestinationFormState,
  existing: AlertDestination | null,
  { removeWebhookHeader = false }: { removeWebhookHeader?: boolean } = {},
): Partial<Record<keyof DestinationFormState, string>> {
  if (form.type !== 'webhook' || removeWebhookHeader) return {}
  const name = form.webhook_header_name.trim()
  const value = form.webhook_header_value.trim()
  const storedName = existing?.webhook_header_name ?? null
  if (value && !name) {
    return { webhook_header_name: 'Name the header this secret is sent in.' }
  }
  // A value is needed alongside a name unless one is already stored for it.
  if (name && !value && !storedName) {
    return { webhook_header_value: 'Enter the value sent in this header, or clear its name.' }
  }
  if (!name && storedName) {
    return {
      webhook_header_name: 'Use "Remove secret header" to stop sending it, or keep its name.',
    }
  }
  return {}
}

/** Human names for the payload keys a server error can point at. */
export const DESTINATION_FIELD_LABELS: Readonly<Record<string, string>> = {
  type: 'Channel',
  name: 'Name',
  enabled: 'Destination enabled',
  delivery_schedule_cron: 'Delivery schedule',
  webhook_url: 'Webhook URL',
  bot_token: 'Bot token',
  chat_id: 'Chat ID',
  target_url: 'Target URL',
  webhook_header_name: 'Secret header name',
  webhook_header_value: 'Secret header value',
  email_recipients: 'Recipients',
  email_from_address: 'From address',
  email_subject_template: 'Subject template',
  jira_base_url: 'Base URL',
  jira_auth_email: 'Auth email',
  jira_api_token: 'API token',
  jira_project_key: 'Project key',
  jira_issue_type: 'Issue type',
  linear_api_key: 'API key',
  linear_team_id: 'Team ID',
  linear_state_id: 'State ID',
  linear_label_ids: 'Label IDs',
}
