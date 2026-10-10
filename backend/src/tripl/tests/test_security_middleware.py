from __future__ import annotations

import pytest
from httpx import AsyncClient
from starlette.datastructures import Headers

from tripl.config import settings
from tripl.middleware.rate_limit import (
    STATUS_RATE_LIMIT_PER_MINUTE,
    RateLimitExceeded,
    TokenBucketLimiter,
    _client_key,
    limiter_for,
    login_rate_limiter,
    register_rate_limiter,
    status_rate_limiter,
)
from tripl.middleware.security_headers import build_security_headers


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """Minimal stand-in exposing only what ``_client_key`` reads."""

    def __init__(self, *, client_host: str, forwarded_for: str | None = None) -> None:
        self.client = _FakeClient(client_host)
        raw = {"x-forwarded-for": forwarded_for} if forwarded_for is not None else {}
        self.headers = Headers(raw)


@pytest.mark.asyncio
async def test_security_headers_present_on_health(anon_client: AsyncClient) -> None:
    response = await anon_client.get("/health")

    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert "camera=()" in response.headers.get("permissions-policy", "")


@pytest.mark.asyncio
async def test_security_headers_on_a_normal_response_match_the_shared_builder(
    anon_client: AsyncClient,
) -> None:
    """Pins the middleware to ``build_security_headers``.

    The catch-all 500 handler in ``main`` has to attach the same set by hand,
    because ServerErrorMiddleware answers from outside this middleware.
    Asserting both ends against the one builder — here for a 200,
    in test_error_handling for a 500 — is what keeps the two from drifting.
    """
    response = await anon_client.get("/health")

    expected = build_security_headers()
    assert expected, "empty header set would make the comparison below vacuous"
    assert {name: response.headers.get(name) for name in expected} == expected


def test_build_security_headers_is_empty_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The enabled/disabled gate lives inside the builder rather than at each
    call site, so an instance with headers turned off cannot end up serving a
    500 that carries more than its 200s do."""
    monkeypatch.setattr(settings, "security_headers_enabled", False)
    assert build_security_headers() == {}


@pytest.mark.asyncio
async def test_request_id_is_echoed_when_provided(anon_client: AsyncClient) -> None:
    response = await anon_client.get("/health", headers={settings.request_id_header: "abc-123"})
    assert response.headers.get(settings.request_id_header) == "abc-123"


@pytest.mark.asyncio
async def test_request_id_is_generated_when_absent(anon_client: AsyncClient) -> None:
    response = await anon_client.get("/health")
    request_id = response.headers.get(settings.request_id_header)
    assert request_id
    # uuid4().hex is 32 hex chars; the middleware uses that path.
    assert len(request_id) == 32


@pytest.mark.asyncio
async def test_unsafe_request_id_is_replaced(anon_client: AsyncClient) -> None:
    response = await anon_client.get(
        "/health", headers={settings.request_id_header: "../../etc/passwd"}
    )
    assert response.headers.get(settings.request_id_header) != "../../etc/passwd"


def test_token_bucket_blocks_after_capacity() -> None:
    bucket = TokenBucketLimiter(capacity=2, per_seconds=60.0, name="t")
    bucket.acquire("k")
    bucket.acquire("k")
    with pytest.raises(RateLimitExceeded):
        bucket.acquire("k")


def test_token_bucket_separate_keys_have_separate_quota() -> None:
    bucket = TokenBucketLimiter(capacity=1, per_seconds=60.0, name="t")
    bucket.acquire("a")
    bucket.acquire("b")
    with pytest.raises(RateLimitExceeded):
        bucket.acquire("a")


def test_limiter_for_zero_disables_route() -> None:
    # A configured value of 0 means "no rate limiting on that route": the
    # limiter is disabled and acquire never raises, no matter how many calls.
    limiter = limiter_for(0, per_seconds=60.0, name="login")
    assert limiter.enabled is False
    for _ in range(100):
        limiter.acquire("same-key")  # must not raise


def test_limiter_for_positive_enforces_capacity() -> None:
    limiter = limiter_for(2, per_seconds=60.0, name="login")
    assert limiter.enabled is True
    limiter.acquire("k")
    limiter.acquire("k")
    with pytest.raises(RateLimitExceeded):
        limiter.acquire("k")


def test_client_key_ignores_spoofed_forwarded_for_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default-deny: a caller rotating X-Forwarded-For must not escape the bucket.
    # Every spoofed value has to collapse onto the real client.host key.
    # monkeypatch restores the setting on teardown even if the assert fails.
    monkeypatch.setattr(settings, "rate_limit_trust_forwarded_for", False)
    keys = {
        _client_key(
            _FakeRequest(client_host="10.0.0.1", forwarded_for=spoofed),  # type: ignore[arg-type]
            "login",
        )
        for spoofed in ("1.1.1.1", "2.2.2.2", "3.3.3.3, 4.4.4.4")
    }
    assert keys == {"login:10.0.0.1"}


def test_client_key_trusts_forwarded_for_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When explicitly enabled (behind a trusted proxy), distinct XFF client IPs
    # legitimately get distinct buckets.
    monkeypatch.setattr(settings, "rate_limit_trust_forwarded_for", True)
    key_a = _client_key(
        _FakeRequest(client_host="10.0.0.1", forwarded_for="1.1.1.1"),  # type: ignore[arg-type]
        "login",
    )
    key_b = _client_key(
        _FakeRequest(client_host="10.0.0.1", forwarded_for="2.2.2.2"),  # type: ignore[arg-type]
        "login",
    )
    assert key_a == "login:1.1.1.1"
    assert key_b == "login:2.2.2.2"


@pytest.mark.asyncio
async def test_login_endpoint_returns_429_when_limit_exceeded(
    anon_client: AsyncClient,
) -> None:
    settings.rate_limit_enabled = True
    login_rate_limiter.reset()
    try:
        # Exhaust the bucket. Wrong credentials so we don't need a registered
        # user — auth failures still consume tokens which is what we want.
        attempts = settings.rate_limit_login_per_minute + 1
        last_status = 200
        for _ in range(attempts):
            response = await anon_client.post(
                "/api/v1/auth/login",
                json={"email": "ghost@example.com", "password": "x"},
            )
            last_status = response.status_code
            if last_status == 429:
                break
        assert last_status == 429
    finally:
        settings.rate_limit_enabled = False
        login_rate_limiter.reset()
        register_rate_limiter.reset()


@pytest.mark.asyncio
async def test_status_endpoint_returns_429_without_consuming_login_quota(
    anon_client: AsyncClient,
) -> None:
    settings.rate_limit_enabled = True
    status_rate_limiter.reset()
    login_rate_limiter.reset()
    try:
        # Exhaust the status bucket. /auth/status is unauthenticated by design
        # (the login screen polls it), so this limiter is its only abuse guard.
        attempts = STATUS_RATE_LIMIT_PER_MINUTE + 1
        last_status = 200
        for _ in range(attempts):
            response = await anon_client.get("/api/v1/auth/status")
            last_status = response.status_code
            if last_status == 429:
                break
        assert last_status == 429

        # Separate bucket: exhausting /auth/status must not have consumed the
        # login limiter. The next login attempt is judged on credentials
        # (401 for a nonexistent user), not rejected with 429.
        login_response = await anon_client.post(
            "/api/v1/auth/login",
            json={"email": "ghost@example.com", "password": "wrong-password"},
        )
        assert login_response.status_code == 401
    finally:
        settings.rate_limit_enabled = False
        status_rate_limiter.reset()
        login_rate_limiter.reset()
