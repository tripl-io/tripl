import { MAX_INBOX_NOTE_LENGTH } from "@/api/alerting"
import type {
  AlertDestinationType,
  AlertMessageFormat,
  AlertRule,
  AlertRuleFilterField,
  AlertRuleFilterOperator,
  AlertRuleFilterPayload,
} from "@/types"

// User-selectable channels for creating a real destination. The demo-only
// ``demo_sink`` (a local, non-sendable sink) is intentionally excluded here — it
// is created by the demo seeder, never through this create UI.
export type DestinationChannel = 'slack' | 'telegram' | 'webhook' | 'email' | 'jira' | 'linear'

export type DestinationFormState = {
  // Any existing destination's type when editing (incl. the read-only
  // ``demo_sink``); the create flow only ever sets a ``DestinationChannel``.
  type: AlertDestinationType
  name: string
  enabled: boolean
  webhook_url: string
  bot_token: string
  chat_id: string
  target_url: string
  webhook_header_name: string
  webhook_header_value: string
  email_recipients: string
  email_from_address: string
  email_subject_template: string
  jira_base_url: string
  jira_auth_email: string
  jira_api_token: string
  jira_project_key: string
  jira_issue_type: string
  linear_api_key: string
  linear_team_id: string
  linear_state_id: string
  linear_label_ids: string
  // The cadence as a cron string, or '' for immediate. The form edits it
  // through the presets in ./deliverySchedule; this is the value that ships.
  delivery_schedule_cron: string
}

export type RuleFilterDraft = {
  uid: string
  field: AlertRuleFilterField
  operator: AlertRuleFilterOperator
  values: string[]
}

export type RuleFormState = {
  name: string
  enabled: boolean
  // '' is the "All scans" option — the API wants null there, so
  // `ruleFormToPayload` converts. Radix Select cannot hold an empty value, so
  // the picker itself uses the `ALL_SCANS_OPTION` sentinel.
  scan_config_id: string
  include_project_total: boolean
  include_event_types: boolean
  include_events: boolean
  include_schema_drifts: boolean
  include_distribution_drifts: boolean
  include_release_regressions: boolean
  include_variable_value_drifts: boolean
  include_metrics: boolean
  include_source_freshness: boolean
  include_lifecycle: boolean
  include_property_drifts: boolean
  notify_on_spike: boolean
  notify_on_drop: boolean
  ai_explanation_enabled: boolean
  // Also email the owners of each affected event type / metric (F07, #260).
  notify_owners: boolean
  // The four numeric settings are held as the TEXT in their inputs and parsed
  // by `ruleFormToPayload` (ALR-16). Holding them as numbers made an emptied
  // box read back as "0" on the next render — the field could not be cleared
  // to retype, and a cleared cooldown shipped as 0, which the API refuses.
  min_percent_delta: string
  min_absolute_delta: string
  min_expected_count: string
  cooldown_minutes: string
  message_template: string
  items_template: string
  message_format: AlertMessageFormat
  filters: RuleFilterDraft[]
}

export const FILTER_FIELD_OPTIONS: { value: AlertRuleFilterField; label: string }[] = [
  { value: 'event_type', label: 'Event type' },
  { value: 'event', label: 'Event' },
  { value: 'direction', label: 'Direction' },
  // A catalog metric, by its definition id (JR-15). Only catalog-metric signals
  // carry one, so every other signal passes a metric filter through.
  { value: 'metric', label: 'Metric' },
]

// Words, not SQL: a filter row reads as a sentence — "Event type · is one
// of · Checkout started" — where "IN" / "!=" asked a PM to read a query (AL-39).
export const FILTER_OPERATOR_OPTIONS: { value: AlertRuleFilterOperator; label: string }[] = [
  { value: 'eq', label: 'is' },
  { value: 'ne', label: 'is not' },
  { value: 'in', label: 'is one of' },
  { value: 'not_in', label: 'is not one of' },
]

export const DIRECTION_VALUE_OPTIONS = [
  { value: 'up', label: 'Spike (up)' },
  { value: 'down', label: 'Drop (down)' },
]

export function isSingleValueOperator(operator: AlertRuleFilterOperator) {
  return operator === 'eq' || operator === 'ne'
}

export function makeFilterUid() {
  return `f-${Math.random().toString(36).slice(2, 10)}`
}

