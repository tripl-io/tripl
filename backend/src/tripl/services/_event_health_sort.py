"""Catalog order "least healthy first" (F15, #268).

``GET /events?order_by=health`` cannot sort in SQL: a score is computed from
facts spread over a dozen tables. The already-filtered catalog query is run for
its ids only (no limit), the whole filtered set is scored in one batched pass,
sorted by (score, name, id), sliced to the page, and only that page is loaded
as ORM rows. ``total`` is unaffected: the caller counts with its own query.

The ordered id list is cached per (project, filter) for
``HEALTH_SORT_CACHE_TTL_SECONDS``. The first page (offset 0) always scores
afresh and replaces the entry; later pages slice the cached list, so scrolling
scores the catalog once per sort session rather than once per page, and a score
that changes mid-scroll cannot move an event across a page boundary (shown
twice or never). A filter whose parameters differ (e.g. a moving timestamp)
simply misses the cache and scores afresh.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from tripl import cache
from tripl.models.event import Event
from tripl.services import event_health_service

# Long enough for a reader to scroll the catalog, short enough that a new
# drift or verdict shows in the order within a minute.
HEALTH_SORT_CACHE_TTL_SECONDS = 60


def _filter_digest(session: AsyncSession, id_query: Select[Any]) -> str:
    """A stable hash of the filtered id query's SQL and its bound parameters."""
    compiled = id_query.compile(dialect=session.get_bind().dialect)
    params = sorted((name, repr(value)) for name, value in compiled.params.items())
    return hashlib.sha256(f"{compiled}|{params}".encode()).hexdigest()


async def _ordered_ids(
    session: AsyncSession, project_id: uuid.UUID, id_query: Select[Any], *, fresh: bool
) -> list[uuid.UUID]:
    key = cache.key_health_sort(project_id, _filter_digest(session, id_query))
    if not fresh:
        cached = await cache.get_json(key)
        if isinstance(cached, list):
            try:
                return [uuid.UUID(str(value)) for value in cached]
            except ValueError:
                await cache.delete(key)
    ordered_ids = await event_health_service.health_sorted_ids(session, project_id, id_query)
    await cache.set_json(
        key, [str(event_id) for event_id in ordered_ids], HEALTH_SORT_CACHE_TTL_SECONDS
    )
    return ordered_ids


async def order_page_by_health(
    session: AsyncSession,
    project_id: uuid.UUID,
    filtered_query: Select[Any],
    offset: int,
    limit: int,
) -> list[Event]:
    """One catalog page of ``filtered_query`` ordered least healthy first."""
    id_query = select(Event.id, Event.name)
    if filtered_query.whereclause is not None:
        id_query = id_query.where(filtered_query.whereclause)
    ordered_ids = await _ordered_ids(session, project_id, id_query, fresh=offset == 0)
    page = ordered_ids[offset : offset + limit]
    if not page:
        return []
    result = await session.execute(
        select(Event).where(Event.id.in_(page)).options(noload(Event.event_type))
    )
    by_id = {event.id: event for event in result.scalars().all()}
    return [by_id[event_id] for event_id in page if event_id in by_id]
