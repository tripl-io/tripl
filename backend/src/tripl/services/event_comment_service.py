"""The discussion on an event: threaded, authored, and not part of the plan.

Every other text box on an event ships to whoever implements it — ``description``
is its own indexed search column, the catalog tooltip and a line in the Markdown
pasted into the ticket — so a question typed there arrives as if it were the
spec. This is the box for the question instead.

Same table as the photo threads, a different anchor. Nothing here reaches
``build_plan_snapshot``, the branch deep copy, the approval hash or the search
index, because every one of those selects on ``photo_id``. The merge does one
thing with it and only one: a thread hanging on a BRANCH row is moved to main —
onto the twin that row already reads through, else the row the merge gives the
event (``_move_event_threads_to_main``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import NamedTuple

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from tripl.models.event import Event
from tripl.models.event_photo_comment import (
    EVENT_COMMENT_STATUS_OPEN,
    EVENT_COMMENT_STATUS_RESOLVED,
    EVENT_COMMENT_STATUS_SNOOZED,
    EventPhotoComment,
)
from tripl.models.event_type import EventType
from tripl.models.user import User
from tripl.schemas.event_photo import EventCommentActionRequest
from tripl.services import (
    notification_announce,
    notification_service,
    subscription_service,
)
from tripl.services._branch_counterparts import main_counterparts
from tripl.services._plan_branch_locks import hold_branch_for_plan_write
from tripl.services.event_photo_service import ensure_comment_deletable
from tripl.services.mentions import excerpt
from tripl.services.project_links import project_link
from tripl.services.project_lookup import resolve_project_id


class EventThread(NamedTuple):
    """Where an event's discussion is written, and every row it is read from."""

    # The row a NEW thread is anchored on: the main twin of a branch copy, else
    # the event itself.
    home: Event
    # ``home`` plus the event's own row. The two differ only for a branch row
    # that has a twin, and that row can still hold a thread of its own — one
    # started before main had the event.
    anchors: frozenset[uuid.UUID]


