"""One event per scan identity: the three steps every scan writer shares.

``uq_event_scan_identity`` (``models/event.py``) forbids two rows under one
event type holding one ``source_name``. Both the scan (``generate_events``) and
the group-rule pass (``merge_existing_events_for_group_rules``) load a type's
rows into a map keyed by identity, adopt an identity for rows that have none,
and insert rows claiming a new one — and each of those steps can produce the
exact collision the constraint forbids. They are written once here so the two
callers cannot drift apart on what "this identity is taken" means.

Not claimed: the adoption UPDATE itself is not raced. It is flushed outside the
savepoint below, so a writer that claims a NULL row's name between the load and
that flush still fails the run. The window is the same TOCTOU the API's
pre-check has, and it closes by itself on the next run — the writer's row then
holds the identity, is filed in the first pass, and the NULL row is left alone.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from tripl.models.event import Event

logger = logging.getLogger(__name__)


def scan_identity_winner_order() -> tuple[ColumnElement[Any], ...]:
    """The ORDER BY under which a contested NAME is adopted as an identity.

    Before the constraint this chose between two rows already holding one
    identity. It no longer can — the schema refuses the second holder — so what
    it decides now is adoption: several rows with NO identity can share one
    NAME (authored through the API under a type with no name format, or seeded
    by the demo), and ``index_events_by_identity`` lets exactly one of them take
    that name. Preference: the row traffic most recently landed on, then the
    older row, then a stable id — the same preference 340d91a8825a picked each
    surviving twin by, so "the live row" means one thing to the repair and to
    the scan.
    """
    return (
        Event.last_seen_at.desc().nullslast(),
        Event.created_at.asc(),
        Event.id.asc(),
    )


def index_events_by_identity(events: Sequence[Event]) -> dict[str, Event]:
    """Map ``source_name`` -> row, adopting a NAME for rows that have no identity.

    Two passes, and the split is the safety. Every row that already carries a
    ``source_name`` is filed first (``setdefault`` — the constraint makes a
    second holder impossible, but if one is ever loaded anyway the first in
    winner order stays). Only then do the NULL rows, in the order given, each
    adopt their own ``name`` as the identity — and only if no row already holds
    it. A NULL row whose name is taken stays NULL, is absent from the map, and
    is not touched by the run.

    Adoption is an UPDATE of ``source_name`` on the next flush, so it is bound
    by ``uq_event_scan_identity`` like any insert. Adopting unconditionally, as
    both callers did before the constraint, would write the two collisions the
    constraint forbids: two NULL rows sharing a name, and a NULL row named like
    an identity another row already holds. An established identity beats a lazy
    adoption because the established row is the one every previous scan has
    been routing to; letting a NULL row take the name would move that history.
    """
    by_identity: dict[str, Event] = {}
    for event in events:
        if event.source_name is not None:
            by_identity.setdefault(event.source_name, event)
    for event in events:
        if event.source_name is not None or event.name in by_identity:
            continue
        # Legacy / API-created rows: adopt the current name as the identity
        # once, so subsequent scans match on it instead of re-creating duplicates.
        event.source_name = event.name
        by_identity[event.name] = event
    return by_identity


def insert_event_claiming_identity(session: Session, event: Event) -> tuple[Event, bool]:
    """INSERT ``event`` under a SAVEPOINT; on a lost race hand back the holder.

    Returns ``(event, True)`` when the row landed and ``(holder, False)`` when
    another transaction claimed ``(event_type_id, source_name)`` between the
    caller's load and this INSERT — two scan configs of one project scanning
    the same type in parallel, or a scan racing an API ``create_event``. The
    holder is the row every later run will route to, so the caller should treat
    it exactly as an existing event. Any other IntegrityError is re-raised.

    Isolated in a SAVEPOINT like ``_ensure_variable`` so the violation does not
    poison the outer transaction. The savepoint holds nothing but this INSERT,
    so a rollback to it discards nothing the run has already done; the nested
    rollback expunges the pending ``event`` and expires only what was dirty
    inside the savepoint, leaving the caller's identity map intact. The key is
    read into locals before the attempt because the expunged object is the one
    thing not safe to rely on afterwards.

    The caller's pending work is settled by the explicit ``flush`` below, and
    the fact that it stands ABOVE the ``try`` is the whole of it.
    ``begin_nested()`` performs that flush itself
    (``SessionTransaction._take_snapshot``) — at ROOT level, before the
    savepoint exists — so leaving it to happen inside the ``try`` put the
    caller's own rows under this function's ``except``: an adoption UPDATE that
    lost its own race, or any other pending row, would be read as this INSERT's
    collision, and the re-select below would then run against a transaction the
    failed flush had already deactivated. The caller would get
    ``PendingRollbackError`` naming nothing, in place of the ``IntegrityError``
    naming the constraint that actually refused the row.
    """
    event_type_id = event.event_type_id
    source_name = event.source_name
    session.flush()
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        holder = session.execute(
            select(Event).where(
                Event.event_type_id == event_type_id,
                Event.source_name == source_name,
            )
        ).scalar_one_or_none()
        if holder is None:
            # Not this constraint: nothing holds the identity we lost to.
            raise
        logger.info(
            "Event identity claimed concurrently; routing to the holder",
            extra={
                "event_type_id": str(event_type_id),
                "source_name": source_name,
                "holder_id": str(holder.id),
            },
        )
        return holder, False
    return event, True
