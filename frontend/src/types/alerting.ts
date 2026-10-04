import type { MetricScopeType } from './metrics'

// 'demo_sink' is the demo-only local sink: it renders + records deliveries with
// no outbound network. It appears here so existing destinations/deliveries can
// carry it, but is intentionally kept out of the user-selectable
// `DestinationChannel` create options in pages/alerting/constants.ts.
export type AlertDestinationType =
  | 'slack'
  | 'telegram'
  | 'webhook'
  | 'email'
  | 'jira'
  | 'linear'
  | 'demo_sink'
export type AlertDeliveryStatus = 'pending' | 'sent' | 'failed'
export type AlertMessageFormat =
  | 'plain'
  | 'slack_mrkdwn'
  | 'telegram_html'
  | 'telegram_markdownv2'

export interface AlertRule {
  id: string
  destination_id: string
  // null means every scan in the project — the default, and what a rule created
  // before this field existed carries.
  scan_config_id: string | null
  name: string
  enabled: boolean
  include_project_total: boolean
  include_event_types: boolean
  include_events: boolean
  include_schema_drifts: boolean
  include_distribution_drifts: boolean
  include_release_regressions: boolean
  include_variable_value_drifts: boolean
  include_metrics: boolean
  // "Data is late" alerts: one per late/overdue scan (F16, #269).
  include_source_freshness: boolean
  // Lifecycle alerts: one per open sunset-watch finding (#258). Optional: a
  // server that predates the flag omits it, which reads as off.
  include_lifecycle?: boolean
  // Property drift alerts: one per open property drift (F23, #306).
  // Optional: a server that predates the flag omits it, which reads as off.
  include_property_drifts?: boolean
  notify_on_spike: boolean
  notify_on_drop: boolean
  ai_explanation_enabled: boolean
  // Also email the owners of each affected event type / catalog metric (F07,
  // #260). Optional: a server that predates the flag omits it, read as off.
  notify_owners?: boolean
  min_percent_delta: number
  min_absolute_delta: number
  min_expected_count: number
  cooldown_minutes: number
  message_template: string | null
  items_template: string | null
  message_format: AlertMessageFormat
  filters: AlertRuleFilter[]
  // Manual snooze state, same shape as MonitorSummaryItem: `muted` is the
  // effective flag, `muted_until` the timestamp it lifts at. Carried on the rule
  // itself so the destinations list can show a snoozed rule without a second
  // round-trip to /monitors-summary.
  muted: boolean
  muted_until: string | null
  // Fired-history counters: how often this rule has actually delivered, and how
  // many incidents it carried. A rule with zero of both is configured but has
  // never proven itself — worth saying out loud rather than leaving to guesswork.
  total_deliveries: number
  incident_count: number
  last_delivery_at: string | null
  last_delivery_status: AlertDeliveryStatus | null
  created_at: string
  updated_at: string
}

// `metric` matches a catalog metric's signal by its MetricDefinition id — a
// catalog signal's scope_ref. Every other signal passes it through.
export type AlertRuleFilterField = 'event_type' | 'event' | 'direction' | 'metric'
export type AlertRuleFilterOperator = 'eq' | 'ne' | 'in' | 'not_in'

export interface AlertRuleFilter {
  id: string
  field: AlertRuleFilterField
  operator: AlertRuleFilterOperator
  values: string[]
}

export interface AlertRuleFilterPayload {
  field: AlertRuleFilterField
  operator: AlertRuleFilterOperator
  values: string[]
}