export const TEMPLATE_VARIABLE_OPTIONS = [
  { name: 'project_name', description: 'Project display name' },
  { name: 'project_slug', description: 'Project slug' },
  { name: 'channel', description: 'Destination channel' },
  { name: 'destination_name', description: 'Destination name' },
  { name: 'rule_name', description: 'Rule name' },
  { name: 'scan_name', description: 'Scan name' },
  { name: 'matched_count', description: 'Number of matched alert items' },
  { name: 'items_count', description: 'Alias for matched_count' },
  { name: 'items_text', description: 'Preformatted list of all matched alert items' },
  // Digest-only, and listed because the backend validator already accepts them:
  // an operator writing a custom message template for a destination on a
  // cadence otherwise has no way to learn the three slots the default digest
  // layout is built from.
  {
    name: 'headline',
    description:
      'Digest summary line, e.g. "24 alerts · 7 down, 17 up · worst checkout down 86%". Describes the whole digest even when it takes several messages (digests only)',
  },
  {
    name: 'window_label',
    description:
      'The period a digest covers, in the project timezone, plus a "2/3" marker on the rare digest that needs more than one message (digests only)',
  },
  {
    name: 'ai_explanation_block',
    description:
      'The AI note with its trailing blank line, or empty when there is none (digests only)',
  },
] as const

export const ITEM_TEMPLATE_VARIABLE_OPTIONS = [
  { name: 'scope_name', description: 'Matched scope name' },
  { name: 'scope_type', description: 'Matched scope type' },
  { name: 'scope_label', description: 'Matched scope label' },
  { name: 'direction', description: 'Direction: spike or drop' },
  { name: 'direction_label', description: 'Direction: up or down' },
  { name: 'direction_arrow', description: 'A single up/down arrow for the direction' },
  {
    name: 'scope_link',
    description:
      'Scope name linked to its incident on formats that support links; the bare name on plain',
  },
  { name: 'actual_count', description: 'Actual count' },
  { name: 'expected_count', description: 'Expected count' },
  {
    name: 'expected_basis',
    description:
      'Says what the expected count was built from, when it is not a plain baseline — e.g. " (adoption-adjusted)" on a release regression. Empty for every other scope.',
  },
  { name: 'absolute_delta', description: 'Absolute delta' },
  // Named here rather than only in the docs because this list is what an
  // operator with a saved custom template reads while editing it: a template
  // written before `percent_delta_label` existed still prints "0.0%" at a zero
  // baseline, and nothing may rewrite it for them.
  { name: 'percent_delta', description: 'Percent delta as a bare number. Prints 0 when there was no baseline, so prefer percent_delta_label unless you need the raw number' },
  { name: 'percent_delta_label', description: 'Percent delta with its "%" sign, or "no baseline" when expected is 0' },
  { name: 'bucket', description: 'Anomaly bucket timestamp' },
  { name: 'details_url', description: 'Details URL' },
  { name: 'monitoring_url', description: 'Monitoring URL' },
  { name: 'details_line', description: 'Rendered details line with leading newline when URL exists' },
  { name: 'monitoring_line', description: 'Rendered monitoring line with leading newline when URL exists' },
  { name: 'drift_field', description: 'Drift field name; the property for value and property drift' },
  { name: 'drift_type', description: 'Drift type; for property drift new_property, missing_required or type_change' },
  { name: 'sample_value', description: 'Drift sample value' },
  { name: 'drift_line', description: 'Rendered schema drift line with leading newline when drift context exists' },
  { name: 'sparkline', description: 'ASCII sparkline of recent bucket counts (empty if no history)' },
  { name: 'sparkline_line', description: 'Rendered sparkline with leading newline when history exists' },
  { name: 'top_movers', description: 'Inline summary of top-3 breakdown movers (empty if none)' },
  { name: 'top_movers_line', description: 'Rendered top-movers line with leading newline when movers exist' },
  {
    name: 'attribution',
    description:
      'Why it moved, as stored at detection time: the breakdown value that explains the delta, e.g. "92% of the drop comes from platform = ios (−3,120 of −3,390)", plus a release that crossed the activation gate in the window (empty if none)',
  },
  { name: 'attribution_line', description: 'Rendered attribution line with leading newline when one exists' },
] as const

export const DEFAULT_MESSAGE_TEMPLATES: Record<AlertMessageFormat, string> = {
  plain: [
    '[tripl] ${matched_count} alerts',
    'Project delivery via ${channel}: ${destination_name}',
    'Rule: ${rule_name}',
    'Scan: ${scan_name}',
    '',
    '${items_text}',
  ].join('\n'),
  slack_mrkdwn: [
    '*[tripl] ${matched_count} alerts*',
    'Project delivery via ${channel}: ${destination_name}',
    'Rule: *${rule_name}*',
    'Scan: `${scan_name}`',
    '',
    '${items_text}',
  ].join('\n'),
  telegram_html: [
    '<b>[tripl] ${matched_count} alerts</b>',
    'Project delivery via ${channel}: ${destination_name}',
    'Rule: <b>${rule_name}</b>',
    'Scan: <code>${scan_name}</code>',
    '',
    '${items_text}',
  ].join('\n'),
  telegram_markdownv2: [
    '*tripl: ${matched_count} alerts*',
    'Project delivery via ${channel}: ${destination_name}',
    'Rule: *${rule_name}*',
    'Scan: `${scan_name}`',
    '',
    '${items_text}',
  ].join('\n'),
}

