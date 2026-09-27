"""Lightweight Redis cache wrapper for read-heavy endpoints.

Design choices:
- Graceful degradation: if Redis is down or ``REDIS_URL`` is empty, every cache
  call is a no-op miss. Callers never see a Redis exception, they see an empty
  cache. This means the app still works with Redis off.
- JSON codec by default (Pydantic-dumpable payloads); callers pass a dumped
  ``str`` or dict and get a ``dict``/``list``/``None`` back.
- Namespaced keys: callers always pass a full key built by a ``key_*`` helper below.
  Invalidation helpers use ``delete_prefix`` which scans via SCAN (not KEYS)
  so it's safe for production-sized keyspaces.

Testing:
- Unit tests (sqlite fixture) do NOT use Redis. Leave ``redis_url`` empty in
  the test Settings and calls become no-ops.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

try:
    import redis as redis_sync
    import redis.asyncio as redis_asyncio
except ImportError:  # pragma: no cover
    redis_asyncio = None  # type: ignore[assignment]
    redis_sync = None  # type: ignore[assignment]

from tripl.config import settings

logger = logging.getLogger(__name__)

_client: redis_asyncio.Redis | None = None
_client_failed: bool = False

# Separate client for the realtime pub/sub bus (see ``get_async_pubsub_client``).
_pubsub_client: redis_asyncio.Redis | None = None
_pubsub_client_failed: bool = False


def _get_client() -> redis_asyncio.Redis | None:
    global _client, _client_failed
    if _client_failed or not settings.redis_url or redis_asyncio is None:
        return None
    if _client is None:
        try:
            _client = redis_asyncio.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis client init failed, disabling cache: %s", exc)
            _client_failed = True
            return None
    return _client


def _get_pubsub_client() -> redis_asyncio.Redis | None:
    """Async client dedicated to the realtime pub/sub bus.

    Deliberately WITHOUT ``socket_timeout``. Pub/sub does long *blocking* reads
    while waiting for the next published event; a ``socket_timeout`` (right for
    short cache GET/SET on the shared client) would make an idle read raise
    ``redis.TimeoutError`` every timeout window and tear the SSE stream down. The
    subscriber bounds each read itself with an explicit ``get_message(timeout=…)``,
    so only a connect timeout is needed here.
    """
    global _pubsub_client, _pubsub_client_failed
    if _pubsub_client_failed or not settings.redis_url or redis_asyncio is None:
        return None
    if _pubsub_client is None:
        try:
            _pubsub_client = redis_asyncio.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=1.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis pubsub client init failed, disabling realtime: %s", exc)
            _pubsub_client_failed = True
            return None
    return _pubsub_client


async def get_json(key: str) -> Any | None:
    """Return the cached JSON value at ``key``, or ``None`` on miss/error."""
    client = _get_client()
    if client is None:
        return None
    try:
        raw = await client.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis GET %s failed: %s", key, exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.warning("redis GET %s: malformed JSON, dropping: %s", key, exc)
        await delete(key)
        return None


async def set_json(key: str, value: Any, ttl_seconds: int) -> None:
    """Store ``value`` as JSON at ``key`` with TTL. Swallows errors."""
    client = _get_client()
    if client is None:
        return
    try:
        payload = json.dumps(value, default=str, separators=(",", ":"))
        await client.set(key, payload, ex=ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis SET %s failed: %s", key, exc)


async def delete(*keys: str) -> None:
    """Delete one or more exact keys. No-op on failure/missing keys."""
    if not keys:
        return
    client = _get_client()
    if client is None:
        return
    try:
        await client.delete(*keys)
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis DEL %s failed: %s", keys, exc)


async def delete_prefix(prefix: str) -> None:
    """Delete all keys starting with ``prefix`` via SCAN + DEL (safe for prod).

    Avoids ``KEYS`` which is O(n) and blocking on large keyspaces.
    """
    client = _get_client()
    if client is None:
        return
    try:
        async for key in client.scan_iter(match=f"{prefix}*", count=500):
            await client.delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis SCAN+DEL %s* failed: %s", prefix, exc)


# ── Sync helpers (for Celery workers — don't bridge back to asyncio) ────

_sync_client: redis_sync.Redis | None = None
_sync_client_failed: bool = False


def _get_sync_client() -> redis_sync.Redis | None:
    global _sync_client, _sync_client_failed
    if _sync_client_failed or not settings.redis_url or redis_sync is None:
        return None
    if _sync_client is None:
        try:
            _sync_client = redis_sync.Redis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis sync client init failed, disabling cache: %s", exc)
            _sync_client_failed = True
            return None
    return _sync_client


def sync_delete_prefix(prefix: str) -> None:
    """Sync variant of :func:`delete_prefix` for Celery workers."""
    client = _get_sync_client()
    if client is None:
        return
    try:
        for key in client.scan_iter(match=f"{prefix}*", count=500):
            client.delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis sync SCAN+DEL %s* failed: %s", prefix, exc)


# ── Shared client accessors ──────────────────────────────────────────────
# The realtime pub/sub bus (``tripl.realtime``) reuses these same lazily-built,
# gracefully-degrading clients so it shares one connection style with the cache
# (and is a no-op when ``REDIS_URL`` is empty — tests). Both return ``None`` when
# Redis is unavailable so callers stay non-blocking.


def get_async_client() -> redis_asyncio.Redis | None:
    """Return the shared async Redis client, or ``None`` when Redis is off."""
    return _get_client()


def get_async_pubsub_client() -> redis_asyncio.Redis | None:
    """Return the shared async pub/sub client (no ``socket_timeout``), or ``None``."""
    return _get_pubsub_client()


def get_sync_client() -> redis_sync.Redis | None:
    """Return the shared sync Redis client, or ``None`` when Redis is off."""
    return _get_sync_client()


async def close() -> None:
    """Close the shared clients — call from FastAPI shutdown if needed."""
    global _client, _pubsub_client
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None
    if _pubsub_client is not None:
        try:
            await _pubsub_client.aclose()
        finally:
            _pubsub_client = None


# ── Key-schema conventions ───────────────────────────────────────────────
# Put all cache keys through these helpers so invalidation prefixes stay
# aligned with reads. Never hand-roll a key at a call site.
#
# Project-scoped keys are built from the project's id, never its slug: a slug
# names a project only inside its organization (F20), so two organizations may
# each own a project ``web`` and must never share a cache entry. Instance-level
# lists are keyed by organization for the same reason.

# Project-scoped key families (the second segment of every key).
_SIGNALS = "signals"
_EVENT_TYPES = "event_types"
_META_FIELDS = "meta_fields"
_HEALTH = "health"


def _project_prefix(family: str, project_id: uuid.UUID) -> str:
    return f"tripl:{family}:{project_id}:"


def key_projects_list(org_id: uuid.UUID) -> str:
    """``GET /projects`` for one organization."""
    return f"tripl:projects:{org_id}:list"


def key_signals_all(project_id: uuid.UUID) -> str:
    return f"{_project_prefix(_SIGNALS, project_id)}all"


def key_signals_all_expanded(project_id: uuid.UUID) -> str:
    """Expanded AnomaliesPage variant: includes per-event scope and keeps
    incident children (tagged) instead of collapsing them. Cached separately
    from :func:`key_signals_all` so the top-bar/overview/events callers keep the
    smaller collapsed payload."""
    return f"{_project_prefix(_SIGNALS, project_id)}all:expanded"


def key_data_sources_list(org_id: uuid.UUID) -> str:
    """``GET /data-sources`` as seen from one organization."""
    return f"tripl:data_sources:{org_id}:list"


def key_event_types_list(project_id: uuid.UUID) -> str:
    return f"{_project_prefix(_EVENT_TYPES, project_id)}list"


def key_meta_fields_list(project_id: uuid.UUID) -> str:
    return f"{_project_prefix(_META_FIELDS, project_id)}list"


def prefix_projects() -> str:
    """Every organization's project list."""
    return "tripl:projects:"