export interface AlertDestination {
  id: string
  project_id: string
  type: AlertDestinationType
  name: string
  enabled: boolean
  webhook_set: boolean
  bot_token_set: boolean
  chat_id: string | null
  target_url_set: boolean
  webhook_header_name: string | null
  email_recipients: string | null
  email_from_address: string | null
  email_subject_template: string | null
  jira_base_url: string | null
  jira_auth_email: string | null
  jira_api_token_set: boolean
  jira_project_key: string | null
  jira_issue_type: string | null
  linear_api_key_set: boolean
  linear_team_id: string | null
  linear_state_id: string | null
  linear_label_ids: string | null
  // Delivery cadence. `null` means immediate — send after every collection,
  // which is what every destination did before this existed. Otherwise a
  // 5-field cron expression read in `project_timezone`.
  delivery_schedule_cron?: string | null
  project_timezone?: string
  last_digest_at?: string | null
  next_digest_at?: string | null
  // Alerts matched and held for the next digest. A cadence puts a
  // destination into "on and quiet" for a whole window by design, and
  // nothing else on the card can tell that from "on and broken".
  //
  // REQUIRED, unlike the schedule fields above it: the backend field carries a
  // default rather than a null, so every response has it. Declared optional it
  // was worse than merely loose — the card reads it as a falsy count and a
  // missing field renders "nothing held", which is a claim about the buffer,
  // not an absence of one. Not representable now.
  held_count: number
  // True for a demo_sink: a local, non-sendable sink badged LOCAL SIMULATED.
  is_local: boolean
  // Traffic this destination has actually carried. A configured destination that
  // has delivered nothing looks identical to a working one in the form, which is
  // the same blind spot the test-send probe closes.
  delivery_count: number
  incident_count: number
  rules: AlertRule[]
  created_at: string
  updated_at: string
}

/**
 * Result of a manual test send. A channel refusal is an ANSWER, not a server
 * fault — a revoked Telegram token and a healthy one look identical in the
 * destination form — so the route answers 200 with `ok: false`
 * and the channel's own message rather than a 5xx the UI would render as
 * "something went wrong on our side".
 */
/** What kind of failure a test send's `error` describes. */
export type DestinationTestErrorKind =
  | 'config'
  | 'policy'
  | 'dns'
  | 'timeout'
  | 'tls'
  | 'network'
  | 'http_status'
  | 'smtp'
  | 'other'

export interface AlertDestinationTestResponse {
  ok: boolean
  error: string | null
  sent_at: string | null
  /** The failure's kind, and the status code when it is `http_status`, so the
   * card reads a field rather than the exception text. Optional:
   * servers and fixtures before them send neither. */
  error_kind?: DestinationTestErrorKind | null
  http_status?: number | null
}

/**
 * `POST /projects/{slug}/alert-destinations/test`: the destination
 * dialog's settings, tested before they are saved. `destination_id` names the
 * saved destination an edit dialog is open on; a secret left blank then means
 * the stored one. Mirrors `AlertDestinationDraftTestRequest`.
 */
export interface AlertDestinationDraftTestRequest {
  destination_id: string | null
  type: AlertDestinationType
  name?: string | null
  webhook_url?: string | null
  bot_token?: string | null
  chat_id?: string | null
  target_url?: string | null
  webhook_header_name?: string | null
  webhook_header_value?: string | null
  email_recipients?: string | null
  email_from_address?: string | null
  jira_base_url?: string | null
  jira_auth_email?: string | null
  jira_api_token?: string | null
  jira_project_key?: string | null
  jira_issue_type?: string | null
  linear_api_key?: string | null
  linear_team_id?: string | null
  linear_state_id?: string | null
  linear_label_ids?: string | null
}

export interface SimulatedRuleFiring {
  anomaly_id: string
  // Distinguishes otherwise identical firings of one scope in different scans.
  scan_config_id: string | null
  // Was a five-member subset while the backend has been simulating metric,
  // release_regression and variable_value_drift firings too — the narrowed union
  // silently mis-typed the scopes it omitted. Mirror the enum instead.
  scope_type: MetricScopeType
  scope_ref: string
  scope_name: string
  event_type_id: string | null
  event_id: string | null
  drift_field: string | null
  drift_type: string | null
  sample_value: string | null
  bucket: string
  // Start of the window the comparison was measured over; `bucket` is its end.
  // Only release regressions carry one — their window is the rollout overlap
  // rather than a scan bucket — so it is `null` for every other family. The
  // table does not render it: the backend already folds it into the
  // `release: … over the 51h rollout overlap` sentence inside `rendered_item`.
  // It is on the wire because the preview and the delivered message only stay
  // identical while both know the window.
  window_from: string | null
  direction: 'spike' | 'drop'
  actual_count: number
  expected_count: number
  absolute_delta: number
  // `null` exactly when `expected_count` is 0: the ratio is undefined, and the
  // replay sends null rather than the stored 0.0 placeholder a consumer cannot
  // tell apart from a real "no change". The same encoding
  // `AlertDeliveryItemResponse.percent_delta` and the inbox card already use, so
  // one incident no longer answers the question two ways depending on whether
  // you read the delivery it produced or the replay that predicted it.
  //
  // No component change came with this: `lib/percentDelta.formatPercentDelta`
  // already takes `number | null` and is what the replay table renders it
  // through, so both the null and the frozen 0.0 in older rows read
  // `no baseline`.
  percent_delta: number | null
  rendered_item: string | null
}

