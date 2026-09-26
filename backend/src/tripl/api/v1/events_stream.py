"""Project-scoped one-way Server-Sent Events stream (tripl-2su6.8).

``GET /projects/{slug}/events/stream`` relays committed state changes (scan jobs,
metric collections, activity, signals, project summaries) to the browser so a
completed background job refreshes every visible surface without a reload.

Auth + scope: the router is mounted with ``Depends(get_current_user)`` (see
``router.py``), which authenticates the session/API key and, for a project-scoped
API key, enforces that it may only reach ITS own project (``_enforce_project_scope``).
Anonymous callers get 401; a foreign project-scoped key gets the same 404 ("Project
not found") an unknown slug gets. The router's project-membership dependency then
404s a caller who is not a member of the project — an instance owner reaches every
project — before the stream opens, so a non-member cannot even learn the slug
exists. The handler resolves the slug, 404-ing an unknown project.

Membership is checked again while the stream is open: a stream can live for hours,
and a member removed from the project must stop receiving its events. Every
:data:`MEMBERSHIP_RECHECK_SECONDS` (one heartbeat interval) the generator re-reads
the caller's project role in a short-lived session and ends the stream once it is
``None``; the browser's reconnect then meets the 404 gate.

The endpoint is excluded from the OpenAPI schema (``include_in_schema=False``): SSE
is consumed by the browser ``EventSource``, not the generated typed client, and a
streaming ``text/event-stream`` response has no useful JSON schema.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tripl import database, realtime
from tripl.api.deps import CurrentUserDep, SessionDep
from tripl.services import project_access
from tripl.services.project_service import get_project_id_by_slug

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{slug}/events", tags=["events"])

# Streaming-friendly headers: disable caching and proxy buffering so events flush
# immediately (nginx honours ``X-Accel-Buffering: no``).
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


# How often an open stream re-checks that its caller is still a member. One
# heartbeat interval: the generator consults the guard on every heartbeat and
# every event, and this bounds the queries to one per interval per stream.
MEMBERSHIP_RECHECK_SECONDS = realtime.HEARTBEAT_SECONDS


def membership_guard(
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    is_disconnected: Callable[[], Awaitable[bool]],
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    interval_seconds: float = MEMBERSHIP_RECHECK_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[], Awaitable[bool]]:
    """An ``is_disconnected`` for the SSE generator that also ends a revoked stream.

    Answers ``True`` (stop streaming) when the client went away, or when at
    least ``interval_seconds`` passed since the last membership read and the
    fresh read says the caller is no longer a member. Once revoked it stays
    revoked. A failed read ends the stream too (fail closed): the client
    reconnects through the membership gate, which answers for it.
    """
    factory = session_factory if session_factory is not None else database.async_session
    last_checked = clock()
    revoked = False

    async def should_stop() -> bool:
        nonlocal last_checked, revoked
        if revoked or await is_disconnected():
            return True
        now = clock()
        if now - last_checked < interval_seconds:
            return False
        last_checked = now
        try:
            revoked = not await project_access.still_member(
                factory, user_id=user_id, project_id=project_id
            )
        except Exception:
            logger.warning(
                "SSE membership re-check failed; ending the stream",
                extra={"project_id": str(project_id), "user_id": str(user_id)},
                exc_info=True,
            )
            revoked = True
        return revoked

    return should_stop


def _parse_last_event_id(request: Request) -> int | None:
    """Reconnect cursor from the ``Last-Event-ID`` header or ``?last_event_id=``.

    ``EventSource`` resends the header natively; a manual reconnect (custom
    backoff) passes the query param instead. Malformed values are ignored.
    """
    raw = request.headers.get("Last-Event-ID") or request.query_params.get("last_event_id")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError, TypeError:
        return None


@router.get("/stream", include_in_schema=False)
async def stream_project_events(
    slug: str,
    request: Request,
    session: SessionDep,
    user: CurrentUserDep,
    # Optional cap on events/heartbeats before the server closes the stream (the
    # client reconnects). Unset in production = an open-ended stream; tests pass
    # ``0`` for a finite greet-and-close response (ASGITransport buffers the whole
    # body, so an unbounded stream can't be read over the test transport).
    max_events: Annotated[int | None, Query(ge=0)] = None,
) -> StreamingResponse:
    # 404 an unknown/foreign project for a session user (a project-scoped API key
    # was already fenced to its own project by the router-level dependency).
    project_id = await get_project_id_by_slug(session, slug)
    user_id = user.id

    last_event_id = _parse_last_event_id(request)
    # The response may live for hours; release the transaction and pooled
    # connection used by authentication and project lookup before streaming.
    await session.close()
    generator = realtime.project_response_stream(
        slug=slug,
        last_event_id=last_event_id,
        is_disconnected=membership_guard(
            user_id=user_id,
            project_id=project_id,
            is_disconnected=request.is_disconnected,
        ),
        max_messages=max_events,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