// `${percent_delta_label}` rather than a bare `${percent_delta}%`: the label
// carries its own unit, so it says "no baseline" for an item whose expected
// count is 0 instead of printing the undefined ratio as "0.0%".
//
// MIRRORS backend `alert_templates.DEFAULT_ALERT_ITEMS_TEMPLATES`, character for
// character, and a backend test asserts that. This is not decoration: the rule
// editor PREFILLS its editable textarea from here, so a variable missing below
// is a variable every hand-edited rule silently drops. That is how
// `${expected_basis}` — the qualifier that stops a release regression reading as
// a raw-count comparison — would have been lost, along with `${top_movers_line}`
// and `${sparkline_line}`, which had already drifted out unnoticed.
export const DEFAULT_ITEMS_TEMPLATES: Record<AlertMessageFormat, string> = {
  plain: '- ${scope_label} ${scope_name}: ${direction_label}, actual=${actual_count}, expected=${expected_count}${expected_basis}, delta=${absolute_delta} (${percent_delta_label})${drift_line}${details_line}${monitoring_line}${attribution_line}${top_movers_line}${sparkline_line}',
  slack_mrkdwn: '- ${scope_label} ${scope_name}: ${direction_label}, actual=${actual_count}, expected=${expected_count}${expected_basis}, delta=${absolute_delta} (${percent_delta_label})${drift_line}${details_line}${monitoring_line}${attribution_line}${top_movers_line}${sparkline_line}',
  telegram_html: '- ${scope_label} ${scope_name}: ${direction_label}, actual=${actual_count}, expected=${expected_count}${expected_basis}, delta=${absolute_delta} (${percent_delta_label})${drift_line}${details_line}${monitoring_line}${attribution_line}${top_movers_line}${sparkline_line}',
  telegram_markdownv2: '\\- ${scope_label} ${scope_name}: ${direction_label}, actual=${actual_count}, expected=${expected_count}${expected_basis}, delta=${absolute_delta} \\(${percent_delta_label}\\)${drift_line}${details_line}${monitoring_line}${attribution_line}${top_movers_line}${sparkline_line}',
}

export const MESSAGE_FORMAT_OPTIONS: Record<AlertDestinationType, { value: AlertMessageFormat; label: string }[]> = {
  slack: [
    { value: 'plain', label: 'Plain text' },
    { value: 'slack_mrkdwn', label: 'Slack mrkdwn' },
  ],
  telegram: [
    { value: 'plain', label: 'Plain text' },
    { value: 'telegram_html', label: 'Telegram HTML' },
    { value: 'telegram_markdownv2', label: 'Telegram MarkdownV2' },
  ],
  webhook: [
    { value: 'plain', label: 'Plain text' },
  ],
  email: [
    { value: 'plain', label: 'Plain text' },
  ],
  jira: [
    { value: 'plain', label: 'Plain text' },
  ],
  linear: [
    { value: 'plain', label: 'Plain text' },
  ],
  // Local demo sink renders plain text only (recorded locally, never sent).
  demo_sink: [
    { value: 'plain', label: 'Plain text' },
  ],
}

export const FORMAT_HELP: Record<AlertMessageFormat, string[]> = {
  plain: [
    'No rich formatting. Variables are inserted as plain text.',
  ],
  slack_mrkdwn: [
    '*bold*',
    '_italic_',
    '~strike~',
    '`code`',
    'Slack mrkdwn does not support underline.',
  ],
  telegram_html: [
    '<b>bold</b>',
    '<i>italic</i>',
    '<u>underline</u>',
    '<s>strike</s>',
    '<code>code</code>',
  ],
  telegram_markdownv2: [
    '*bold*',
    '_italic_',
    '__underline__',
    '~strike~',
    '`code`',
  ],
}

export function defaultDestinationForm(type: DestinationChannel): DestinationFormState {
  return {
    type,
    name: '',
    enabled: true,
    webhook_url: '',
    bot_token: '',
    chat_id: '',
    target_url: '',
    webhook_header_name: '',
    webhook_header_value: '',
    email_recipients: '',
    email_from_address: '',
    email_subject_template: '',
    jira_base_url: '',
    jira_auth_email: '',
    jira_api_token: '',
    jira_project_key: '',
    jira_issue_type: type === 'jira' ? 'Task' : '',
    linear_api_key: '',
    linear_team_id: '',
    linear_state_id: '',
    linear_label_ids: '',
    delivery_schedule_cron: '',
  }
}

export function getDefaultMessageTemplate(messageFormat: AlertMessageFormat): string {
  return DEFAULT_MESSAGE_TEMPLATES[messageFormat]
}

export function getDefaultItemsTemplate(messageFormat: AlertMessageFormat): string {
  return DEFAULT_ITEMS_TEMPLATES[messageFormat]
}

export function normalizeRuleTemplate(value: string | null | undefined): string {
  return (value ?? '').trim()
}