export interface AlertRuleSimulateResponse {
  rule_id: string
  rule_name: string
  days: number
  window_from: string
  window_to: string
  anomalies_considered: number
  matched_before_cooldown: number
  firings: SimulatedRuleFiring[]
  noisy: boolean
  // Every tunable the simulation can override reports the same pair: `_used` is
  // the value this run applied, `_saved` the value stored on the rule. Without
  // both, a preview run and the rule's real behaviour are indistinguishable on
  // screen and "what would happen if I raised the threshold" cannot be answered.
  // Sigma is the odd pair: it has no rule-level column at all,
  // so its `_saved` is the PROJECT's Detection-settings threshold — the same
  // number the detector scores with. It stays `number | null` only because that
  // is the shipped wire shape; the server now always sends a
  // number, so a null here is a contract leftover and not a state to design for.
  cooldown_minutes_used: number
  cooldown_minutes_saved: number
  min_percent_delta_used: number
  min_percent_delta_saved: number
  min_expected_count_used: number
  min_expected_count_saved: number
  sigma_threshold_used: number | null
  sigma_threshold_saved: number | null
  rendered_message: string | null
}

export interface AlertDeliveryItem {
  id: string
  delivery_id: string
  // `metric`, `release_regression` and `variable_value_drift` were missing from
  // the inline union that used to live here while the backend has been
  // delivering them for releases — the audit table renders whatever arrives, so
  // nothing crashed and nothing narrowed. Mirroring the enum keeps that from
  // recurring the next time a scope kind is added.
  scope_type: MetricScopeType
  scope_ref: string
  scope_name: string
  event_type_id: string | null
  event_id: string | null
  bucket: string
  direction: 'spike' | 'drop'
  actual_count: number
  expected_count: number
  absolute_delta: number
  // NULL when there was no baseline to divide by — the API has
  // sent null since then while this said `number`. `formatPercentDelta` in
  // lib/percentDelta already handles null; go through it rather than coercing to
  // 0 and printing "0%", which reads as "nothing changed" for exactly the items
  // that changed most.
  percent_delta: number | null
  details_path: string | null
  monitoring_path: string | null
  drift_field: string | null
  drift_type: string | null
  sample_value: string | null
  correlation_group_id: string | null
}

export interface AlertDelivery {
  id: string
  project_id: string
  scan_config_id: string
  scan_job_id: string | null
  destination_id: string
  rule_id: string
  destination_name: string
  rule_name: string
  scan_name: string
  status: AlertDeliveryStatus
  channel: AlertDestinationType
  matched_count: number
  payload_snapshot: Record<string, unknown> | null
  error_message: string | null
  // True when a demo_sink recorded this delivery locally (no external send).
  is_local: boolean
  is_simulated: boolean
  created_at: string
  updated_at: string
  sent_at: string | null
}

/**
 * `pending` is a row claimed by a sender that has not finished yet (the
 * server re-claims one left pending past its lease on the next run).
 */
export type AlertOwnerNotificationStatus = 'pending' | 'sent' | 'failed' | 'skipped'

/**
 * One owner emailed about a delivery (F07, #260). `user_id` is null once the
 * user was deleted — the row, and the address it went to, stay on record.
 */
export interface AlertOwnerNotification {
  user_id: string | null
  name: string | null
  email: string
  status: AlertOwnerNotificationStatus
  /** Why a `failed`/`skipped` row did not send. Optional: not every server sends it. */
  error?: string | null
  /** When the email went out; null until it did. Optional: older servers omit it. */
  sent_at?: string | null
}