def prefix_signals(project_id: uuid.UUID | None = None) -> str:
    return _project_prefix(_SIGNALS, project_id) if project_id is not None else "tripl:signals:"


def prefix_data_sources() -> str:
    """Every organization's data-source list."""
    return "tripl:data_sources:"


def prefix_event_types(project_id: uuid.UUID | None = None) -> str:
    if project_id is None:
        return "tripl:event_types:"
    return _project_prefix(_EVENT_TYPES, project_id)


def prefix_meta_fields(project_id: uuid.UUID | None = None) -> str:
    if project_id is None:
        return "tripl:meta_fields:"
    return _project_prefix(_META_FIELDS, project_id)


def key_project_health(project_id: uuid.UUID, trend_days: int) -> str:
    """``GET /projects/{slug}/health`` (F15): short-TTL, never invalidated."""
    return f"{_project_prefix(_HEALTH, project_id)}project:{trend_days}"


def key_health_sort(project_id: uuid.UUID, filter_digest: str) -> str:
    """Catalog ``order_by=health`` id order (F15) per project and filter; short TTL."""
    return f"tripl:health_sort:{project_id}:{filter_digest}"


def prefix_health(project_id: uuid.UUID | None = None) -> str:
    return _project_prefix(_HEALTH, project_id) if project_id is not None else "tripl:health:"