export function isDefaultMessageTemplate(
  value: string | null | undefined,
  messageFormat: AlertMessageFormat,
): boolean {
  return normalizeRuleTemplate(value) === normalizeRuleTemplate(getDefaultMessageTemplate(messageFormat))
}

export function isDefaultItemsTemplate(
  value: string | null | undefined,
  messageFormat: AlertMessageFormat,
): boolean {
  return normalizeRuleTemplate(value) === normalizeRuleTemplate(getDefaultItemsTemplate(messageFormat))
}

// Radix Select rejects an empty string as an item value, so "every scan in the
// project" needs a sentinel in the picker even though the wire value is null.
export const ALL_SCANS_OPTION = 'all'

/**
 * Said wherever project-wide detection is off (AL-45): on Detection settings
 * and above Alerting's rules, which cannot fire while it is.
 */
export const DETECTION_OFF_MESSAGE =
  'Detection is off for this project. No new signals are raised, so no alert rule can fire.'

/** The percent gate a new rule starts at (AL-2); see `defaultRuleForm`. */
export const DEFAULT_RULE_MIN_PERCENT_DELTA = 30

export function defaultRuleForm(): RuleFormState {
  return {
    name: '',
    enabled: true,
    scan_config_id: '',
    include_project_total: true,
    include_event_types: true,
    include_events: true,
    include_schema_drifts: false,
    include_distribution_drifts: false,
    include_release_regressions: false,
    include_variable_value_drifts: false,
    include_metrics: false,
    // Off by default like the drift kinds: an opt-in "data is late" alert
    // (F16, #269), not a change to what an existing kind of rule sends.
    include_source_freshness: false,
    // Opt-in too: lifecycle findings (#258) are a new kind of alert, not a
    // change to what an existing rule sends.
    include_lifecycle: false,
    // Opt-in like the other drift kinds (F23, #306).
    include_property_drifts: false,
    notify_on_spike: true,
    notify_on_drop: true,
    ai_explanation_enabled: false,
    // Opt-in: a rule keeps sending to its destination only, as before (F07).
    notify_owners: false,
    // 30, not the server's old 100 (AL-2). The gate is
    // |actual − expected| / expected × 100, so a DROP can reach at most 100% —
    // and only when volume falls to zero. At 100 the obvious "tell me when X
    // drops" rule ignored a 50% or a 90% fall. 30% is a move worth hearing
    // about in either direction; the backend default is the same
    // (DEFAULT_MIN_PERCENT_DELTA = 30 in backend/src/tripl/models/alert_rule.py).
    min_percent_delta: String(DEFAULT_RULE_MIN_PERCENT_DELTA),
    min_absolute_delta: '0',
    min_expected_count: '0',
    cooldown_minutes: '1440',
    message_template: getDefaultMessageTemplate('plain'),
    items_template: getDefaultItemsTemplate('plain'),
    message_format: 'plain',
    filters: [],
  }
}

export function ruleToForm(rule: AlertRule): RuleFormState {
  return {
    name: rule.name,
    enabled: rule.enabled,
    scan_config_id: rule.scan_config_id ?? '',
    include_project_total: rule.include_project_total,
    include_event_types: rule.include_event_types,
    include_events: rule.include_events,
    include_schema_drifts: rule.include_schema_drifts,
    include_distribution_drifts: rule.include_distribution_drifts,
    include_release_regressions: rule.include_release_regressions,
    include_variable_value_drifts: rule.include_variable_value_drifts,
    include_metrics: rule.include_metrics,
    // `?? false`: a rule from a server that predates the flag reads as off.
    include_source_freshness: rule.include_source_freshness ?? false,
    include_lifecycle: rule.include_lifecycle ?? false,
    include_property_drifts: rule.include_property_drifts ?? false,
    notify_on_spike: rule.notify_on_spike,
    notify_on_drop: rule.notify_on_drop,
    ai_explanation_enabled: rule.ai_explanation_enabled,
    // `?? false`: a rule from a server that predates owner routing reads as off.
    notify_owners: rule.notify_owners ?? false,
    min_percent_delta: String(rule.min_percent_delta),
    min_absolute_delta: String(rule.min_absolute_delta),
    min_expected_count: String(rule.min_expected_count),
    cooldown_minutes: String(rule.cooldown_minutes),
    message_template: rule.message_template ?? getDefaultMessageTemplate(rule.message_format),
    items_template: rule.items_template ?? getDefaultItemsTemplate(rule.message_format),
    message_format: rule.message_format,
    filters: rule.filters.map(filter => ({
      uid: filter.id,
      field: filter.field,
      operator: filter.operator,
      values: [...filter.values],
    })),
  }
}

