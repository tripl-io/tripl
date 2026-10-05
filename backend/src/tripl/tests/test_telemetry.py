"""Opt-in anonymous usage telemetry (``telemetry_service``).

Off by default; on, one ping a day with nothing that names anyone, counts only
as ranges, and the last one kept for the operator to read. A public demo
sends nothing, and a receiver that is down changes nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.config import settings
from tripl.models.app_setting import AppSetting
from tripl.services import safe_http, telemetry_service
from tripl.tests._platform_world import build_world
from tripl.tests._tenancy import use_public_demo
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.celery_app import celery_app

pytestmark = pytest.mark.asyncio

STATUS = "/api/v1/platform/settings/telemetry"


class Receiver:
    def __init__(self) -> None:
        self.pings: list[tuple[str, dict[str, Any]]] = []
        self.up = True

    def post(self, endpoint: str, payload: dict[str, Any]) -> bool:
        self.pings.append((endpoint, payload))
        return self.up


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> Receiver:
    fake = Receiver()
    monkeypatch.setattr(telemetry_service, "post", fake.post)
    return fake


async def _send(now: datetime | None = None) -> dict[str, Any]:
    async with TestSessionLocal() as session:
        return await telemetry_service.send(session, now)


async def _rows() -> list[AppSetting]:
    async with TestSessionLocal() as session:
        return list(await session.scalars(select(AppSetting).where(AppSetting.key == "telemetry")))


def test_counts_are_ranges() -> None:
    assert [telemetry_service.bucket(n) for n in (0, 1, 10, 11, 100, 101, 1000, 5000)] == [
        "0", "1-10", "1-10", "11-100", "11-100", "101-1000", "1000+", "1000+",
    ]  # fmt: skip


async def test_off_by_default_nothing_is_sent_or_stored(receiver: Receiver) -> None:
    assert settings.telemetry_enabled is False
    assert await _send() == {"sent": False, "reason": "disabled"}
    assert receiver.pings == []
    assert await _rows() == []


async def test_a_ping_names_nobody_and_is_kept_for_the_operator(
    client: AsyncClient, receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "telemetry_enabled", True)
    created = await client.post("/api/v1/projects", json={"name": "Secret shop", "slug": "secret"})
    assert created.status_code == 201, created.text
    first = datetime(2026, 10, 1, tzinfo=UTC)

    assert await _send(first) == {"sent": True}
    assert await _send(first + timedelta(days=3)) == {"sent": True}

    (endpoint, payload), (_, later) = receiver.pings
    assert endpoint == "https://telemetry.tripl.io/v1/ping"
    assert set(payload) == {
        "schema", "instance_id", "version", "edition", "deployment_mode",
        "warehouse_engines", "projects", "event_types", "users", "scans_last_day",
        "days_since_install",
    }  # fmt: skip
    assert payload["edition"] == "community"
    assert payload["projects"] == "1-10"
    assert payload["users"] == "1-10"
    assert payload["days_since_install"] == 0
    assert later["instance_id"] == payload["instance_id"]
    assert later["days_since_install"] == 3
    text = json.dumps(payload)
    for private in ("Secret shop", "secret", "test@example.com", "example.com"):
        assert private not in text

    status = (await client.get(STATUS)).json()
    assert status["enabled"] is True
    assert status["instance_id"] == payload["instance_id"]
    assert status["last_payload"] == later
    assert status["last_delivered"] is True


async def test_a_receiver_that_is_down_changes_nothing(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "telemetry_enabled", True)
    receiver.up = False
    assert await _send() == {"sent": False}
    (row,) = await _rows()
    assert row.value["last_delivered"] is False


def test_posting_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(*_args: object, **_kwargs: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(safe_http, "send", down)
    assert telemetry_service.post("https://telemetry.example", {"version": "1"}) is False


async def test_a_public_demo_sends_nothing(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "telemetry_enabled", True)
    use_public_demo(monkeypatch)
    assert await _send() == {"sent": False, "reason": "public demo"}
    assert receiver.pings == []


async def test_only_a_platform_admin_reads_it() -> None:
    world = await build_world()
    try:
        assert (await world.bob.get(STATUS)).status_code == 403
        status = (await world.operator.get(STATUS)).json()
        assert status == {
            "enabled": False,
            "reason": "disabled",
            "endpoint": "https://telemetry.tripl.io/v1/ping",
            "instance_id": None,
            "last_attempt_at": None,
            "last_delivered": None,
            "last_payload": None,
        }
    finally:
        await world.close()


def test_it_runs_daily() -> None:
    entry = celery_app.conf.beat_schedule["send-telemetry"]
    assert entry["task"] == "tripl.worker.tasks.telemetry.send_telemetry"
    assert entry["schedule"].hour == {7}
