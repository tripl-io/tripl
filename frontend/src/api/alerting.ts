import { api } from './client'
import type {
  AlertDeliveryDetail,
  AlertInboxAction,
  AlertInboxActionResponse,
  AlertInboxBulkAction,
  AlertInboxBulkActionResponse,
  AlertInboxGroup,
  AlertInboxListResponse,
  AlertInboxStatus,
  AlertDeliveryListResponse,
  AlertOwnerNotification,
  AlertDestination,
  AlertDestinationDraftTestRequest,
  AlertDestinationTestResponse,
  AlertRule,
  AlertRuleFilterPayload,
  AlertRuleSimulateResponse,
  MetricScopeType,
  MonitorDetail,
  MonitorsSummaryResponse,
  NotifyOwnersResponse,
  SignalTriageScope,
} from '../types'

/**
 * The owners a manual "Notify owners" tried (F07, #260). Accepts the bare list
 * as well as the `{ owners }` envelope, so the card reads the same rows
 * whichever of the two the route answers with.
 */
function notifiedOwners(
  body: NotifyOwnersResponse | AlertOwnerNotification[] | null | undefined,
): AlertOwnerNotification[] {
  if (Array.isArray(body)) return body
  return body?.owners ?? []
}

/**
 * The largest selection {@link alertingApi.applyInboxBulkAction} may send.
 *
 * The server's half of this number is `MAX_BULK_INBOX_ACTION_GROUPS` in
 * backend/src/tripl/schemas/alerting.py, where it is a `max_length` on the id
 * list: a longer list is a 422 that never touches the database. This is the ONE
 * place the frontend spells it, so the bar that stops an operator at the cap and
 * the request that would be refused by it cannot drift apart — a second literal
 * `200` typed into a component is a cap that silently stops matching the server
 * the day the server's changes.
 *
 * It lives here, beside the client function bound by it, rather than in
 * `pages/alerting/constants.ts`: that module holds this page's form options and
 * message templates, none of which the server constrains, while this is a wire
 * contract and belongs with the wire. `VARIABLES_PAGE_LIMIT` in api/variables.ts
 * is the same shape of constant in the same place, for the same reason.
 *
 * Reachable in practice, which is why the UI must not leave it to the 422: the
 * inbox is an ACCUMULATING infinite list at 50 rows per page, so four "Load
 * more" clicks put 200 tickable rows on screen and the fifth puts 250 there.
 */
export const MAX_BULK_INBOX_ACTION_GROUPS = 200

/**
 * The longest note either inbox route will store.
 *
 * The server's half is `max_length=2000` on `AlertInboxActionRequest.note` AND
 * on `AlertInboxBulkActionRequest.note` (schemas/alerting.py) — already written
 * twice there, and a third time as a bare `maxLength={2000}` on the incident
 * card's editor. The bulk bar's editor would have made four, so it is named once
 * here for the reason the cap above is: a wire contract, beside the wire.
 *
 * Unlike the bulk cap, this one is NOT enforced by refusing the request. Both
 * editors hand it to `maxLength`, which stops accepting keystrokes SILENTLY —
 * so the only way an operator learns the limit exists is by being told before
 * they reach it (see `noteBudgetLabel`).
 */
export const MAX_INBOX_NOTE_LENGTH = 2000

// Matches AlertRuleCreate/AlertRuleUpdate and the alert_rules.name column.
export const MAX_ALERT_RULE_NAME_LENGTH = 255

/**
 * A rule PATCH body. Also what a replay of unsaved edits sends: the simulate
 * route lays it over the saved rule without writing it.
 */