export function ruleFormToPayload(ruleForm: RuleFormState) {
  const normalizedTemplate = normalizeRuleTemplate(ruleForm.message_template)
  const normalizedItemsTemplate = normalizeRuleTemplate(ruleForm.items_template)
  // Every row goes on the wire, an empty one included. Dropping a row with no
  // values used to save the rule WITHOUT the filter the form still showed, so
  // it matched everything (ALR-5). `ruleFormProblems` refuses the submit
  // instead, naming the row; should a caller skip it, the API's own "Filter
  // must have at least one value" is the right answer, not a broader rule.
  const filters: AlertRuleFilterPayload[] = ruleForm.filters.map(filter => ({
    field: filter.field,
    operator: filter.operator,
    values: isSingleValueOperator(filter.operator)
      ? filter.values.slice(0, 1)
      : filter.values,
  }))
  const { filters: _ignored, ...rest } = ruleForm
  void _ignored
  return {
    ...rest,
    min_percent_delta: Number(ruleForm.min_percent_delta.trim()),
    min_absolute_delta: Number(ruleForm.min_absolute_delta.trim()),
    min_expected_count: Number(ruleForm.min_expected_count.trim()),
    cooldown_minutes: Number(ruleForm.cooldown_minutes.trim()),
    // Explicit null, never omitted: PATCH distinguishes "not mentioned" from
    // "widen this rule back to the whole project".
    scan_config_id: ruleForm.scan_config_id || null,
    filters,
    message_template:
      !normalizedTemplate || isDefaultMessageTemplate(normalizedTemplate, ruleForm.message_format)
        ? null
        : normalizedTemplate,
    items_template:
      !normalizedItemsTemplate || isDefaultItemsTemplate(normalizedItemsTemplate, ruleForm.message_format)
        ? null
        : normalizedItemsTemplate,
  }
}

/** The rule setting a numeric input edits. */
export type RuleNumericField =
  | 'min_percent_delta'
  | 'min_absolute_delta'
  | 'min_expected_count'
  | 'cooldown_minutes'

/**
 * Why the rule form cannot be saved as it stands, per field.
 *
 * Each of these used to reach the API and come back as a raw 422 at the bottom
 * of the dialog — or, worse, not reach it at all and save something else: an
 * emptied cooldown shipped as 0 (ALR-16), a filter row with no values was
 * dropped so the rule matched everything (ALR-5), and a rule with no scope or
 * no direction saved and could never fire (ALR-14, ALR-15). The bounds mirror
 * `AlertRuleBase` (backend schemas/alerting.py): the three thresholds are
 * `ge=0` floats and the cooldown an `int` with `ge=1`.
 */
export interface RuleFormProblems {
  numeric: Partial<Record<RuleNumericField, string>>
  scopes: string | null
  direction: string | null
  /** Keyed by the filter row's `uid`. */
  filters: Record<string, string>
}

const SCOPE_KEYS = [
  'include_project_total',
  'include_event_types',
  'include_events',
  'include_schema_drifts',
  'include_distribution_drifts',
  'include_release_regressions',
  'include_variable_value_drifts',
  'include_metrics',
  'include_source_freshness',
  'include_lifecycle',
  'include_property_drifts',
] as const satisfies readonly (keyof RuleFormState)[]

function numberProblem(text: string, { integer, min }: { integer: boolean; min: number }): string | null {
  const trimmed = text.trim()
  if (trimmed === '') return 'Enter a number.'
  const value = Number(trimmed)
  if (!Number.isFinite(value)) return 'Enter a number.'
  if (integer && !Number.isInteger(value)) return 'Enter a whole number of minutes (or pick a larger unit).'
  if (value < min) return min === 0 ? 'Enter 0 or more.' : `Enter ${min} or more.`
  return null
}

export function ruleFormProblems(ruleForm: RuleFormState): RuleFormProblems {
  const numeric: RuleFormProblems['numeric'] = {}
  const cooldown = numberProblem(ruleForm.cooldown_minutes, { integer: true, min: 1 })
  if (cooldown) numeric.cooldown_minutes = cooldown
  for (const field of ['min_percent_delta', 'min_absolute_delta', 'min_expected_count'] as const) {
    const problem = numberProblem(ruleForm[field], { integer: false, min: 0 })
    if (problem) numeric[field] = problem
  }
  const filters: Record<string, string> = {}
  for (const filter of ruleForm.filters) {
    if (filter.values.length === 0) {
      filters[filter.uid] = 'Pick at least one value, or remove this filter.'
    }
  }
  return {
    numeric,
    scopes: SCOPE_KEYS.some(key => ruleForm[key])
      ? null
      : 'Pick at least one signal kind — a rule with none can never fire.',
    direction: ruleForm.notify_on_spike || ruleForm.notify_on_drop
      ? null
      : 'Pick at least one direction — a rule with none can never fire.',
    filters,
  }
}

export function hasRuleFormProblems(problems: RuleFormProblems): boolean {
  return (
    Object.keys(problems.numeric).length > 0
    || problems.scopes !== null
    || problems.direction !== null
    || Object.keys(problems.filters).length > 0
  )
}

