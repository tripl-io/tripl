"""Alerting service — public API.

Implementation is split across private sibling modules:

* ``_alerting_destinations``  — destinations/rules CRUD, secrets, filters
* ``_alerting_deliveries``    — deliveries listing, inbox, correlation states
* ``_alerting_monitors``      — monitors rollup, rule mute state
* ``_alerting_test_send``     — manual "does this channel work?" probe
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.alerting_matching import (
    SCOPE_DISTRIBUTION_DRIFT,
    SCOPE_METRIC,
    SCOPE_RELEASE_REGRESSION,
    SCOPE_VARIABLE_VALUE_DRIFT,
    AlertMatchCandidate,
    DistributionDriftAlertCandidate,
    DriftAlertCandidate,
    SchemaDriftAlertCandidate,
    distribution_drift_scope_ref,
    rule_matches_anomaly,
    simulate_rule_firings,
)
from tripl.models.domain_enums import DistributionDriftBand, MetricScopeType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.project_anomaly_settings import (
    DEFAULT_SIGMA_THRESHOLD,
    ProjectAnomalySettings,
)
from tripl.models.scan_config import ScanConfig
from tripl.schemas.alerting import (
    AlertRuleSimulateResponse,
    AlertRuleUpdate,
    SimulatedRuleFiring,
)
from tripl.services._alerting_deliveries import (
    INBOX_LOOKBACK_DAYS,
    INBOX_MAX_SOURCE_ITEMS,
    FalsePositiveScope,
    InboxFilters,
    SignalIncidentRef,
    apply_alert_inbox_action,
    apply_alert_inbox_bulk_action,
    apply_signal_verdict_to_incident,
    count_open_incidents,
    dedupe_correlation_group_ids,
    delivery_to_response,
    get_alert_inbox_group,
    get_delivery,
    incident_refs_for_signals,
    list_alert_inbox,
    list_deliveries,
    retry_delivery,
    tune_false_positive_scopes,
)
from tripl.services._alerting_destinations import (
    clear_rule_states,
    create_destination,
    create_rule,
    delete_destination,
    delete_rule,
    destination_to_response,
    draft_rule,
    get_destination_response,
    get_rule,
    list_destinations,
    rule_to_response,
    update_destination,
    update_rule,
    validate_filters,
)
from tripl.services._alerting_destinations import (
    get_destination_response as get_destination,
)
from tripl.services._alerting_monitors import (
    get_monitor,
    get_monitors_summary,
    mute_monitor,
    unmute_monitor,
)
from tripl.services._alerting_test_send import (
    send_destination_test,
    send_draft_destination_test,
)
from tripl.services.alerting_rendering import (
    SCOPE_SCHEMA_DRIFT,
)
from tripl.services.alerting_rendering import (
    format_distribution_drift_sample as _format_distribution_drift_sample,
)
from tripl.services.alerting_rendering import (
    render_firings_message as _render_firings_message,
)
from tripl.services.alerting_rendering import (
    trim_alert_text as _trim_alert_text,
)
from tripl.services.project_links import project_org_slugs
from tripl.services.project_lookup import resolve_project as _get_project

# The one scope family whose label is a CONSTANT rather than a row lookup.
# ``worker.tasks.metrics.alert_payload`` imports the identical string as
# ``SCOPE_PROJECT_TOTAL`` from ``core.analyzers.anomaly_detector``; it is spelled
# off the enum here instead because that module imports numpy and statsmodels at
# module scope and nothing else on this async request path pulls them in.
SCOPE_PROJECT_TOTAL = MetricScopeType.project_total.value

# Rechecked against read-only production replay after per-(rule, scan, scope)
# cooldown counting (2026-09-23). The one live rule produced 331 firings over
# 30 days at its 100% delta threshold; tightening that same rule to 200% and
# 300% produced 41 and 15. A 50-firing badge still separates its noisy current
# configuration from those quieter what-if settings. This is one rule, not a
# population-wide calibration; repeat the check as more rules are deployed.
SIMULATE_NOISY_THRESHOLD = 50
SIMULATE_MAX_DAYS = 90

# Re-export everything the API router accesses via ``alerting_service.<name>``
__all__ = [
    "INBOX_LOOKBACK_DAYS",
    "INBOX_MAX_SOURCE_ITEMS",
    "SIMULATE_MAX_DAYS",
    "SIMULATE_NOISY_THRESHOLD",
    "FalsePositiveScope",
    "SignalIncidentRef",
    "apply_alert_inbox_action",
    "apply_alert_inbox_bulk_action",
    "apply_signal_verdict_to_incident",
    "count_open_incidents",
    "clear_rule_states",
    "create_destination",
    "create_rule",
    "dedupe_correlation_group_ids",
    "incident_refs_for_signals",
    "tune_false_positive_scopes",
    "delete_destination",
    "delete_rule",
    "delivery_to_response",
    "destination_to_response",
    "get_alert_inbox_group",
    "get_delivery",
    "get_destination",
    "get_destination_response",
    "get_monitor",
    "get_monitors_summary",
    "get_rule",
    "InboxFilters",
    "list_alert_inbox",
    "list_deliveries",
    "list_destinations",
    "mute_monitor",
    "retry_delivery",
    "rule_to_response",
    "send_destination_test",
    "send_draft_destination_test",
    "simulate_rule",
    "unmute_monitor",
    "update_destination",
    "update_rule",
    "validate_filters",
]


async def _build_event_type_by_event_id(
    session: AsyncSession,
    anomalies: list[AlertMatchCandidate],
) -> dict[uuid.UUID, uuid.UUID]:
    """Event -> event type for candidates carrying an event but no type.

    The async twin of ``worker.tasks.metrics.alert_payload
    ._build_event_type_by_event_id``; same predicate, same query, so the in-UI
    replay narrows an ``event_type`` filter exactly as live dispatch does
    (tripl-0zpq.7).
    """
    from sqlalchemy import select

    from tripl.models.event import Event

    event_ids = {
        anomaly.event_id
        for anomaly in anomalies
        if anomaly.event_id is not None and anomaly.event_type_id is None
    }
    if not event_ids:
        return {}
    rows = await session.execute(
        select(Event.id, Event.event_type_id).where(Event.id.in_(event_ids))
    )
    return {event_id: event_type_id for event_id, event_type_id in rows.all()}


async def _build_scope_name_map(
    session: AsyncSession,
    anomalies: list[AlertMatchCandidate],
) -> dict[tuple[str, str], str]:
    from sqlalchemy import select

    from tripl.models.event import Event
    from tripl.models.event_type import EventType
    from tripl.models.metric_definition import MetricDefinition

    event_ids = {anomaly.event_id for anomaly in anomalies if anomaly.event_id is not None}
    event_type_ids = {
        anomaly.event_type_id for anomaly in anomalies if anomaly.event_type_id is not None
    }

    # Project-total firings are NAMED BY A CONSTANT, not resolved from a row:
    # ``detect`` writes ``scope_ref=str(config.id)``, so the ref is the scan
    # CONFIG's uuid and there is no entity to look up. Without this seed the
    # caller's ``scope_names.get(..., anomaly.scope_ref)`` fallback labels the
    # row with that uuid, while the delivery built from the very same firing says
    # "All events" — and because ``include_project_total`` defaults on, that is
    # the MODAL preview/send disagreement rather than an edge case.
    #
    # Seeded first and unconditionally, exactly as the live twin
    # ``alert_payload._build_alert_scope_names`` opens, so every branch below can
    # only add to it. The label is spelled twice for the same reason
    # ``alerting_rendering._ADOPTION_ADJUSTED_LABEL`` is — the live builder sits
    # in the worker package and the two share no leaf to import it from — so the
    # copy is pinned by ``tests/test_batch4_replay.py``, which runs one
    # project-total candidate through BOTH builders and asserts the maps are
    # equal.
    names: dict[tuple[str, str], str] = {
        (SCOPE_PROJECT_TOTAL, anomaly.scope_ref): "All events"
        for anomaly in anomalies
        if anomaly.scope_type == SCOPE_PROJECT_TOTAL
    }
    event_names: dict[uuid.UUID, str] = {}
    event_type_names: dict[uuid.UUID, str] = {}
    if event_ids:
        rows = await session.execute(select(Event.id, Event.name).where(Event.id.in_(event_ids)))
        for event_id, name in rows.all():
            event_names[event_id] = name
            names[("event", str(event_id))] = name
    if event_type_ids:
        rows = await session.execute(
            select(EventType.id, EventType.display_name).where(EventType.id.in_(event_type_ids))
        )
        for event_type_id, display_name in rows.all():
            event_type_names[event_type_id] = display_name
            names[("event_type", str(event_type_id))] = display_name
    for anomaly in anomalies:
        if anomaly.event_type_id is None:
            continue
        event_type_name = event_type_names.get(anomaly.event_type_id, "Event type")
        drift_field = getattr(anomaly, "drift_field", None) or anomaly.scope_ref
        if anomaly.scope_type == SCOPE_SCHEMA_DRIFT:
            names[(SCOPE_SCHEMA_DRIFT, anomaly.scope_ref)] = f"{event_type_name}.{drift_field}"
        elif anomaly.scope_type == SCOPE_DISTRIBUTION_DRIFT:
            names[(SCOPE_DISTRIBUTION_DRIFT, anomaly.scope_ref)] = (
                f"{event_type_name}.{drift_field}"
            )
    for anomaly in anomalies:
        if anomaly.scope_type != SCOPE_DISTRIBUTION_DRIFT or anomaly.event_type_id is not None:
            continue
        drift_field = getattr(anomaly, "drift_field", None) or anomaly.scope_ref
        names[(SCOPE_DISTRIBUTION_DRIFT, anomaly.scope_ref)] = f"All events.{drift_field}"

    # A variable-value drift is anchored on an EVENT and keeps the variable in
    # ``drift_field``, so it reads "<event>.<variable>" — the live rule, from
    # ``worker.tasks.metrics.alert_payload._build_alert_scope_names``. Its
    # ``scope_ref`` is the drift ROW's uuid, so without this branch every such
    # firing in the replay table would be named by a raw id (tripl-0zpq.158).
    for anomaly in anomalies:
        if anomaly.scope_type != SCOPE_VARIABLE_VALUE_DRIFT or anomaly.event_id is None:
            continue
        event_name = event_names.get(anomaly.event_id, "Event")
        drift_field = getattr(anomaly, "drift_field", None) or anomaly.scope_ref
        names[(SCOPE_VARIABLE_VALUE_DRIFT, anomaly.scope_ref)] = f"{event_name}.{drift_field}"

    # Release regressions borrow the name of the event / event type they were
    # measured on, again mirroring the live builder: their ``scope_ref`` IS that
    # entity's id, so "Login" is both available and the only honest label. Runs
    # after both lookups above so the borrow always has something to find.
    for anomaly in anomalies:
        if anomaly.scope_type != SCOPE_RELEASE_REGRESSION:
            continue
        if anomaly.event_id is not None:
            underlying = names.get(("event", str(anomaly.event_id)))
        elif anomaly.event_type_id is not None:
            underlying = names.get(("event_type", str(anomaly.event_type_id)))
        else:
            underlying = None
        if underlying is not None:
            names[(SCOPE_RELEASE_REGRESSION, anomaly.scope_ref)] = underlying

    # Catalog metric anomalies resolve to the metric's display name (scope_ref is
    # the metric-definition id), mirroring the live worker's _build_alert_scope_names.
    # scope_ref is a string while MetricDefinition.id is a UUID, so parse defensively
    # and skip anything that is not a valid id. Unknown ids fall through to the
    # scope_ref fallback applied by the caller (same as the live setdefault).
    metric_ids: list[uuid.UUID] = []
    for anomaly in anomalies:
        if anomaly.scope_type != SCOPE_METRIC:
            continue
        try:
            metric_ids.append(uuid.UUID(anomaly.scope_ref))
        except ValueError:
            continue
    if metric_ids:
        rows = await session.execute(
            select(MetricDefinition.id, MetricDefinition.display_name).where(
                MetricDefinition.id.in_(set(metric_ids))
            )
        )
        for metric_id, display_name in rows.all():
            names[(SCOPE_METRIC, str(metric_id))] = display_name
    return names


async def _build_metric_unit_map(
    session: AsyncSession,
    anomalies: list[AlertMatchCandidate],
) -> dict[str, str | None]:
    """Display unit per metric-definition id for metric-scope candidates.

    One batched query; the map feeds render_firings_message so simulator
    previews of percent metrics match the live worker's ×100 rendering
    (shared-predicates parity).
    """
    from sqlalchemy import select

    from tripl.models.metric_definition import MetricDefinition

    metric_ids: list[uuid.UUID] = []
    for anomaly in anomalies:
        if anomaly.scope_type != SCOPE_METRIC:
            continue
        try:
            metric_ids.append(uuid.UUID(anomaly.scope_ref))
        except ValueError:
            continue
    if not metric_ids:
        return {}
    rows = await session.execute(
        select(MetricDefinition.id, MetricDefinition.unit).where(
            MetricDefinition.id.in_(set(metric_ids))
        )
    )
    return {str(metric_id): unit for metric_id, unit in rows.all()}


async def _load_schema_drift_candidates(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    window_from: datetime,
    window_to: datetime,
) -> list[SchemaDriftAlertCandidate]:
    """Replay twin of ``signals._get_active_schema_drift_candidates``.

    Same join, the same open/unsnoozed gate and the same field mapping — field
    name -> ``drift_field``, the drift kind -> ``drift_type``, the trimmed
    sample -> ``sample_value``. Two predicates differ, and both differ because
    the QUESTION differs:

    * the live loader runs per collection and asks "is this drift open NOW", so
      it reads a fixed 30-day retention cutoff. A replay is asked about a
      window, so the window bounds ``detected_at`` instead.
    * the live loader matches ONE ``scan_config_id``; a replay spans the
      project, so it matches the project and requires the column to be set —
      the same NOT NULL ``_load_variable_value_drift_candidates`` below carries,
      for the same reason. ``scan_config_id`` is ``SET NULL`` when a scan is
      deleted (``scan_service.delete_scan_config`` and the
      ``DataSource.scan_configs`` cascade both reach it) and nothing purges the
      orphaned rows before the 30-day prune, while ``signals`` selects
      ``SchemaDrift.scan_config_id == config.id`` and a NULL never equals a
      config id. Live goes quiet on those rows the moment the scan goes, so a
      replay that kept listing them would be LOUDER than the pipeline it
      predicts, which is the same defect as being quieter (tripl-0zpq.158).
      Nothing is hidden permanently: ``_upsert_schema_drifts``'s ``coalesce``
      re-stamps the provenance as soon as any scan re-detects the same (event
      type, field, kind), and the row becomes deliverable and replayable again
      together.
    """
    from datetime import UTC

    from sqlalchemy import select

    from tripl.models.event_type import EventType
    from tripl.models.schema_drift import SchemaDrift

    rows = (
        (
            await session.execute(
                select(SchemaDrift)
                .join(EventType, EventType.id == SchemaDrift.event_type_id)
                .where(
                    EventType.project_id == project_id,
                    SchemaDrift.scan_config_id.is_not(None),
                    SchemaDrift.detected_at >= window_from,
                    SchemaDrift.detected_at < window_to,
                    SchemaDrift.status.in_(("open", "snoozed")),
                    (SchemaDrift.status != "snoozed")
                    | (SchemaDrift.snoozed_until.is_(None))
                    | (SchemaDrift.snoozed_until <= datetime.now(UTC)),
                )
                .order_by(SchemaDrift.detected_at)
            )
        )
        .scalars()
        .all()
    )
    return [
        SchemaDriftAlertCandidate(
            id=drift.id,
            scan_config_id=drift.scan_config_id,
            scope_type=SCOPE_SCHEMA_DRIFT,
            scope_ref=str(drift.id),
            event_id=None,
            event_type_id=drift.event_type_id,
            bucket=drift.detected_at,
            direction="spike",
            actual_count=1,
            expected_count=0.0,
            drift_field=drift.field_name,
            drift_type=drift.drift_type,
            sample_value=_trim_alert_text(drift.sample_value),
        )
        for drift in rows
    ]


async def _load_distribution_drift_candidates(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    window_from: datetime,
    window_to: datetime,
) -> list[DistributionDriftAlertCandidate]:
    from sqlalchemy import select

    from tripl.models.distribution_drift import DistributionDrift

    rows = (
        (
            await session.execute(
                select(DistributionDrift)
                .join(ScanConfig, ScanConfig.id == DistributionDrift.scan_config_id)
                .where(
                    ScanConfig.project_id == project_id,
                    DistributionDrift.band == DistributionDriftBand.significant.value,
                    DistributionDrift.bucket >= window_from,
                    DistributionDrift.bucket < window_to,
                )
                .order_by(DistributionDrift.bucket)
            )
        )
        .scalars()
        .all()
    )
    candidates: list[DistributionDriftAlertCandidate] = []
    for drift in rows:
        owner_id = drift.event_type_id or drift.scan_config_id
        candidates.append(
            DistributionDriftAlertCandidate(
                id=drift.id,
                scan_config_id=drift.scan_config_id,
                scope_type=SCOPE_DISTRIBUTION_DRIFT,
                scope_ref=distribution_drift_scope_ref(owner_id, drift.field_name),
                event_id=None,
                event_type_id=drift.event_type_id,
                bucket=drift.bucket,
                direction="spike",
                actual_count=drift.current_total,
                expected_count=float(drift.baseline_total),
                drift_field=drift.field_name,
                drift_type="distribution_shift",
                sample_value=_format_distribution_drift_sample(drift),
            )
        )
    return candidates


async def _load_variable_value_drift_candidates(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    window_from: datetime,
    window_to: datetime,
) -> list[DriftAlertCandidate]:
    """Replay twin of ``signals._get_active_variable_value_drift_candidates``.

    Same rows and the same field mapping — variable display name ->
    ``drift_field``, ``"value_drift"`` -> ``drift_type``, the sampled novel
    values -> ``sample_value``, the per-event anchor through ``event_id`` so
    event filters still apply. Two predicates differ, and both differ because
    the QUESTION differs:

    * the live loader runs per collection and asks "is this drift open NOW", so
      it reads a fixed 30-day retention cutoff. A replay is asked about a
      window, so the window bounds ``detected_at`` instead — the same trade
      ``_load_schema_drift_candidates`` above already makes.
    * the live loader matches ONE ``scan_config_id``; a replay spans the
      project, so it matches the project and requires the column to be set. That
      NOT NULL is load-bearing rather than tidy: ``scan_config_id`` is ``SET
      NULL`` when a scan is deleted, and a NULL can never equal a config id, so
      live can never deliver such a row. A replay that showed it would be LOUDER
      than the pipeline it predicts, which is the same defect as being quieter
      (tripl-0zpq.158) — and the demo seeds exactly one of them on purpose
      (``services/demo/builders/variables._build_value_drift``).

    The join to ``Variable`` carries both the display name and the
    ``excluded_from_scans`` gate; the live docstring explains why that flag has
    to be asked here rather than trusted to the exclude endpoint's purge.
    """
    from datetime import UTC

    from sqlalchemy import select

    from tripl.models.variable import Variable
    from tripl.models.variable_value_drift import VariableValueDrift

    rows = (
        await session.execute(
            select(VariableValueDrift, Variable.name)
            .join(Variable, Variable.id == VariableValueDrift.variable_id)
            .where(
                VariableValueDrift.project_id == project_id,
                VariableValueDrift.scan_config_id.is_not(None),
                Variable.excluded_from_scans.is_(False),
                VariableValueDrift.detected_at >= window_from,
                VariableValueDrift.detected_at < window_to,
                VariableValueDrift.status.in_(("open", "snoozed")),
                (VariableValueDrift.status != "snoozed")
                | (VariableValueDrift.snoozed_until.is_(None))
                | (VariableValueDrift.snoozed_until <= datetime.now(UTC)),
            )
            .order_by(VariableValueDrift.detected_at)
        )
    ).all()
    return [
        DriftAlertCandidate(
            id=drift.id,
            scan_config_id=drift.scan_config_id,
            scope_type=SCOPE_VARIABLE_VALUE_DRIFT,
            scope_ref=str(drift.id),
            event_id=drift.event_id,
            event_type_id=None,
            bucket=drift.detected_at,
            direction="spike",
            actual_count=float(len(drift.observed_values or [])),
            expected_count=0.0,
            drift_field=variable_name,
            drift_type="value_drift",
            sample_value=_trim_alert_text(", ".join(drift.observed_values or [])),
        )
        for drift, variable_name in rows
    ]


async def _load_release_regression_candidates(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    window_from: datetime,
    window_to: datetime,
) -> list[DriftAlertCandidate]:
    """Replay twin of ``signals._get_active_release_regression_candidates``.

    Every stored row is CURRENT — the recalculation keeps only the latest
    release's regressions — which is why the live loader needs no time filter at
    all: it runs per collection and re-reads whatever the last pass wrote. A
    replay cannot reconstruct that history, because the table holds one row per
    (scan, scope, release) and not one per collection. So it places each row at
    the window it actually measured and admits it when that window's END falls
    inside the replay range.

    The consequence is worth stating rather than discovering: a standing
    regression contributes AT MOST ONE firing to a replay, while the live rule
    re-sends it once per cooldown for as long as it persists. For this one
    family the replay is a LOWER bound, not an estimate — see
    ``website/docs/use/alerting.md``. It is still the honest answer available
    from the rows that exist, and it is enormously closer than the zero this
    replay used to report (tripl-0zpq.158).

    The ``app_version_column`` clause mirrors the live short-circuit instead of
    trusting "no rows exist anyway". Rows outlive the setting — nothing purges
    them when an operator clears the column — while live stops delivering them
    the moment it is cleared, so the replay has to stop as well.
    """
    from sqlalchemy import select

    from tripl.models.release_regression import ReleaseRegression

    rows = (
        (
            await session.execute(
                select(ReleaseRegression)
                .join(ScanConfig, ScanConfig.id == ReleaseRegression.scan_config_id)
                .where(
                    ScanConfig.project_id == project_id,
                    ScanConfig.app_version_column.is_not(None),
                    ScanConfig.app_version_column != "",
                    ReleaseRegression.window_to >= window_from,
                    ReleaseRegression.window_to < window_to,
                )
                .order_by(ReleaseRegression.window_to, ReleaseRegression.scope_ref)
            )
        )
        .scalars()
        .all()
    )
    return [
        DriftAlertCandidate(
            id=regression.id,
            scan_config_id=regression.scan_config_id,
            scope_type=SCOPE_RELEASE_REGRESSION,
            scope_ref=regression.scope_ref,
            event_id=regression.event_id,
            event_type_id=regression.event_type_id,
            bucket=regression.window_to,
            direction="drop",
            actual_count=regression.observed_count,
            expected_count=regression.expected_count,
            drift_field=regression.version,
            drift_type=regression.kind,
            sample_value=regression.previous_version,
            # ``bucket`` above is window_to, and carrying the other end is what
            # lets the PREVIEW name the rollout overlap the send already names.
            # TWO hops carry it and both are load-bearing: this one, and
            # ``simulate_rule``'s ``SimulatedRuleFiring(window_from=...)``, which
            # reads it back off the candidate. Drop either and
            # ``build_drift_line`` silently loses the clause on the preview side
            # only — the exact preview/send split the shared builder exists to
            # close (tripl-0zpq.165).
            window_from=regression.window_from,
        )
        for regression in rows
    ]


def _clears_sigma(anomaly: AlertMatchCandidate, sigma_threshold: float) -> bool:
    """Would the detector still have recorded this anomaly at ``sigma_threshold``?

    ``AlertMatchCandidate`` is a Protocol and only the ``MetricAnomaly`` members
    of it carry a ``z_score``. The four dataclass families — schema drift,
    distribution drift, variable-value drift and release regressions — come from
    schema comparisons, PSI, allowed-value lists and release composition shares
    respectively, and not one of them is scored in sigmas at all. Those answer
    True: a detector sensitivity has nothing to say about them, the same way the
    rule's numeric thresholds do not gate them.
    """
    z_score = getattr(anomaly, "z_score", None)
    if z_score is None:
        return True
    return abs(float(z_score)) >= sigma_threshold


async def simulate_rule(
    session: AsyncSession,
    slug: str,
    destination_id: uuid.UUID,
    rule_id: uuid.UUID,
    days: int,
    cooldown_minutes_override: int | None = None,
    min_percent_delta_override: float | None = None,
    min_expected_count_override: float | None = None,
    sigma_threshold_override: float | None = None,
    draft: AlertRuleUpdate | None = None,
) -> AlertRuleSimulateResponse:
    """Replay a rule over the last ``days`` and report what it would have sent.

    ``draft`` is the editor's unsaved PATCH body. When given, the replay runs
    the stored rule with those changes laid over it (``draft_rule``: validated
    as Save validates, never written), so a user can see what an edit would
    have sent before saving it onto a rule that may be live (ALR-12). The
    ``*_saved`` fields still report the stored rule.

    Every override answers a what-if WITHOUT writing anything: the rule under
    test is usually live-routing to a real channel, so "would min_percent_delta
    300 have cut these incidents" must not be asked by saving 300 and waiting
    (tripl-oxkt.17 part 3). Each is reported back as ``*_used`` beside the rule's
    stored ``*_saved`` value.

    "What it would have sent" is the IMMEDIATE-delivery answer, and for one
    destination class that is not what production does. ``destination`` is loaded
    below for the rendered preview only; the counting is done by
    ``simulate_rule_firings``, which gates every candidate on
    ``cooldown_minutes``, while live switches that gate off for a destination
    carrying a ``delivery_schedule_cron`` and lets the cadence and the digest
    buffer limit it instead (``dispatch._prepare_alert_deliveries``,
    ``_buffer_pending_items``). For such a destination ``firings``, ``noisy`` and
    ``rendered_message`` describe a limiter production does not apply — see
    ``simulate_rule_firings`` for which way the count is off and for why
    mirroring the live gate alone would make it worse rather than better. The
    number on this response that no limiter touches either way is
    ``matched_before_cooldown``, computed straight from the shared matcher; the
    replay dialog already shows it as "Matched N before cooldown".
    """
    from datetime import UTC, timedelta

    from fastapi import HTTPException
    from sqlalchemy import select

    from tripl.models.metric_definition import MetricDefinition

    if days <= 0 or days > SIMULATE_MAX_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"days must be between 1 and {SIMULATE_MAX_DAYS}",
        )
    if cooldown_minutes_override is not None and cooldown_minutes_override < 0:
        raise HTTPException(
            status_code=422,
            detail="cooldown_minutes_override must be >= 0",
        )
    if min_percent_delta_override is not None and min_percent_delta_override < 0:
        raise HTTPException(
            status_code=422,
            detail="min_percent_delta_override must be >= 0",
        )
    if min_expected_count_override is not None and min_expected_count_override < 0:
        raise HTTPException(
            status_code=422,
            detail="min_expected_count_override must be >= 0",
        )
    # Strictly positive, unlike the two rule thresholds: sigma divides the gap by
    # the spread, so 0 is not "no filter", it is "everything is infinitely far
    # out" — the detector itself never scores against it.
    if sigma_threshold_override is not None and sigma_threshold_override <= 0:
        raise HTTPException(
            status_code=422,
            detail="sigma_threshold_override must be > 0",
        )

    project = await _get_project(session, slug)
    destination, rule = await get_rule(
        session,
        project_id=project.id,
        destination_id=destination_id,
        rule_id=rule_id,
    )
    saved_rule = rule
    if draft is not None:
        rule = await draft_rule(
            session, project=project, destination=destination, rule=saved_rule, data=draft
        )

    window_to = datetime.now(UTC)
    window_from = window_to - timedelta(days=days)

    metric_anomalies = list(
        (
            await session.execute(
                select(MetricAnomaly)
                .join(ScanConfig, ScanConfig.id == MetricAnomaly.scan_config_id)
                .where(
                    ScanConfig.project_id == project.id,
                    MetricAnomaly.bucket >= window_from,
                    MetricAnomaly.bucket < window_to,
                )
                .order_by(MetricAnomaly.bucket)
            )
        )
        .scalars()
        .all()
    )
    # Catalog metric anomalies are project-global (scope_type='metric', NULL
    # scan_config_id), so the scan-config inner join above drops them. Load them
    # separately, scoped to the project via their MetricDefinition — mirroring the
    # live detect/signals path (MetricDefinition.project_id) — then merge, keeping
    # the combined list ordered by bucket for the cooldown replay.
    project_metric_scope_refs = [
        str(metric_id)
        for metric_id in (
            await session.execute(
                select(MetricDefinition.id).where(MetricDefinition.project_id == project.id)
            )
        ).scalars()
    ]
    if project_metric_scope_refs:
        global_metric_anomalies = list(
            (
                await session.execute(
                    select(MetricAnomaly)
                    .where(
                        MetricAnomaly.scan_config_id.is_(None),
                        MetricAnomaly.scope_type == SCOPE_METRIC,
                        MetricAnomaly.scope_ref.in_(project_metric_scope_refs),
                        MetricAnomaly.bucket >= window_from,
                        MetricAnomaly.bucket < window_to,
                    )
                    .order_by(MetricAnomaly.bucket)
                )
            )
            .scalars()
            .all()
        )
        if global_metric_anomalies:
            metric_anomalies = sorted(
                [*metric_anomalies, *global_metric_anomalies],
                key=lambda anomaly: anomaly.bucket,
            )
    schema_candidates = await _load_schema_drift_candidates(
        session,
        project_id=project.id,
        window_from=window_from,
        window_to=window_to,
    )
    distribution_candidates = await _load_distribution_drift_candidates(
        session,
        project_id=project.id,
        window_from=window_from,
        window_to=window_to,
    )
    variable_value_drift_candidates = await _load_variable_value_drift_candidates(
        session,
        project_id=project.id,
        window_from=window_from,
        window_to=window_to,
    )
    release_regression_candidates = await _load_release_regression_candidates(
        session,
        project_id=project.id,
        window_from=window_from,
        window_to=window_to,
    )
    # FIVE sources, matching the five ``dispatch._prepare_alert_deliveries``
    # merges (worker/tasks/metrics/dispatch.py). It was three until
    # tripl-0zpq.158: variable-value drifts and release regressions were never
    # loaded, so a rule with ``include_variable_value_drifts`` or
    # ``include_release_regressions`` on replayed SILENT while the pipeline
    # paged on every one of those rows — and silent in every field at once,
    # because the whole response is derived from this list:
    # ``anomalies_considered``, ``matched_before_cooldown``, ``firings``,
    # ``noisy`` and ``rendered_message``. The operator read "quiet, not noisy"
    # for a rule that pages thirty times a week, and troubleshooting.md's "if it
    # doesn't match in the simulator it won't match live either" was false for
    # precisely the two scopes nothing here could produce.
    #
    # Both toggles are gated inside ``rule_matches_anomaly``, so loading the
    # rows here is all it takes to make the gate reachable; nothing about WHICH
    # of them fires is decided in this module — that is the point of
    # ``tripl.alerting_matching``.
    #
    # The one dispatch source deliberately NOT replayed is ``source_freshness``
    # (``signals._get_source_freshness_candidates``, issue #269). Freshness is
    # computed from the scan's CURRENT ``last_event_at`` / ``last_collection_at``
    # and nothing records what the lag was at a past instant, so there is no
    # history to replay; inventing one from today's columns would report the
    # current delay at every bucket of the window.
    anomalies: list[AlertMatchCandidate] = [
        *metric_anomalies,
        *schema_candidates,
        *distribution_candidates,
        *variable_value_drift_candidates,
        *release_regression_candidates,
    ]
    # A sigma what-if is a question about DETECTION, not about the rule, so it is
    # applied to the candidate list itself: in the world being simulated those
    # anomalies were never recorded, so they have to be missing from
    # ``anomalies_considered`` as well, not merely from the firings.
    #
    # It can only narrow. The detector writes a row when |z| clears its own
    # threshold, so raising the bar re-reads rows that exist on disk, while
    # lowering it asks about rows nobody ever wrote and there is nothing to bring
    # back — see the docstring on ``AlertRuleSimulateResponse``. Candidates with
    # no z-score — all four non-metric families: schema drift, distribution
    # drift, variable-value drift and release regressions — pass through
    # untouched, exactly as they bypass the rule's numeric thresholds.
    if sigma_threshold_override is not None:
        anomalies = [
            anomaly for anomaly in anomalies if _clears_sigma(anomaly, sigma_threshold_override)
        ]

    # Event-anchored candidates (event scope, and the drift/regression families
    # once they are loaded here) store a NULL ``event_type_id`` on purpose, so an
    # ``event_type`` filter can only narrow them through this lookup. Built with
    # the same predicate the live path uses in
    # ``dispatch._prepare_alert_deliveries`` — the replay and the pipeline have to
    # answer the same question, which is the whole point of
    # ``tripl.alerting_matching`` (tripl-0zpq.7).
    event_type_by_event_id = await _build_event_type_by_event_id(session, anomalies)

    matched_before_cooldown = sum(
        1
        for anomaly in anomalies
        if rule_matches_anomaly(
            rule,
            anomaly,
            min_percent_delta_override=min_percent_delta_override,
            min_expected_count_override=min_expected_count_override,
            event_type_by_event_id=event_type_by_event_id,
        )
    )
    fired = simulate_rule_firings(
        rule,
        anomalies,
        cooldown_minutes_override=cooldown_minutes_override,
        min_percent_delta_override=min_percent_delta_override,
        min_expected_count_override=min_expected_count_override,
        event_type_by_event_id=event_type_by_event_id,
    )

    scope_names = await _build_scope_name_map(session, fired)
    metric_units = await _build_metric_unit_map(session, fired)

    # Every field is read off the candidate by ``SimulatedRuleFiring.from_candidate``,
    # the constructor the demo seeder shares, so the replay and the demo cannot
    # disagree about a firing's shape (tripl-0zpq.324). It also owns the two
    # guarantees this loop used to spell out: the delta goes through the SHARED
    # ``alert_templates.percent_delta_of`` (the simulator reporting 0.0% where
    # dispatch reported 200% for the same signed catalog metric was
    # tripl-0zpq.102), and the name is trimmed with ``trim_scope_name`` the way
    # ``alert_payload`` trims it before the 255-character column, so the preview
    # shows the label the send actually delivers.
    firings: list[SimulatedRuleFiring] = [
        SimulatedRuleFiring.from_candidate(
            anomaly,
            scope_name=scope_names.get((anomaly.scope_type, anomaly.scope_ref), anomaly.scope_ref),
        )
        for anomaly in fired
    ]

    rendered_items, rendered_message = _render_firings_message(
        rule,
        firings,
        destination=destination,
        project=project,
        metric_units=metric_units,
        org_slug=(await project_org_slugs(session, [project.id])).get(project.id, ""),
    )
    for firing, rendered_item in zip(firings, rendered_items, strict=True):
        firing.rendered_item = rendered_item or None

    effective_cooldown = (
        cooldown_minutes_override
        if cooldown_minutes_override is not None
        else rule.cooldown_minutes
    )
    # The detector threshold this replay is measured against, read from the one
    # place the detector itself reads it: the PROJECT's Detection settings
    # (tripl-0zpq.160). This used to quote ``ScanConfig.sigma_threshold`` — a
    # per-scan copy of the same number that nothing scores against and no API
    # writes. ``worker.tasks.metrics.detect`` builds its
    # ``AnomalyDetectionSettings`` from ``ProjectAnomalySettings`` alone
    # (``_build_anomaly_settings``), so an operator who raised sigma to 5.0 in
    # Detection settings — as anomaly-detection.md recommends for cutting noise —
    # was shown the stale 4.0 the column still held, and every what-if typed into
    # this dialog was then reasoned about against a threshold nothing detects
    # with: an "override" of 4.5 looked stricter and dropped nothing, because
    # every stored row had already cleared 5.0.
    #
    # ``DEFAULT_SIGMA_THRESHOLD`` when there is no settings row, the same
    # fallback the false-positive ratchet already uses for the same value
    # (``_alerting_deliveries``). The row is created lazily by the Detection
    # settings endpoint, and detect.py treats its absence as "detection off,
    # purge everything", so a project in that state has no anomalies to replay
    # anyway — the default is what the row would be born holding.
    #
    # Per-scope ``AnomalyScopeOverride`` rows can still raise the effective
    # threshold ABOVE this for individual scopes. This is the project-wide base,
    # which is the only thing a single number in the dialog can honestly be.
    settings_sigma = await session.scalar(
        select(ProjectAnomalySettings.sigma_threshold).where(
            ProjectAnomalySettings.project_id == project.id
        )
    )
    sigma_threshold_saved = (
        DEFAULT_SIGMA_THRESHOLD if settings_sigma is None else float(settings_sigma)
    )

    return AlertRuleSimulateResponse(
        rule_id=rule.id,
        rule_name=rule.name,
        days=days,
        window_from=window_from,
        window_to=window_to,
        anomalies_considered=len(anomalies),
        matched_before_cooldown=matched_before_cooldown,
        firings=firings,
        noisy=len(firings) > SIMULATE_NOISY_THRESHOLD,
        cooldown_minutes_used=effective_cooldown,
        cooldown_minutes_saved=saved_rule.cooldown_minutes,
        min_percent_delta_used=(
            rule.min_percent_delta
            if min_percent_delta_override is None
            else min_percent_delta_override
        ),
        min_percent_delta_saved=saved_rule.min_percent_delta,
        min_expected_count_used=(
            rule.min_expected_count
            if min_expected_count_override is None
            else min_expected_count_override
        ),
        min_expected_count_saved=saved_rule.min_expected_count,
        sigma_threshold_used=(
            sigma_threshold_saved if sigma_threshold_override is None else sigma_threshold_override
        ),
        sigma_threshold_saved=sigma_threshold_saved,
        rendered_message=rendered_message or None,
    )
