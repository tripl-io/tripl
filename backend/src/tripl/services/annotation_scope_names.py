"""Human names for the series a chart annotation or planned event is scoped to.

Both are scoped by a ``(scope_type, scope_ref)`` pair whose ref is an id: an
event, an event type, a catalog metric, or — for ``project_total`` — the scan
config whose total it is. The project-wide Annotations page lists them away
from their chart, so it needs the name the chart would have shown in its
title. Resolved in one query per scope type, and only within the project, so a
ref that names another project's row (or a deleted one) resolves to nothing.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.metric_definition import MetricDefinition
from tripl.models.scan_config import ScanConfig

ScopeKey = tuple[str, str]

# scope_type -> (id column, project column, label column)
_SCOPE_COLUMNS: dict[str, tuple[InstrumentedAttribute[Any], ...]] = {
    "event": (Event.id, Event.project_id, Event.name),
    "event_type": (EventType.id, EventType.project_id, EventType.display_name),
    "metric": (MetricDefinition.id, MetricDefinition.project_id, MetricDefinition.display_name),
    "project_total": (ScanConfig.id, ScanConfig.project_id, ScanConfig.name),
}


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def resolve_scope_names(
    session: AsyncSession,
    project_id: uuid.UUID,
    scopes: Iterable[tuple[str | None, str | None]],
) -> dict[ScopeKey, str]:
    """``{(scope_type, scope_ref): name}`` for every scope that resolves."""
    ids_by_type: dict[str, set[uuid.UUID]] = defaultdict(set)
    for scope_type, scope_ref in scopes:
        if scope_type is None or scope_ref is None:
            continue
        parsed = _as_uuid(scope_ref)
        if parsed is not None:
            ids_by_type[str(scope_type)].add(parsed)

    names: dict[ScopeKey, str] = {}
    for scope_type, wanted in ids_by_type.items():
        columns = _SCOPE_COLUMNS.get(scope_type)
        if columns is None:
            continue
        id_column, project_column, label_column = columns
        conditions: list[ColumnElement[bool]] = [
            id_column.in_(wanted),
            project_column == project_id,
        ]
        rows = await session.execute(select(id_column, label_column).where(*conditions))
        for row_id, label in rows.all():
            names[(scope_type, str(row_id))] = str(label)
    return names