/**
 * The message format a rule keeps when it is pointed at another destination.
 *
 * Formats are per channel, so `slack_mrkdwn` chosen for a Slack destination is
 * refused by the API once the rule routes to Telegram (ALR-4) — and the format
 * Select rendered blank, because the value was not among its options. A format
 * the new channel supports is kept; anything else falls back to plain text,
 * which every channel accepts.
 */
export function messageFormatForDestination(
  current: AlertMessageFormat,
  destinationType: AlertDestinationType,
): AlertMessageFormat {
  return MESSAGE_FORMAT_OPTIONS[destinationType].some(option => option.value === current)
    ? current
    : 'plain'
}

/**
 * Switch a rule draft to another message format, carrying the templates along
 * when they are still the defaults of the format being left (a hand-edited
 * template is the operator's and is kept as written).
 */
export function withMessageFormat(
  ruleForm: RuleFormState,
  messageFormat: AlertMessageFormat,
): RuleFormState {
  if (messageFormat === ruleForm.message_format) return ruleForm
  const shouldResetTemplate =
    !normalizeRuleTemplate(ruleForm.message_template)
    || isDefaultMessageTemplate(ruleForm.message_template, ruleForm.message_format)
  const shouldResetItemsTemplate =
    !normalizeRuleTemplate(ruleForm.items_template)
    || isDefaultItemsTemplate(ruleForm.items_template, ruleForm.message_format)
  return {
    ...ruleForm,
    message_format: messageFormat,
    message_template: shouldResetTemplate
      ? getDefaultMessageTemplate(messageFormat)
      : ruleForm.message_template,
    items_template: shouldResetItemsTemplate
      ? getDefaultItemsTemplate(messageFormat)
      : ruleForm.items_template,
  }
}

// The backend's own token pattern (`_ALERT_TEMPLATE_VAR_RE` in
// alert_templates.py), so the two agree about what counts as a variable.
const TEMPLATE_VARIABLE_PATTERN = /\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g

/**
 * Variables in a template that the given list does not know, in first-seen
 * order (ALR-21). A typo such as `${scope_nme}`, or an item variable used in
 * the message template, used to surface only as a 422 after submit, printed
 * far below the textarea it was about.
 */
export function unknownTemplateVariables(
  template: string,
  options: readonly { name: string }[],
): string[] {
  const known = new Set(options.map(option => option.name))
  const unknown: string[] = []
  for (const match of template.matchAll(TEMPLATE_VARIABLE_PATTERN)) {
    const name = match[1]
    if (name !== undefined && !known.has(name) && !unknown.includes(name)) unknown.push(name)
  }
  return unknown
}

/**
 * How much room is left in a note, said only once it is worth saying.
 *
 * Both note editors hand {@link MAX_INBOX_NOTE_LENGTH} to `maxLength`, and a
 * `maxLength` that has been reached does not warn, error or truncate visibly —
 * it silently stops accepting keystrokes. Somebody pasting a stack trace into an
 * incident note therefore watches their typing stop with nothing on screen
 * saying why, and the obvious reading is that the page has frozen.
 *
 * Returns `null` for the overwhelmingly common case, because a counter that is
 * always on screen is noise on every note anyone actually writes: incident notes
 * are a sentence, and 2000 characters is roughly a page and a half. It appears
 * only inside the last {@link NOTE_BUDGET_WARNING_AT} characters — far enough
 * ahead that a reader can still choose to be brief, close enough not to be
 * decoration.
 *
 * The exhausted case gets its own sentence rather than "0 characters left",
 * which states the number without stating the consequence — and the consequence
 * (keystrokes are being dropped right now) is the entire reason this exists.
 */
export const NOTE_BUDGET_WARNING_AT = 200

export function noteBudgetLabel(length: number): string | null {
  const remaining = MAX_INBOX_NOTE_LENGTH - length
  if (remaining > NOTE_BUDGET_WARNING_AT) return null
  // `<= 0` and not `=== 0`: `maxLength` makes a negative unreachable by typing,
  // but a draft restored from state is not typing, and "-12 characters left"
  // would be the one number here nobody can act on.
  if (remaining <= 0) return 'Full — further characters are not being accepted'
  return `${remaining} characters left`
}

export function formatCooldown(minutes: number) {
  if (minutes % 1440 === 0) return `${minutes / 1440}d`
  if (minutes % 60 === 0) return `${minutes / 60}h`
  return `${minutes}m`
}

/** "1 day", "6 hours", "45 minutes" — the cooldown in words, for sentences. */
export function formatCooldownLong(minutes: number): string {
  const unit = COOLDOWN_UNITS.find(option => minutes > 0 && minutes % option.minutes === 0)
    ?? COOLDOWN_UNITS[COOLDOWN_UNITS.length - 1]!
  const amount = minutes / unit.minutes
  return `${amount} ${amount === 1 ? unit.singular : unit.value}`
}

export type CooldownUnit = 'days' | 'hours' | 'minutes'

