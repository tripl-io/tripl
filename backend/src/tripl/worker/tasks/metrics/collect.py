from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from tripl.models.event import Event, EventStatus
from tripl.models.event_change import EVENT_CHANGE_SOURCE_SCAN, create_event_change
from tripl.models.event_field_value import EventFieldValue
from tripl.models.field_definition import FieldDefinition
from tripl.models.implementation_ticket import ImplementationTicket
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.services._id_chunks import chunked

logger = logging.getLogger(__name__)

# The Celery task the tracker integration exposes for "seen in data" comments
# (#258): ``args=[ticket_id, [event_id, ...], seen_at_iso]``. It posts ONE
# comment per ticket — "Seen in production data at <time>; marked live." — on
# Jira or Linear, whichever the ticket was opened on. Published by name so this
# module does not import the tracker clients.
SEEN_IN_DATA_COMMENT_TASK = "tripl.worker.tasks.implementation_tickets.comment_seen_in_data"

# ``session.info`` keys for the post-commit ticket-comment queue.
_SEEN_PENDING_KEY = "tripl.lifecycle.seen_in_data_pending"
_SEEN_LISTENING_KEY = "tripl.lifecycle.seen_in_data_listening"

# The statuses first data promotes to ``live``. Ordered by lifecycle rank.
AUTO_LIVE_FROM: tuple[EventStatus, ...] = (EventStatus.ready_for_dev, EventStatus.implemented)

# PostgreSQL/psycopg caps a single statement at 65535 bind parameters, so the
# batched statements below have to stay under that ceiling. We keep the same
# margin ``metric_rows`` keeps. The last_seen_at/first_seen_at UPDATE is the
# widest of them: it spends five parameters per event — key and value of each
# of the two CASEs, and the IN-list entry. Each CASE is rendered more than once
# (SET and WHERE) but costs nothing extra: the compiler emits one named bind per
# branch and psycopg collapses the repeated placeholders onto the same
# server-side parameter.
_MAX_BIND_PARAMS = 60000
_MAX_EVENTS_PER_BUMP = _MAX_BIND_PARAMS // 5


def _bump_event_last_seen(
    session: Session,
    *,
    event_agg: dict[tuple[uuid.UUID, uuid.UUID, datetime], int],
) -> None:
    """Project the collected buckets into Event.last_seen_at / first_seen_at.

    Monotonic both ways: ``last_seen_at`` only moves forward and
    ``first_seen_at`` (#258) only moves back, so a historical replay of an older
    window can neither rewind a freshly-collected last_seen_at nor push the
    first sighting later than the data says.

    AUTO-LIVE: MAIN-branch events with status 'ready_for_dev' or 'implemented'
    that receive fresh data AND carry a non-empty value for every required field
    of their event type are promoted to 'live'. The transition is a DATA FACT
    (#258): it bypasses the plan-branch review rules, applies to main only, and
    writes an EventChange (user_id=None, source='scan', read as ``SCAN_AUTHOR_LABEL``) that the
    history and the activity rail show. An event covered by an implementation
    ticket also gets a ticket comment, published after the commit. The status
    check makes this naturally idempotent — already-live events are never
    re-transitioned. An event missing a required value stays where it is until
    the plan is filled in; the next collection that sees it promotes it.

    ``ready_for_dev`` joined ``implemented``: on production the
    handoff goes analyst → developer → data, and nobody flips the row to
    "implemented" by hand before the first rows land, so the tracker read
    "0 implemented" for a feature whose events had been firing for weeks.
    Drafts and events in review stay put — ``in_review`` is the scan's own
    review queue, and a draft reaching the warehouse is news to surface, not a
    status to skip past.

    Every write here is batched. ``process_chunk`` calls this
    once per replay chunk, so a per-event round trip multiplied out to
    chunks x catalog statements on the sync worker engine, which has no
    pipelining. One chunk now costs one UPDATE (both sighting columns), one
    SELECT for the waiting events and — only when some are waiting — one SELECT
    for the required-fields check, one SELECT for covering tickets and at most
    one UPDATE per distinct promoted-from status.
    """
    if not event_agg:
        return

    latest_by_event: dict[uuid.UUID, datetime] = {}
    earliest_by_event: dict[uuid.UUID, datetime] = {}
    for (_scan_config_id, event_id, bucket), count in event_agg.items():
        if count <= 0:
            continue
        current = latest_by_event.get(event_id)
        if current is None or bucket > current:
            latest_by_event[event_id] = bucket
        first = earliest_by_event.get(event_id)
        if first is None or bucket < first:
            earliest_by_event[event_id] = bucket

    for batch in chunked(list(latest_by_event.items()), _MAX_EVENTS_PER_BUMP):
        # The CASEs resolve to each row's OWN buckets, so the monotonic guards
        # stay per row exactly as the per-event form had them. The IN list is
        # load-bearing rather than redundant: without it a row outside the
        # batch would see NULL CASEs, and ``last_seen_at IS NULL`` would then
        # match it and write that NULL back over the whole table.
        #
        # ``first_seen_at`` (#258) rides the SAME statement rather than a
        # second UPDATE, so one chunk still costs one events UPDATE per batch;
        # each column keeps its stored value unless its own guard passes.
        #
        # synchronize_session=False: we don't need ORM identity-map sync here
        # (the writer doesn't re-read Event in the same session), and the
        # default in-memory evaluator chokes when the stored value is naive
        # (SQLite test backend) but the new bucket is tz-aware.
        batch_ids = [event_id for event_id, _bucket in batch]
        latest_case = case(dict(batch), value=Event.id)
        earliest_case = case(
            {event_id: earliest_by_event[event_id] for event_id in batch_ids}, value=Event.id
        )
        moves_last = (Event.last_seen_at.is_(None)) | (Event.last_seen_at < latest_case)
        moves_first = (Event.first_seen_at.is_(None)) | (Event.first_seen_at > earliest_case)
        session.execute(
            update(Event)
            .where(Event.id.in_(batch_ids), moves_last | moves_first)
            # updated_at pinned to itself: TimestampMixin's onupdate would
            # otherwise stamp now() on every bump, and this bookkeeping write
            # is not a plan edit — the activity rail orders events by
            # updated_at and re-announced every live event each tick.
            .values(
                last_seen_at=case((moves_last, latest_case), else_=Event.last_seen_at),
                first_seen_at=case((moves_first, earliest_case), else_=Event.first_seen_at),
                updated_at=Event.updated_at,
            )
            .execution_options(synchronize_session=False)
        )

    _promote_seen_events(session, latest_by_event, earliest_by_event)