export interface AlertDeliveryDetail extends AlertDelivery {
  items: AlertDeliveryItem[]
  /**
   * The owners this delivery's rule emailed (F07, #260). Optional: a server
   * that predates owner routing omits it, and an empty list means none.
   */
  owner_notifications?: AlertOwnerNotification[]
}

/** An owner of an incident's or signal's event type / metric (F07, #260). */
export interface SignalOwnerRef {
  user_id: string
  name: string
}

/** What a manual "Notify owners" did: one row per owner it tried. */
export interface NotifyOwnersResponse {
  owners: AlertOwnerNotification[]
}

export interface AlertDeliveryListResponse {
  items: AlertDelivery[]
  total: number
  /**
   * Opaque keyset cursor for the page after this one; `null` on the last page.
   * The server always sends it; a reader falls back to offset
   * paging when it is null.
   */
  next_cursor: string | null
}

export type AlertInboxStatus =
  | 'open'
  | 'acknowledged'
  | 'resolved'
  | 'muted'
  | 'false_positive'

/**
 * One rule that carried an incident: id AND name, together.
 *
 * Replaces the parallel `rule_ids` / `rule_names` arrays, which could not be
 * zipped — `rule_ids` came back sorted by UUID and `rule_names` by name, so
 * index *i* of one had nothing to do with index *i* of the other and the card
 * linked "Volume rule" to whichever monitor happened to sort first. Two rules
 * in one group can even share a name, so no client-side join could repair it
 * either. Link with `rules`; `rule_names` stays display text.
 */
export interface AlertInboxRuleRef {
  id: string
  name: string
}

export interface AlertInboxGroup {
  correlation_group_id: string
  status: AlertInboxStatus
  // `muted` is the effective flag (true iff status === 'muted'); `muted_until`
  // is null unless that mute is actually in force. The card used to derive
  // "muted" from a `muted_until` that outlived the mute, so a reopened group
  // kept rendering as snoozed.
  muted: boolean
  muted_until: string | null
  note: string | null
  false_positive_count: number
  item_count: number
  delivery_count: number
  latest_bucket: string
  // Both ends of the incident's life. `first_delivery_at` is what "started 3
  // days ago" is measured from — an incident with one timestamp cannot be told
  // apart from one that has been firing all week.
  first_delivery_at: string
  latest_delivery_at: string
  direction: 'spike' | 'drop'
  // Size of the newest item, so the card can say what actually happened instead
  // of only how many deliveries it took. percent_delta is NULL when
  // expected_count is 0 — a zero baseline has no percentage; render "new" /
  // "from nothing" there, never "0%". max_abs_percent_delta is the worst
  // magnitude across the group and is null for the same reason.
  actual_count: number
  expected_count: number
  percent_delta: number | null
  max_abs_percent_delta: number | null
  // Routable identity of the newest item. `scope_names` is display text, so
  // these are what the card links with. `scope_types` is the distinct set of
  // kinds in the group: a mixed incident cannot be described by the newest
  // item's `scope_type` alone.
  scope_type: MetricScopeType
  scope_types: MetricScopeType[]
  scope_ref: string
  event_id: string | null
  scope_names: string[]
  destination_names: string[]
  rules: AlertInboxRuleRef[]
  rule_names: string[]
  scan_names: string[]
  acted_at: string | null
  acted_by: string | null
  // Resolved display name of `acted_by` (a user id). "Acknowledged by
  // 3f2a…-c91b" told nobody anything.
  acted_by_name: string | null
  /**
   * Owners of the affected event type / catalog metric (F07, #260) — the
   * people "Notify owners" would email. Optional: a server that predates
   * owner routing omits it, which renders as no owners line.
   */
  owners?: SignalOwnerRef[]
}

/**
 * What an inbox action DID, not only what the group looks like afterwards.
 *
 * `overrides_written` is null for every action except `false_positive`, where
 * it counts the scopes actually tightened. It is zero for release regressions,
 * which the ratchet does not tune — the button promised a detection change it
 * never made on 10 of 57 production groups. Never guess this
 * client-side from `scope_type`; that is only the newest item's.
 */
export interface AlertInboxActionResponse {
  group: AlertInboxGroup
  overrides_written: number | null
}