export interface AlertRuleUpdatePayload {
  name?: string
  enabled?: boolean
  scan_config_id?: string | null
  include_project_total?: boolean
  include_event_types?: boolean
  include_events?: boolean
  include_schema_drifts?: boolean
  include_distribution_drifts?: boolean
  include_release_regressions?: boolean
  include_variable_value_drifts?: boolean
  // Accepted by the API and carried on AlertRule, but was missing from both
  // payloads — a form could show the metric toggle and never save it.
  include_metrics?: boolean
  include_source_freshness?: boolean
  include_lifecycle?: boolean
  include_property_drifts?: boolean
  notify_on_spike?: boolean
  notify_on_drop?: boolean
  ai_explanation_enabled?: boolean
  // Also email the owners of each affected event type / metric (F07, #260).
  notify_owners?: boolean
  min_percent_delta?: number
  min_absolute_delta?: number
  min_expected_count?: number
  cooldown_minutes?: number
  message_template?: string | null
  items_template?: string | null
  message_format?: 'plain' | 'slack_mrkdwn' | 'telegram_html' | 'telegram_markdownv2'
  filters?: AlertRuleFilterPayload[]
}

export const alertingApi = {
  listDestinations: (slug: string) =>
    api.get<AlertDestination[]>(`/projects/${slug}/alert-destinations`),

  getMonitorsSummary: (slug: string) =>
    api.get<MonitorsSummaryResponse>(`/projects/${slug}/monitors-summary`),

  createDestination: (
    slug: string,
    data: {
      type: 'slack' | 'telegram' | 'webhook' | 'email' | 'jira' | 'linear' | 'pagerduty' | 'teams'
      name: string
      enabled?: boolean
      webhook_url?: string | null
      bot_token?: string | null
      chat_id?: string | null
      target_url?: string | null
      webhook_header_name?: string | null
      webhook_header_value?: string | null
      email_recipients?: string | null
      email_from_address?: string | null
      email_subject_template?: string | null
      jira_base_url?: string | null
      jira_auth_email?: string | null
      jira_api_token?: string | null
      jira_project_key?: string | null
      jira_issue_type?: string | null
      linear_api_key?: string | null
      linear_team_id?: string | null
      linear_state_id?: string | null
      linear_label_ids?: string | null
      pagerduty_routing_key?: string | null
      pagerduty_severity?: string | null
      teams_webhook_url?: string | null
      // null means immediate — send after every collection. Otherwise a
      // 5-field cron expression read in the project's timezone.
      delivery_schedule_cron?: string | null
    },
  ) => api.post<AlertDestination>(`/projects/${slug}/alert-destinations`, data),

  updateDestination: (
    slug: string,
    destinationId: string,
    data: {
      name?: string
      enabled?: boolean
      webhook_url?: string | null
      bot_token?: string | null
      chat_id?: string | null
      target_url?: string | null
      webhook_header_name?: string | null
      webhook_header_value?: string | null
      email_recipients?: string | null
      email_from_address?: string | null
      email_subject_template?: string | null
      jira_base_url?: string | null
      jira_auth_email?: string | null
      jira_api_token?: string | null
      jira_project_key?: string | null
      jira_issue_type?: string | null
      linear_api_key?: string | null
      linear_team_id?: string | null
      linear_state_id?: string | null
      linear_label_ids?: string | null
      pagerduty_routing_key?: string | null
      pagerduty_severity?: string | null
      teams_webhook_url?: string | null
      // null means immediate — send after every collection. Otherwise a
      // 5-field cron expression read in the project's timezone.
      delivery_schedule_cron?: string | null
    },
  ) => api.patch<AlertDestination>(`/projects/${slug}/alert-destinations/${destinationId}`, data),

  deleteDestination: (slug: string, destinationId: string) =>
    api.del(`/projects/${slug}/alert-destinations/${destinationId}`),

  /**
   * Send a probe through this destination's real channel. Resolves 200 even
   * when the channel refuses — inspect `ok`/`error` rather than catching, since
   * a rejected token IS the answer the button was pressed for.
   */
  testDestination: (slug: string, destinationId: string) =>
    api.post<AlertDestinationTestResponse>(
      `/projects/${slug}/alert-destinations/${destinationId}/test`,
      undefined,
    ),

  /**
   * Send the test message through settings the destination dialog has not
   * saved. Same contract as `testDestination`: resolves 200 even when
   * the channel refuses, so read `ok`/`error` rather than catching.
   */
  testDestinationDraft: (slug: string, body: AlertDestinationDraftTestRequest) =>
    api.post<AlertDestinationTestResponse>(`/projects/${slug}/alert-destinations/test`, body),

  createRule: (
    slug: string,
    destinationId: string,
    data: {
      name: string
      enabled?: boolean
      scan_config_id?: string | null
      include_project_total?: boolean
      include_event_types?: boolean
      include_events?: boolean
      include_schema_drifts?: boolean
      include_distribution_drifts?: boolean
      include_release_regressions?: boolean
      include_variable_value_drifts?: boolean
      // Accepted by the API and carried on AlertRule, but was missing from both
      // payloads — a form could show the metric toggle and never save it.
      include_metrics?: boolean
      include_source_freshness?: boolean
      include_lifecycle?: boolean
      include_property_drifts?: boolean
      notify_on_spike?: boolean
      notify_on_drop?: boolean
      ai_explanation_enabled?: boolean
      notify_owners?: boolean
      min_percent_delta?: number
      min_absolute_delta?: number
      min_expected_count?: number
      cooldown_minutes?: number
      message_template?: string | null
      items_template?: string | null
      message_format?: 'plain' | 'slack_mrkdwn' | 'telegram_html' | 'telegram_markdownv2'
      filters?: AlertRuleFilterPayload[]
    },
  ) => api.post<AlertRule>(`/projects/${slug}/alert-destinations/${destinationId}/rules`, data),

  updateRule: (
    slug: string,
    destinationId: string,
    ruleId: string,
    data: AlertRuleUpdatePayload,
  ) => api.patch<AlertRule>(`/projects/${slug}/alert-destinations/${destinationId}/rules/${ruleId}`, data),

  deleteRule: (slug: string, destinationId: string, ruleId: string) =>
    api.del(`/projects/${slug}/alert-destinations/${destinationId}/rules/${ruleId}`),

  simulateRule: (
    slug: string,
    destinationId: string,
    ruleId: string,
    days: number,
    /**
     * Thresholds to try WITHOUT saving them to the rule. Each omitted override
     * falls back to the rule's stored value; the response echoes both as
     * `*_used` / `*_saved` so the preview can be labelled honestly.
     */
    overrides?: {
      cooldownMinutes?: number
      minPercentDelta?: number
      minExpectedCount?: number
      sigmaThreshold?: number
    },
    /** Unsaved edits to replay instead of the saved rule; never written. */
    draft?: AlertRuleUpdatePayload,
  ) => {
    const params = new URLSearchParams({ days: String(days) })
    if (overrides?.cooldownMinutes !== undefined) {
      params.set('cooldown_minutes_override', String(overrides.cooldownMinutes))
    }
    if (overrides?.minPercentDelta !== undefined) {
      params.set('min_percent_delta_override', String(overrides.minPercentDelta))
    }
    if (overrides?.minExpectedCount !== undefined) {
      params.set('min_expected_count_override', String(overrides.minExpectedCount))
    }
    if (overrides?.sigmaThreshold !== undefined) {
      params.set('sigma_threshold_override', String(overrides.sigmaThreshold))
    }
    return api.post<AlertRuleSimulateResponse>(
      `/projects/${slug}/alert-destinations/${destinationId}/rules/${ruleId}/simulate?${params}`,
      draft,
    )
  },

  listDeliveries: (
    slug: string,
    params?: {
      status?: string
      channel?: string
      destination_id?: string
      rule_id?: string
      scan_config_id?: string
      /** One incident's deliveries — what the incident card lists under it. */
      correlation_group_id?: string
      /**
       * Deliveries belonging to no incident. Not expressible through
       * `correlation_group_id`, and without it they would vanish from a page
       * that lists deliveries under incidents.
       */
      ungrouped?: boolean
      date_from?: string
      date_to?: string
      offset?: number
      /** A previous page's `next_cursor`; never sent with an offset. */
      cursor?: string
      limit?: number
    },
  ) => {
    const sp = new URLSearchParams()
    if (params?.status) sp.set('status', params.status)
    if (params?.channel) sp.set('channel', params.channel)
    if (params?.destination_id) sp.set('destination_id', params.destination_id)
    if (params?.rule_id) sp.set('rule_id', params.rule_id)
    if (params?.scan_config_id) sp.set('scan_config_id', params.scan_config_id)
    if (params?.correlation_group_id) sp.set('correlation_group_id', params.correlation_group_id)
    if (params?.ungrouped) sp.set('ungrouped', 'true')
    if (params?.date_from) sp.set('date_from', params.date_from)
    if (params?.date_to) sp.set('date_to', params.date_to)
    if (params?.offset !== undefined) sp.set('offset', String(params.offset))
    // Keyset continuation from a previous page's `next_cursor`; the API refuses
    // it together with a non-zero offset.
    if (params?.cursor) sp.set('cursor', params.cursor)
    if (params?.limit !== undefined) sp.set('limit', String(params.limit))
    const qs = sp.toString()
    return api.get<AlertDeliveryListResponse>(`/projects/${slug}/alert-deliveries${qs ? `?${qs}` : ''}`)
  },

  getDelivery: (slug: string, deliveryId: string) =>
    api.get<AlertDeliveryDetail>(`/projects/${slug}/alert-deliveries/${deliveryId}`),

  retryDelivery: (slug: string, deliveryId: string) =>
    api.post<AlertDeliveryDetail>(
      `/projects/${slug}/alert-deliveries/${deliveryId}/retry`,
      undefined,
    ),

  getMonitor: (slug: string, ruleId: string) =>
    api.get<MonitorDetail>(`/projects/${slug}/monitors/${ruleId}`),

  getMonitorHistory: (
    slug: string,
    ruleId: string,
    params?: {
      status?: string
      channel?: string
      date_from?: string
      date_to?: string
      offset?: number
      limit?: number
    },
  ) => {
    const sp = new URLSearchParams()
    sp.set('rule_id', ruleId)
    if (params?.status) sp.set('status', params.status)
    if (params?.channel) sp.set('channel', params.channel)
    if (params?.date_from) sp.set('date_from', params.date_from)
    if (params?.date_to) sp.set('date_to', params.date_to)
    if (params?.offset !== undefined) sp.set('offset', String(params.offset))
    if (params?.limit !== undefined) sp.set('limit', String(params.limit))
    return api.get<AlertDeliveryListResponse>(
      `/projects/${slug}/alert-deliveries?${sp.toString()}`,
    )
  },

  muteMonitor: (slug: string, ruleId: string, mutedUntil: string) =>
    api.post<MonitorDetail>(`/projects/${slug}/monitors/${ruleId}/mute`, {
      muted_until: mutedUntil,
    }),

  unmuteMonitor: (slug: string, ruleId: string) =>
    api.post<MonitorDetail>(`/projects/${slug}/monitors/${ruleId}/unmute`, undefined),

  listInbox: (
    slug: string,
    // Every narrowing member is its union, not a bare string: the API 422s on
    // anything else, and a filter typo should fail at build time rather than as
    // an empty inbox nobody can explain.
    //
    // `lastFiredFrom`/`lastFiredTo` are instants, and both bound
    // `latest_delivery_at` — when the incident last spoke, the column the card
    // leads with. They narrow INSIDE the list's own 30-day window; neither can
    // reach an older incident, which is why the control that sets them says so.
    params?: {
      status?: AlertInboxStatus
      lastFiredFrom?: string
      lastFiredTo?: string
      scopeType?: MetricScopeType
      direction?: 'spike' | 'drop'
      scope?: string
      offset?: number
      /** A previous page's `next_cursor`; never sent with an offset. */
      cursor?: string
      limit?: number
    },
  ) => {
    const sp = new URLSearchParams()
    if (params?.status) sp.set('status', params.status)
    if (params?.lastFiredFrom) sp.set('last_fired_from', params.lastFiredFrom)
    if (params?.lastFiredTo) sp.set('last_fired_to', params.lastFiredTo)
    if (params?.scopeType) sp.set('scope_type', params.scopeType)
    if (params?.direction) sp.set('direction', params.direction)
    if (params?.scope) sp.set('scope', params.scope)
    if (params?.offset !== undefined) sp.set('offset', String(params.offset))
    if (params?.cursor) sp.set('cursor', params.cursor)
    if (params?.limit !== undefined) sp.set('limit', String(params.limit))
    const qs = sp.toString()
    return api.get<AlertInboxListResponse>(`/projects/${slug}/alert-inbox${qs ? `?${qs}` : ''}`)
  },

  /**
   * One incident by id, ignoring the 30-day window the list is scoped to. A
   * deep link to an older incident used to land on an empty inbox because the
   * only way to fetch a group was to find it in that page.
   */
  getInboxGroup: (slug: string, correlationGroupId: string) =>
    api.get<AlertInboxGroup>(`/projects/${slug}/alert-inbox/${correlationGroupId}`),

  applyInboxAction: (
    slug: string,
    correlationGroupId: string,
    data: {
      action: AlertInboxAction
      note?: string | null
      muted_until?: string | null
    },
  ) =>
    api.post<AlertInboxActionResponse>(
      `/projects/${slug}/alert-inbox/${correlationGroupId}/actions`,
      data,
    ),

  /**
   * One triage decision, applied to several incidents in ONE request.
   *
   * A triage shortcut, not an incident record: the server copies this body into
   * each selected incident's own state, so afterwards every selected row
   * carries the same note, `acted_at` and `acted_by` and is indistinguishable
   * from N single-incident clicks. There is no group-of-groups object to fetch
   * afterwards, which is why the response hands back the rebuilt cards.
   *
   * ALL-OR-NOTHING. Every id is validated in one query up front; if any is
   * unknown to this project the whole batch is rejected with a 404 and NOTHING
   * was mutated. There is no partial success and no per-item error array, so a
   * caller never has to reconcile "which of my twelve went through".
   *
   * `action` is {@link AlertInboxBulkAction}, so `false_positive` cannot even be
   * spelled here — see that type for why the server refuses it with a 422.
   *
   * `muted_until` follows the single-incident route exactly: pass it only with
   * a `mute`, and pass it EXPLICITLY as `null` for the open-ended mute rather
   * than omitting the key. Every other action nulls the column
   * server-side regardless of what is sent.
   */
  applyInboxBulkAction: (
    slug: string,
    data: {
      // First, matching the `<entity>_ids`-first convention of every other bulk
      // body in the repo — and matching the server schema field order, so the
      // two read side by side. Capped at {@link MAX_BULK_INBOX_ACTION_GROUPS} by
      // the server (the only bulk id list in the repo that is capped); a longer
      // list is a 422, not a slow request.
      correlation_group_ids: string[]
      action: AlertInboxBulkAction
      note?: string | null
      muted_until?: string | null
    },
  ) =>
    api.post<AlertInboxBulkActionResponse>(
      `/projects/${slug}/alert-inbox/bulk-actions`,
      data,
    ),

  /**
   * Email the owners of an incident's event type / metric once, now (F07,
   * #260). Editor only. Resolves with one row per owner tried — a skipped or
   * failed owner is an answer, not a thrown error.
   */
  notifyIncidentOwners: async (slug: string, correlationGroupId: string) =>
    notifiedOwners(
      await api.post<NotifyOwnersResponse | AlertOwnerNotification[]>(
        `/projects/${slug}/alert-inbox/${correlationGroupId}/notify-owners`,
        undefined,
      ),
    ),

  /**
   * The same for a signal no rule routed to an incident — the "Notify owners"
   * on the drilldown's Signal card (F07, #260).
   */
  notifySignalOwners: async (slug: string, scope: SignalTriageScope) =>
    notifiedOwners(
      await api.post<NotifyOwnersResponse | AlertOwnerNotification[]>(
        `/projects/${slug}/signals/notify-owners`,
        scope,
      ),
    ),
}
