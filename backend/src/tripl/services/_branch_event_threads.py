"""Keep a branch event's discussion when the row it hangs on goes away.

A branch row holds a thread of its own only when it had no main twin to anchor
on at the time — an event created on the branch, or a question asked before a
scan (or an author) gave main a row for that identity. Once that twin exists,
``event_comment_service.event_thread`` reads both anchors, so the discussion is
visible from either side. Deleting the branch row then took its half through
the ``event_photo_comments`` FK cascade, although the twin that shows it is
still there.

The merge already moves these threads onto main. Every other
door that deletes a branch event — the event delete and bulk delete, the event
type delete that cascades its events, the revert of an ``added`` entity, and
deleting the branch itself — runs ``rescue_branch_event_threads`` first, with
the same UPDATE the merge uses (``move_event_threads``).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.event import Event
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.services import subscription_service
from tripl.services._branch_counterparts import main_counterparts

__all__ = ["move_event_threads", "rescue_branch_event_threads"]


async def move_event_threads(
    session: AsyncSession, *, target_by_event_id: Mapping[uuid.UUID, uuid.UUID]
) -> None:
    """Re-anchor each event's own discussion (``photo_id IS NULL``) on its target.

    The rows are MOVED, not copied: ids, replies and resolution state go
    unchanged, beside whatever thread the target row already had. A reply
    always hangs on its parent's anchor (``event_comment_service.create_comment``),
    so one UPDATE per anchor moves whole threads and never strands an answer.

    The event's watchers (``subscriptions``, #259) move with the thread: they
    are kept under the id the discussion lives on, so a subscription left on
    the source row would stop hearing about the thread it followed.
    """
    await subscription_service.rekey_event_subscriptions(
        session, target_by_event_id=dict(target_by_event_id)
    )
    for event_id, target_id in target_by_event_id.items():
        await session.execute(
            update(EventPhotoComment)
            .where(
                EventPhotoComment.event_id == event_id,
                EventPhotoComment.photo_id.is_(None),
            )
            # Re-anchored, not edited: named explicitly so the column's onupdate
            # does not stamp the move time on every row that moved.
            .values(event_id=target_id, updated_at=EventPhotoComment.updated_at)
        )


async def rescue_branch_event_threads(
    session: AsyncSession, *, project_id: uuid.UUID, event_ids: Collection[uuid.UUID]
) -> None:
    """Before *event_ids* are deleted, hand each one's thread to its main twin.

    Only rows that hold a thread AND have a twin move: main's own rows are not
    in ``main_counterparts``' answer, and a branch row with no twin has nowhere
    for its discussion to go, so it goes with the row, as it does in the merge
    when main has deleted the event. The twin is the one ``event_thread`` reads
    through right now, so after the delete the discussion reads exactly as it
    did before — only from one anchor instead of two.

    Bounded by how many of the doomed rows hold a thread of their own, not by
    how many are being deleted.
    """
    if not event_ids:
        return
    anchored_ids = set(
        (
            await session.execute(
                select(EventPhotoComment.event_id)
                .where(
                    EventPhotoComment.event_id.in_(list(event_ids)),
                    EventPhotoComment.photo_id.is_(None),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    if not anchored_ids:
        return
    anchored = (
        (
            await session.execute(
                select(Event).where(Event.project_id == project_id, Event.id.in_(anchored_ids))
            )
        )
        .scalars()
        .all()
    )
    twins = await main_counterparts(session, project_id=project_id, events=anchored)
    await move_event_threads(
        session,
        target_by_event_id={event_id: twin.id for event_id, twin in twins.items()},
    )