/** Largest first: `splitCooldown` picks the largest unit that divides evenly. */
export const COOLDOWN_UNITS: readonly {
  value: CooldownUnit
  singular: string
  minutes: number
}[] = [
  { value: 'days', singular: 'day', minutes: 1440 },
  { value: 'hours', singular: 'hour', minutes: 60 },
  { value: 'minutes', singular: 'minute', minutes: 1 },
]

/**
 * The cooldown as the editor shows it: an amount and a unit, instead of the
 * raw "1440" the API stores (AL-6). The form keeps `cooldown_minutes` as the
 * text that ships, so these two must round-trip: `joinCooldown(splitCooldown(x))`
 * is `x` for every string, a half-typed or invalid one included — the dialog
 * re-derives its amount/unit pair whenever the two disagree, and a pair that
 * did not round-trip would re-derive forever.
 */
export function splitCooldown(minutesText: string): { amount: string; unit: CooldownUnit } {
  // Only a canonical positive integer is converted; anything else ('', '0',
  // ' 45 ', '1.5', 'abc') stays verbatim in minutes, where joining is identity.
  if (!/^[1-9]\d*$/.test(minutesText)) return { amount: minutesText, unit: 'minutes' }
  const minutes = Number(minutesText)
  const unit = COOLDOWN_UNITS.find(option => minutes % option.minutes === 0)!
  return { amount: String(minutes / unit.minutes), unit: unit.value }
}

export function joinCooldown(amount: string, unit: CooldownUnit): string {
  if (unit === 'minutes') return amount
  const trimmed = amount.trim()
  const value = Number(trimmed)
  // Left as typed when it is not a number: `ruleFormProblems` names it.
  if (trimmed === '' || !Number.isFinite(value)) return amount
  const factor = COOLDOWN_UNITS.find(option => option.value === unit)!.minutes
  return String(Math.round(value * factor * 1000) / 1000)
}

// `include_lifecycle` is optional here: an `AlertRule` or monitor from a server
// that predates the flag (#258) omits it, and every reader treats absent as off.
type RuleScopeFlags = Partial<Pick<RuleFormState, 'include_lifecycle' | 'include_property_drifts'>> & Pick<
  RuleFormState,
  | 'include_project_total'
  | 'include_event_types'
  | 'include_events'
  | 'include_metrics'
  | 'include_schema_drifts'
  | 'include_distribution_drifts'
  | 'include_variable_value_drifts'
  | 'include_release_regressions'
  | 'include_source_freshness'
>

/**
 * The two kinds of signal a rule listens to (AL-38): volume changes, by the
 * level they are measured at, and the drift detectors. One list feeds the
 * editor's checkboxes, the rule list's condition line and the monitor page's
 * "Watching" chips, so the three cannot name one scope three ways.
 */
export const RULE_SIGNAL_GROUPS: readonly {
  id: 'volume' | 'drift'
  label: string
  hint: string
  scopes: readonly { key: keyof RuleScopeFlags; label: string; short: string; hint: string }[]
}[] = [
  {
    id: 'volume',
    label: 'Volume changes in',
    hint: 'A spike or drop in how often something is tracked.',
    scopes: [
      { key: 'include_project_total', label: 'Project total', short: 'project total', hint: 'Every event in the project, counted together.' },
      { key: 'include_event_types', label: 'Event types', short: 'event types', hint: 'Each event type on its own.' },
      { key: 'include_events', label: 'Events', short: 'events', hint: 'Each event on its own.' },
      { key: 'include_metrics', label: 'Metrics', short: 'metrics', hint: 'Catalog metrics. Project-wide, not tied to a scan.' },
    ],
  },
  {
    id: 'drift',
    label: 'Also alert on',
    hint: 'Changes in what the data looks like, not how much of it there is.',
    scopes: [
      { key: 'include_schema_drifts', label: 'Schema drift', short: 'schema drift', hint: 'A field appears, disappears or changes type.' },
      { key: 'include_distribution_drifts', label: 'Distribution drift', short: 'distribution drift', hint: 'The mix of values in a watched column shifts.' },
      { key: 'include_variable_value_drifts', label: 'Value drift', short: 'value drift', hint: 'A property takes a value outside its documented list.' },
      { key: 'include_property_drifts', label: 'Property drift', short: 'property drift', hint: "An event carries a property its list does not name, a required property goes missing, or a property's values stop matching its type. One alert per drift." },
      { key: 'include_release_regressions', label: 'Release regressions', short: 'release regressions', hint: 'A new app version tracks less than the one before.' },
      { key: 'include_source_freshness', label: 'Source freshness', short: 'source freshness', hint: "A scan's newest data is later than its interval allows, or the scan stopped running. One alert per delay; drop signals are held meanwhile. Needs Drops under Notify on." },
      { key: 'include_lifecycle', label: 'Lifecycle', short: 'lifecycle', hint: 'A deprecated event still receives data after its sunset date, or its successor has received nothing for 7 days. One alert per finding, checked daily.' },
    ],
  },
]

