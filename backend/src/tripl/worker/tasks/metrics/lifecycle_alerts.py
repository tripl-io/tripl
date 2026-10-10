"""Lifecycle alert candidates (GH #258).

The daily sunset watch (``lifecycle_findings``) records two event-lifecycle
problems: a deprecated event still receiving volume past its ``sunset_at``
(``sunset_overdue``) and a successor receiving no volume in the last 7 days
(``successor_silent``). This module turns every OPEN finding into one
``lifecycle`` alert candidate, which the per-run dispatch
(``dispatch._prepare_alert_deliveries``) routes through the same rule matcher,
``AlertRuleState`` send gate, cooldown and digest buffer as every other family.

PROJECT-GLOBAL, LIKE A CATALOG METRIC. A finding belongs to an event, not to a
scan, so EVERY config's run emits the same candidates and their alert states
live in the project-global partition (``scan_config_id`` NULL —
``dispatch._scope_partition_id``), exactly as a ``metric`` scope's do. All the
runs converge on one state row per (rule, finding); ``_claim_rule_state``'s
converging INSERT lets exactly one of them send, and the rest see a state whose
bucket is not newer. A multi-scan project therefore alerts once per finding,
and there is no anchor config whose creation or deletion could move the state
and re-send it. When a finding resolves it drops out of every run, the state
closes, and a later recurrence is a new incident.

``bucket`` is the finding's ``first_seen_at``, which does not move while the
finding stays open: the gate's "newer bucket AND cooldown elapsed" therefore
sends one alert per finding, never one per scan run.

The shared drift columns carry the context (``alert_templates.lifecycle_line``):
kind -> ``drift_type`` (``AlertDriftType.sunset_overdue`` / ``successor_silent``,
added to the native enum by d5f7b9c1e3a8 — without them the delivery INSERT
fails, a known trap), the event the message names -> ``drift_field``,
the rendered "<event> <what is wrong>" clause -> ``sample_value``. The
candidate's ``event_id`` is always the finding's (the DEPRECATED event), so
the alert links to the event whose lifecycle is being enforced; a silent
successor is named only in ``drift_field`` / ``sample_value``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from tripl.alert_templates import (
    LIFECYCLE_KIND_SUCCESSOR_SILENT,
    LIFECYCLE_KIND_SUNSET_OVERDUE,
)
from tripl.alerting_matching import SCOPE_LIFECYCLE, DriftAlertCandidate
from tripl.core.bucketing import optional_to_utc
from tripl.models.event import Event
from tripl.models.lifecycle_finding import LifecycleFinding
from tripl.models.scan_config import ScanConfig
from tripl.services.alerting_rendering import trim_alert_text

logger = logging.getLogger(__name__)

# ``scope_ref`` is String(64): ``successor_silent:`` (17) + a uuid hex (32) = 49.
_LIFECYCLE_KINDS = (LIFECYCLE_KIND_SUNSET_OVERDUE, LIFECYCLE_KIND_SUCCESSOR_SILENT)


def lifecycle_scope_ref(kind: str, event_id: uuid.UUID) -> str:
    """The stable alert scope key for one (kind, event) finding."""
    return f"{kind}:{event_id.hex}"


def format_per_day(value: float | int | None) -> str:
    """``1240.4`` -> ``"1,240"``: a whole per-day count with thousands separators."""
    return f"{round(float(value or 0)):,}"


def _lifecycle_detail(
    *,
    kind: str,
    event_name: str,
    volume_24h: float | int | None,
    sunset_at: datetime | None,
) -> str:
    """The clause after the headline.

    e.g. ``signup still receives 1,240/day, sunset 2026-09-01``.
    """
    if kind == LIFECYCLE_KIND_SUNSET_OVERDUE:
        detail = f"{event_name} still receives {format_per_day(volume_24h)}/day"
        sunset = optional_to_utc(sunset_at)
        if sunset is not None:
            detail += f", sunset {sunset.date().isoformat()}"
        return detail
    return f"{event_name} received no events in 7 days"


def _get_lifecycle_candidates(
    session: Session,
    config: ScanConfig,
) -> dict[tuple[str, str], DriftAlertCandidate]:
    """One ``lifecycle`` candidate per open finding of ``config``'s project.

    Every config of the project returns the same candidates (see the module
    docstring for why that alerts once). Read-only. Never raises: a failure here
    is logged and yields nothing, so the lifecycle family can never sink the
    collection run that carries every other alert.
    """
    # The caller's pending ORM work is flushed OUTSIDE the guard: the query
    # below would autoflush it anyway, and a failure there is the caller's, not
    # a lifecycle failure to be swallowed.
    session.flush()
    try:
        # A SAVEPOINT, so a failed read (say, the findings table not migrated
        # yet) rolls back only itself: on Postgres a bare failed statement
        # aborts the whole transaction, and every later dispatch query with it.
        with session.begin_nested():
            return _load_lifecycle_candidates(session, config)
    except Exception:
        logger.exception("lifecycle alert candidates failed for scan config %s", config.id)
        return {}


def _load_lifecycle_candidates(
    session: Session,
    config: ScanConfig,
) -> dict[tuple[str, str], DriftAlertCandidate]:
    related = aliased(Event)
    successor = aliased(Event)
    rows = session.execute(
        select(
            LifecycleFinding.id,
            LifecycleFinding.event_id,
            LifecycleFinding.kind,
            LifecycleFinding.first_seen_at,
            LifecycleFinding.volume_24h,
            LifecycleFinding.related_event_id,
            related.name,
            Event.name,
            Event.sunset_at,
            Event.superseded_by_event_id,
            successor.name,
        )
        .join(Event, Event.id == LifecycleFinding.event_id)
        .outerjoin(related, related.id == LifecycleFinding.related_event_id)
        .outerjoin(successor, successor.id == Event.superseded_by_event_id)
        .where(
            LifecycleFinding.project_id == config.project_id,
            LifecycleFinding.resolved_at.is_(None),
            LifecycleFinding.kind.in_(_LIFECYCLE_KINDS),
        )
        .order_by(LifecycleFinding.first_seen_at, LifecycleFinding.event_id)
    ).all()

    candidates: dict[tuple[str, str], DriftAlertCandidate] = {}
    for (
        finding_id,
        event_id,
        kind,
        first_seen_at,
        volume_24h,
        related_event_id,
        related_name,
        event_name,
        sunset_at,
        superseded_by_event_id,
        successor_name,
    ) in rows:
        kind_value = str(kind)
        is_sunset = kind_value == LIFECYCLE_KIND_SUNSET_OVERDUE
        # Both kinds hang on the DEPRECATED event, and so does the candidate's
        # ``event_id``. A silent-successor message NAMES the silent one: the
        # finding's ``related_event_id``, falling back to the event's current
        # ``superseded_by_event_id``.
        name = event_name
        if not is_sunset:
            if related_event_id is not None and related_name:
                name = related_name
            elif superseded_by_event_id is not None and successor_name:
                name = successor_name
        bucket = optional_to_utc(first_seen_at) or datetime.now(UTC)
        candidate = DriftAlertCandidate(
            # Lands on ``source_anomaly_id`` (no FK): the finding row.
            id=finding_id,
            # Project-global, like a catalog metric: no scan config.
            scan_config_id=None,
            scope_type=SCOPE_LIFECYCLE,
            # Keyed on the finding's identity, (kind, deprecated event), so a
            # finding that resolves and comes back reuses its rule state.
            scope_ref=lifecycle_scope_ref(kind_value, event_id),
            event_id=event_id,
            event_type_id=None,
            bucket=bucket,
            # Sunset overdue: volume where there should be none. Silent
            # successor: none where there should be some. The matcher does not
            # gate lifecycle on direction; this only colours the message.
            direction="spike" if is_sunset else "drop",
            actual_count=float(volume_24h or 0) if is_sunset else 0.0,
            expected_count=0.0,
            drift_field=trim_alert_text(name, max_length=255),
            drift_type=kind_value,
            sample_value=trim_alert_text(
                _lifecycle_detail(
                    kind=kind_value,
                    event_name=name,
                    volume_24h=volume_24h,
                    sunset_at=sunset_at,
                )
            ),
        )
        candidates[(candidate.scope_type, candidate.scope_ref)] = candidate
    return candidates
