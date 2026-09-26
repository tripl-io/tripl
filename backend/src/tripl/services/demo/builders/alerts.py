"""Alerts builder: a local, no-network alerting scenario over the demo signals.

Seeds a demo-only ``demo_sink`` destination (a local sink that renders and
records deliveries with no outbound network), a FIRING alert rule driven by the
real seeded anomalies (project-total / event-type / event / catalog-metric
scopes, plus the schema / distribution / variable-value / release-regression
opt-ins) and a HEALTHY quiet rule, their per-scope ``AlertRuleState`` rows, one
locally-delivered ``AlertDelivery`` + items with rendered template output, an
inbox correlation group, a chart annotation explaining the injected spike, and
one automatic release marker.

Everything is produced with ZERO network side effects: the delivery text is
rendered via the same pure renderer the in-UI simulator uses
(``render_firings_message``) and recorded with a local/simulated marker, so
Monitors, the inbox, delivery history, retry and simulate are all fully
explorable in a demo without any credentials. The builder runs AFTER monitoring
and catalog so the anomalies/metrics it references already exist; like the other
builders it only ``flush``es (the caller owns the single transaction) and never
calls a service that commits internally.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.alert_templates import DEMO_SINK_LOCAL_NOTICE
from tripl.models.alert_correlation_state import AlertCorrelationState
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.domain_enums import (
    AlertMessageFormat,
    AnomalyDirection,
    ChartAnnotationScopeType,
    ChartAnnotationSource,
    MetricScopeType,
)
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.project import Project
from tripl.schemas.alerting import SimulatedRuleFiring
from tripl.services.alerting_rendering import render_firings_message
from tripl.services.demo.builders.warehouse import (
    SPIKE_ANNOTATION_LABEL,
    SPIKE_EVENT_NAME,
)
from tripl.services.demo.scenario import DemoContext
from tripl.services.release_annotations import (
    RELEASE_ANNOTATION_COLOR,
    release_annotation_label,
)

# Deterministic namespace so the seeded inbox correlation-group id is stable for
# a given project id (mirrors dispatch._CORRELATION_NAMESPACE's intent).
_DEMO_ALERT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "tripl-demo-alert-sink")

_DEMO_SINK_NAME = "Local demo sink (no external delivery)"
_DISABLED_EXTERNAL_NAME = "Slack (disabled — connect a webhook to enable)"
_FIRING_RULE_NAME = "Spike & drift watch (demo)"
_HEALTHY_RULE_NAME = "Weekly release health (quiet)"

# The demo's one automatic release marker: the newest version the synthetic
# warehouse carries, rolled out a few days back. Seeded rather than derived,
# because the synthetic dataset spreads its versions evenly from its first hour,
# so the worker's gate sees them all live at once and — correctly — marks none.
# Labelled through the worker's own helper, so a real scan of the demo can never
# add a second "Release 1.4.0" (the release label is unique per project).
DEMO_RELEASE_VERSION = "1.4.0"
DEMO_RELEASE_AGE = timedelta(days=6)

# Seeded catalog-metric spike: scored against the median of the whole stored
# series, over the most recent complete buckets, so the signal is on-grid and
# genuinely high against its own baseline (bd tripl-jfm3.63).
_METRIC_ANOMALY_RECENT_BUCKETS = 7
_METRIC_ANOMALY_MIN_POINTS = 3
_METRIC_ANOMALY_Z_SCORE = 3.0
_METRIC_ANOMALY_MIN_STDDEV = 0.001

# One FAILED earlier attempt at the same incident. The Audit table only offers
# Retry on a failed row, and the local sink cannot fail, so without a seeded
# failure the retry the docs promise is unreachable in a demo (tripl-jfm3.59).
# It is recorded against the local sink, so pressing Retry re-dispatches it down
# the normal path and it succeeds — a complete, safe round trip.
_FAILED_DELIVERY_AGE_HOURS = 3
_FAILED_DELIVERY_ERROR = (
    "Simulated transport failure (demo): the local sink was unavailable on the "
    "first attempt. Use Retry to re-dispatch this delivery."
)


async def build_alerts(session: AsyncSession, ctx: DemoContext) -> None:
    project = await session.get(Project, ctx.project_id)
    if project is None or ctx.scan_config_id is None:
        return

    # A metric-scope anomaly over a seeded catalog metric, so the firing rule's
    # ``include_metrics`` opt-in covers a REAL signal (catalog metrics otherwise
    # ship values but no anomaly). Project-global rows carry NULL scan_config_id.
    metric_anomaly = await _seed_catalog_metric_anomaly(session, ctx)

    # The demo-only local sink + a visibly-disabled external example. The
    # disabled destination carries NO credentials and is clearly labelled
    # disabled, so it is non-functional by construction (never sends).
    demo_sink = AlertDestination(
        project_id=ctx.project_id,
        type=AlertDestinationType.demo_sink.value,
        name=_DEMO_SINK_NAME,
        enabled=True,
    )
    disabled_external = AlertDestination(
        project_id=ctx.project_id,
        type=AlertDestinationType.slack.value,
        name=_DISABLED_EXTERNAL_NAME,
        enabled=False,
    )
    session.add_all([demo_sink, disabled_external])
    await session.flush()

    firing_rule = AlertRule(
        destination_id=demo_sink.id,
        name=_FIRING_RULE_NAME,
        enabled=True,
        include_project_total=True,
        include_event_types=True,
        include_events=True,
        include_schema_drifts=True,
        include_distribution_drifts=True,
        include_variable_value_drifts=True,
        include_release_regressions=True,
        include_metrics=True,
        notify_on_spike=True,
        notify_on_drop=True,
        message_format=AlertMessageFormat.plain.value,
    )
    healthy_rule = AlertRule(
        destination_id=demo_sink.id,
        name=_HEALTHY_RULE_NAME,
        enabled=True,
        include_project_total=True,
        include_event_types=False,
        include_events=False,
        include_release_regressions=True,
        notify_on_spike=True,
        notify_on_drop=True,
        message_format=AlertMessageFormat.plain.value,
    )
    session.add_all([firing_rule, healthy_rule])
    await session.flush()

    # The latest anomaly per seeded scope (+ the catalog metric anomaly) drives
    # both the active rule-state rows (=> a "firing" monitor) and the recorded
    # local delivery. The healthy rule intentionally gets no states (=> healthy).
    firing_anomalies = await _select_firing_anomalies(session, ctx, metric_anomaly)
    if not firing_anomalies:
        # Defensive: no seeded anomalies. Leave the destinations + rules so the
        # demo still shows a local sink and a healthy monitor.
        return

    firings = await _build_firings(session, project, firing_anomalies)
    correlation_group_id = uuid.uuid5(
        _DEMO_ALERT_NAMESPACE, f"{ctx.project_id}:{firing_rule.id}:incident"
    )

    # Render the delivery text through the SAME pure renderer the simulator uses
    # — no session, no network, deterministic for a given clock+seed.
    _rendered_items, rendered_message = render_firings_message(
        firing_rule,
        firings,
        destination=demo_sink,
        project=project,
    )

    delivery = AlertDelivery(
        project_id=ctx.project_id,
        scan_config_id=ctx.scan_config_id,
        destination_id=demo_sink.id,
        rule_id=firing_rule.id,
        status=AlertDeliveryStatus.sent.value,
        channel=AlertDestinationType.demo_sink.value,
        matched_count=len(firings),
        sent_at=ctx.now,
        payload_snapshot={
            "message_format": AlertMessageFormat.plain.value,
            "rendered_message": rendered_message,
            "delivery_mode": "local_sink",
            "is_local": True,
            "simulated": True,
            "local_notice": DEMO_SINK_LOCAL_NOTICE,
        },
    )
    session.add(delivery)
    # The earlier, failed attempt at the same incident. It carries the same
    # rendered text and, below, its own copy of the incident's items.
    failed_delivery = AlertDelivery(
        project_id=ctx.project_id,
        scan_config_id=ctx.scan_config_id,
        destination_id=demo_sink.id,
        rule_id=firing_rule.id,
        status=AlertDeliveryStatus.failed.value,
        channel=AlertDestinationType.demo_sink.value,
        matched_count=len(firings),
        dispatch_attempts=1,
        error_message=_FAILED_DELIVERY_ERROR,
        created_at=ctx.now - timedelta(hours=_FAILED_DELIVERY_AGE_HOURS),
        payload_snapshot={
            "message_format": AlertMessageFormat.plain.value,
            "rendered_message": rendered_message,
            "delivery_mode": "local_sink",
            "is_local": True,
            "simulated": True,
            "local_notice": DEMO_SINK_LOCAL_NOTICE,
        },
    )
    session.add(failed_delivery)
    await session.flush()

    # BOTH deliveries get their own copy of the incident's items, because the
    # failed row is not decoration: Retry re-dispatches it through
    # ``send_alert_delivery``, which re-renders the message from
    # ``delivery.items`` and OVERWRITES ``payload_snapshot["rendered_message"]``
    # with the result. While the failed row owned no items, the one retry the
    # demo exists to demonstrate replaced the seeded message with a header
    # counting ``matched_count`` signals above an empty list, and left the row
    # reading "sent" with nothing in it (tripl-0zpq.247).
    #
    # The copies share ``correlation_group_id`` because that is the shape live
    # dispatch writes — items are stamped with the incident's group id when the
    # delivery row is created, and a send that fails does not take them back —
    # and because it is what keeps the failed attempt reachable: the Inbox
    # card's "show what was sent" list asks the API for deliveries having an
    # item in this group (frontend IncidentDeliveries), so a NULL group here
    # would hide the failed row from the incident the docs say it belongs to,
    # and with it the Retry. The card consequently counts this incident's items
    # across both deliveries, exactly as it does for any scope that fires twice.
    for firing in firings:
        session.add(_delivery_item(firing, delivery.id, correlation_group_id))
        session.add(_delivery_item(firing, failed_delivery.id, correlation_group_id))

    session.add(
        AlertCorrelationState(
            project_id=ctx.project_id,
            correlation_group_id=correlation_group_id,
            status="open",
            last_seen_at=max(firing.bucket for firing in firings),
        )
    )

    # Active per-scope states for the FIRING rule => a firing monitor. The
    # anomaly bucket is anchored at ``ctx.now`` so the rollup reads "firing"
    # (recent) when the demo is opened.
    for firing in firings:
        session.add(
            AlertRuleState(
                rule_id=firing_rule.id,
                # A ``metric`` firing is project-global and stores NULL, which is
                # the key live dispatch writes (tripl-0zpq.28). Seeding it on a
                # real scan config would leave the demo carrying a state row the
                # dispatcher could never load again — and the builder seeds a
                # catalog-metric firing, so this arm is reached every time.
                scan_config_id=(
                    None
                    if firing.scope_type == MetricScopeType.metric.value
                    else ctx.scan_config_id
                ),
                scope_type=firing.scope_type.value,
                scope_ref=firing.scope_ref,
                is_active=True,
                opened_at=ctx.now - timedelta(hours=2),
                last_anomaly_bucket=ctx.now,
                last_notified_at=ctx.now,
                last_notified_delivery_id=delivery.id,
            )
        )

    # Chart annotation explaining the controlled scenario change (the spike),
    # dated at the bucket the warehouse builder injected that spike into rather
    # than at ``ctx.now``. ``ctx.now`` is the still-open hour, one bucket PAST the
    # newest stored point, so a marker there never lines up with the series it
    # explains: on the default hourly view the chart snaps it onto the one-step
    # dashed forecast point, and once the demo runtime has appended that hour for
    # real the marker labels an ordinary bucket sitting right after the spike
    # (tripl-0zpq.249). ``ChartAnnotation.bucket`` is NOT NULL, so ``or ctx.now``
    # keeps the row valid for a context assembled without the warehouse builder;
    # inside the recipe that cannot happen, because the early return at the top
    # of this builder already requires the scan config warehouse writes.
    #
    # The demo runtime's retention pass deletes this row once its bucket falls
    # behind the retention cutoff — the same cutoff that prunes the anomaly it
    # explains. The runtime appends plain noise with no new spike, so an aging
    # demo retires the story rather than re-injecting it (tripl-0zpq.322).
    spike_event_id = ctx.event_ids.get(SPIKE_EVENT_NAME)
    session.add(
        ChartAnnotation(
            project_id=ctx.project_id,
            scope_type=(ChartAnnotationScopeType.event.value if spike_event_id else None),
            scope_ref=str(spike_event_id) if spike_event_id else None,
            bucket=ctx.spike_bucket or ctx.now,
            label=SPIKE_ANNOTATION_LABEL,
            description=(
                "Controlled demo scenario: a synthetic traffic spike was injected "
                "into this series to drive the seeded anomaly and the local alert "
                "delivery. No real data or external notification is involved."
            ),
            created_by_user_id=ctx.created_by,
        )
    )
    session.add(
        ChartAnnotation(
            project_id=ctx.project_id,
            scope_type=None,
            scope_ref=None,
            bucket=(ctx.now - DEMO_RELEASE_AGE).replace(minute=0, second=0, microsecond=0),
            label=release_annotation_label(DEMO_RELEASE_VERSION),
            description=(
                f"App version {DEMO_RELEASE_VERSION} reached its active share of traffic "
                "(app_version)."
            ),
            color=RELEASE_ANNOTATION_COLOR,
            source=ChartAnnotationSource.release.value,
            created_by_user_id=None,
        )
    )

    await session.flush()


def _delivery_item(
    firing: SimulatedRuleFiring,
    delivery_id: uuid.UUID,
    correlation_group_id: uuid.UUID,
) -> AlertDeliveryItem:
    """One ``AlertDeliveryItem`` for ``firing``, attached to ``delivery_id``.

    One constructor for both the sent delivery and the seeded failed attempt, so
    the two carry identical signals rather than two hand-maintained lists. That
    is the point rather than tidiness: a field set on one copy and forgotten on
    the other would make a retried delivery re-render the incident differently
    from the way it was first delivered — the same class of divergence the
    failed row's missing items already produced.
    """
    return AlertDeliveryItem(
        delivery_id=delivery_id,
        scope_type=firing.scope_type.value,
        scope_ref=firing.scope_ref,
        # Already inside ``SCOPE_NAME_MAX_LEN``: ``_build_firings`` builds every
        # firing through ``SimulatedRuleFiring.from_candidate``, which runs the
        # label through ``trim_scope_name``, so the message rendered from these
        # firings and the column written from them carry the same string.
        # Trimmed there rather than here deliberately —
        # re-trimming an already-trimmed label is a no-op, but trimming ONLY
        # here would let the two diverge.
        scope_name=firing.scope_name,
        event_type_id=firing.event_type_id,
        event_id=firing.event_id,
        bucket=firing.bucket,
        direction=firing.direction.value,
        actual_count=firing.actual_count,
        expected_count=firing.expected_count,
        absolute_delta=firing.absolute_delta,
        percent_delta=firing.percent_delta,
        drift_field=firing.drift_field,
        drift_type=firing.drift_type,
        sample_value=firing.sample_value,
        # Every item the builder writes belongs to one demo incident, so the
        # inbox group is explorable out of the box.
        correlation_group_id=correlation_group_id,
    )


async def _seed_catalog_metric_anomaly(
    session: AsyncSession, ctx: DemoContext
) -> MetricAnomaly | None:
    """Seed one ``metric``-scope anomaly over the demo's first catalog metric.

    Catalog metrics ship per-bucket values but no anomaly, so without this the
    firing rule's ``include_metrics`` opt-in would have nothing to match. The
    row is project-global (NULL ``scan_config_id``), matching the live metric
    detector.

    The anomaly describes a REAL feature of the stored series (bd tripl-jfm3.63):
    it lands ON an existing stored bucket — so it is always on the metric's
    interval grid instead of adding a half-day-offset point to a daily chart —
    reports that bucket's stored value as ``actual``, and scores it against the
    series' median rather than against the newest bucket (which is the current
    PARTIAL period, and therefore itself the lowest point on the chart).
    """
    metrics = (
        (
            await session.execute(
                select(MetricDefinition)
                .where(MetricDefinition.project_id == ctx.project_id)
                # ``order`` first: every demo metric is inserted with the same
                # ``created_at``, so ordering by it alone let Postgres break the
                # tie arbitrarily and the seeded anomaly landed on a different
                # metric per install (bd tripl-jfm3.63).
                .order_by(MetricDefinition.order, MetricDefinition.created_at, MetricDefinition.id)
            )
        )
        .scalars()
        .all()
    )
    # The FIRST metric (in display order) whose stored series is long enough to
    # score. Ordering alone is not sufficient: a metric whose values could not be
    # derived would otherwise silently leave the firing rule's ``include_metrics``
    # opt-in with nothing to match.
    scored: tuple[datetime, float, float] | None = None
    metric: MetricDefinition | None = None
    for candidate in metrics:
        stored = (
            (
                await session.execute(
                    select(MetricValue.bucket, MetricValue.value)
                    .where(MetricValue.metric_definition_id == candidate.id)
                    .order_by(MetricValue.bucket)
                )
            )
            .tuples()
            .all()
        )
        scored = _pick_metric_anomaly_bucket(stored)
        if scored is not None:
            metric = candidate
            break
    if metric is None or scored is None:
        return None
    bucket, actual, expected = scored

    anomaly = MetricAnomaly(
        scan_config_id=None,
        scope_type=MetricScopeType.metric.value,
        scope_ref=str(metric.id),
        event_id=None,
        event_type_id=None,
        bucket=bucket,
        actual_count=actual,
        expected_count=expected,
        # Consistent with z: actual = expected + z * stddev. Floored so a
        # near-flat series can never store a zero/degenerate stddev.
        stddev=max((actual - expected) / _METRIC_ANOMALY_Z_SCORE, _METRIC_ANOMALY_MIN_STDDEV),
        z_score=_METRIC_ANOMALY_Z_SCORE,
        direction=AnomalyDirection.spike.value,
    )
    session.add(anomaly)
    await session.flush()
    return anomaly


def _pick_metric_anomaly_bucket(
    stored: Sequence[tuple[datetime, float]],
) -> tuple[datetime, float, float] | None:
    """``(bucket, actual, expected)`` for the seeded metric spike, or ``None``.

    ``actual`` is the largest stored value in the recent candidate window and
    ``expected`` the median of the whole series, so the seeded alert quotes a
    value that really is high against the surrounding baseline. The newest bucket
    is excluded: it is the current, still-filling period.
    """
    candidates = list(stored[:-1])
    if len(candidates) < _METRIC_ANOMALY_MIN_POINTS:
        return None
    expected = float(median(value for _bucket, value in candidates))
    bucket, actual = max(
        candidates[-_METRIC_ANOMALY_RECENT_BUCKETS:], key=lambda point: (point[1], point[0])
    )
    if float(actual) <= expected:
        return None
    return bucket, float(actual), expected


async def _select_firing_anomalies(
    session: AsyncSession,
    ctx: DemoContext,
    metric_anomaly: MetricAnomaly | None,
) -> list[MetricAnomaly]:
    rows = (
        (
            await session.execute(
                select(MetricAnomaly)
                .where(MetricAnomaly.scan_config_id == ctx.scan_config_id)
                .order_by(MetricAnomaly.bucket.desc())
            )
        )
        .scalars()
        .all()
    )
    latest_by_scope: dict[tuple[str, str], MetricAnomaly] = {}
    for anomaly in rows:
        latest_by_scope.setdefault((anomaly.scope_type, anomaly.scope_ref), anomaly)
    selected = sorted(
        latest_by_scope.values(),
        key=lambda anomaly: (anomaly.scope_type, anomaly.scope_ref),
    )
    if metric_anomaly is not None:
        selected.append(metric_anomaly)
    return selected


async def _build_firings(
    session: AsyncSession,
    project: Project,
    anomalies: list[MetricAnomaly],
) -> list[SimulatedRuleFiring]:
    event_ids = {anomaly.event_id for anomaly in anomalies if anomaly.event_id is not None}
    event_type_ids = {
        anomaly.event_type_id for anomaly in anomalies if anomaly.event_type_id is not None
    }
    metric_ids: set[uuid.UUID] = set()
    for anomaly in anomalies:
        if anomaly.scope_type == MetricScopeType.metric.value:
            try:
                metric_ids.add(uuid.UUID(anomaly.scope_ref))
            except ValueError:
                continue

    event_names: dict[uuid.UUID, str] = {}
    event_type_names: dict[uuid.UUID, str] = {}
    metric_names: dict[str, str] = {}
    if event_ids:
        for event_id, name in (
            await session.execute(select(Event.id, Event.name).where(Event.id.in_(event_ids)))
        ).all():
            event_names[event_id] = name
    if event_type_ids:
        for et_id, display_name in (
            await session.execute(
                select(EventType.id, EventType.display_name).where(EventType.id.in_(event_type_ids))
            )
        ).all():
            event_type_names[et_id] = display_name
    if metric_ids:
        for metric_id, display_name in (
            await session.execute(
                select(MetricDefinition.id, MetricDefinition.display_name).where(
                    MetricDefinition.id.in_(metric_ids)
                )
            )
        ).all():
            metric_names[str(metric_id)] = display_name

    firings: list[SimulatedRuleFiring] = []
    for anomaly in anomalies:
        # Built by ``SimulatedRuleFiring.from_candidate``, the constructor
        # ``alerting_service.simulate_rule`` uses too, so the field list exists
        # once: a field added to the DTO reaches the demo and the live replay
        # together (tripl-0zpq.324). It also trims the scope name and computes
        # the delta through the shared ``percent_delta_of``.
        #
        # The trim matters here: this list feeds both ``render_firings_message``
        # (``payload_snapshot["rendered_message"]``) and, through
        # ``_delivery_item``, the ``scope_name`` column. ``_resolve_scope_name``
        # reads ``Event.name`` (String(500)) and ``EventType.display_name`` back
        # out of the DB, and a demo recipe that one day seeds a realistically
        # long event name would otherwise reproduce tripl-0zpq.253 inside
        # ``create_demo_project``, where the Postgres "value too long" would
        # roll the whole seed back.
        #
        # SQLite drops tz on round-trip while freshly-built rows stay tz-aware;
        # normalise so every firing bucket is comparable (max/last_seen_at).
        bucket = anomaly.bucket if anomaly.bucket.tzinfo else anomaly.bucket.replace(tzinfo=UTC)
        firings.append(
            SimulatedRuleFiring.from_candidate(
                anomaly,
                scope_name=_resolve_scope_name(
                    anomaly,
                    project=project,
                    event_names=event_names,
                    event_type_names=event_type_names,
                    metric_names=metric_names,
                ),
                bucket=bucket,
            )
        )
    return firings


def _resolve_scope_name(
    anomaly: MetricAnomaly,
    *,
    project: Project,
    event_names: dict[uuid.UUID, str],
    event_type_names: dict[uuid.UUID, str],
    metric_names: dict[str, str],
) -> str:
    if anomaly.scope_type == MetricScopeType.project_total.value:
        return project.name
    if anomaly.scope_type == MetricScopeType.event_type.value and anomaly.event_type_id is not None:
        return event_type_names.get(anomaly.event_type_id, "Event type")
    if anomaly.scope_type == MetricScopeType.event.value and anomaly.event_id is not None:
        return event_names.get(anomaly.event_id, "Event")
    if anomaly.scope_type == MetricScopeType.metric.value:
        return metric_names.get(anomaly.scope_ref, "Metric")
    return anomaly.scope_ref
