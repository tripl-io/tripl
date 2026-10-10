import type { components } from './api.gen'

// Alerting payloads, taken from the generated OpenAPI schema (`api.gen.ts`)
// rather than restated, so a backend change is a compile error where it is
// read. A field the backend declares with a `None` default is optional here
// even though the server always sends it: read it with `== null` or `??`.
type Schemas = components['schemas']

/**
 * Where a rule delivers. `demo_sink` is the demo-only local sink: it renders
 * and records deliveries with no outbound network, so it is kept out of the
 * user-selectable create options in pages/alerting/constants.ts.
 */
export type AlertDestinationType = Schemas['AlertDestinationType']
export type AlertDeliveryStatus = Schemas['AlertDeliveryStatus']
export type AlertMessageFormat = Schemas['AlertMessageFormat']

/**
 * An alert rule. `scan_config_id` null means every scan in the project.
 * `muted`/`muted_until` are the rule's manual snooze, carried here so the
 * destinations list can show a snoozed rule without a second round-trip.
 * `total_deliveries` and `incident_count` say whether the rule has ever
 * delivered: a rule with zero of both is configured but unproven.
 */
export type AlertRule = Schemas['AlertRuleResponse']

/**
 * What a rule filter matches. `metric` matches a catalog metric's signal by
 * its MetricDefinition id (its scope_ref); every other signal passes it.
 */
export type AlertRuleFilterField = Schemas['AlertRuleFilterField']
export type AlertRuleFilterOperator = Schemas['AlertRuleFilterOperator']
export type AlertRuleFilter = Schemas['AlertRuleFilterResponse']
export type AlertRuleFilterPayload = Schemas['AlertRuleFilterPayload']

/**
 * An alert destination. Secrets are write-only: the response says only
 * whether one is on file (`*_set`). `delivery_schedule_cron` null means
 * immediate; otherwise a 5-field cron read in `project_timezone`.
 * `held_count` is how many alerts wait for the next digest, so the card can
 * tell "on and quiet" from "on and broken". `is_local` marks a demo sink.
 */
export type AlertDestination = Schemas['AlertDestinationResponse']

/**
 * Result of a manual test send. A channel refusal is an ANSWER, not a server
 * fault, so the route answers 200 with `ok: false` and the channel's own
 * message. `error_kind` (and `http_status` for `http_status`) let the card
 * read a field rather than the exception text.
 */
export type AlertDestinationTestResponse = Schemas['AlertDestinationTestResponse']

/** What kind of failure a test send's `error` describes. */
export type DestinationTestErrorKind = NonNullable<AlertDestinationTestResponse['error_kind']>

/**
 * `POST /projects/{slug}/alert-destinations/test`: the destination dialog's
 * settings, tested before they are saved. `destination_id` names the saved
 * destination an edit dialog is open on; a secret left blank then means the
 * stored one.
 */
export type AlertDestinationDraftTestRequest = Schemas['AlertDestinationDraftTestRequest']

/**
 * One firing a rule replay predicts. `window_from` is set only on a release
 * regression, whose window is the rollout overlap; `percent_delta` is null
 * exactly when `expected_count` is 0, the same encoding the delivery items
 * and the inbox card use (render it through `formatPercentDelta`).
 */
export type SimulatedRuleFiring = Schemas['SimulatedRuleFiring']

/**
 * A rule replay. Each tunable reports `_used` (what this run applied) and
 * `_saved` (what the rule stores); sigma's `_saved` is the project's
 * detection threshold, since a rule has none of its own.
 */
export type AlertRuleSimulateResponse = Schemas['AlertRuleSimulateResponse']

/**
 * One item of a delivery. `percent_delta` is null when there was no baseline
 * to divide by: go through `formatPercentDelta`, never coerce it to 0.
 */
export type AlertDeliveryItem = Schemas['AlertDeliveryItemResponse']

/** One delivery. `is_local` is a demo sink's local record (no external send). */
export type AlertDelivery = Schemas['AlertDeliveryResponse']

/**
 * `pending` is a row claimed by a sender that has not finished yet (the
 * server re-claims one left pending past its lease on the next run).
 */
export type AlertOwnerNotificationStatus = Schemas['AlertOwnerNotificationStatus']

/**
 * One owner emailed about a delivery (F07, #260). `user_id` is null once the
 * user was deleted — the row, and the address it went to, stay on record.
 */
export type AlertOwnerNotification = Schemas['AlertOwnerNotificationResponse']

/** A delivery with its items and the owners its rule emailed (F07, #260). */
export type AlertDeliveryDetail = Schemas['AlertDeliveryDetailResponse']

