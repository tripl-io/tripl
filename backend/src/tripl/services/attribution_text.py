"""Alert-side reading of the stored "why did this move" attribution (GH #255).

The attribution itself is computed ONCE, by the metrics worker, right after the
anomaly is written, and stored in ``metric_anomaly_attributions`` — so an alert
and the drilldown's Why panel quote the same numbers. This module only reads
that row back and phrases it:

    92% of the drop comes from platform = ios (−3,120 of −3,390)

``format_attribution_line`` is a pure function over the stored shape — exactly
``core.analyzers.attribution.attribution_headline`` (+ ``release_line``) — so it
accepts the ORM row or the plain dict the API serializes; ``load_attributions``
and ``load_attributions_for_scopes`` are the two small DB readers.

Nothing here may raise into an alert send: the callers wrap the loaders, and the
formatter degrades to ``""`` on any shape it does not understand.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.core.analyzers.attribution import attribution_headline, release_line
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_anomaly_attribution import MetricAnomalyAttribution

logger = logging.getLogger(__name__)

ScopeKey = tuple[str, str, datetime]


def _field(source: object, name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _as_utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def format_attribution_line(
    attribution: object,
    direction: str | None = None,
    *,
    bucket: datetime | None = None,
) -> str:
    """The alert item's "why" one-liner, or ``""``.

    Exactly ``attribution_headline`` (+ ``"; " + release_line`` when there is
    one) from ``core.analyzers.attribution`` — the strings the API returns and
    the UI renders — so the alert and the drilldown can never disagree. The
    release half needs the flagged ``bucket`` for its lead time and is left out
    without it. Escaping is the renderer's job (per message format), never
    done here.
    """
    if attribution is None:
        return ""
    payload = {
        "delta": _field(attribution, "delta"),
        "columns": _field(attribution, "columns") or [],
        "release": _field(attribution, "release"),
    }
    try:
        parts = [attribution_headline(payload, direction)]
        if bucket is not None:
            parts.append(release_line(payload, anomaly_bucket=bucket, direction=direction))
    except TypeError, ValueError, ArithmeticError:
        logger.warning("Unreadable stored attribution", exc_info=True)
        return ""
    return "; ".join(part for part in parts if part)


def load_attributions(
    session: Session, anomaly_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, MetricAnomalyAttribution]:
    """``anomaly_id -> stored attribution row`` for the ids that have one."""
    ids = {anomaly_id for anomaly_id in anomaly_ids if anomaly_id is not None}
    if not ids:
        return {}
    model = MetricAnomalyAttribution
    return {
        row.anomaly_id: row
        for row in session.execute(select(model).where(model.anomaly_id.in_(ids))).scalars()
    }


def load_attributions_for_scopes(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    keys: Iterable[ScopeKey],
) -> dict[ScopeKey, MetricAnomalyAttribution]:
    """Attribution rows for ``(scope_type, scope_ref, bucket)`` keys of one scan.

    Alert items do not carry the anomaly id, but ``metric_anomalies`` is unique
    on ``(scan_config_id, scope_type, scope_ref, bucket)``, which is exactly
    what an item does carry — so the join is by that key.
    """
    wanted = set(keys)
    if not wanted:
        return {}
    model = MetricAnomalyAttribution
    refs = {scope_ref for _, scope_ref, _ in wanted}
    buckets = {bucket for _, _, bucket in wanted}
    rows = session.execute(
        select(MetricAnomaly.scope_type, MetricAnomaly.scope_ref, MetricAnomaly.bucket, model)
        .join(model, model.anomaly_id == MetricAnomaly.id)
        .where(
            MetricAnomaly.scan_config_id == scan_config_id,
            MetricAnomaly.scope_ref.in_(refs),
            MetricAnomaly.bucket.in_(buckets),
        )
    ).all()
    found: dict[ScopeKey, MetricAnomalyAttribution] = {}
    normalized = {(t, r, _as_utc(b)): (t, r, b) for t, r, b in wanted}
    for scope_type, scope_ref, bucket, row in rows:
        original = normalized.get((scope_type, scope_ref, _as_utc(bucket)))
        if original is not None:
            found[original] = row
    return found


def attribution_line_for_scope(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str,
    scope_ref: str,
    bucket: datetime,
    direction: str | None,
) -> str:
    """One item's one-liner, or ``""`` when nothing is stored for it."""
    key = (scope_type, scope_ref, bucket)
    row = load_attributions_for_scopes(session, scan_config_id=scan_config_id, keys=[key]).get(key)
    return format_attribution_line(row, direction, bucket=bucket)
