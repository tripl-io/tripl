"""The auth limiter shares one quota across workers through Redis.

The unit suite has no Redis, so the client is a stub that runs the bucket in
Python the way the Lua script does, keyed by the Redis key it is handed. Two
limiter instances stand in for two workers: with Redis they drain one bucket,
without it each keeps its own.
"""

from __future__ import annotations

from typing import Any

import pytest

from tripl import cache
from tripl.middleware import rate_limit
from tripl.middleware.rate_limit import RateLimitExceeded, TokenBucketLimiter

pytestmark = pytest.mark.asyncio


class _StubRedis:
    """Refill-free bucket per key: enough to tell shared state from local."""

    def __init__(self) -> None:
        self.tokens: dict[str, float] = {}
        self.calls: list[tuple[Any, ...]] = []

    async def eval(self, script: str, numkeys: int, key: str, *argv: str) -> str:
        assert script == rate_limit._TAKE_TOKEN_LUA
        assert numkeys == 1
        self.calls.append((key, *argv))
        capacity, rate = float(argv[0]), float(argv[1])
        left = self.tokens.get(key, capacity)
        if left < 1:
            return repr((1 - left) / rate)
        self.tokens[key] = left - 1
        return "0"


class _BrokenRedis:
    async def eval(self, *args: Any) -> str:
        raise ConnectionError("redis is down")


def _workers() -> tuple[TokenBucketLimiter, TokenBucketLimiter]:
    return (
        TokenBucketLimiter(capacity=2, per_seconds=60.0, name="login"),
        TokenBucketLimiter(capacity=2, per_seconds=60.0, name="login"),
    )


async def test_two_workers_draw_on_one_redis_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubRedis()
    monkeypatch.setattr(cache, "get_async_client", lambda: stub)
    worker_a, worker_b = _workers()

    await worker_a.acquire_shared("login:10.0.0.1")
    await worker_b.acquire_shared("login:10.0.0.1")
    with pytest.raises(RateLimitExceeded) as exc:
        await worker_a.acquire_shared("login:10.0.0.1")

    assert exc.value.retry_after_seconds == pytest.approx(30.0)
    assert {call[0] for call in stub.calls} == {"tripl:ratelimit:login:10.0.0.1"}
    # capacity, refill rate per second, and a TTL just past a full refill.
    assert stub.calls[0][1:] == ("2", repr(2 / 60.0), "61")


async def test_without_redis_each_worker_keeps_its_own_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cache, "get_async_client", lambda: None)
    worker_a, worker_b = _workers()

    for _ in range(2):
        await worker_a.acquire_shared("login:10.0.0.1")
        await worker_b.acquire_shared("login:10.0.0.1")
    with pytest.raises(RateLimitExceeded):
        await worker_a.acquire_shared("login:10.0.0.1")


async def test_a_redis_failure_falls_back_to_the_local_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cache, "get_async_client", lambda: _BrokenRedis())
    limiter = TokenBucketLimiter(capacity=1, per_seconds=60.0, name="login")

    await limiter.acquire_shared("login:10.0.0.1")
    with pytest.raises(RateLimitExceeded):
        await limiter.acquire_shared("login:10.0.0.1")


async def test_a_disabled_limiter_never_calls_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubRedis()
    monkeypatch.setattr(cache, "get_async_client", lambda: stub)
    limiter = TokenBucketLimiter(capacity=1, per_seconds=60.0, name="login", enabled=False)

    for _ in range(3):
        await limiter.acquire_shared("login:10.0.0.1")
    assert stub.calls == []
