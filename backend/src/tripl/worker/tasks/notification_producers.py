"""Worker-side notification producers (#259): signals, lifecycle findings, property drift.

ONE public entry, :func:`produce_notifications` ``(session, source, subject)``,
called best-effort AFTER the caller's own commit:

* ``"signals"`` with a ``ScanConfig`` — from ``metrics.tasks.collect_metrics``
  once a scheduled collection has written its anomalies;
* ``"lifecycle"`` with ``[(project_id, event_id, kind), ...]`` — from
  ``lifecycle.check_lifecycle_findings`` for the findings it opened or reopened;
* ``"property_drifts"`` with a ``ScanConfig`` — from the same place as
  ``"signals"``: the active per-event property drifts (F23, #306) that config
  detected, one notification per drift per watcher of the event. The drift id
  rides the URL (``?property_drift=<id>``) and is the dedup key, looked up over
  the drift retention window, so a drift that stays open is announced once.

Each source only turns its input into :class:`_Draft` rows (who is watching
what, and the text); every draft goes through the same
``notification_service.notify_sync`` with a ``kind`` — no per-event helper.
``notify_sync`` does the shared filtering: never the actor, current project
members only, muted subscriptions dropped, the signal throttle.

Signals: the open signals of the run (the same "latest active" sets alert
dispatch reads — event, event type and catalog metric scopes; the project total
has nobody to watch it). A signal the team triaged — muted scope, acknowledged,
or any verdict — never notifies. One notification per anomaly per subscriber:
the anomaly's bucket rides in the notification ``url`` (``?signal=<bucket>``),
so a signal re-scored by the next run is recognised and not sent again. On top,
at most one signal notification per entity per subscriber per 6h
(``notification_service.SIGNAL_THROTTLE``). The per-anomaly dedup looks back
seven days (``ALREADY_TOLD_WINDOW``).

Nothing here may fail the work that called it: every failure is logged and
rolled back, and the caller's committed results stand.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from tripl.alerting_property_drift import active_property_drift_filters, property_drift_sample
from tripl.core.bucketing import to_utc
from tripl.models.domain_enums import MetricScopeType, SignalTriageAction
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.lifecycle_finding import LifecycleFindingKind
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.notification import Notification, NotificationKind
from tripl.models.property_drift import PropertyDrift, PropertyDriftKind
from tripl.models.scan_config import ScanConfig
from tripl.models.signal_triage import SignalTriage
from tripl.models.subscription import SubscriptionEntityType
from tripl.models.variable import Variable
from tripl.services import notification_service
from tripl.services.project_links import project_link_slugs_sync, project_url

logger = logging.getLogger(__name__)

_EVENT = SubscriptionEntityType.event.value
_EVENT_TYPE = SubscriptionEntityType.event_type.value
_METRIC = SubscriptionEntityType.metric.value

# The query parameter carrying a signal's bucket on its notification URL: the
# per-anomaly dedup key (see the module docstring).
SIGNAL_URL_PARAM = "signal"

# How far back "was this exact signal already announced" looks.
ALREADY_TOLD_WINDOW = timedelta(days=7)

# The query parameter carrying a property drift's id on its notification URL.
PROPERTY_DRIFT_URL_PARAM = "property_drift"
# A property drift stays open until someone triages it, so its dedup looks back
# over the whole drift retention (``variable_value_drift_service``'s 30 days)
# rather than a week: an untriaged drift is not re-announced every run.
PROPERTY_DRIFT_TOLD_WINDOW = timedelta(days=30)


@dataclass(frozen=True)
class _Draft:
    """One ``notify_sync`` call, minus the session."""

    kind: str
    project_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    title: str
    url: str
    body: str = ""
    watchers_of: tuple[tuple[str, uuid.UUID], ...] = ()
    exclude_user_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    throttle: timedelta | None = None


# --- the one entry point -----------------------------------------------------------


def produce_notifications(session: Session, source: str, subject: Any) -> int:
    """Write the notifications ``source`` implies for ``subject``; the number written.

    Best-effort by contract: never raises, rolls back its own writes on any
    failure, and commits only what it wrote.
    """
    collector = _SOURCES.get(source)
    if collector is None:
        logger.warning("Unknown notification source %r", source)
        return 0
    try:
        written = 0
        for draft in collector(session, subject):
            written += len(
                notification_service.notify_sync(
                    session,
                    project_id=draft.project_id,
                    kind=draft.kind,
                    entity_type=draft.entity_type,
                    entity_id=draft.entity_id,
                    title=draft.title,
                    url=draft.url,
                    body=draft.body,
                    watchers_of=draft.watchers_of,
                    exclude_user_ids=draft.exclude_user_ids,
                    throttle=draft.throttle,
                )
            )
        session.commit()
        return written
    except Exception:  # noqa: BLE001 — a notification must never fail the producer's work
        logger.exception("Notification producer %r failed", source)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            logger.exception("Rollback after notification producer %r failed", source)
        return 0


# --- shared lookups ------------------------------------------------------------------


def _slugs(session: Session, project_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, tuple[str, str]]:
    """``project_id → (org_slug, project_slug)``: what a notification link names."""
    return project_link_slugs_sync(session, project_ids)


def _events(session: Session, event_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Event]:
    ids = set(event_ids)
    if not ids:
        return {}
    return {event.id: event for event in session.scalars(select(Event).where(Event.id.in_(ids)))}


def _as_uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError, TypeError, AttributeError:
        return None


def _format_count(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


# --- source: signals -----------------------------------------------------------------


def _triaged(
    session: Session, project_id: uuid.UUID, anomalies: list[MetricAnomaly], now: datetime
) -> set[uuid.UUID]:
    """Ids among ``anomalies`` already triaged: scope muted, acknowledged, or a verdict."""
    if not anomalies:
        return set()
    refs = {anomaly.scope_ref for anomaly in anomalies}
    rows = session.execute(
        select(
            SignalTriage.scan_config_id,
            SignalTriage.scope_type,
            SignalTriage.scope_ref,
            SignalTriage.action,
            SignalTriage.bucket,
        ).where(
            SignalTriage.project_id == project_id,
            SignalTriage.scope_ref.in_(refs),
            or_(
                SignalTriage.action != SignalTriageAction.muted.value,
                SignalTriage.muted_until.is_(None),
                SignalTriage.muted_until > now,
            ),
        )
    ).all()
    muted_scopes: set[tuple[uuid.UUID | None, str, str]] = set()
    signal_rows: set[tuple[uuid.UUID | None, str, str, datetime]] = set()
    for scan_config_id, scope_type, scope_ref, action, bucket in rows:
        key = _scope_key(scan_config_id, str(scope_type), str(scope_ref))
        if str(action) == SignalTriageAction.muted.value:
            muted_scopes.add(key)
        elif bucket is not None:
            signal_rows.add((*key, to_utc(bucket)))
    hit: set[uuid.UUID] = set()
    for anomaly in anomalies:
        key = _scope_key(anomaly.scan_config_id, str(anomaly.scope_type), anomaly.scope_ref)
        if key in muted_scopes or (*key, to_utc(anomaly.bucket)) in signal_rows:
            hit.add(anomaly.id)
    return hit


def _scope_key(
    scan_config_id: uuid.UUID | None, scope_type: str, scope_ref: str
) -> tuple[uuid.UUID | None, str, str]:
    # A catalog metric is project-global whatever config wrote it.
    if scope_type == MetricScopeType.metric.value:
        return (None, scope_type, scope_ref)
    return (scan_config_id, scope_type, scope_ref)


def _url_forms(url: str) -> tuple[str, ...]:
    """``url`` and its pre-F20-PR8 org-less form (``/o/{org}/p/...`` → ``/p/...``).

    Rows written before org-qualified links still carry ``/p/{slug}/...``
    (they are not migrated: the frontend redirects them). Matching both keeps
    the first run after the upgrade from re-announcing every open signal.
    """
    if url.startswith("/o/"):
        _empty, _o, _org, rest = url.split("/", 3)
        return (url, f"/{rest}")
    return (url,)


def _already_told(
    session: Session,
    entity_type: str,
    entity_id: uuid.UUID,
    url: str,
    now: datetime,
    *,
    kind: str = NotificationKind.signal.value,
    window: timedelta = ALREADY_TOLD_WINDOW,
) -> frozenset[uuid.UUID]:
    """Users who already have a notification about exactly this signal.

    Bounded to the last :data:`ALREADY_TOLD_WINDOW`: an open signal is
    re-scored every run for as long as it stays open, and the read has to stay
    an index range (``ix_notification_entity_kind``) rather than grow with the
    entity's whole notification history. A signal still open after a week is
    worth one more reminder.
    """
    rows = session.scalars(
        select(Notification.user_id).where(
            Notification.entity_type == entity_type,
            Notification.entity_id == entity_id,
            Notification.kind == kind,
            Notification.created_at >= now - window,
            Notification.url.in_(_url_forms(url)),
        )
    )
    return frozenset(rows.all())


def _signal_drafts(session: Session, config: ScanConfig) -> list[_Draft]:
    from tripl.worker.tasks.metrics.signals import (
        _get_active_metric_anomaly_candidates,
        _get_latest_active_anomalies,
    )

    project_id = config.project_id
    now = datetime.now(UTC)
    open_signals = [
        *_get_latest_active_anomalies(session, config).values(),
        *_get_active_metric_anomaly_candidates(session, config).values(),
    ]
    triaged = _triaged(session, project_id, open_signals, now)
    signals = [anomaly for anomaly in open_signals if anomaly.id not in triaged]
    if not signals:
        return []
    slugs = _slugs(session, [project_id]).get(project_id)
    if slugs is None:
        return []
    org_slug, slug = slugs

    event_ids = {
        anomaly.event_id or _as_uuid(anomaly.scope_ref)
        for anomaly in signals
        if str(anomaly.scope_type) == MetricScopeType.event.value
    }
    events = _events(session, {event_id for event_id in event_ids if event_id is not None})
    type_ids = {
        anomaly.event_type_id or _as_uuid(anomaly.scope_ref)
        for anomaly in signals
        if str(anomaly.scope_type) == MetricScopeType.event_type.value
    }
    type_names = {
        type_id: display_name or name
        for type_id, name, display_name in session.execute(
            select(EventType.id, EventType.name, EventType.display_name).where(
                EventType.id.in_({type_id for type_id in type_ids if type_id is not None})
            )
        ).all()
    }
    metric_refs = {
        _as_uuid(anomaly.scope_ref)
        for anomaly in signals
        if str(anomaly.scope_type) == MetricScopeType.metric.value
    }
    metric_ids = {metric_id for metric_id in metric_refs if metric_id is not None}
    metric_rows = (
        session.execute(
            select(MetricDefinition.id, MetricDefinition.name, MetricDefinition.display_name).where(
                MetricDefinition.project_id == project_id,
                MetricDefinition.id.in_(metric_ids),
            )
        ).all()
        if metric_ids
        else []
    )
    metric_names = {
        metric_id: display_name or name for metric_id, name, display_name in metric_rows
    }

    drafts: list[_Draft] = []
    for anomaly in signals:
        scope = str(anomaly.scope_type)
        watchers: tuple[tuple[str, uuid.UUID], ...]
        if scope == MetricScopeType.event.value:
            event_key = anomaly.event_id or _as_uuid(anomaly.scope_ref)
            event = events.get(event_key) if event_key is not None else None
            if event is None:
                continue
            entity_type, entity_id, label = _EVENT, event.id, event.name
            path_scope = "event"
            # Watching an event type covers its events.
            watchers = ((_EVENT, event.id), (_EVENT_TYPE, event.event_type_id))
        elif scope == MetricScopeType.event_type.value:
            type_id = anomaly.event_type_id or _as_uuid(anomaly.scope_ref)
            if type_id is None or type_id not in type_names:
                continue
            entity_type, entity_id, label = _EVENT_TYPE, type_id, type_names[type_id]
            path_scope = "event-type"
            watchers = ((_EVENT_TYPE, type_id),)
        elif scope == MetricScopeType.metric.value:
            metric_id = _as_uuid(anomaly.scope_ref)
            if metric_id is None or metric_id not in metric_names:
                continue
            entity_type, entity_id, label = _METRIC, metric_id, metric_names[metric_id]
            path_scope = "metric"
            watchers = ((_METRIC, metric_id),)
        else:
            # project_total and anything else: nothing a person watches.
            continue

        bucket = to_utc(anomaly.bucket)
        url = project_url(
            org_slug,
            slug,
            f"/monitoring/{path_scope}/{entity_id}"
            f"?{SIGNAL_URL_PARAM}={bucket.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        )
        direction = str(anomaly.direction)
        drafts.append(
            _Draft(
                kind=NotificationKind.signal.value,
                project_id=project_id,
                entity_type=entity_type,
                entity_id=entity_id,
                title=f"Signal: {direction} on {label}",
                body=(
                    f"{_format_count(anomaly.actual_count)} vs "
                    f"{_format_count(anomaly.expected_count)} expected, "
                    f"bucket {bucket.strftime('%Y-%m-%d %H:%M')} UTC."
                ),
                url=url,
                watchers_of=watchers,
                exclude_user_ids=_already_told(session, entity_type, entity_id, url, now),
                throttle=notification_service.SIGNAL_THROTTLE,
            )
        )
    return drafts


# --- source: lifecycle findings --------------------------------------------------------

_LIFECYCLE_TITLES: dict[str, str] = {
    LifecycleFindingKind.sunset_overdue.value: "{name} is past its sunset and still receives data",
    LifecycleFindingKind.successor_silent.value: "The successor of {name} has gone silent",
}


def _lifecycle_drafts(
    session: Session, findings: Iterable[tuple[uuid.UUID, uuid.UUID, str]]
) -> list[_Draft]:
    items = list(findings)
    if not items:
        return []
    slugs = _slugs(session, {project_id for project_id, _event_id, _kind in items})
    events = _events(session, {event_id for _project_id, event_id, _kind in items})
    drafts: list[_Draft] = []
    for project_id, event_id, kind in items:
        event = events.get(event_id)
        link_slugs = slugs.get(project_id)
        if event is None or link_slugs is None:
            continue
        template = _LIFECYCLE_TITLES.get(kind, "Lifecycle finding on {name}")
        drafts.append(
            _Draft(
                kind=NotificationKind.lifecycle.value,
                project_id=project_id,
                entity_type=_EVENT,
                entity_id=event.id,
                title=f"Lifecycle: {template.format(name=event.name)}",
                body="A deprecated event needs attention: open it to see the finding.",
                url=project_url(*link_slugs, f"/events/detail/{event.id}"),
                watchers_of=((_EVENT, event.id), (_EVENT_TYPE, event.event_type_id)),
            )
        )
    return drafts


# --- source: property drift ------------------------------------------------------------

_PROPERTY_DRIFT_TITLES: dict[str, str] = {
    PropertyDriftKind.missing_required.value: "required property {prop} is missing on {name}",
    PropertyDriftKind.new_property.value: "new property {prop} on {name}",
}


def _property_drift_drafts(session: Session, config: ScanConfig) -> list[_Draft]:
    """One draft per active per-event property drift this config detected.

    A type change (``event_id`` NULL) is about a property, which nobody
    watches, so it notifies no one; it still alerts through
    ``include_property_drifts`` rules.
    """
    now = datetime.now(UTC)
    rows = session.execute(
        select(PropertyDrift, Variable.name)
        .join(Variable, Variable.id == PropertyDrift.variable_id)
        .where(
            PropertyDrift.project_id == config.project_id,
            PropertyDrift.scan_config_id == config.id,
            PropertyDrift.event_id.is_not(None),
            PropertyDrift.detected_at >= now - PROPERTY_DRIFT_TOLD_WINDOW,
            *active_property_drift_filters(now),
        )
        .order_by(PropertyDrift.detected_at, PropertyDrift.id)
    ).all()
    if not rows:
        return []
    link_slugs = _slugs(session, [config.project_id]).get(config.project_id)
    if link_slugs is None:
        return []
    events = _events(session, {drift.event_id for drift, _name in rows if drift.event_id})
    drafts: list[_Draft] = []
    for drift, variable_name in rows:
        event = events.get(drift.event_id) if drift.event_id is not None else None
        if event is None:
            continue
        template = _PROPERTY_DRIFT_TITLES.get(str(drift.kind), "property drift on {name}")
        url = project_url(
            *link_slugs,
            f"/monitoring/event/{event.id}?{PROPERTY_DRIFT_URL_PARAM}={drift.id}",
        )
        sample = property_drift_sample(str(drift.kind), drift.detail or {})
        drafts.append(
            _Draft(
                kind=NotificationKind.property_drift.value,
                project_id=config.project_id,
                entity_type=_EVENT,
                entity_id=event.id,
                title="Property drift: " + template.format(prop=variable_name, name=event.name),
                body=f"{variable_name}: {sample}.",
                url=url,
                watchers_of=((_EVENT, event.id), (_EVENT_TYPE, event.event_type_id)),
                exclude_user_ids=_already_told(
                    session,
                    _EVENT,
                    event.id,
                    url,
                    now,
                    kind=NotificationKind.property_drift.value,
                    window=PROPERTY_DRIFT_TOLD_WINDOW,
                ),
            )
        )
    return drafts


_SOURCES: dict[str, Callable[[Session, Any], list[_Draft]]] = {
    "signals": _signal_drafts,
    "lifecycle": _lifecycle_drafts,
    "property_drifts": _property_drift_drafts,
}


__all__ = [
    "ALREADY_TOLD_WINDOW",
    "PROPERTY_DRIFT_URL_PARAM",
    "SIGNAL_URL_PARAM",
    "produce_notifications",
]