/**
 * `note` records a comment on the incident and changes nothing else — it does
 * not move `status` and does not stamp `acted_at`.
 */
export type AlertInboxAction =
  | 'acknowledge'
  | 'resolve'
  | 'mute'
  | 'reopen'
  | 'false_positive'
  | 'note'

/**
 * The actions POST /alert-inbox/bulk-actions will accept.
 *
 * Written as an `Exclude` of the full union rather than as a fresh list of five
 * literals, for the same reason `MUTE_PRESETS` and `INDEFINITE_MUTE` are two
 * types instead of one list in `lib/mutePresets`: the exclusion is the CONTRACT,
 * so it should be a compile error to offer the refused action in bulk, not a
 * review comment. A hand-written copy of the union would also silently go stale
 * the day a seventh action is added — this one grows with its parent.
 *
 * WHY `false_positive` is the one refusal, restated here because this is where
 * a reader meets it first. Direction is part of the correlation key, so one
 * scope's spike and one scope's drop are two separate incidents sitting side by
 * side in the list — exactly what an operator sweeping a noisy scope selects
 * together. `_tune_false_positive_thresholds` dedupes only within a single call
 * and each step compounds off the scope's current value, so bulk-marking both
 * would take two permanent ratchet steps on one scope for one human decision,
 * with nothing in the record saying it was a single click. The server refuses it
 * with a 422; this type is the client-side half of the same rule, so the bulk
 * bar cannot render the button at all. Marking a false positive stays available
 * one incident at a time, on the incident's own action row.
 */
export type AlertInboxBulkAction = Exclude<AlertInboxAction, 'false_positive'>

/**
 * What a bulk triage decision DID — the rebuilt cards, and the audit batch id.
 *
 * A body, not the house 204 that the events/variables/metrics bulk routes
 * answer with: this route replaces cards the operator is LOOKING AT, so the
 * client can redraw N rows without re-listing the whole inbox. The standing
 * precedent for a bulk route with a body is `DeadEventArchiveResponse`.
 *
 * `groups` comes back in REQUEST ORDER with duplicates dropped, and can only be
 * SHORTER than the request when an incident's deliveries were deleted
 * concurrently, leaving no rows to render a card from — the state change still
 * landed and was still audited. Match on `correlation_group_id`, never on
 * position, and never count these to decide how many incidents were acted on.
 *
 * `overrides_written` is ALWAYS null here and is always SENT. Null means "not
 * applicable" — `false_positive` is the only action that can ratchet anything
 * and this route refuses it. Never read a missing key as 0 and announce "no
 * scopes tightened" after a bulk acknowledge (a defect once fixed on
 * the single-incident route).
 */
export interface AlertInboxBulkActionResponse {
  groups: AlertInboxGroup[]
  /** The id shared by every audit row this one call wrote — one row per group. */
  batch_id: string
  overrides_written: number | null
}

/** Incidents per effective status, over the whole window. */
export type AlertInboxStatusCounts = Record<AlertInboxStatus, number>

export interface AlertInboxListResponse {
  items: AlertInboxGroup[]
  total: number
  /**
   * Incidents per status after every other filter and before the status one,
   * so a status option can say what picking it would list. Always
   * sent; optional so fixtures written before it still type.
   */
  status_counts?: AlertInboxStatusCounts
  /**
   * Where the list's window really starts, or `null` when the documented
   * 30-day one held.
   *
   * Non-null means the server's per-project row cap bit before the window did,
   * so incidents that last fired before this instant are absent — and absent
   * looks exactly like handled unless the page says otherwise.
   * ALWAYS SENT: required here and required in `api.gen.ts`, so the two cannot
   * disagree about a key the server never omits.
   */
  window_truncated_at: string | null
  /** Opaque keyset cursor for "Load more"; see AlertDeliveryListResponse. */
  next_cursor: string | null
}

export type MonitorStatus = 'firing' | 'warning' | 'healthy'

