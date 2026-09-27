"""Shared plumbing for the request-path notification hooks (#259).

Every write that announces itself — an event comment, a photo comment, a
branch comment or review step, a new event — does two things after its own
rows are in the session: auto-subscribe someone, and ``notify``. Neither may
fail the write. :func:`best_effort` runs one such step inside a SAVEPOINT: a
failure rolls back only what the step wrote, is logged, and the caller's
transaction commits as if the step had never run.

:func:`announce_mentions` is the one @mention pass every comment surface uses,
so an event comment, a photo comment and a branch comment reach a mentioned
person the same way: always (mute ignored), and exactly once per comment.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.user import User
from tripl.services import notification_service
from tripl.services.mentions import mentioned_user_ids

logger = logging.getLogger(__name__)


async def best_effort(
    session: AsyncSession, what: str, step: Callable[[], Awaitable[object]]
) -> None:
    """Run ``step`` in a savepoint; on any failure roll it back, log, and carry on."""
    try:
        async with session.begin_nested():
            await step()
    except Exception:  # noqa: BLE001 — a notification must never fail the write
        logger.exception("Notification step %r failed; the write stands", what)


async def actor_label(session: AsyncSession, actor_id: uuid.UUID | None) -> str:
    """How a notification title names whoever acted."""
    actor = await session.get(User, actor_id) if actor_id is not None else None
    return (actor.name or actor.email) if actor is not None else "Someone"


async def announce_mentions(
    session: AsyncSession,
    *,
    body: str,
    title: str,
    common: notification_service.NotifyCommon,
) -> set[uuid.UUID]:
    """The ``mention`` pass: every @mentioned member, subscribed or not, mute ignored.

    Answers who was reached, for the caller's later passes to exclude.
    """
    user_ids = mentioned_user_ids(body)
    if not user_ids:
        return set()
    return await notification_service.notify(
        session,
        kind="mention",
        title=title,
        user_ids=user_ids,
        honour_mute=False,
        **common,
    )


__all__ = ["actor_label", "announce_mentions", "best_effort"]
