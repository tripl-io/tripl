"""Pure rule/anomaly matching helpers shared by live and simulated paths.

The live alert pipeline (worker/tasks/metrics/dispatch.py) and the in-UI rule
simulator both apply the SAME predicates to anomalies — extracting them here
guarantees the simulator never diverges from production about WHICH signals a
rule admits.

That is the whole of the promise, and the limit is deliberate rather than an
omission. The predicate half is shared CODE: ``rule_matches_anomaly`` is the one
function both callers run, and it takes no destination because no destination
can change whether a signal matches. The RATE LIMITER is not shared —
``simulate_rule_firings`` below re-implements the live cooldown in memory — and
there is one destination class it gets wrong, because dispatch switches the
cooldown off for a destination carrying a ``delivery_schedule_cron`` (its
cadence is its rate limiter) and nothing in this module is handed a destination
to notice. The argument lives on ``simulate_rule_firings`` and on
``services.alerting_service.simulate_rule``, where the gate is actually applied,
so there is one copy of it to keep true rather than three.

These functions never touch the session and never mutate state.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_filter import AlertRuleFilter
from tripl.models.domain_enums import MetricScopeType

SCOPE_DISTRIBUTION_DRIFT = MetricScopeType.distribution.value
SCOPE_RELEASE_REGRESSION = MetricScopeType.release_regression.value
SCOPE_METRIC = MetricScopeType.metric.value
SCOPE_VARIABLE_VALUE_DRIFT = MetricScopeType.variable_value_drift.value
# One candidate per late/overdue scan config (issue #269): ``scope_ref`` is the
# scan config id, so the scope is already partitioned by scan like
# ``project_total`` and the ordinary per-config AlertRuleState gives it one
# cooldown clock per scan.
SCOPE_SOURCE_FRESHNESS = MetricScopeType.source_freshness.value


def _utc_bucket(bucket: datetime) -> datetime:
    """Compare UTC buckets consistently across SQLite and PostgreSQL drivers."""
    if bucket.tzinfo is None:
        return bucket.replace(tzinfo=UTC)
    return bucket.astimezone(UTC)


class AlertMatchCandidate(Protocol):
    id: uuid.UUID
    # The scan this signal came from, or NULL when it is project-global
    # (``metric`` scope). ``rule_matches_anomaly`` needs it to honour a
    # scan-bound rule, so EVERY candidate type has to carry it — the dataclass
    # ones below included, or a drift candidate would slip past the gate.
    scan_config_id: uuid.UUID | None
    scope_type: str
    scope_ref: str
    event_id: uuid.UUID | None
    event_type_id: uuid.UUID | None
    bucket: datetime
    direction: str
    # Float: fractional catalog metrics carry sub-unit actuals (tripl-68bc).
    actual_count: float
    expected_count: float


@dataclass
class DriftAlertCandidate:
    """Anomaly candidate carrying drift metadata (schema or distribution).

    Schema drift and distribution drift produce structurally identical candidate
    rows, so they share this one dataclass. The aliases below preserve the two
    domain-specific names used at call sites.
    """

    id: uuid.UUID
    scan_config_id: uuid.UUID | None
    scope_type: str
    scope_ref: str
    event_id: uuid.UUID | None
    event_type_id: uuid.UUID | None
    bucket: datetime
    direction: str
    actual_count: float
    expected_count: float
    drift_field: str | None
    drift_type: str | None
    sample_value: str | None
    # Start of the window the comparison was measured over. Only release
    # regressions set it (``bucket`` carries the end): their window is the
    # activation-anchored rollout overlap, not the scan's bucket, and a message
    # that quotes an adoption-adjusted expectation has to be able to say which
    # window produced it. Everything else leaves it None and renders unchanged.
    window_from: datetime | None = None


# Same shape, distinct domain names kept for call-site readability.
SchemaDriftAlertCandidate = DriftAlertCandidate
DistributionDriftAlertCandidate = DriftAlertCandidate


def distribution_drift_scope_ref(owner_id: uuid.UUID, field_name: str) -> str:
    field_hash = hashlib.sha1(field_name.encode("utf-8")).hexdigest()[:12]
    return f"{owner_id.hex}:{field_hash}"


def filter_matches_anomaly(
    filter_row: AlertRuleFilter,
    anomaly: AlertMatchCandidate,
    *,
    event_type_by_event_id: Mapping[uuid.UUID, uuid.UUID] | None = None,
) -> bool:
    """Does this one filter admit this candidate?

    ``event_type_by_event_id`` resolves the type of an EVENT-ANCHORED candidate
    that carries no ``event_type_id`` of its own — event-scope anomalies,
    variable-value drifts and event-scope release regressions all store NULL
    there on purpose (stamping the column would leak those rows into the
    event-TYPE series several read paths select by it alone). Without the map
    their type reads as "field absent" and the ``actual is None`` passthrough
    below admits every signal an ``event_type`` filter was written to narrow, in
    both directions (tripl-0zpq.7). BOTH production call sites must supply it —
    ``dispatch._prepare_alert_deliveries`` and ``alerting_service.simulate_rule``
    — or that bypass comes straight back.
    """
    if filter_row.field == "event_type":
        event_type_id = anomaly.event_type_id
        if event_type_id is None and anomaly.event_id is not None and event_type_by_event_id:
            event_type_id = event_type_by_event_id.get(anomaly.event_id)
        actual = str(event_type_id) if event_type_id is not None else None
    elif filter_row.field == "event":
        actual = str(anomaly.event_id) if anomaly.event_id is not None else None
    elif filter_row.field == "direction":
        actual = "up" if anomaly.direction == "spike" else "down"
    elif filter_row.field == "metric":
        # A catalog-metric signal's scope_ref IS its MetricDefinition id (see
        # worker/tasks/metrics/detect.py); no other scope names a metric.
        actual = anomaly.scope_ref if anomaly.scope_type == SCOPE_METRIC else None
    else:
        return True

    # A genuinely event-less signal (the project-total / event-type rollups, a
    # catalog metric) carries no such field at all and still passes through —
    # and, symmetrically, a volume or drift signal passes a ``metric`` filter.
    if actual is None:
        return True

    values = set(filter_row.values or [])
    if filter_row.operator in ("eq", "in"):
        return actual in values
    if filter_row.operator in ("ne", "not_in"):
        return actual not in values
    return True


def rule_matches_anomaly(
    rule: AlertRule,
    anomaly: AlertMatchCandidate,
    *,
    min_percent_delta_override: float | None = None,
    min_expected_count_override: float | None = None,
    event_type_by_event_id: Mapping[uuid.UUID, uuid.UUID] | None = None,
) -> bool:
    """Would this rule deliver this signal?

    The two overrides exist for the in-UI replay and are ``None`` on every live
    path, which then reads the rule exactly as before. They are keyword-only and
    named like ``simulate_rule_firings``' ``cooldown_minutes_override`` for the
    same reason it exists: answering "would min_percent_delta 300 have cut these
    incidents" must not require SAVING 300 onto a rule that is live-routing to a
    real channel and waiting to find out (tripl-oxkt.17).

    ``event_type_by_event_id`` is forwarded whole to
    :func:`filter_matches_anomaly`; every PRODUCTION caller must supply it, and
    omitting it restores the pre-fix ``event_type``-filter passthrough for
    event-anchored signals.
    """
    # Scan gate. NULL on the rule means the whole project — the behaviour every
    # rule had before the column existed, so the migration is a no-op.
    #
    # It lives HERE rather than in ``dispatch._prepare_alert_deliveries`` on
    # purpose. Dispatch already runs per scan config, so an early ``continue``
    # there would be enough for production and invisible to the in-UI simulator,
    # which replays a whole project's anomalies through this function in one
    # pass — the simulator would then over-report a scan-bound rule, which is
    # exactly the drift this module exists to prevent.
    #
    # A ``metric``-scope anomaly is project-global and carries NULL here, so it
    # never equals a bound scan: ``include_metrics`` goes inert on a scan-bound
    # rule, deliberately. A rule that says "this one scan" has nothing to say
    # about a project-wide catalog series.
    if rule.scan_config_id is not None and anomaly.scan_config_id != rule.scan_config_id:
        return False

    # Scope gates.
    if anomaly.scope_type == MetricScopeType.project_total.value and not rule.include_project_total:
        return False
    if anomaly.scope_type == MetricScopeType.event_type.value and not rule.include_event_types:
        return False
    if anomaly.scope_type == MetricScopeType.event.value and not rule.include_events:
        return False
    if anomaly.scope_type == MetricScopeType.schema.value and not rule.include_schema_drifts:
        return False
    if anomaly.scope_type == SCOPE_DISTRIBUTION_DRIFT and not rule.include_distribution_drifts:
        return False
    if anomaly.scope_type == SCOPE_RELEASE_REGRESSION and not rule.include_release_regressions:
        return False
    if anomaly.scope_type == SCOPE_VARIABLE_VALUE_DRIFT and not rule.include_variable_value_drifts:
        return False
    # "Data is late" alerts are opt-in like the drift families: a rule that
    # never asked for them keeps delivering exactly what it did before.
    if anomaly.scope_type == SCOPE_SOURCE_FRESHNESS and not rule.include_source_freshness:
        return False
    # Catalog metric anomalies are opt-in (SAFE OFF): a rule must explicitly
    # subscribe via include_metrics. They flow through the numeric-threshold
    # branch below (actual/expected counts), like the volume scopes.
    if anomaly.scope_type == SCOPE_METRIC and not rule.include_metrics:
        return False

    # Direction gates.
    if anomaly.direction == "spike" and not rule.notify_on_spike:
        return False
    if anomaly.direction == "drop" and not rule.notify_on_drop:
        return False

    if anomaly.scope_type in {
        MetricScopeType.schema.value,
        SCOPE_DISTRIBUTION_DRIFT,
        SCOPE_RELEASE_REGRESSION,
        SCOPE_VARIABLE_VALUE_DRIFT,
        # Lag hours against the allowed hours, not a count against a baseline:
        # the volume thresholds (min_expected_count, min_percent_delta) are
        # about counts and would silently gate a delay on its hour figures.
        SCOPE_SOURCE_FRESHNESS,
    }:
        return all(
            filter_matches_anomaly(
                filter_row, anomaly, event_type_by_event_id=event_type_by_event_id
            )
            for filter_row in rule.filters
        )

    # Numeric thresholds. The effective values, so a replay can ask a what-if
    # without the rule being edited underneath a live channel.
    min_expected_count = (
        rule.min_expected_count
        if min_expected_count_override is None
        else min_expected_count_override
    )
    min_percent_delta = (
        rule.min_percent_delta if min_percent_delta_override is None else min_percent_delta_override
    )
    # Magnitude, not sign: a catalog metric whose level legitimately sits below
    # zero is as substantial as the same level above it, and the rule's floor is
    # ``ge=0`` by schema, so a signed expectation failed every rule there was
    # (tripl-0zpq.102). Identity for every non-negative expectation.
    if abs(anomaly.expected_count) < min_expected_count:
        return False
    absolute_delta = abs(anomaly.actual_count - anomaly.expected_count)
    if absolute_delta < rule.min_absolute_delta:
        return False
    # A relative threshold has nothing to divide by when the baseline is zero,
    # and the old fallback answered 0.0 — reporting the largest possible relative
    # move as the smallest. It cost nothing while min_percent_delta defaulted to
    # 0 (``0 < 0`` is false, so such a candidate passed anyway); at the measured
    # default of 100 it silences the whole class, so a scope resuming after an
    # outage, or an event firing for the first time, would match no rule carrying
    # a percent threshold. The asymmetry is what gives it away: the mirror case,
    # actual 0 against a positive expectation, is exactly 100% and alerts.
    #
    # Deliberately NOT scored against ``max(expected, 1)`` the way the UI's
    # relative effect is. That divisor assumes counts, and catalog metrics arrive
    # here with fractional values gated only at 1e-6: a ratio expected 0.2 and
    # observed 0.9 scores 350% today and would score 70% under a floor of one,
    # dropping below the very threshold this is about.
    #
    # The divisor is the MAGNITUDE for the same reason the gate above is: a
    # negative expectation is a real baseline, and keeping the old
    # ``expected_count > 0`` test would have dropped the whole signed class
    # through to the no-baseline branch, skipping the percent gate entirely — a
    # second hole opened by closing the first.
    if anomaly.expected_count != 0:
        if absolute_delta / abs(anomaly.expected_count) * 100 < min_percent_delta:
            return False
    elif absolute_delta <= 0:
        # No baseline and no movement: nothing to report.
        return False

    return all(
        filter_matches_anomaly(filter_row, anomaly, event_type_by_event_id=event_type_by_event_id)
        for filter_row in rule.filters
    )


def rule_covers_event(
    rule: AlertRule,
    *,
    event_id: uuid.UUID,
    event_type_id: uuid.UUID,
) -> bool:
    """Whether an enabled rule *monitors* this event — coverage, not firing.

    Coverage answers "is this event watched by an alert rule at all", a static
    property of the event's identity, as opposed to :func:`rule_matches_anomaly`
    which answers whether a *live anomaly* would deliver. It therefore gates only
    on the event scope toggle (``include_events``) and the identity filters
    (``event`` / ``event_type``); the direction and numeric-threshold gates
    depend on an anomaly's counts, which are not a property of the event, so they
    are deliberately ignored here. Non-identity filters (e.g. ``direction``) are
    firing-time gates and never narrow coverage.

    ``rule.scan_config_id`` is ignored for the same reason: an event belongs to
    a project, not to a scan, and several scans can observe the same one. A
    scan-bound rule still watches the event — just in one scan — so the catalog's
    Monitor column stays truthful.
    """
    if not rule.enabled or not rule.include_events:
        return False
    for filter_row in rule.filters:
        if filter_row.field == "event":
            actual = str(event_id)
        elif filter_row.field == "event_type":
            actual = str(event_type_id)
        else:
            continue
        values = set(filter_row.values or [])
        if filter_row.operator in ("eq", "in") and actual not in values:
            return False
        if filter_row.operator in ("ne", "not_in") and actual in values:
            return False
    return True


def simulate_rule_firings(
    rule: AlertRule,
    anomalies: list[AlertMatchCandidate],
    *,
    cooldown_minutes_override: int | None = None,
    min_percent_delta_override: float | None = None,
    min_expected_count_override: float | None = None,
    event_type_by_event_id: Mapping[uuid.UUID, uuid.UUID] | None = None,
) -> list[AlertMatchCandidate]:
    """Replay anomalies through a rule with in-memory cooldown gating.

    Returns the subset that would have triggered a delivery, in bucket order.

    Cooldown is applied per (scope_type, scope_ref, scan config) — the partition
    ``uq_alert_rule_state_scope`` gives AlertRuleState, which is the clock the
    live pipeline gates on for a destination that delivers IMMEDIATELY. (For one
    that does not, see the last two paragraphs: it gates on no clock at all.)
    Keying it on (scope_type, scope_ref) alone was ONE place this module's
    no-divergence promise was false
    (tripl-0zpq.42): ``dispatch._prepare_alert_deliveries`` runs once per scan
    config and loads only THAT config's states, while the replay hands a whole
    project's anomalies through in a single pass, and a rule is project-wide
    unless deliberately bound to one scan. An event that is anomalous in scan A
    at 09:00 and in scan B at 09:30 therefore has two live clocks and sends
    twice; the scan-blind key collapsed both onto one and reported a single
    firing, so the what-if under-counted the rule it was asked about. Which
    scopes this moves is decided by the scope_ref: ``event`` and ``event_type``
    refs are the bare entity id and genuinely repeat across configs, whereas
    ``project_total`` (the config id) and schema drift (the drift row id) are
    already partitioned inside the ref and are unaffected either way.

    ``metric`` is the deliberate exception and stays project-global: a catalog
    metric is not a per-scan series, so its live state carries NO scan config at
    all — ``AlertRuleState.scan_config_id`` is NULL for a metric scope, and a
    partial unique index over that NULL space is what gives it one clock per
    (rule, scope) for the whole project (tripl-0zpq.28). The branch below reads
    the scope rather than the candidate's ``scan_config_id`` anyway: metric
    candidates carry NULL there too, so the two agree, and keying on the scope
    says what the partition IS rather than what one candidate happens to hold.

    When ``cooldown_minutes_override`` is set, that value is used in place of
    ``rule.cooldown_minutes`` so the simulator can A/B different cooldowns
    without writing back to the rule.

    The two threshold overrides do the same for the numeric gates, and are
    forwarded whole to ``rule_matches_anomaly`` — the gate has to move INSIDE the
    replay, not after it, because a signal the stricter rule would never have
    matched must not consume the cooldown slot that then hides the next one.
    ``event_type_by_event_id`` is forwarded for exactly that reason too: a
    candidate the ``event_type`` filter rejects must not burn the cooldown slot.

    THE COOLDOWN IS THE IMMEDIATE PATH'S LIMITER AND THIS FUNCTION APPLIES IT TO
    EVERY REPLAY, INCLUDING ONES LIVE DOES NOT APPLY IT TO.
    ``dispatch._prepare_alert_deliveries`` computes ``cooldown_applies =
    destination.delivery_schedule_cron is None`` and both of its send gates read
    ``not cooldown_applies or _cooldown_elapsed(...)``, so a destination holding
    its alerts for a digest never consults ``cooldown_minutes`` at all. The clock
    is not absent — ``alert_digest_send`` runs ``alerts._stamp_rule_state`` for
    every member of a sent digest, exactly as the immediate path does, and a
    scope no digest has carried yet simply still holds NULL — it is just never
    read while the cadence is set. What limits that destination instead is the
    buffer's unique key ``uq_alert_pending_item_scope``: one row per
    (destination, rule, scan config, scope, direction), so a scope re-firing all
    day occupies one digest line per direction per cron window
    (``dispatch._buffer_pending_items``).

    This function is handed no destination and models no cron window, so for a
    cadence destination it answers the immediate-delivery question, and it is off
    in whichever direction the two periods differ: hourly digests under the
    default 1440-minute cooldown UNDER-count (one firing a day per scope against
    up to 24 digest lines), a daily digest under a 60-minute cooldown
    OVER-counts. Mirroring ``cooldown_applies`` here — just skipping the gate
    when a cadence is set — would be worse than either, because with no limiter
    every matched bucket becomes a firing (~2000 a week per scope at five-minute
    collections, against a digest that ships one line) and ``noisy`` would trip
    for every cadence destination in the deployment. A faithful mirror has to
    replay the cron windows (``core.alert_schedule``) and collapse per (scope,
    direction) inside each, which reports digest LINES rather than rule firings
    and leaves ``cooldown_minutes_override`` — the knob the replay dialog exists
    to A/B — with nothing to vary. That is a different feature, so the gap is
    stated here and on ``simulate_rule`` rather than half-closed.
    """
    effective_cooldown = (
        cooldown_minutes_override
        if cooldown_minutes_override is not None
        else rule.cooldown_minutes
    )
    cooldown = timedelta(0) if effective_cooldown < 0 else timedelta(minutes=effective_cooldown)

    fired: list[AlertMatchCandidate] = []
    # Third element is the scan partition described above: the candidate's own
    # scan, or None for the project-global ``metric`` scope.
    last_fired_at: dict[tuple[str, str, uuid.UUID | None], datetime] = {}

    for anomaly in sorted(anomalies, key=lambda a: _utc_bucket(a.bucket)):
        if not rule_matches_anomaly(
            rule,
            anomaly,
            min_percent_delta_override=min_percent_delta_override,
            min_expected_count_override=min_expected_count_override,
            event_type_by_event_id=event_type_by_event_id,
        ):
            continue
        scan_partition = None if anomaly.scope_type == SCOPE_METRIC else anomaly.scan_config_id
        key = (anomaly.scope_type, anomaly.scope_ref, scan_partition)
        last = last_fired_at.get(key)
        if last is not None and _utc_bucket(anomaly.bucket) - _utc_bucket(last) < cooldown:
            continue
        fired.append(anomaly)
        last_fired_at[key] = anomaly.bucket

    return fired