export interface MonitorSummaryItem {
  rule_id: string
  rule_name: string
  destination_id: string
  destination_name: string
  destination_type: AlertDestinationType
  enabled: boolean
  status: MonitorStatus
  active_scope_count: number
  firing_scope_count: number
  last_anomaly_at: string | null
  last_notified_at: string | null
  notify_on_spike: boolean
  notify_on_drop: boolean
  min_percent_delta: number
  min_expected_count: number
  cooldown_minutes: number
  // Manual snooze state: `muted` is the effective flag (muted_until in the
  // future); `muted_until` is the raw timestamp the mute lifts at.
  muted: boolean
  muted_until: string | null
}

/**
 * Whether each drift-style scope has any source data at all, PROJECT-wide.
 *
 * Not a per-rule verdict and not a prediction: it answers "could this scope ever
 * produce a candidate here", so a screen can tell an enabled-but-inert toggle
 * from a quiet one.
 */
export interface AlertScopeReadiness {
  /**
   * False when nothing on the MAIN branch documents an allowed-values list —
   * neither a variable's own list nor a per-event override — for a variable
   * scans still observe, and no value drift has been collected yet. Collected
   * rows alone are enough.
   */
  variable_value_drift: boolean
  /**
   * False when no scan lists distribution drift fields AND no drift has been
   * collected yet. Collected rows alone are enough.
   */
  distribution_drift: boolean
}

export interface MonitorsSummaryResponse {
  monitors: MonitorSummaryItem[]
  firing_count: number
  warning_count: number
  healthy_count: number
  total: number
  // On the envelope, never on `MonitorSummaryItem`: one project fact, not N
  // copies of a value that cannot differ between rules.
  scope_readiness: AlertScopeReadiness
}

/**
 * One scope of a monitor that is firing now.
 *
 * Chosen by the same horizon test as `firing_scope_count`, so the list and the
 * count cannot disagree. `scope_name`, `event_id` and `direction` come from the
 * delivery that last notified the scope, and all three are null for a scope the
 * rule has not notified yet (a cooldown or a mute can hold the first message
 * back while the state is already open).
 */
export interface MonitorFiringScope {
  scope_type: MetricScopeType
  scope_ref: string
  scan_config_id: string | null
  scope_name: string | null
  event_id: string | null
  direction: 'spike' | 'drop' | null
  last_anomaly_bucket: string
  last_notified_at: string | null
}

/** A single monitor with the extra context a drill-in detail view needs. */
export interface MonitorDetail extends MonitorSummaryItem {
  // Raw enable flags (the summary `enabled` is the AND of these two).
  rule_enabled: boolean
  destination_enabled: boolean
  /**
   * Which scan's anomalies this rule can see — same name and same meaning as
   * `AlertRule.scan_config_id`: null means every scan in the project.
   * `scan_name` is that scan's display name, null exactly when the id is; the
   * detail page runs no scans query to resolve an id against.
   *
   * NOT an input to `scope_readiness` below, which stays a PROJECT fact. A rule
   * bound to a scan that feeds nothing can still read ready because a sibling
   * scan does — a known limitation, which these two fields make
   * visible rather than fix.
   */
  scan_config_id: string | null
  scan_name: string | null
  // Which signal kinds this monitor subscribes to.
  include_project_total: boolean
  include_event_types: boolean
  include_events: boolean
  include_schema_drifts: boolean
  include_distribution_drifts: boolean
  include_release_regressions: boolean
  include_variable_value_drifts: boolean
  include_metrics: boolean
  // "Data is late" alerts: one per late/overdue scan (F16, #269).
  include_source_freshness: boolean
  // Lifecycle alerts: one per open sunset-watch finding (#258). Optional: a
  // server that predates the flag omits it, which reads as off.
  include_lifecycle?: boolean
  // Property drift alerts (F23, #306). Optional like include_lifecycle.
  include_property_drifts?: boolean
  // Quick fired-history stats (full history via GET /alert-deliveries?rule_id=).
  total_deliveries: number
  last_delivery_at: string | null
  last_delivery_status: AlertDeliveryStatus | null
  // The same block the monitors list carries, under the same name: both screens
  // render the same two toggles and must not disagree about what feeds them.
  scope_readiness: AlertScopeReadiness
  /** The scopes behind `firing_scope_count`, for the "Firing now" panel. */
  firing_scopes: MonitorFiringScope[]
}