def _events_missing_required_values(session: Session, event_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """The events among ``event_ids`` that lack a value for a required field.

    "Required fields present" (#258) = every ``FieldDefinition`` with
    ``is_required`` on the event's type has an ``EventFieldValue`` on the event
    whose value is not blank. A missing row and a whitespace-only value both
    count as absent. One SELECT per batch.
    """
    missing: set[uuid.UUID] = set()
    for batch_ids in chunked(event_ids, _MAX_EVENTS_PER_BUMP):
        rows = session.execute(
            select(Event.id)
            .join(
                FieldDefinition,
                and_(
                    FieldDefinition.event_type_id == Event.event_type_id,
                    FieldDefinition.is_required.is_(True),
                ),
            )
            .outerjoin(
                EventFieldValue,
                and_(
                    EventFieldValue.event_id == Event.id,
                    EventFieldValue.field_definition_id == FieldDefinition.id,
                ),
            )
            .where(
                Event.id.in_(batch_ids),
                or_(
                    EventFieldValue.id.is_(None),
                    func.coalesce(func.trim(EventFieldValue.value), "") == "",
                ),
            )
            .distinct()
        ).scalars()
        missing.update(rows)
    return missing


def _promote_seen_events(
    session: Session,
    latest_by_event: dict[uuid.UUID, datetime],
    earliest_by_event: dict[uuid.UUID, datetime],
) -> None:
    """AUTO-LIVE: promote the waiting main-branch events that data just reached."""
    # Collected per previous status so the promotion costs one UPDATE per
    # status present rather than one per event.
    waiting: list[tuple[uuid.UUID, uuid.UUID, EventStatus]] = []
    for batch_ids in chunked(list(latest_by_event.keys()), _MAX_EVENTS_PER_BUMP):
        rows = session.execute(
            select(Event.id, Event.project_id, Event.status)
            # Main only: the transition is a data fact about the live plan. A
            # working branch's copy follows when the branch is updated from main.
            # ``PlanBranch.project_id`` is part of the join for the reason
            # ``activity_service._event_items`` gives.
            .join(
                PlanBranch,
                (PlanBranch.id == Event.branch_id) & (PlanBranch.project_id == Event.project_id),
            )
            .where(
                Event.id.in_(batch_ids),
                Event.status.in_(AUTO_LIVE_FROM),
                PlanBranch.kind == BranchKind.main.value,
            )
        ).all()
        waiting.extend((eid, project_id, EventStatus(status)) for eid, project_id, status in rows)
    if not waiting:
        return

    incomplete = _events_missing_required_values(session, [eid for eid, _p, _s in waiting])
    by_previous: dict[EventStatus, list[uuid.UUID]] = {}
    promoted: dict[uuid.UUID, uuid.UUID] = {}
    for eid, project_id, previous in waiting:
        if eid in incomplete:
            continue
        by_previous.setdefault(previous, []).append(eid)
        promoted[eid] = project_id
        session.add(
            create_event_change(
                event_id=eid,
                user_id=None,
                field="status",
                old_value=previous,
                new_value=EventStatus.live,
                source=EVENT_CHANGE_SOURCE_SCAN,
            )
        )

    for previous, promoted_ids in by_previous.items():
        # Grouped by the previous status rather than swept with a single
        # ``status.in_(AUTO_LIVE_FROM)``: that keeps the per-row
        # ``status == previous_status`` guard the per-event form had, so a
        # concurrent ready_for_dev → implemented move still cannot be promoted
        # under a stale ``old_value``.
        #
        # ``updated_at`` pinned, like the bumps above: the transition reaches
        # the activity rail through its EventChange row, attributed to the scan
        # (``activity_service._auto_transition_items``), not as an anonymous
        # "Event implemented" re-announcement of the row.
        for batch_ids in chunked(promoted_ids, _MAX_EVENTS_PER_BUMP):
            session.execute(
                update(Event)
                .where(Event.id.in_(batch_ids), Event.status == previous)
                .values(status=EventStatus.live, updated_at=Event.updated_at)
                .execution_options(synchronize_session=False)
            )

    if promoted:
        _queue_seen_in_data_comments(session, promoted, earliest_by_event, latest_by_event)


def _queue_seen_in_data_comments(
    session: Session,
    promoted: dict[uuid.UUID, uuid.UUID],
    earliest_by_event: dict[uuid.UUID, datetime],
    latest_by_event: dict[uuid.UUID, datetime],
) -> None:
    """Queue one "seen in data" comment per implementation ticket covering a promotion.

    Only tickets the tracker has answered (``external_key`` set) can take a
    comment. ``ImplementationTicket.event_ids`` is a JSON list, so the match is
    made here over the promoted projects' tickets — promotions are rare, and a
    project holds one ticket per merged branch. The publish waits for the
    COMMIT (``after_commit``): a comment about a transition that rolled back
    would be a lie on someone else's tracker.
    """
    tickets = session.execute(
        select(ImplementationTicket.id, ImplementationTicket.event_ids).where(
            ImplementationTicket.project_id.in_(set(promoted.values())),
            ImplementationTicket.external_key.is_not(None),
        )
    ).all()
    promoted_keys = {str(eid): eid for eid in promoted}
    pending: list[tuple[str, list[str], str]] = []
    for ticket_id, event_ids in tickets:
        covered = [key for key in (event_ids or []) if key in promoted_keys]
        if not covered:
            continue
        seen_at = min(
            earliest_by_event.get(promoted_keys[key], latest_by_event[promoted_keys[key]])
            for key in covered
        )
        pending.append((str(ticket_id), covered, seen_at.isoformat()))
    if not pending:
        return

    queue = session.info.get(_SEEN_PENDING_KEY)
    if queue is None:
        queue = []
        session.info[_SEEN_PENDING_KEY] = queue
    queue.extend(pending)
    if not session.info.get(_SEEN_LISTENING_KEY):
        session.info[_SEEN_LISTENING_KEY] = True
        sa_event.listen(session, "after_commit", _publish_seen_in_data_comments)
        sa_event.listen(session, "after_rollback", _drop_seen_in_data_comments)


def _publish_seen_in_data_comments(session: Session) -> None:
    pending: list[tuple[str, list[str], str]] = session.info.pop(_SEEN_PENDING_KEY, None) or []
    if not pending:
        return
    # Lazy: the Celery app imports settings and the task graph, which this
    # module must not pull in at import time.
    from tripl.worker.celery_app import celery_app

    for ticket_id, event_ids, seen_at in pending:
        try:
            celery_app.send_task(SEEN_IN_DATA_COMMENT_TASK, args=[ticket_id, event_ids, seen_at])
        except Exception:
            # The transition is committed and recorded; a lost comment is a
            # missing courtesy on the tracker, never a reason to fail the scan.
            logger.exception("Failed to queue seen-in-data comment for ticket %s", ticket_id)


def _drop_seen_in_data_comments(session: Session) -> None:
    session.info.pop(_SEEN_PENDING_KEY, None)
