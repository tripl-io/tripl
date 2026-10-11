"""A crowd behind one address can load the sign-in page and sign in with a provider.

The public demo is reached from conference Wi-Fi and office NATs: many visitors
share one client address. The status and provider sign-in buckets are sized
for that, the status answer is cached briefly so the crowd does not reach the
database once per visitor, and an operator behind Cloudflare can key the
buckets on the header Cloudflare sets.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers
from starlette.requests import Request

from tripl.api.v1 import auth as auth_routes
from tripl.config import settings
from tripl.middleware import rate_limit
from tripl.middleware.rate_limit import (
    INVITATION_ACCEPT_RATE_LIMIT_PER_HOUR,
    SSO_RATE_LIMIT_PER_MINUTE,
    STATUS_RATE_LIMIT_PER_MINUTE,
    _client_key,
    invitation_accept_rate_limiter,
    register_rate_limiter,
    sso_rate_limiter,
    status_rate_limiter,
)
from tripl.services import auth_service

STATUS = "/api/v1/auth/status"


def _key(headers: dict[str, str]) -> str:
    request = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"), headers=Headers(headers))
    return _client_key(cast(Request, request), "sso")


def _fresh_rate_limit_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """A private copy of the module, built from the settings as they are now.

    Registered under its own name while it loads (its dataclass looks itself up
    there); the real module and its limiters are untouched.
    """
    spec = importlib.util.spec_from_file_location("_rate_limit_copy", rate_limit.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


# ── Limits ───────────────────────────────────────────────────────────────────


def test_status_takes_three_hundred_a_minute() -> None:
    assert STATUS_RATE_LIMIT_PER_MINUTE == 300
    assert status_rate_limiter.capacity == 300
    assert status_rate_limiter.per_seconds == 60.0


def test_invitation_accept_has_its_own_hourly_bucket() -> None:
    # Not sign-up's 3 an hour: a team invited together from one office
    # address all joins in the same hour.
    assert INVITATION_ACCEPT_RATE_LIMIT_PER_HOUR == 30
    assert invitation_accept_rate_limiter.capacity == 30
    assert invitation_accept_rate_limiter.per_seconds == 3600.0
    assert invitation_accept_rate_limiter.name != register_rate_limiter.name
    route = next(
        r
        for r in auth_routes.router.routes
        if isinstance(r, APIRoute) and r.path.endswith("/accept")
    )
    # enforce() closes over exactly its limiter.
    limiters = [
        inspect.getclosurevars(d.call).nonlocals.get("limiter")
        for d in route.dependant.dependencies
        if d.call is not None
    ]
    assert invitation_accept_rate_limiter in limiters
    assert register_rate_limiter not in limiters


def test_provider_sign_in_follows_the_setting() -> None:
    assert settings.rate_limit_sso_per_minute == 100
    assert settings.rate_limit_sso_per_minute == SSO_RATE_LIMIT_PER_MINUTE
    assert sso_rate_limiter.capacity == SSO_RATE_LIMIT_PER_MINUTE
    assert sso_rate_limiter.enabled


def test_password_limits_stay_where_they_were() -> None:
    assert settings.rate_limit_login_per_minute == 5
    assert settings.rate_limit_register_per_hour == 3


def test_a_configured_provider_limit_sizes_the_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_sso_per_minute", 7)
    module = _fresh_rate_limit_module(monkeypatch)
    assert module.SSO_RATE_LIMIT_PER_MINUTE == 7
    assert module.sso_rate_limiter.capacity == 7
    assert module.sso_rate_limiter.enabled


def test_zero_turns_the_provider_limit_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_sso_per_minute", 0)
    module = _fresh_rate_limit_module(monkeypatch)
    assert module.SSO_RATE_LIMIT_PER_MINUTE == 0
    assert not module.sso_rate_limiter.enabled
    for _ in range(50):
        module.sso_rate_limiter.acquire("sso:192.0.2.1")


# ── Client-IP header ─────────────────────────────────────────────────────────


def test_the_named_header_wins_when_proxies_are_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_trust_forwarded_for", True)
    monkeypatch.setattr(settings, "rate_limit_client_ip_header", "CF-Connecting-IP")
    headers = {
        "cf-connecting-ip": " 198.51.100.23 ",
        "x-real-ip": "172.17.0.1",
        "x-forwarded-for": "1.1.1.1, 172.17.0.1",
    }
    assert _key(headers) == "sso:198.51.100.23"


def test_without_the_named_header_the_old_order_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_trust_forwarded_for", True)
    monkeypatch.setattr(settings, "rate_limit_client_ip_header", "CF-Connecting-IP")
    assert _key({"x-real-ip": "192.0.2.9"}) == "sso:192.0.2.9"
    assert _key({"cf-connecting-ip": "  ", "x-real-ip": "192.0.2.9"}) == "sso:192.0.2.9"
    assert _key({"x-forwarded-for": "1.1.1.1, 203.0.113.7"}) == "sso:203.0.113.7"
    assert _key({}) == "sso:10.0.0.1"


def test_no_header_named_keeps_x_real_ip_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_trust_forwarded_for", True)
    monkeypatch.setattr(settings, "rate_limit_client_ip_header", "")
    headers = {"cf-connecting-ip": "198.51.100.23", "x-real-ip": "192.0.2.9"}
    assert _key(headers) == "sso:192.0.2.9"


def test_the_named_header_is_ignored_without_proxy_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_trust_forwarded_for", False)
    monkeypatch.setattr(settings, "rate_limit_client_ip_header", "CF-Connecting-IP")
    headers = {"cf-connecting-ip": "198.51.100.23", "x-real-ip": "192.0.2.9"}
    assert _key(headers) == "sso:10.0.0.1"


# ── Cached /auth/status ──────────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(auth_routes, "time", fake)
    return fake


@pytest.fixture
def counts(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """One entry per user count (the status handler's, and registration's own)."""
    seen: list[int] = []
    real = auth_service.has_any_users

    async def counting(session: AsyncSession) -> bool:
        seen.append(1)
        return await real(session)

    monkeypatch.setattr(auth_service, "has_any_users", counting)
    return seen


async def _register(client: AsyncClient, email: str) -> None:
    resp = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": "Password123!"}
    )
    assert resp.status_code == 201, resp.text


async def test_a_reload_within_the_window_reads_no_database(
    anon_client: AsyncClient, clock: _Clock, counts: list[int]
) -> None:
    await _register(anon_client, "owner@example.com")
    counts.clear()
    first = await anon_client.get(STATUS)
    clock.now += 4.9
    second = await anon_client.get(STATUS)
    assert first.json() == second.json()
    assert first.json()["has_users"] is True
    assert len(counts) == 1


async def test_the_answer_is_read_again_after_the_window(
    anon_client: AsyncClient, clock: _Clock, counts: list[int]
) -> None:
    await _register(anon_client, "owner@example.com")
    counts.clear()
    await anon_client.get(STATUS)
    clock.now += 5.0
    await anon_client.get(STATUS)
    assert len(counts) == 2


async def test_an_empty_instance_is_never_cached(
    anon_client: AsyncClient, clock: _Clock, counts: list[int]
) -> None:
    assert (await anon_client.get(STATUS)).json()["has_users"] is False
    await _register(anon_client, "owner@example.com")
    counts.clear()
    # Same instant: had the empty answer been kept, the first-account note
    # would still be on offer to the next visitor.
    assert (await anon_client.get(STATUS)).json()["has_users"] is True
    assert len(counts) == 1


async def test_a_reset_forgets_the_cached_answer(
    anon_client: AsyncClient, clock: _Clock, counts: list[int]
) -> None:
    await _register(anon_client, "owner@example.com")
    counts.clear()
    await anon_client.get(STATUS)
    auth_routes.reset_status_cache()
    await anon_client.get(STATUS)
    assert len(counts) == 2


async def test_settings_read_in_process_are_never_cached(
    anon_client: AsyncClient, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(anon_client, "owner@example.com")
    assert (await anon_client.get(STATUS)).json()["google_sign_in"] is False
    monkeypatch.setattr(settings, "google_client_id", "client-1")
    monkeypatch.setattr(settings, "google_client_secret", "secret-1")
    # Within the window: the cached part is reused, the provider flag is not.
    assert (await anon_client.get(STATUS)).json()["google_sign_in"] is True
