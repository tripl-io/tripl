"""Project-scoped SSE live-update stream.

Covers three layers without a live Redis (tests run with ``redis_url`` empty, so
the pub/sub bus degrades to a no-op):

* auth + project scope on ``GET /projects/{slug}/events/stream``;
* the pub/sub publish helpers no-op safely when Redis is off;
* the core SSE generator (``sse_response_stream``) — hello, replay, live delivery
  from a faked message iterator, and heartbeat-only degraded mode.

Heavy browser E2E (two clients on one job, reconnect, fallback) is deferred to
tripl-2su6.10.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient

from tripl import realtime


async def _create_project(client: AsyncClient, slug: str) -> None:
    resp = await client.post("/api/v1/projects", json={"name": slug.upper(), "slug": slug})
    assert resp.status_code == 201, resp.text


async def _issue_project_key(client: AsyncClient, *, project_slug: str) -> str:
    resp = await client.post(
        "/api/v1/me/api-keys",
        json={"name": "agent", "scope": "read", "project_slug": project_slug},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _fake_messages(items: list[dict | None]) -> AsyncIterator[dict | None]:
    for item in items:
        yield item


class _FakePubSub:
    """Scripted stand-in for ``redis.asyncio.client.PubSub`` (no live Redis).

    Each ``get_message`` pops the next scripted step: ``None`` = an idle poll,
    a ``dict`` = a delivered message, an ``Exception`` instance = raised.
    """

    def __init__(self, script: list) -> None:
        self._script = list(script)

    async def subscribe(self, *_channels: str) -> None:
        return None

    async def get_message(
        self, ignore_subscribe_messages: bool = False, timeout: float | None = None
    ):
        assert ignore_subscribe_messages is True
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    async def unsubscribe(self, *_channels: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _FakePubSubClient:
    def __init__(self, pubsub: _FakePubSub) -> None:
        self._pubsub = pubsub

    def pubsub(self) -> _FakePubSub:
        return self._pubsub


# ── Endpoint auth + scope ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_requires_auth(anon_client: AsyncClient) -> None:
    resp = await anon_client.get("/api/v1/projects/whatever/events/stream")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stream_unknown_project_is_404_for_session_user(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/projects/does-not-exist/events/stream")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_session_user_can_subscribe(client: AsyncClient) -> None:
    await _create_project(client, "stream-owner")
    # ``max_events=0`` greets and closes so the (buffering) test transport can read
    # a finite body; production leaves it unset for an open-ended stream.
    resp = await client.get(
        "/api/v1/projects/stream-owner/events/stream?max_events=0",
        headers={"Accept-Encoding": "br"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-accel-buffering"] == "no"
    assert "content-encoding" not in resp.headers
    # Connection-specific fields are forbidden in HTTP/2 and HTTP/3.  The edge
    # translates the origin's HTTP/1.1 response, so the application must not
    # emit one that can be forwarded as a malformed HTTP/3 response.
    assert "connection" not in resp.headers
    # The endpoint greets with a `hello` event announcing whether the realtime
    # backend is live; with Redis off (tests) it reports "degraded".
    assert "event: hello" in resp.text
    assert "degraded" in resp.text


@pytest.mark.asyncio
async def test_scoped_key_may_subscribe_to_its_own_project(client: AsyncClient) -> None:
    await _create_project(client, "scoped-own")
    token = await _issue_project_key(client, project_slug="scoped-own")
    resp = await client.get(
        "/api/v1/projects/scoped-own/events/stream?max_events=0", headers=_bearer(token)
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_scoped_key_cannot_subscribe_to_foreign_project(client: AsyncClient) -> None:
    await _create_project(client, "scoped-a")
    await _create_project(client, "scoped-b")
    token = await _issue_project_key(client, project_slug="scoped-a")
    resp = await client.get("/api/v1/projects/scoped-b/events/stream", headers=_bearer(token))
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Project not found"


# ── Pub/sub bus degrades to a no-op with Redis off ───────────────────────


@pytest.mark.asyncio
async def test_publish_is_noop_without_redis() -> None:
    # Must not raise and must be a no-op (redis_url empty in tests).
    project_id = uuid.uuid4()
    realtime.publish_project_event(
        project_id, "s", realtime.EVENT_SCAN_JOB_UPDATED, {"job_id": "1"}
    )
    await realtime.async_publish_project_event(project_id, "s", realtime.EVENT_SIGNALS_UPDATED, {})
    assert await realtime.replay_buffered_events(project_id, 5) == []
    assert realtime.backend_available() is False


# ── Core SSE generator ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generator_delivers_hello_then_live_event() -> None:
    envelope = {
        "id": 7,
        "type": realtime.EVENT_SCAN_JOB_UPDATED,
        "data": {"project_slug": "p", "status": "completed"},
    }
    chunks = [
        chunk
        async for chunk in realtime.sse_response_stream(
            hello_payload={"project_slug": "p", "backend": "redis"},
            replay=[],
            messages=_fake_messages([envelope]),
            heartbeat_seconds=5,
            max_messages=1,
        )
    ]
    body = "".join(chunks)
    assert "event: hello" in body
    assert f"event: {realtime.EVENT_SCAN_JOB_UPDATED}" in body
    assert '"status": "completed"' in body
    assert "id: 7" in body


@pytest.mark.asyncio
async def test_generator_replays_buffered_events_before_live() -> None:
    replayed = {
        "id": 3,
        "type": realtime.EVENT_SIGNALS_UPDATED,
        "data": {"project_slug": "p"},
    }
    chunks = [
        chunk
        async for chunk in realtime.sse_response_stream(
            hello_payload={"project_slug": "p", "backend": "redis"},
            replay=[replayed],
            messages=_fake_messages([]),
            heartbeat_seconds=5,
        )
    ]
    body = "".join(chunks)
    assert body.index("event: hello") < body.index(f"event: {realtime.EVENT_SIGNALS_UPDATED}")


@pytest.mark.asyncio
async def test_generator_degraded_mode_heartbeats_only() -> None:
    chunks = [
        chunk
        async for chunk in realtime.sse_response_stream(
            hello_payload={"project_slug": "p", "backend": "degraded"},
            replay=[],
            messages=None,
            heartbeat_seconds=0.01,
            max_messages=2,
        )
    ]
    body = "".join(chunks)
    assert "event: hello" in body
    assert ": heartbeat" in body
    # No live events are fabricated in degraded mode.
    assert f"event: {realtime.EVENT_SCAN_JOB_UPDATED}" not in body


@pytest.mark.asyncio
async def test_generator_treats_none_as_heartbeat_and_keeps_streaming() -> None:
    # Regression: an idle Redis poll surfaces as a ``None`` tick from the message
    # iterator. It must become a heartbeat comment and the stream must keep going
    # to deliver the next event — not raise (which tore down the SSE response and
    # produced ERR_HTTP2_PROTOCOL_ERROR / ERR_QUIC_PROTOCOL_ERROR at the edge).
    envelope = {
        "id": 9,
        "type": realtime.EVENT_ACTIVITY_CREATED,
        "data": {"project_slug": "p"},
    }
    chunks = [
        chunk
        async for chunk in realtime.sse_response_stream(
            hello_payload={"project_slug": "p", "backend": "redis"},
            replay=[],
            messages=_fake_messages([None, envelope]),
            max_messages=2,
        )
    ]
    body = "".join(chunks)
    assert ": heartbeat" in body
    assert f"event: {realtime.EVENT_ACTIVITY_CREATED}" in body
    assert body.index(": heartbeat") < body.index(f"event: {realtime.EVENT_ACTIVITY_CREATED}")


@pytest.mark.asyncio
async def test_redis_iterator_survives_idle_and_swallows_read_timeout(monkeypatch) -> None:
    # Regression: the pub/sub client must not carry a ``socket_timeout`` (it turns
    # an idle blocking read into ``redis.TimeoutError``). An idle poll yields a
    # ``None`` heartbeat tick; a later read timeout ends the iterator cleanly
    # instead of propagating and killing the stream mid-response.
    import json

    import redis

    raw = json.dumps(
        {"id": 1, "type": realtime.EVENT_SIGNALS_UPDATED, "data": {"project_slug": "p"}}
    )
    pubsub = _FakePubSub(
        [
            None,  # idle poll -> heartbeat tick
            {"type": "message", "data": raw},  # delivered event
            redis.exceptions.TimeoutError("Timeout reading from redis:6379"),
        ]
    )
    monkeypatch.setattr(
        realtime.cache, "get_async_pubsub_client", lambda: _FakePubSubClient(pubsub)
    )
    items = [item async for item in realtime.redis_message_iterator(uuid.uuid4())]
    assert items[0] is None
    assert any(isinstance(x, dict) and x.get("id") == 1 for x in items)
    # The read timeout did not escape — the comprehension completed.


def test_sse_formatting_helpers() -> None:
    frame = realtime.format_sse_event({"id": 42, "type": "x.y", "data": {"a": 1}})
    assert frame == 'id: 42\nevent: x.y\ndata: {"a": 1}\n\n'
    assert realtime.format_sse_comment("heartbeat") == ": heartbeat\n\n"
