"""Project-scoped one-way realtime bus.

A completed scan / metric collection / demo tick publishes a small named event to
a per-project Redis channel; the SSE endpoint (``GET /projects/{slug}/events/stream``)
resolves the slug to the project's id within the request's organization,
subscribes to that channel and relays events to the browser, which invalidates the
affected React-Query keys. This replaces most component-local polling; adaptive
polling remains a resilient fallback.

Design choices mirror :mod:`tripl.cache`:

- **Graceful degradation.** Every publish is a no-op when ``REDIS_URL`` is empty
  or Redis is down (tests, single-container dev without Redis). The SSE endpoint
  still connects and heartbeats in that case — clients see ``backend="degraded"``
  on the ``hello`` event and keep polling.
- **One-way only.** Server → client. There is no client → server path over the
  stream.
- **Publish after commit.** Callers publish only once the state change is
  committed, so a subscriber never sees an event for uncommitted state.
- **Keyed by project id.** Channel, replay buffer, sequence and epoch keys are
  built from the project's id, never its slug: a slug names a project only inside
  its organization (F20), so two organizations' ``web`` projects never share a
  stream.
- **Bounded buffering + monotonic ids.** Each project channel keeps a capped
  ring buffer (``LPUSH`` + ``LTRIM``) keyed by a per-project ``INCR`` sequence so
  a reconnecting client can replay events it missed via the ``Last-Event-ID``
  cursor. Invalidation is idempotent, so a duplicated replay is harmless.

Testing: with Redis off the pub/sub calls no-op. The SSE generator
(:func:`sse_response_stream`) takes an injectable async message iterator, so
delivery + heartbeat + degraded behaviour are unit-testable without a live Redis.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from tripl import cache

try:
    from redis.exceptions import RedisError
except ImportError:  # pragma: no cover - redis is a hard dependency in practice

    class RedisError(Exception):  # type: ignore[no-redef]
        pass


logger = logging.getLogger(__name__)

# ── Event types (SSE ``event:`` names) ───────────────────────────────────
EVENT_HELLO = "hello"
EVENT_SCAN_JOB_UPDATED = "scan_job.updated"
EVENT_METRIC_COLLECTION_UPDATED = "metric_collection.updated"
EVENT_ACTIVITY_CREATED = "activity.created"
EVENT_SIGNALS_UPDATED = "signals.updated"
EVENT_PROJECT_SUMMARY_UPDATED = "project_summary.updated"

# All publishable event types (``hello`` is synthesised by the endpoint, not
# published), kept in one place so the endpoint and tests agree.
PUBLISHABLE_EVENT_TYPES = (
    EVENT_SCAN_JOB_UPDATED,
    EVENT_METRIC_COLLECTION_UPDATED,
    EVENT_ACTIVITY_CREATED,
    EVENT_SIGNALS_UPDATED,
    EVENT_PROJECT_SUMMARY_UPDATED,
)

# Seconds between heartbeat comments — keeps proxies/load balancers from culling
# an idle stream and lets the endpoint notice a disconnected client.
HEARTBEAT_SECONDS = 15.0
# Per-project replay buffer depth. Small: reconnect replay is a nicety on top of
# idempotent invalidation + the polling fallback, not a delivery guarantee.
BUFFER_SIZE = 50

_CHANNEL_PREFIX = "tripl:events:"


def channel(project_id: uuid.UUID) -> str:
    """Redis pub/sub channel for a project's event stream."""
    return f"{_CHANNEL_PREFIX}{project_id}"


def _buffer_key(project_id: uuid.UUID) -> str:
    return f"{channel(project_id)}:buffer"


def _seq_key(project_id: uuid.UUID) -> str:
    return f"{channel(project_id)}:seq"


def _epoch_key(project_id: uuid.UUID) -> str:
    return f"{channel(project_id)}:epoch"


