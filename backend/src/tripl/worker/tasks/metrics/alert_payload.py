"""Alert payload helpers: scope-name lookup, destination loader, snapshot.

The live alert dispatch loop lives in `dispatch.py`; these small builders stay
separate because they are also useful to inspect and test independently.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.alert_templates import percent_delta_of, percent_delta_or_none
from tripl.alerting_matching import (
    SCOPE_DISTRIBUTION_DRIFT,
    SCOPE_LIFECYCLE,
    SCOPE_METRIC,
    SCOPE_RELEASE_REGRESSION,
    SCOPE_SOURCE_FRESHNESS,
    SCOPE_VARIABLE_VALUE_DRIFT,
    AlertMatchCandidate,
)
from tripl.core.analyzers.anomaly_detector import (
    SCOPE_EVENT,
    SCOPE_EVENT_TYPE,
    SCOPE_PROJECT_TOTAL,
)
from tripl.models.alert_delivery_item import trim_scope_name
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.metric_definition import MetricDefinition
from tripl.models.scan_config import ScanConfig
from tripl.services.attribution_text import (
    format_attribution_line,
    load_attributions_for_scopes,
)

from ._helpers import SCOPE_SCHEMA_DRIFT
from .urls import _build_item_paths

logger = logging.getLogger(__name__)

# The scopes the metrics worker stores an attribution for (GH #255).
_ATTRIBUTION_SCOPES = frozenset({SCOPE_PROJECT_TOTAL, SCOPE_EVENT_TYPE, SCOPE_EVENT})


def _build_alert_scope_names(
    session: Session,
    anomalies: list[AlertMatchCandidate],
) -> dict[tuple[str, str], str]:
    """``(scope_type, scope_ref)`` -> the label an alert shows for that scope.

    Every candidate gets an entry, and every value fits the ``scope_name``
    column — see ``trim_scope_name`` and the trim at the bottom of this
    function.
    """
    scope_names: dict[tuple[str, str], str] = {
        (SCOPE_PROJECT_TOTAL, anomaly.scope_ref): "All events"
        for anomaly in anomalies
        if anomaly.scope_type == SCOPE_PROJECT_TOTAL
    }

    event_type_ids = {
        anomaly.event_type_id for anomaly in anomalies if anomaly.event_type_id is not None
    }
    if event_type_ids:
        event_type_names: dict[uuid.UUID, str] = {}
        for event_type_id, display_name, name in session.execute(
            select(EventType.id, EventType.display_name, EventType.name).where(
                EventType.id.in_(event_type_ids)
            )
        ).all():
            event_type_name = display_name or name
            event_type_names[event_type_id] = event_type_name
            scope_names[(SCOPE_EVENT_TYPE, str(event_type_id))] = event_type_name
        for anomaly in anomalies:
            if anomaly.event_type_id is None:
                continue
            event_type_name = event_type_names.get(anomaly.event_type_id, "Event type")
            drift_field = getattr(anomaly, "drift_field", None) or anomaly.scope_ref
            if anomaly.scope_type == SCOPE_SCHEMA_DRIFT:
                scope_names[(SCOPE_SCHEMA_DRIFT, anomaly.scope_ref)] = (
                    f"{event_type_name}.{drift_field}"
                )
            elif anomaly.scope_type == SCOPE_DISTRIBUTION_DRIFT:
                scope_names[(SCOPE_DISTRIBUTION_DRIFT, anomaly.scope_ref)] = (
                    f"{event_type_name}.{drift_field}"
                )

    for anomaly in anomalies:
        if anomaly.scope_type != SCOPE_DISTRIBUTION_DRIFT or anomaly.event_type_id is not None:
            continue
        drift_field = getattr(anomaly, "drift_field", None) or anomaly.scope_ref
        scope_names[(SCOPE_DISTRIBUTION_DRIFT, anomaly.scope_ref)] = f"All events.{drift_field}"

    event_ids = {anomaly.event_id for anomaly in anomalies if anomaly.event_id is not None}
    if event_ids:
        event_names_by_id: dict[uuid.UUID, str] = {}
        for event_id, name in session.execute(
            select(Event.id, Event.name).where(Event.id.in_(event_ids))
        ).all():
            event_names_by_id[event_id] = name
            scope_names[(SCOPE_EVENT, str(event_id))] = name
        for anomaly in anomalies:
            if anomaly.scope_type != SCOPE_VARIABLE_VALUE_DRIFT or anomaly.event_id is None:
                continue
            event_name = event_names_by_id.get(anomaly.event_id, "Event")
            drift_field = getattr(anomaly, "drift_field", None) or anomaly.scope_ref
            scope_names[(SCOPE_VARIABLE_VALUE_DRIFT, anomaly.scope_ref)] = (
                f"{event_name}.{drift_field}"
            )

    # Catalog metric anomalies resolve to the metric's display name (scope_ref is
    # the metric definition id).
    metric_scope_refs = {
        anomaly.scope_ref for anomaly in anomalies if anomaly.scope_type == SCOPE_METRIC
    }
    if metric_scope_refs:
        metric_ids = {uuid.UUID(ref) for ref in metric_scope_refs}
        for metric_id, display_name in session.execute(
            select(MetricDefinition.id, MetricDefinition.display_name).where(
                MetricDefinition.id.in_(metric_ids)
            )
        ).all():
            scope_names[(SCOPE_METRIC, str(metric_id))] = display_name

    # Release regressions borrow the name of their underlying event / event type
    # (already resolved above) so the message shows "Login", not a raw UUID.
    for anomaly in anomalies:
        if anomaly.scope_type != SCOPE_RELEASE_REGRESSION:
            continue
        if anomaly.event_id is not None:
            underlying = scope_names.get((SCOPE_EVENT, str(anomaly.event_id)))
        elif anomaly.event_type_id is not None:
            underlying = scope_names.get((SCOPE_EVENT_TYPE, str(anomaly.event_type_id)))
        else:
            underlying = None
        if underlying is not None:
            scope_names[(anomaly.scope_type, anomaly.scope_ref)] = underlying

    # A lifecycle finding (GH #258) is named after the DEPRECATED event it
    # hangs on — resolved above through its ``event_id`` — so the message shows
    # "signup", not "sunset_overdue:<hex>". A silent successor is named by the
    # drift line (``drift_field`` / ``sample_value``), not by the scope.
    for anomaly in anomalies:
        if anomaly.scope_type != SCOPE_LIFECYCLE or anomaly.event_id is None:
            continue
        lifecycle_name = scope_names.get((SCOPE_EVENT, str(anomaly.event_id)))
        if lifecycle_name is not None:
            scope_names[(anomaly.scope_type, anomaly.scope_ref)] = lifecycle_name

    # A source-freshness scope is a whole scan config (scope_ref is its id);
    # the candidate already carries the scan's name on ``drift_field``.
    for anomaly in anomalies:
        if anomaly.scope_type != SCOPE_SOURCE_FRESHNESS:
            continue
        scan_name = getattr(anomaly, "drift_field", None)
        if scan_name:
            scope_names[(anomaly.scope_type, anomaly.scope_ref)] = scan_name

    for anomaly in anomalies:
        key = (anomaly.scope_type, anomaly.scope_ref)
        scope_names.setdefault(key, anomaly.scope_ref)
    # Trimmed here, on the way out, rather than at each of the eight
    # assignments above: this is the one loop every key already passes through,
    # so a scope family added above it cannot reach a writer untrimmed. Both
    # persisted writers of the label are downstream of this return — the typed
    # ``AlertDeliveryItem`` rows and the ``AlertPendingItem`` digest buffer,
    # whose ON CONFLICT arm rewrites ``scope_name`` on every collection — as is
    # the frozen ``payload_snapshot`` that quotes the same dict. That is why
    # ``dispatch.py`` needs no guard of its own (tripl-0zpq.253).
    #
    # The ``setdefault`` fallback needs no trim of its own but gets one anyway,
    # for free: ``scope_ref`` is String(64) on the same rows.
    return {key: trim_scope_name(name) for key, name in scope_names.items()}


def _build_event_type_by_event_id(
    session: Session,
    anomalies: list[AlertMatchCandidate],
) -> dict[uuid.UUID, uuid.UUID]:
    """Event -> event type, for the candidates that carry an event but no type.

    Event-scope anomalies, variable-value drifts and event-scope release
    regressions are all anchored to a real ``event_id`` and deliberately store
    ``event_type_id = NULL`` (stamping it would leak them into the event-TYPE
    series that ``metrics_service`` / ``project_service`` / ``activity_service``
    select by that column alone). ``alerting_matching.filter_matches_anomaly``
    resolves the type through this map instead, so an ``event_type`` filter
    narrows them the same way ``rule_covers_event`` already does for the
    catalog's Monitor column (tripl-0zpq.7).

    One query per dispatch run, covering all three candidate families at once,
    and none at all when no such candidate is present.
    """
    event_ids = {
        anomaly.event_id
        for anomaly in anomalies
        if anomaly.event_id is not None and anomaly.event_type_id is None
    }
    if not event_ids:
        return {}
    return {
        event_id: event_type_id
        for event_id, event_type_id in session.execute(
            select(Event.id, Event.event_type_id).where(Event.id.in_(event_ids))
        ).all()
    }


def _build_attribution_lines(
    session: Session,
    config: ScanConfig,
    anomalies: list[AlertMatchCandidate],
) -> dict[tuple[str, str], str]:
    """``(scope_type, scope_ref)`` -> the stored attribution one-liner.

    Frozen into ``payload_snapshot`` so the audit record says what the alert
    said. Keyed by scope rather than by candidate id because the digest flush
    rebuilds its candidates from the pending buffer; the metric_anomalies
    unique key ``(scan_config_id, scope_type, scope_ref, bucket)`` is what every
    path still carries. Best-effort: a failed read freezes no line, and never
    costs the delivery.
    """
    candidates = [
        anomaly
        for anomaly in anomalies
        if anomaly.scope_type in _ATTRIBUTION_SCOPES and anomaly.scan_config_id == config.id
    ]
    if not candidates:
        return {}
    keys = {(a.scope_type, a.scope_ref, a.bucket): a for a in candidates}
    try:
        with session.no_autoflush:
            rows = load_attributions_for_scopes(session, scan_config_id=config.id, keys=keys)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to load anomaly attributions for the snapshot", exc_info=True)
        return {}
    lines: dict[tuple[str, str], str] = {}
    for key, row in rows.items():
        anomaly = keys[key]
        line = format_attribution_line(row, anomaly.direction, bucket=anomaly.bucket)
        if line:
            lines[(anomaly.scope_type, anomaly.scope_ref)] = line
    return lines


def _load_enabled_alert_destinations(
    session: Session,
    project_id: uuid.UUID,
) -> list[AlertDestination]:
    return list(
        session.execute(
            select(AlertDestination)
            .where(
                AlertDestination.project_id == project_id,
                AlertDestination.enabled.is_(True),
            )
            .order_by(AlertDestination.created_at.desc())
        )
        .scalars()
        .unique()
        .all()
    )


def _build_delivery_snapshot(
    config: ScanConfig,
    *,
    project_slug: str,
    app_base_url: str,
    org_slug: str,
    rule: AlertRule,
    destination: AlertDestination,
    anomalies: list[AlertMatchCandidate],
    scope_names: dict[tuple[str, str], str],
    delivery_id: uuid.UUID | None = None,
    attribution_lines: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, object]:
    """Freeze what this delivery said, for the audit log and the Inbox.

    ``attribution_lines`` (from ``_build_attribution_lines``) adds each volume
    item's stored "why" one-liner as ``attribution_line``; ``None`` for items
    without one, and for every item when the caller passes nothing.

    ``delivery_id`` is threaded in because release-regression items link back
    to this delivery's own audit row — the only surface that can show their
    numbers for one scope. Callers flush the delivery first so the id exists;
    ``None`` degrades to a link-less item rather than a wrong one.

    ``app_base_url`` arrives the same way, and is required rather than
    defaulted so that this snapshot and the typed ``AlertDeliveryItem`` rows
    minted beside it come from ONE read of the setting rather than two.
    ``app_settings_service.get_runtime_config_sync`` has no cache and swallows
    a failed read, falling back to the env config whose ``app_base_url`` is
    ``""`` — and every builder in ``urls.py`` returns ``None`` on an empty
    base. Two independent reads could therefore hand this frozen blob a
    different base from the rows it is frozen beside, or no links at all beside
    rows that have them, with nothing but a ``logger.warning`` to say why.
    ``dispatch._create_deliveries`` resolves the value once, before the chunk
    loop, and hands that one string here and to the per-item
    ``_build_item_paths`` call that mints the rows. ``org_slug`` is resolved
    once beside it, for the same reason.

    That buys agreement about the BASE, and deliberately nothing more. This is
    the FIRST of the two ``_build_item_paths`` calls per item — the snapshot is
    frozen right after the delivery flushes, before the loop that mints the
    rows — and it passes no ``correlation_group_id``, which the row call does
    pass (both mint paths fill that map for every anomaly). So for an ordinary
    scope the two encodings carry different link SHAPES on purpose: the row
    gets the incident-anchored audit URL and a ``None`` monitoring path, while
    the frozen item keeps the pre-incident pair — event details plus a
    monitoring page. Nothing renders a link out of this blob; the message, the
    webhook body and the Inbox all read the typed rows (``alerts_messages``,
    ``AlertDeliveryRow.tsx``), so re-shaping it would rewrite audit history for
    no reader.
    """
    items: list[dict[str, object]] = []
    for anomaly in anomalies:
        details_path, monitoring_path = _build_item_paths(
            project_slug,
            org_slug=org_slug,
            app_base_url=app_base_url,
            scope_type=anomaly.scope_type,
            scope_ref=anomaly.scope_ref,
            event_id=anomaly.event_id,
            delivery_id=delivery_id,
        )
        expected = anomaly.expected_count
        absolute_delta = abs(anomaly.actual_count - expected)
        # NOT rounded. Gate and output must read the same number, and the number
        # they must both read is the true one.
        #
        # This blob used to round while gating the percent on the unrounded
        # value, so 0 < expected < 0.5 emitted ``expected_count: 0`` beside a
        # non-null percent — the pair alerting.md calls impossible. Rounding the
        # GATE instead fixed that pair and broke a worse one: a real fractional
        # baseline (0.2 is ordinary for a ratio-shaped catalog metric — see
        # ``test_a_fractional_baseline_below_one_keeps_its_full_percent``, where
        # it is a 350% move) became ``expected_count: 0, percent_delta: null``
        # here while the typed ``items[]`` and the webhook kept 0.2 and the real
        # percentage. One delivery, three machine-readable encodings, two
        # answers. Dropping the rounding is what makes all three agree.
        #
        # Nothing on screen regresses: the audit row renders the TYPED items, and
        # the only thing the UI reads from this blob is ``items.length``
        # (AlertDeliveryRow.tsx). The rounding served no reader.
        items.append(
            {
                "scope_type": anomaly.scope_type,
                "scope_ref": anomaly.scope_ref,
                "scope_name": scope_names[(anomaly.scope_type, anomaly.scope_ref)],
                "direction": anomaly.direction,
                "actual_count": anomaly.actual_count,
                "expected_count": expected,
                "absolute_delta": absolute_delta,
                # ``null`` rather than the stored 0.0 placeholder when there was
                # no baseline: this blob is read as JSON (the Inbox, the audit
                # API, anything reading ``AlertDelivery.payload_snapshot``), and
                # a number there is indistinguishable from "no change"
                # (tripl-l429.27). Rows written before that change still carry
                # 0.0 — a frozen record is not rewritten — so a consumer reading
                # historical deliveries disambiguates on ``expected_count == 0``.
                #
                # The RATIO comes from ``alert_templates.percent_delta_of`` —
                # the same definition ``dispatch._create_deliveries`` stores and
                # the simulator replays — rather than a local copy. A local copy
                # is what let this line keep an ``expected > 0`` divisor after
                # the outer encoding had already moved to ``!= 0``
                # (tripl-0zpq.102): a signed catalog metric at a baseline of
                # -3 moving to -9 got ``percent_delta: 0.0`` frozen into the
                # snapshot while the typed ``items[]`` beside it rendered the
                # true 200.0%, so one delivery disagreed with itself.
                "percent_delta": percent_delta_or_none(
                    percent_delta_of(anomaly.actual_count, expected),
                    expected,
                ),
                "details_path": details_path,
                "monitoring_path": monitoring_path,
                "drift_field": getattr(anomaly, "drift_field", None),
                "drift_type": getattr(anomaly, "drift_type", None),
                "sample_value": getattr(anomaly, "sample_value", None),
                "attribution_line": (attribution_lines or {}).get(
                    (anomaly.scope_type, anomaly.scope_ref)
                ),
            }
        )
    return {
        "project_slug": project_slug,
        # The organization the links above name (F20 PR8). Beside the project
        # slug, which on its own names a project only inside one organization.
        "org_slug": org_slug,
        "scan_name": config.name,
        "destination_name": destination.name,
        "rule_name": rule.name,
        "channel": destination.type,
        "matched_count": len(anomalies),
        "items": items,
    }
