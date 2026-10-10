"""Who owns an event, as the health score's Documentation detail says it.

The score counts an owner on the event or at least one owner on its event type
(website/docs/use/health-score.md). The event page names the event's own owner
and, without one, the type's owners as the type's; the detail says the same
instead of a bare "owner set" for an event that has no owner of its own.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from tripl.models.event import Event
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.user import User
from tripl.services.health_score import EventHealthFacts, score_event
from tripl.tests.conftest import TestSessionLocal

_NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _facts(**overrides: object) -> EventHealthFacts:
    facts = EventHealthFacts(
        event_id=uuid.UUID(int=1),
        event_type_id=uuid.UUID(int=2),
        name="home_screen_view",
        status="draft",
        last_seen_at=_NOW - timedelta(days=1),
        has_description=True,
        has_owner=True,
    )
    return replace(facts, **overrides)  # type: ignore[arg-type]


def _documentation(facts: EventHealthFacts) -> Any:
    health = score_event(facts, _NOW)
    return next(c for c in health.components if c.key == "documentation")


def test_own_owner_reads_as_owner_set() -> None:
    part = _documentation(_facts())
    assert part.value == 1.0
    assert part.detail == "Description and owner set"


def test_type_owner_scores_the_same_and_says_where_it_comes_from() -> None:
    part = _documentation(_facts(owner_from_type=True))
    assert part.value == 1.0
    assert part.detail == "Description set; owner from event type"
    assert part.counts == {"has_description": 1, "has_owner": 1}


def test_missing_parts_are_named_whatever_the_owner_source() -> None:
    no_description = _documentation(_facts(has_description=False, owner_from_type=True))
    assert no_description.value == 0.5
    assert no_description.detail == "No description"
    nothing = _documentation(_facts(has_description=False, has_owner=False))
    assert nothing.detail == "No description, No owner"


async def _post(client: AsyncClient, url: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post(url, json=body)
    assert resp.status_code in (200, 201), resp.text
    data = resp.json()
    assert isinstance(data, dict)
    return data


async def _documentation_detail(client: AsyncClient, base: str, event_id: str) -> str:
    resp = await client.get(f"{base}/events/{event_id}/health")
    assert resp.status_code == 200, resp.text
    part = next(c for c in resp.json()["components"] if c["key"] == "documentation")
    assert part["value"] == 1.0
    return str(part["detail"])


@pytest.mark.asyncio
async def test_event_health_names_the_owner_source(client: AsyncClient) -> None:
    slug = "prelaunch-owner"
    await _post(client, "/api/v1/projects", {"name": slug, "slug": slug})
    base = f"/api/v1/projects/{slug}"
    event_type = await _post(client, f"{base}/event-types", {"name": "screen", "display_name": "S"})
    event = await _post(
        client,
        f"{base}/events",
        {
            "event_type_id": event_type["id"],
            "name": "home_screen_view",
            "description": "User lands on the home screen.",
        },
    )
    async with TestSessionLocal() as session:
        user = User(email="type-owner@example.com", name="Type Owner", password_hash="x")
        session.add(user)
        await session.flush()
        session.add(EventTypeOwner(event_type_id=uuid.UUID(event_type["id"]), user_id=user.id))
        await session.commit()
        user_id = user.id

    # Only the type's owners: counted, and named as the type's.
    assert (
        await _documentation_detail(client, base, event["id"])
        == "Description set; owner from event type"
    )

    # An owner of its own wins, whatever the type has.
    async with TestSessionLocal() as session:
        await session.execute(
            update(Event).where(Event.id == uuid.UUID(event["id"])).values(owner_id=user_id)
        )
        await session.commit()
    assert await _documentation_detail(client, base, event["id"]) == "Description and owner set"