/** An owner of an incident's or signal's event type / metric (F07, #260). */
export type SignalOwnerRef = Schemas['AlertOwnerRef']

/** What a manual "Notify owners" did: one row per owner it tried. */
export type NotifyOwnersResponse = Schemas['NotifyOwnersResponse']

/**
 * A page of deliveries. `next_cursor` is the opaque keyset cursor for the
 * page after this one, null on the last page.
 */
export type AlertDeliveryListResponse = Schemas['AlertDeliveryListResponse']

export type AlertInboxStatus = Schemas['AlertInboxStatus']

/**
 * One rule that carried an incident: id AND name, together. Link with
 * `rules`; `rule_names` stays display text (two rules can share a name).
 */
export type AlertInboxRuleRef = Schemas['AlertInboxRuleRef']

/**
 * One incident in the inbox.
 *
 * - `muted` is the effective flag (true iff status is `muted`); `muted_until`
 *   is null unless that mute is in force.
 * - `first_delivery_at`/`latest_delivery_at` are both ends of its life.
 * - `actual_count`/`expected_count`/`percent_delta` size the newest item;
 *   `percent_delta` is null when `expected_count` is 0 (render "new", never
 *   "0%"), and `max_abs_percent_delta` is the worst magnitude in the group.
 * - `scope_type`/`scope_ref`/`event_id` are the newest item's routable
 *   identity; `scope_types` is the distinct set of kinds in the group.
 * - `acted_by_name` is the display name of `acted_by` (a user id).
 * - `owners` (F07, #260) are the people "Notify owners" would email.
 */
export type AlertInboxGroup = Schemas['AlertInboxGroupResponse']

/**
 * What an inbox action DID, not only what the group looks like afterwards.
 * `overrides_written` is null for every action except `false_positive`, where
 * it counts the scopes actually tightened (zero for release regressions,
 * which the ratchet does not tune). Never guess it from `scope_type`.
 */
export type AlertInboxActionResponse = Schemas['AlertInboxActionResponse']

/**
 * `note` records a comment on the incident and changes nothing else — it does
 * not move `status` and does not stamp `acted_at`.
 */
export type AlertInboxAction = Schemas['AlertInboxActionRequest']['action']

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
 * `groups` comes back in REQUEST ORDER with duplicates dropped, and can only be
 * SHORTER than the request when an incident's deliveries were deleted
 * concurrently. Match on `correlation_group_id`, never on position, and never
 * count these to decide how many incidents were acted on.
 *
 * `overrides_written` is ALWAYS null here and is always sent: `false_positive`
 * is the only action that can ratchet anything and this route refuses it.
 * Never read it as 0 and announce "no scopes tightened" after a bulk action.
 */
export type AlertInboxBulkActionResponse = Schemas['AlertInboxBulkActionResponse']

/** Incidents per effective status, over the whole window. */
export type AlertInboxStatusCounts = Schemas['AlertInboxStatusCounts']

/**
 * A page of the inbox. `status_counts` counts incidents per status after
 * every other filter and before the status one. `window_truncated_at` is
 * where the list's window really starts when the server's per-project row
 * cap bit before the 30-day window did, null when that window held.
 */
export type AlertInboxListResponse = Schemas['AlertInboxListResponse']

/**
 * A monitor (one rule) on the monitors list. `muted` is the effective flag;
 * `muted_until` the raw timestamp the mute lifts at.
 */
export type MonitorSummaryItem = Schemas['MonitorSummaryItem']

export type MonitorStatus = MonitorSummaryItem['status']

/**
 * Whether each drift-style scope has any source data at all, PROJECT-wide, so
 * a screen can tell an enabled-but-inert toggle from a quiet one. Not a
 * per-rule verdict and not a prediction.
 */
export type AlertScopeReadiness = Schemas['AlertScopeReadiness']

export type MonitorsSummaryResponse = Schemas['MonitorsSummaryResponse']

/**
 * One scope of a monitor that is firing now, chosen by the same horizon test
 * as `firing_scope_count`. `scope_name`, `event_id` and `direction` come from
 * the delivery that last notified the scope, and are null for a scope the
 * rule has not notified yet.
 */
export type MonitorFiringScope = Schemas['MonitorFiringScope']

/**
 * A single monitor with what its detail page needs: the raw enable flags
 * (`enabled` is their AND), the scan it watches (`scan_config_id` null means
 * every scan, `scan_name` null exactly when the id is), the signal kinds it
 * subscribes to, its delivery stats and the scopes firing now.
 */
export type MonitorDetail = Schemas['MonitorDetailResponse']