async def async_drop_project_keys(project_id: uuid.UUID) -> None:
    """Delete a purged project's sequence, epoch and replay ring (async).

    Called after a project row is gone for good (delete, demo reset): its id is
    never reused, so nothing would ever read or overwrite these keys again.
    No-op when Redis is off; never raises.
    """
    client = cache.get_async_client()
    if client is None:
        return
    try:
        await client.delete(_seq_key(project_id), _epoch_key(project_id), _buffer_key(project_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("realtime key cleanup %s failed: %s", project_id, exc)


def backend_available() -> bool:
    """Whether the async Redis client is live (pub/sub can actually deliver)."""
    return cache.get_async_client() is not None


def _envelope(seq: int, event_type: str, slug: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": seq,
        "type": event_type,
        "data": {"project_slug": slug, **payload},
    }


# ── Publish (after-commit hooks) ─────────────────────────────────────────


def publish_project_event(
    project_id: uuid.UUID, slug: str, event_type: str, payload: dict[str, Any]
) -> None:
    """Publish an event to a project's channel (SYNC — Celery workers).

    ``project_id`` keys the channel, ring and sequence; ``slug`` is only the
    envelope's ``project_slug``.
    No-op when Redis is off. Never raises: a realtime failure must not fail the
    task that produced the (already-committed) state change.
    """
    client = cache.get_sync_client()
    if client is None:
        return
    try:
        seq = int(client.incr(_seq_key(project_id)))
        raw = json.dumps(_envelope(seq, event_type, slug, payload), default=str)
        client.lpush(_buffer_key(project_id), raw)
        client.ltrim(_buffer_key(project_id), 0, BUFFER_SIZE - 1)
        client.publish(channel(project_id), raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("realtime publish %s/%s failed: %s", project_id, event_type, exc)


async def async_publish_project_event(
    project_id: uuid.UUID, slug: str, event_type: str, payload: dict[str, Any]
) -> None:
    """Async variant of :func:`publish_project_event` (FastAPI request path)."""
    client = cache.get_async_client()
    if client is None:
        return
    try:
        seq = int(await client.incr(_seq_key(project_id)))
        raw = json.dumps(_envelope(seq, event_type, slug, payload), default=str)
        await client.lpush(_buffer_key(project_id), raw)
        await client.ltrim(_buffer_key(project_id), 0, BUFFER_SIZE - 1)
        await client.publish(channel(project_id), raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("realtime async publish %s/%s failed: %s", project_id, event_type, exc)


@dataclass(frozen=True)
class ResumePoint:
    """Where a (re)connecting stream starts, read in ONE Redis transaction.

    ``seq`` is the project's sequence number and ``epoch`` the identity of the
    sequence itself: it is created with the counter's first read and lost with
    it, so a Redis restart without persistence shows up as a new epoch even once
    the restarted sequence has climbed back past the client's cursor. ``replay``
    is the buffered events past the cursor, ascending. ``seq`` and ``epoch`` are
    ``None`` when they could not be read — the client must then resync.
    """

    seq: int | None
    epoch: str | None
    replay: list[dict[str, Any]]


def _parse_replay(raw_items: Sequence[Any], after_id: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw in raw_items:  # newest → oldest (LPUSH order)
        try:
            envelope = json.loads(raw)
        except ValueError, TypeError:
            continue
        if isinstance(envelope, dict) and int(envelope.get("id", 0)) > after_id:
            events.append(envelope)
    events.reverse()  # ascending by id
    return events


def _decode(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


async def read_resume_point(project_id: uuid.UUID, after_id: int | None) -> ResumePoint:
    """The sequence number, its epoch and the replay past ``after_id``, atomically.

    One ``MULTI``: the sequence and the ring are read at the same instant, so no
    event published in between can trim a missed id out of the ring after the
    sequence number has promised it. A failed read reports
    ``seq=None`` rather than a sequence number with an empty replay, which the
    client would take for "nothing missed".
    """
    client = cache.get_async_client()
    if client is None:
        return ResumePoint(seq=None, epoch=None, replay=[])
    try:
        async with client.pipeline(transaction=True) as pipe:
            pipe.setnx(_epoch_key(project_id), uuid.uuid4().hex)
            pipe.get(_seq_key(project_id))
            pipe.get(_epoch_key(project_id))
            pipe.lrange(_buffer_key(project_id), 0, BUFFER_SIZE - 1)
            _created, raw_seq, raw_epoch, raw_items = await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("realtime resume read %s failed: %s", project_id, exc)
        return ResumePoint(seq=None, epoch=None, replay=[])
    seq = int(raw_seq) if raw_seq is not None else 0
    replay = _parse_replay(raw_items or [], after_id) if after_id is not None else []
    return ResumePoint(seq=seq, epoch=_decode(raw_epoch), replay=replay)


async def replay_buffered_events(
    project_id: uuid.UUID, after_id: int | None
) -> list[dict[str, Any]]:
    """Buffered events with ``id > after_id`` in ascending order (reconnect replay).

    Returns ``[]`` when Redis is off, no cursor was supplied, or nothing is
    buffered past the cursor.
    """
    if after_id is None:
        return []
    return (await read_resume_point(project_id, after_id)).replay


def hello_payload(
    slug: str, *, backend: str, seq: int | None, epoch: str | None = None
) -> dict[str, Any]:
    """The ``hello`` event body.

    ``seq`` is the project's sequence number when the stream opened, ``epoch``
    the identity of that sequence, and ``buffer_size`` how many events the
    replay ring holds. A reconnecting client compares them with what it saw: a
    gap the ring covers, in the same epoch, arrives as replay right after this
    event, and the client still checks those ids arrive without a hole. Anything
    else — a wider gap, a new epoch, a sequence below its cursor, ``seq`` null —
    calls for refetching everything.
    """
    return {
        "project_slug": slug,
        "backend": backend,
        "seq": seq,
        "epoch": epoch,
        "buffer_size": BUFFER_SIZE,
    }


async def redis_message_iterator(
    project_id: uuid.UUID, *, poll_timeout: float = HEARTBEAT_SECONDS
) -> AsyncIterator[dict[str, Any] | None]:
    """Yield live envelope dicts for a project's channel, plus ``None`` heartbeats.

    Reads via the dedicated pub/sub client (no ``socket_timeout``) and bounds each
    read with ``get_message(timeout=poll_timeout)``. An idle poll yields ``None``
    (a heartbeat tick, so the caller keeps the stream open) and a published
    message yields its envelope. A Redis error ends the iterator *cleanly* — the
    endpoint closes the stream and the browser reconnects — rather than raising
    and tearing the response down mid-flight (which the edge surfaces as
    ``ERR_HTTP2_PROTOCOL_ERROR`` / ``ERR_QUIC_PROTOCOL_ERROR``). An empty async
    generator when Redis is off.
    """
    client = cache.get_async_pubsub_client()
    if client is None:
        return
    pubsub = client.pubsub()
    try:
        try:
            await pubsub.subscribe(channel(project_id))
        except RedisError as exc:
            logger.warning("realtime subscribe %s failed: %s", project_id, exc)
            return
        while True:
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=poll_timeout
                )
            except RedisError as exc:
                logger.warning("realtime read %s failed: %s", project_id, exc)
                return
            if message is None:
                yield None  # idle poll — surfaces as a heartbeat, keeps stream open
                continue
            data = message.get("data")
            try:
                envelope = json.loads(data)
            except ValueError, TypeError:
                continue
            if isinstance(envelope, dict):
                yield envelope
    finally:
        try:
            await pubsub.unsubscribe(channel(project_id))
            await pubsub.aclose()  # type: ignore[no-untyped-call]
        except Exception:  # noqa: BLE001
            pass


@asynccontextmanager
async def subscribed_messages(
    project_id: uuid.UUID,
) -> AsyncIterator[AsyncIterator[dict[str, Any] | None] | None]:
    """Subscribe before reading replay; yield None if Redis cannot be reached."""
    client = cache.get_async_pubsub_client()
    if client is None:
        yield None
        return
    pubsub = client.pubsub()
    try:
        try:
            await pubsub.subscribe(channel(project_id))
        except RedisError as exc:
            logger.warning("realtime subscribe %s failed: %s", project_id, exc)
            yield None
            return

        async def messages() -> AsyncIterator[dict[str, Any] | None]:
            while True:
                try:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=HEARTBEAT_SECONDS
                    )
                except RedisError as exc:
                    logger.warning("realtime read %s failed: %s", project_id, exc)
                    return
                if message is None:
                    yield None
                    continue
                try:
                    envelope = json.loads(message.get("data"))
                except ValueError, TypeError:
                    continue
                if isinstance(envelope, dict):
                    yield envelope

        yield messages()
    finally:
        try:
            await pubsub.unsubscribe(channel(project_id))
            await pubsub.aclose()  # type: ignore[no-untyped-call]
        except Exception:  # noqa: BLE001
            logger.warning("realtime subscription cleanup failed for %s", project_id, exc_info=True)


async def project_response_stream(
    *,
    project_id: uuid.UUID,
    slug: str,
    last_event_id: int | None,
    is_disconnected: Callable[[], Awaitable[bool]],
    max_messages: int | None,
) -> AsyncIterator[str]:
    """The SSE body for one project: subscribe to its id channel, then greet.

    ``project_id`` keys the channel and replay; ``slug`` is only what the
    ``hello`` event reports, the name the client subscribed with.
    """
    async with subscribed_messages(project_id) as messages:
        # Read AFTER subscribing: every event past ``seq`` then reaches the client
        # live, by replay, or both (de-duplicated by id).
        resume = (
            await read_resume_point(project_id, last_event_id)
            if messages is not None
            else ResumePoint(seq=None, epoch=None, replay=[])
        )
        async for frame in sse_response_stream(
            hello_payload=hello_payload(
                slug,
                backend="redis" if messages else "degraded",
                seq=resume.seq,
                epoch=resume.epoch,
            ),
            replay=resume.replay,
            messages=messages,
            is_disconnected=is_disconnected,
            max_messages=max_messages,
        ):
            yield frame
        # A live subscription only ends when Redis fails. Stay connected in
        # degraded mode so the browser enables its polling fallback without a
        # rapid reconnect loop while Redis remains unavailable.
        if messages is not None and max_messages is None and not await is_disconnected():
            async for frame in sse_response_stream(
                hello_payload=hello_payload(slug, backend="degraded", seq=None),
                replay=[],
                messages=None,
                is_disconnected=is_disconnected,
            ):
                yield frame


# ── SSE formatting + core stream generator ───────────────────────────────


def format_sse_event(envelope: dict[str, Any]) -> str:
    """Render an envelope as an ``id:``/``event:``/``data:`` SSE frame."""
    event_type = str(envelope.get("type", "message"))
    event_id = envelope.get("id")
    data = json.dumps(envelope.get("data", {}), default=str)
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"


def format_sse_comment(text: str) -> str:
    """Render an SSE comment (heartbeat) — ignored by ``EventSource``."""
    return f": {text}\n\n"


async def sse_response_stream(
    *,
    hello_payload: dict[str, Any],
    replay: Sequence[dict[str, Any]],
    messages: AsyncIterator[dict[str, Any] | None] | None,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    max_messages: int | None = None,
) -> AsyncIterator[str]:
    """Core SSE generator: hello → replay → live events interleaved with heartbeats.

    ``messages`` is an async iterator that yields envelope dicts *and* ``None``
    heartbeat ticks (Redis, or a fake in tests); pass ``None`` for the Redis-off /
    degraded case (heartbeat only). The iterator bounds its own reads, so we never
    wrap ``__anext__`` in ``asyncio.wait_for`` — cancelling that on timeout would
    finalize the async generator and kill the stream. ``max_messages`` bounds the
    loop for tests. Never raises on a disconnected client — it returns, letting
    ``StreamingResponse`` finish cleanly.
    """
    yield format_sse_event({"id": 0, "type": EVENT_HELLO, "data": hello_payload})
    for envelope in replay:
        yield format_sse_event(envelope)

    if max_messages == 0:
        return

    delivered = 0

    if messages is None:
        # Degraded: keep the socket warm so the client stays connected and polls.
        while max_messages is None or delivered < max_messages:
            if is_disconnected is not None and await is_disconnected():
                return
            await asyncio.sleep(heartbeat_seconds)
            yield format_sse_comment("heartbeat")
            delivered += 1
        return

    async for item in messages:
        if is_disconnected is not None and await is_disconnected():
            return
        if item is None:
            yield format_sse_comment("heartbeat")
        else:
            yield format_sse_event(item)
        delivered += 1
        if max_messages is not None and delivered >= max_messages:
            return
