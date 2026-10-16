"""When a shadow-event candidate's identity already belongs to a catalog event.

A shadow candidate is warehouse traffic that matched no catalog event when it
was collected. That stops being true the moment an event takes the identity —
a scheduled catalog sync auto-creates one for an unplanned name, or someone
adds the event by hand — but the candidate row stays ``new``, so the inbox kept
offering it and Accept could only 409 on the duplicate identity: an inbox row
nobody could clear.

One predicate decides "held", used by both sides so they cannot disagree:

* the inbox (``reconciliation_service.list_shadow_events``) hides a ``new``
  candidate the predicate holds;
* the collector (``worker.tasks.metrics.tasks``) resolves those candidates after
  each catalog sync — ``accepted`` and linked to the event, with no
  ``resolved_by`` because nobody pressed anything (:func:`resolve_held_candidates`).

"Held" mirrors how the collector matches traffic: an event on the project's
MAIN plan (the plan a scan reads), of the candidate's event type (a candidate
with no resolved type matches project-wide), whose identity — ``source_name``,
or its ``name`` where it has none, the identity an authored event adopts — is
the candidate's ``event_name``. An ARCHIVED event is left out: the inbox hides
those candidates under its own older rule, and an archived event is not an
answer to "which event is this traffic".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, Select, and_, func, or_, select, update
from sqlalchemy.orm import Session

from tripl.models.event import Event, EventStatus
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.shadow_event_candidate import (
    SHADOW_STATUS_ACCEPTED,
    SHADOW_STATUS_NEW,
    ShadowEventCandidate,
)


def _holding_event_condition() -> ColumnElement[bool]:
    """The held-by rule over ``Event`` joined to its ``PlanBranch``."""
    return and_(
        Event.project_id == ShadowEventCandidate.project_id,
        PlanBranch.kind == BranchKind.main.value,
        Event.status != EventStatus.archived,
        or_(
            ShadowEventCandidate.event_type_id.is_(None),
            Event.event_type_id == ShadowEventCandidate.event_type_id,
        ),
        func.coalesce(Event.source_name, Event.name) == ShadowEventCandidate.event_name,
    )


def held_by_catalog_event() -> ColumnElement[bool]:
    """Correlated EXISTS: a main-plan event holds this candidate's identity."""
    return (
        select(Event.id)
        .join(PlanBranch, PlanBranch.id == Event.branch_id)
        .where(_holding_event_condition())
        .exists()
    )


def _held_candidates(scan_config_id: uuid.UUID) -> Select[uuid.UUID, uuid.UUID]:
    """``(candidate id, holding event id)`` pairs, oldest event first per candidate."""
    return (
        select(ShadowEventCandidate.id, Event.id)
        .select_from(ShadowEventCandidate)
        .join(Event, Event.project_id == ShadowEventCandidate.project_id)
        .join(PlanBranch, PlanBranch.id == Event.branch_id)
        .where(
            ShadowEventCandidate.scan_config_id == scan_config_id,
            ShadowEventCandidate.status == SHADOW_STATUS_NEW,
            _holding_event_condition(),
        )
        .order_by(ShadowEventCandidate.id, Event.created_at, Event.id)
    )


def resolve_held_candidates(
    session: Session, scan_config_id: uuid.UUID, *, now: datetime | None = None
) -> int:
    """Mark this config's ``new`` candidates an event now holds as accepted.

    Linked to the holding event (``accepted_event_id``), stamped resolved now,
    ``resolved_by`` left empty: the scan matched it, nobody chose to. Returns
    how many were resolved. Flushes; the caller commits.
    """
    pairs = session.execute(_held_candidates(scan_config_id)).all()
    if not pairs:
        return 0
    resolved_at = now or datetime.now(UTC)
    by_id: dict[uuid.UUID, uuid.UUID] = {}
    for candidate_id, event_id in pairs:
        # Two events holding one identity is a catalog the identity rule should
        # not allow; the oldest is the one the scan has been matching longest.
        by_id.setdefault(candidate_id, event_id)
    by_event: dict[uuid.UUID, list[uuid.UUID]] = {}
    for candidate_id, event_id in by_id.items():
        by_event.setdefault(event_id, []).append(candidate_id)
    resolved = 0
    for event_id, candidate_ids in by_event.items():
        # Re-checks ``new`` in the UPDATE itself: an editor who dismissed or
        # accepted the candidate between the SELECT above and this write keeps
        # their decision, rather than the resolver overwriting it.
        result = session.execute(
            update(ShadowEventCandidate)
            .where(
                ShadowEventCandidate.id.in_(candidate_ids),
                ShadowEventCandidate.status == SHADOW_STATUS_NEW,
            )
            .values(
                status=SHADOW_STATUS_ACCEPTED,
                accepted_event_id=event_id,
                resolved_at=resolved_at,
            )
        )
        resolved += int(getattr(result, "rowcount", 0) or 0)
    session.flush()
    return resolved
