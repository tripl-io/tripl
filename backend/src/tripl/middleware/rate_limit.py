"""In-process token-bucket rate limiter for auth endpoints.

Scope: protect login, registration, status, invitations, and password-reset
routes from abuse by a single source IP. Each limiter is keyed on the client
IP plus its limiter name; routes using the same limiter share a quota.

When ``REDIS_URL`` is set the buckets live in Redis, so every worker (and
every replica pointed at the same Redis) draws on one quota per client. Without
Redis — or while Redis is unreachable — each worker falls back to its own
in-memory bucket, which multiplies the effective limit by the worker count;
the limits are then defence in depth rather than an aggregate cap.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Request

from tripl import cache
from tripl.config import settings

logger = logging.getLogger(__name__)

# One atomic refill-and-take on a Redis hash, timed by the Redis clock so every
# worker agrees on "now". Returns the wait in seconds as a string (0 = taken);
# a string because Redis truncates a Lua number reply to an integer.
_TAKE_TOKEN_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
  tokens = capacity
  ts = now
end
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)
local wait = 0
if tokens < 1 then
  wait = (1 - tokens) / rate
else
  tokens = tokens - 1
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return tostring(wait)
"""


class RateLimitExceeded(Exception):
    """Raised when a caller exceeds the configured rate."""

    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(f"Rate limit exceeded; retry in {retry_after_seconds:.1f}s")
        self.retry_after_seconds = retry_after_seconds


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    """Refilling token bucket. ``capacity`` is the burst, ``per_seconds`` the refill window.

    A limiter built with ``enabled=False`` represents a route whose configured
    per-window count is 0 (rate limiting disabled for that route). Its
    :meth:`acquire` is a no-op, so the dependency wiring stays uniform whether
    or not the route is limited.
    """

    def __init__(
        self, *, capacity: int, per_seconds: float, name: str, enabled: bool = True
    ) -> None:
        if capacity <= 0 or per_seconds <= 0:
            raise ValueError("capacity and per_seconds must be > 0")
        self.capacity = capacity
        self.per_seconds = per_seconds
        self.name = name
        self.enabled = enabled
        self._rate = capacity / per_seconds
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        # Keep memory bounded — eviction is opportunistic on each call.
        self._max_keys = 10_000

    def acquire(self, key: str) -> None:
        """Consume one token for ``key``. Raises :class:`RateLimitExceeded` if empty.

        No-op when the limiter is disabled (route configured with a 0 limit).
        """
        if not self.enabled:
            return
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_keys:
                    self._evict_oldest_locked()
                bucket = _Bucket(tokens=self.capacity - 1, updated_at=now)
                self._buckets[key] = bucket
                return

            elapsed = now - bucket.updated_at
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self._rate)
            bucket.updated_at = now
            if bucket.tokens < 1.0:
                deficit = 1.0 - bucket.tokens
                retry_in = deficit / self._rate
                raise RateLimitExceeded(retry_after_seconds=retry_in)
            bucket.tokens -= 1.0

    def _evict_oldest_locked(self) -> None:
        oldest_key = min(self._buckets, key=lambda k: self._buckets[k].updated_at)
        del self._buckets[oldest_key]

    async def acquire_shared(self, key: str) -> None:
        """Consume one token for ``key`` from the Redis bucket all workers share.

        Falls back to this worker's in-memory bucket when Redis is not configured
        or the call fails: an outage must degrade the limit, not the login page.
        """
        if not self.enabled:
            return
        client = cache.get_async_client()
        if client is None:
            self.acquire(key)
            return
        # A full bucket refills in ``per_seconds``; past that the stored state
        # is indistinguishable from a fresh one, so it can expire.
        ttl = math.ceil(self.per_seconds) + 1
        try:
            reply = await client.eval(
                _TAKE_TOKEN_LUA,
                1,
                f"tripl:ratelimit:{key}",
                str(self.capacity),
                repr(self._rate),
                str(ttl),
            )
            wait = float(reply)
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis rate limit failed, using the in-memory bucket: %s", exc)
            self.acquire(key)
            return
        if wait > 0:
            raise RateLimitExceeded(retry_after_seconds=wait)

    def reset(self) -> None:
        """Drop all bucket state. Intended for tests."""
        with self._lock:
            self._buckets.clear()


