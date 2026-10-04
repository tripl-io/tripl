import { REQUIRED_MESSAGE } from '@/components/forms/validation'

import type { DestinationFormState } from './constants'
import { DESTINATION_FIELD_LABELS } from './destinationForm'
import type { SplitFieldErrors } from './fieldErrors'

type FieldKey = keyof DestinationFormState

/**
 * The backend's own names for the destination fields, as its validators start
 * their sentences ("Webhook target_url must …", alerting_validation.py), mapped
 * to the input each one is about.
 */
const SERVER_FIELD_PREFIXES: readonly (readonly [RegExp, FieldKey])[] = [
  [/^Slack webhook_url\b/i, 'webhook_url'],
  [/^Telegram bot_token\b/i, 'bot_token'],
  [/^Telegram chat_id\b/i, 'chat_id'],
  [/^Webhook target_url\b/i, 'target_url'],
  [/^Webhook header name\b/i, 'webhook_header_name'],
  [/^Webhook header value\b/i, 'webhook_header_value'],
  [/^Jira base_url\b/i, 'jira_base_url'],
  [/^Jira auth_email\b/i, 'jira_auth_email'],
  [/^Jira api_token\b/i, 'jira_api_token'],
  [/^Jira project_key\b/i, 'jira_project_key'],
  [/^Jira issue_type\b/i, 'jira_issue_type'],
  [/^Linear api_key\b/i, 'linear_api_key'],
  [/^Linear team_id\b/i, 'linear_team_id'],
  [/^Linear state_id\b/i, 'linear_state_id'],
  [/^Linear label_ids\b/i, 'linear_label_ids'],
]

/** The validator tails that have a plainer way to say them. */
function plainTail(rest: string): string | null {
  if (/^must be a valid https URL$/i.test(rest)) return 'Use an https:// URL.'
  if (/^must not point to a private or internal address$/i.test(rest)) {
    return "Private or internal addresses aren't allowed."
  }
  if (/^host could not be resolved$/i.test(rest)) return "This host couldn't be found. Check the address."
  const hosts = /^must point to one of: (.+)$/i.exec(rest)
  if (hosts) return `Use a URL on ${hosts[1]}.`
  if (/^is required$/i.test(rest)) return REQUIRED_MESSAGE
  return null
}

/**
 * One server sentence about a destination field, in the form's words — and
 * which field it is about, when it names one.
 *
 * "Webhook target_url must be a valid https URL" named the API field and sat
 * above the footer, not under Target URL. A sentence that does not start with
 * a backend field name is returned unchanged with no field: it is already in
 * words ("Slack webhook URL must start with https://hooks.slack.com/").
 */
export function describeDestinationServerError(message: string): { field: FieldKey | null; text: string } {
  const text = message.trim()
  for (const [pattern, field] of SERVER_FIELD_PREFIXES) {
    const match = pattern.exec(text)
    if (!match) continue
    const rest = text.slice(match[0].length).trim()
    const label = DESTINATION_FIELD_LABELS[field] ?? field
    return { field, text: plainTail(rest) ?? `${label} ${rest}` }
  }
  return { field: null, text }
}

/**
 * {@link splitApiFieldErrors}' result with every message put into words and a
 * whole-request refusal that names an input moved under that input.
 *
 * The second half matters for the SSRF guard: it runs after body parsing and
 * answers with a plain-string 422, which `splitApiFieldErrors` can only file
 * as the form-level message — above the footer, away from the URL it is about.
 */
export function attachDestinationServerErrors<K extends string>(
  split: SplitFieldErrors<K>,
  known: readonly K[],
): SplitFieldErrors<K> {
  const fields: Partial<Record<K, string>> = {}
  for (const [key, value] of Object.entries(split.fields) as [K, string | undefined][]) {
    if (value) fields[key] = describeDestinationServerError(value).text
  }
  let message = split.message
  if (message) {
    const described = describeDestinationServerError(message)
    const field = described.field as K | null
    if (field && known.includes(field) && !fields[field]) {
      fields[field] = described.text
      message = null
    } else {
      message = described.text
    }
  }
  return { fields, message }
}
