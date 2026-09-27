"""Name warnings for the scan dry run (GH #265, F12): explosions and duplicates.

Best-effort by contract: :func:`dry_run_name_warnings` never raises, because
the dry run's job is to report, and a warning that failed to compute must not
take the rest of the preview down with it. Lexical only — the dry run runs in
the sync worker and has no business calling an embedding provider per name.

Uses the same pure engine as the request path
(``core.analyzers.duplicate_matching`` / ``name_similarity``), so "duplicate"
means one thing in the create form and in the scan preview.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.core.analyzers.duplicate_matching import (
    EXPLOSION_MIN_NAMES,
    CatalogName,
    NameIndex,
    find_combinatorial_explosions,
)
from tripl.core.analyzers.name_rules import compile_rule
from tripl.models.event import Event, EventStatus
from tripl.worker.plan_scope import main_branch_id

logger = logging.getLogger(__name__)

#: The most would-be-new names checked for duplicates in one dry run.
MAX_DUPLICATE_CHECKS = 200
#: The most catalog rows per event type read for the comparison.
MAX_CATALOG_ROWS = 5000


def _explosions(
    type_name: str,
    names: Sequence[str],
    name_format: str | None,
    cardinality_threshold: int | None,
) -> list[dict[str, object]]:
    rule = compile_rule(name_format)
    warnings: list[dict[str, object]] = []
    for explosion in find_combinatorial_explosions(
        names, rule, cardinality_threshold=cardinality_threshold
    ):
        examples = ", ".join(explosion.samples[:3])
        fix = (
            f"lower the cardinality threshold ({cardinality_threshold}) below "
            f"{explosion.count} so the column becomes a ${{...}} template"
            if cardinality_threshold is not None
            else "lower the cardinality threshold so the column becomes a ${...} template"
        )
        warnings.append(
            {
                "code": "combinatorial_explosion",
                "event_type": type_name,
                "message": (
                    f"{explosion.count} new names under {type_name!r} differ only in "
                    f"{explosion.label} (e.g. {examples}). A high-cardinality value is part "
                    f"of the event name; {fix}, or drop it from the name format."
                ),
                "count": explosion.count,
                "slot": explosion.slot,
                "slot_label": explosion.label,
                "pattern": explosion.pattern,
                "samples": list(explosion.samples),
            }
        )
    return warnings


def _catalog(
    session: Session, project_id: uuid.UUID, event_type_id: uuid.UUID
) -> list[CatalogName]:
    query = (
        select(Event.id, Event.name, Event.event_type_id, Event.status)
        .where(
            Event.project_id == project_id,
            Event.event_type_id == event_type_id,
            Event.status != EventStatus.archived,
        )
        .order_by(Event.name, Event.id)
        .limit(MAX_CATALOG_ROWS)
    )
    branch = main_branch_id(session, project_id)
    if branch is not None:
        query = query.where(Event.branch_id == branch)
    return [
        CatalogName(event_id, name or "", type_id, str(getattr(status, "value", status)))
        for event_id, name, type_id, status in session.execute(query).all()
    ]


def _duplicates(
    session: Session,
    project_id: uuid.UUID,
    type_name: str,
    event_type_id: uuid.UUID,
    names: Sequence[str],
    name_format: str | None,
    budget: int,
) -> list[dict[str, object]]:
    if budget <= 0 or not names:
        return []
    entries = _catalog(session, project_id, event_type_id)
    if not entries:
        return []
    index = NameIndex(entries, formats_by_type={event_type_id: name_format} if name_format else {})
    warnings: list[dict[str, object]] = []
    for name in names[:budget]:
        found = index.find(name, event_type_id, limit=1)
        if not found:
            continue
        match = found[0]
        warnings.append(
            {
                "code": "duplicate",
                "event_type": type_name,
                "name": name,
                "message": (
                    f"New event {name!r} looks like {match.name!r} "
                    f"({round(match.score * 100)}%) already in your plan."
                ),
                "duplicate_of": {
                    "event_id": str(match.event_id),
                    "name": match.name,
                    "score": match.score,
                    "status": match.status,
                },
            }
        )
    return warnings


def dry_run_name_warnings(
    session: Session,
    project_id: uuid.UUID,
    *,
    new_names_by_type: Mapping[str, Sequence[str]],
    event_type_ids: Mapping[str, uuid.UUID | None],
    name_format: str | None,
    cardinality_threshold: int | None = None,
) -> list[dict[str, object]]:
    """Explosion and duplicate warnings for the names a run would CREATE.

    ``new_names_by_type`` maps an event type NAME to the would-be-new event
    names under it; ``event_type_ids`` maps it to the plan's type id (None when
    the run would add the type, so there is nothing to compare against).
    Explosions are looked for only under a naming rule, against the scan's
    ``cardinality_threshold``. Never raises: a failure (the catalog read
    included) is logged and whatever was computed before it is returned.
    """
    warnings: list[dict[str, object]] = []
    try:
        for type_name, names in new_names_by_type.items():
            if len(names) > EXPLOSION_MIN_NAMES:
                warnings.extend(_explosions(type_name, names, name_format, cardinality_threshold))
        budget = MAX_DUPLICATE_CHECKS
        with session.begin_nested():
            for type_name, names in new_names_by_type.items():
                event_type_id = event_type_ids.get(type_name)
                if event_type_id is None:
                    continue
                found = _duplicates(
                    session, project_id, type_name, event_type_id, names, name_format, budget
                )
                budget -= len(names)
                warnings.extend(found)
    except Exception:
        logger.warning("Scan dry run: name warnings could not be computed", exc_info=True)
    return warnings