async def event_thread(session: AsyncSession, slug: str, event_id: uuid.UUID) -> EventThread:
    """The rows this event's discussion lives on: the main twin of a branch copy.

    An event has ONE discussion. Deep-copying it onto every branch would fork
    the conversation, and merging the forks back is exactly the machinery that
    turned a comment into an unmergeable branch. Reading through
    to the twin — the same pattern, nothing written back — means a
    question raised on a branch is the same question main can answer.

    An event that exists only on a branch has no twin yet, so it keeps its own
    thread until the merge moves it onto the row it gives the event on main.
    A twin can also appear WITHOUT a merge — a scan creates the main row for
    that identity, or someone authors it on main — and reading only the twin
    from then on hid every thread the branch row already held, the note typed
    at creation included. So a branch row reads its own anchor
    as well as its twin's; it just never starts a new thread there once the
    twin exists.

    Main's own row reads only its own anchor: a question drafted on a branch
    main has not merged reaches main with the merge, like the rest of the
    branch.
    """
    project_id = await resolve_project_id(session, slug)
    event = (
        await session.execute(
            select(Event).where(Event.id == event_id, Event.project_id == project_id)
        )
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    twins = await main_counterparts(session, project_id=project_id, events=[event])
    home = twins.get(event.id, event)
    return EventThread(home=home, anchors=frozenset({home.id, event.id}))


async def list_comments(
    session: AsyncSession, slug: str, event_id: uuid.UUID
) -> list[EventPhotoComment]:
    thread = await event_thread(session, slug, event_id)
    rows = await session.execute(
        select(EventPhotoComment)
        .where(EventPhotoComment.event_id.in_(list(thread.anchors)))
        .order_by(EventPhotoComment.created_at.asc())
    )
    return list(rows.scalars().all())


async def _wait_out_a_merge_of_the_events_branch(
    session: AsyncSession, slug: str, event_id: uuid.UUID
) -> None:
    """Hold the event's working branch row FOR SHARE before the thread is resolved.

    A merge holds that row FOR UPDATE from its first read to its commit, and it
    moves the branch row's threads to main in between. Resolving the twin and
    the parent before the merge commits sees the pre-merge anchors, so a reply
    landed on the branch row while its question moved to main.
    Waiting here puts every read below after the merge: the twin exists, the
    question is on it, and the reply follows it there. Same lock as a plan
    write's (``_plan_branch_locks``), so a comment and a merge are ordered the
    same way an edit and a merge are.

    Main's row is not held: nothing moves main's threads, and a comment is not
    plan content, so it has no reason to wait for a merge into main. Neither is
    a merged or closed branch refused: discussion stays open on both. An event
    the caller cannot see is left to ``event_thread``'s 404.
    """
    project_id = await resolve_project_id(session, slug)
    branch_id = await session.scalar(
        select(Event.branch_id).where(Event.id == event_id, Event.project_id == project_id)
    )
    if branch_id is not None:
        await hold_branch_for_plan_write(session, branch_id, working_only=True)


async def create_comment(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    *,
    body: str,
    parent_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
) -> EventPhotoComment:
    await _wait_out_a_merge_of_the_events_branch(session, slug, event_id)
    thread = await event_thread(session, slug, event_id)
    anchor_id = thread.home.id
    if parent_id is not None:
        parent = await session.get(EventPhotoComment, parent_id)
        if parent is None or parent.event_id is None or parent.event_id not in thread.anchors:
            raise HTTPException(status_code=400, detail="parent_id must belong to this event")
        # A reply joins its parent's thread wherever that hangs, so a thread
        # stays on one anchor. Both bulk moves rely on that — the branch
        # merge's and ``_merge_event_into_group``'s, each one UPDATE by anchor —
        # and so does every door that deletes a branch row: whole threads move
        # to its twin, or go with the row, not a parent on one row with its
        # answers stranded on another.
        #
        # Holds under concurrency with a merge too: the branch row was held
        # above before the parent was read, so a merge in flight has committed
        # its move by now and the parent is read where the merge left it.
        anchor_id = parent.event_id

    comment = EventPhotoComment(
        event_id=anchor_id,
        photo_id=None,
        parent_id=parent_id,
        user_id=user_id,
        body=body.strip(),
    )
    session.add(comment)
    await session.flush()
    # Best-effort, each step in a savepoint of the comment's transaction: the
    # subscription and the notifications land with the comment, and a failure
    # in either is logged and rolled back alone — it never fails the comment.
    await _announce_comment(session, slug, event_id, thread, comment)
    await session.commit()
    await session.refresh(comment)
    return comment


async def _announce_comment(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    thread: EventThread,
    comment: EventPhotoComment,
) -> None:
    """Subscribe the commenter, then tell whoever should hear about the comment.

    Three passes through the one ``notify``, each excluding whoever an earlier
    pass already reached, so nobody gets two rows for one comment:

    1. ``mention`` — every @mentioned member, subscribed or not, mute ignored;
    2. ``open_question`` (a new top-level thread only) — the event's author and
       owner, plus the OWNERS of its event type (``watcher_reasons=('owner',)``:
       someone who merely watches the type is not asked);
    3. ``comment`` / ``reply`` — everyone else watching the event itself.
       Watching an event TYPE does not bring its events' ordinary comments.

    Subscriptions are read from every anchor of the discussion plus the row
    the page was on, but kept under the discussion's home id, which is also
    the entity the notification (and the mute) is about.
    """
    home = thread.home
    actor_id = comment.user_id

    async def subscribe_commenter() -> None:
        await subscription_service.subscribe(
            session,
            user_id=actor_id,
            project_id=home.project_id,
            entity_type=subscription_service.EVENT,
            entity_id=home.id,
            reason="commenter",
        )

    async def notify_watchers() -> None:
        who = await notification_announce.actor_label(session, actor_id)
        event_label = home.name or home.title or "an event"
        common: notification_service.NotifyCommon = {
            "project_id": home.project_id,
            "entity_type": subscription_service.EVENT,
            "entity_id": home.id,
            "url": await project_link(session, home.project_id, f"/events/detail/{home.id}"),
            "body": excerpt(comment.body),
            "actor_user_id": actor_id,
        }
        event_refs = [
            (subscription_service.EVENT, anchor) for anchor in {*thread.anchors, event_id}
        ]
        reached = await notification_announce.announce_mentions(
            session,
            body=comment.body,
            title=f"{who} mentioned you on {event_label}",
            common=common,
        )
        if comment.parent_id is None:
            reached |= await notification_service.notify(
                session,
                kind="open_question",
                title=f"{who} asked a question on {event_label}",
                watchers_of=[(subscription_service.EVENT_TYPE, home.event_type_id)],
                watcher_reasons=("owner",),
                user_ids=await subscription_service.subscriber_ids(
                    session, event_refs, reasons=("author", "owner")
                ),
                exclude_user_ids=reached,
                **common,
            )
        is_reply = comment.parent_id is not None
        await notification_service.notify(
            session,
            kind="reply" if is_reply else "comment",
            title=f"{who} {'replied' if is_reply else 'commented'} on {event_label}",
            watchers_of=event_refs,
            exclude_user_ids=reached,
            **common,
        )

    await notification_announce.best_effort(session, "subscribe commenter", subscribe_commenter)
    await notification_announce.best_effort(session, "event comment", notify_watchers)


async def delete_comment(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    comment_id: uuid.UUID,
    *,
    user: User,
) -> None:
    thread = await event_thread(session, slug, event_id)
    comment = await session.get(EventPhotoComment, comment_id)
    if comment is None or comment.event_id not in thread.anchors:
        raise HTTPException(status_code=404, detail="Comment not found")
    await ensure_comment_deletable(session, comment, user, thread.home.project_id)
    await session.delete(comment)
    await session.commit()


def unanswered_clause(now: datetime | None = None) -> ColumnElement[bool]:
    """Top-level event threads that still want an answer.

    A LAPSED snooze counts as open again, and that is decided here at read time
    rather than written back by a sweeper: a stored flag would be wrong for
    exactly as long as it took the next job to run, and the whole point of the
    snooze is that nobody is watching the thread in the meantime.

    Replies are excluded: the thread is the unit that gets answered, so a
    three-reply conversation is one open question, not four.
    """
    moment = now or datetime.now(UTC)
    return and_(
        EventPhotoComment.event_id.is_not(None),
        EventPhotoComment.parent_id.is_(None),
        or_(
            EventPhotoComment.status == EVENT_COMMENT_STATUS_OPEN,
            and_(
                EventPhotoComment.status == EVENT_COMMENT_STATUS_SNOOZED,
                EventPhotoComment.snoozed_until <= moment,
            ),
        ),
    )


async def apply_comment_action(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    comment_id: uuid.UUID,
    data: EventCommentActionRequest,
    user_id: uuid.UUID | None,
) -> EventPhotoComment:
    """Resolve, snooze or reopen one thread.

    Refused on a reply (400): resolution belongs to the thread, and letting a
    reply carry its own would make "is this question answered" a question with
    several contradictory answers.
    """
    thread = await event_thread(session, slug, event_id)
    comment = await session.get(EventPhotoComment, comment_id)
    if comment is None or comment.event_id not in thread.anchors:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.parent_id is not None:
        raise HTTPException(
            status_code=400,
            detail="Only a top-level comment carries resolution state",
        )

    now = datetime.now(UTC)
    if data.action == "resolve":
        comment.status = EVENT_COMMENT_STATUS_RESOLVED
        comment.resolved_at = now
        comment.resolved_by = user_id
        comment.snoozed_until = None
    elif data.action == "snooze":
        comment.status = EVENT_COMMENT_STATUS_SNOOZED
        comment.snoozed_until = data.snoozed_until
        comment.resolved_at = None
        comment.resolved_by = user_id
    else:
        comment.status = EVENT_COMMENT_STATUS_OPEN
        comment.resolved_at = None
        comment.resolved_by = None
        comment.snoozed_until = None

    # OMITTED and EXPLICITLY NULL are different requests and only
    # ``model_fields_set`` tells them apart — the distinction schema_drift_service
    # had to learn the hard way, where assigning unconditionally erased a note an
    # earlier action had stored. Reopening still clears it: a reopened thread has
    # no resolution any more.
    if data.action == "reopen":
        comment.resolution_note = None
    elif "note" in data.model_fields_set:
        comment.resolution_note = data.note

    await session.commit()
    await session.refresh(comment)
    return comment


def _identity_column() -> ColumnElement[str]:
    """The scan identity as SQL: ``source_name`` where there is one, else ``name``.

    The same key ``_branch_counterparts._identity`` computes in Python, spelled
    for the database so a branch row can be paired with its main twin without
    loading either.
    """
    return func.coalesce(Event.source_name, Event.name)


async def events_with_open_questions(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    main_branch_id: uuid.UUID,
) -> set[uuid.UUID]:
    """Ids ON THIS BRANCH whose discussion has an unanswered thread.

    An event has ONE discussion, hanging on its main twin, so a branch listing
    cannot simply match comment rows against branch ids — the filter would come
    back empty on every branch and read as "no open questions" rather than as a
    question the filter cannot answer.

    Two small queries: the anchors that hold an open thread, then the branch rows
    that pair with them. The first set is bounded by how many questions people
    have actually left unanswered, not by the size of the catalog.
    """
    anchors = set(
        (
            await session.execute(
                # Joined to events for the project scope, not filtered afterwards:
                # the comment table spans every project on the instance, and an
                # unscoped set would put another project's threads into this
                # project's IN list.
                select(EventPhotoComment.event_id)
                .join(Event, Event.id == EventPhotoComment.event_id)
                .where(Event.project_id == project_id, unanswered_clause())
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    if not anchors or branch_id == main_branch_id:
        # On main the anchor IS the row. A branch-only event also anchors its own
        # thread, which the union below keeps.
        return {anchor for anchor in anchors if anchor is not None}

    # Main's anchors only. Another branch's own row can hold a thread on the
    # same key — two branches each drafting "checkout:new_tap" — and matching
    # through it listed this branch's row under "has open questions" beside a
    # count of ?0, since that thread is no anchor of this row.
    keys = (
        await session.execute(
            select(EventType.name, _identity_column())
            .select_from(Event)
            .join(EventType, EventType.id == Event.event_type_id)
            .where(Event.id.in_(anchors), Event.branch_id == main_branch_id)
        )
    ).all()
    if not keys:
        return {anchor for anchor in anchors if anchor is not None}
    # A copy matches through the main row it was made from and nothing else;
    # only a row without a live origin matches by key — the rule
    # ``main_counterparts`` reads the thread by. By key alone, a question on one
    # of two namesakes listed the other one's copy too, beside a count of ?0.
    origin_row = aliased(Event)
    origin_is_live = (
        select(origin_row.id)
        .where(origin_row.id == Event.origin_id, origin_row.branch_id == main_branch_id)
        .correlate(Event)
        .exists()
    )
    branch_ids = (
        (
            await session.execute(
                select(Event.id)
                .select_from(Event)
                .join(EventType, EventType.id == Event.event_type_id)
                .where(
                    Event.project_id == project_id,
                    Event.branch_id == branch_id,
                    or_(
                        Event.origin_id.in_(anchors),
                        and_(
                            ~origin_is_live,
                            tuple_(EventType.name, _identity_column()).in_(
                                [tuple(k) for k in keys]
                            ),
                        ),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    # Union, not replacement: an event that exists only on this branch has no
    # twin and keeps its own thread, and a thread started before the twin
    # existed stays on the branch row until the merge moves it. The key match
    # reaches the twin on main and the union the row's own anchor — the same
    # two anchors ``open_question_counts`` adds up. Anchors
    # on other branches ride along in the set but never match a row listed
    # here, which is scoped to this branch.
    return set(branch_ids) | {anchor for anchor in anchors if anchor is not None}


async def open_question_counts(
    session: AsyncSession, *, project_id: uuid.UUID, events: Sequence[Event]
) -> dict[uuid.UUID, int]:
    """Event id → unanswered threads on its discussion, twin-aware.

    Counted over the same anchors ``event_thread`` reads: the twin's and the
    row's own. Counting the twin alone showed ?0 on a branch row that
    ``events_with_open_questions`` had just matched through its own anchor.

    Page-sized: the twin lookup and the count are one round of queries over the
    rows being rendered, the same budget ``attach_main_last_seen`` spends.
    """
    if not events:
        return {}
    twins = await main_counterparts(session, project_id=project_id, events=events)
    anchors_by_event = {ev.id: {ev.id, twins.get(ev.id, ev).id} for ev in events}
    rows = (
        await session.execute(
            select(EventPhotoComment.event_id, func.count(EventPhotoComment.id))
            .where(
                EventPhotoComment.event_id.in_(
                    {anchor for anchors in anchors_by_event.values() for anchor in anchors}
                ),
                unanswered_clause(),
            )
            .group_by(EventPhotoComment.event_id)
        )
    ).all()
    by_anchor = {anchor: count for anchor, count in rows}
    return {
        event_id: sum(by_anchor.get(anchor, 0) for anchor in anchors)
        for event_id, anchors in anchors_by_event.items()
    }