def _client_key(request: Request, route: str) -> str:
    # By default ignore proxy-supplied client-IP headers: a raw X-Forwarded-For
    # is attacker-controlled, so honouring it lets a caller rotate the header per
    # request and land each one in a fresh bucket, fully bypassing the limit.
    # Only trust proxy headers when explicitly enabled (rate_limit_trust_-
    # forwarded_for), and even then only behind a proxy that *overwrites* them.
    #
    # When trust is enabled we prefer X-Real-IP: the shipped nginx config sets it
    # to $remote_addr on every request, so it carries exactly one value the
    # client cannot influence. We fall back to the leftmost X-Forwarded-For entry
    # (the original client as recorded by the trusted proxy) only when X-Real-IP
    # is absent.
    if settings.rate_limit_trust_forwarded_for:
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            return f"{route}:{real_ip}"
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return f"{route}:{forwarded.split(',')[0].strip()}"
    ip = request.client.host if request.client is not None else "unknown"
    return f"{route}:{ip}"


def _limiter_for(configured: int, *, per_seconds: float, name: str) -> TokenBucketLimiter:
    """Build a limiter for a configured per-window count.

    A configured value of ``0`` disables rate limiting on that route (per the
    ``config`` docstring); it is represented as a disabled limiter whose
    ``acquire`` is a no-op, so callers keep the same wiring either way.
    """
    enabled = configured > 0
    return TokenBucketLimiter(
        capacity=configured if enabled else 1,
        per_seconds=per_seconds,
        name=name,
        enabled=enabled,
    )


login_rate_limiter = _limiter_for(
    settings.rate_limit_login_per_minute, per_seconds=60.0, name="login"
)

register_rate_limiter = _limiter_for(
    settings.rate_limit_register_per_hour, per_seconds=3600.0, name="register"
)

# ``/auth/status`` is an unauthenticated read the login screen queries before
# anyone signs in, so it gets a modest fixed per-IP limit. A module constant
# rather than an env knob on purpose: the endpoint only guards a cheap COUNT(*)
# and there is no operational reason to tune it. It is a SEPARATE limiter with
# its own "status" key prefix, so probing /auth/status can never consume
# login/register quota (and vice versa).
STATUS_RATE_LIMIT_PER_MINUTE = 30

status_rate_limiter = _limiter_for(STATUS_RATE_LIMIT_PER_MINUTE, per_seconds=60.0, name="status")

# ``/auth/verify-email/request`` sends a mail through the operator's relay each
# time it is accepted, so it gets its own small hourly bucket: resending never
# consumes login/register quota, and a signed-in caller cannot turn the relay
# into a mail cannon. A module constant for the same reason as the status one.
VERIFY_EMAIL_RATE_LIMIT_PER_HOUR = 10

verify_email_rate_limiter = _limiter_for(
    VERIFY_EMAIL_RATE_LIMIT_PER_HOUR, per_seconds=3600.0, name="verify_email"
)


def enforce(limiter: TokenBucketLimiter) -> Callable[[Request], Awaitable[None]]:
    """FastAPI dependency that applies ``limiter`` to the inbound request."""

    async def dependency(request: Request) -> None:
        if not settings.rate_limit_enabled or not limiter.enabled:
            return
        key = _client_key(request, limiter.name)
        try:
            await limiter.acquire_shared(key)
        except RateLimitExceeded as exc:
            from fastapi import HTTPException

            retry_after = max(1, int(exc.retry_after_seconds + 0.999))
            raise HTTPException(
                status_code=429,
                detail="Too many requests; please retry shortly.",
                headers={"Retry-After": str(retry_after)},
            ) from exc

    return dependency