/** The scopes a rule has switched on, by group, in the words the editor uses. */
export function ruleSignalLabels(rule: RuleScopeFlags): { volume: string[]; drift: string[] } {
  const pick = (id: 'volume' | 'drift') =>
    RULE_SIGNAL_GROUPS.find(group => group.id === id)!.scopes
      .filter(scope => rule[scope.key])
      .map(scope => scope.label)
  return { volume: pick('volume'), drift: pick('drift') }
}

/** "Spikes & drops", "Spikes", "Drops" — or null when neither is on. */
export function directionPhrase(rule: Pick<RuleFormState, 'notify_on_spike' | 'notify_on_drop'>): string | null {
  if (rule.notify_on_spike && rule.notify_on_drop) return 'Spikes & drops'
  if (rule.notify_on_spike) return 'Spikes'
  if (rule.notify_on_drop) return 'Drops'
  return null
}

/**
 * The rule list's condition as a sentence (AL-11, JR-15): "Spikes & drops
 * ≥ 30% · 1d cooldown", and on a second line what it watches — so a rule that
 * only watches metrics no longer reads exactly like every other rule.
 */
export function ruleConditionSummary(rule: AlertRule): { condition: string; watches: string } {
  const condition = [
    directionPhrase(rule),
    rule.min_percent_delta > 0 ? `≥ ${rule.min_percent_delta}%` : null,
  ].filter(Boolean).join(' ')
  const labels = ruleSignalLabels(rule)
  const scopes = [...labels.volume, ...labels.drift]
  const watches = [
    scopes.length > 0 ? scopes.join(', ') : 'No signals',
    rule.filters.length > 0 ? countFilters(rule.filters.length) : null,
  ].filter(Boolean).join(' · ')
  return {
    condition: [condition || 'No direction', `${formatCooldown(rule.cooldown_minutes)} cooldown`].join(' · '),
    watches,
  }
}

function countFilters(count: number): string {
  return count === 1 ? '1 filter' : `${count} filters`
}

/**
 * One line above the rule editor's footer saying what Create will set up
 * (AL-1): "Sends to Alerts when any of project total, event types, events
 * spikes or drops by at least 30%, then waits 1 day before alerting on the
 * same scope again."
 */
export function ruleDraftSummary(ruleForm: RuleFormState, destinationName: string | null): string | null {
  if (!destinationName) return null
  const direction = ruleForm.notify_on_spike && ruleForm.notify_on_drop
    ? 'spikes or drops'
    : ruleForm.notify_on_spike ? 'spikes' : ruleForm.notify_on_drop ? 'drops' : null
  if (!direction) return null
  const labels = ruleSignalLabels(ruleForm)
  const scopes = [...labels.volume, ...labels.drift].map(label => label.toLowerCase())
  if (scopes.length === 0) return null
  const subject = ruleForm.filters.length > 0
    ? `a matching ${scopes.length === 1 ? scopes[0] : 'signal'}`
    : scopes.length === 1 ? `one of your ${scopes[0]}` : `any of ${scopes.join(', ')}`
  const percent = Number(ruleForm.min_percent_delta.trim())
  const threshold = ruleForm.min_percent_delta.trim() !== '' && Number.isFinite(percent) && percent > 0
    ? ` by at least ${percent}%`
    : ''
  const cooldown = Number(ruleForm.cooldown_minutes.trim())
  const cadence = Number.isInteger(cooldown) && cooldown >= 1
    ? `, then waits ${formatCooldownLong(cooldown)} before alerting on the same scope again`
    : ''
  return `Sends to ${destinationName} when ${subject} ${direction}${threshold}${cadence}.`
}

export function scopeSummary(rule: AlertRule) {
  return [
    rule.include_project_total ? 'total' : null,
    rule.include_event_types ? 'groups' : null,
    rule.include_events ? 'events' : null,
    rule.include_schema_drifts ? 'schema' : null,
    rule.include_distribution_drifts ? 'distribution' : null,
    rule.include_release_regressions ? 'regressions' : null,
    rule.include_variable_value_drifts ? 'value drift' : null,
    rule.include_metrics ? 'metrics' : null,
    rule.include_source_freshness ? 'freshness' : null,
    rule.include_lifecycle ? 'lifecycle' : null,
    rule.include_property_drifts ? 'property drift' : null,
  ].filter(Boolean).join(', ')
}

export function directionSummary(rule: AlertRule) {
  return [
    rule.notify_on_spike ? 'up' : null,
    rule.notify_on_drop ? 'down' : null,
  ].filter(Boolean).join(' / ')
}

export function findTemplateVariableToken(value: string, cursor: number) {
  const beforeCursor = value.slice(0, cursor)
  const start = beforeCursor.lastIndexOf('${')
  if (start === -1) return null
  if (beforeCursor.indexOf('}', start) !== -1) return null
  const query = beforeCursor.slice(start + 2)
  if (!/^[a-zA-Z0-9_]*$/.test(query)) return null
  return { start, end: cursor, query }
}
