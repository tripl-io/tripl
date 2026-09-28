"""The platform admin's read-only step-in (F20 PR14).

* opening one takes a reason and a TTL (5..240 minutes); it is refused for an
  organization the admin belongs to, and for one being deleted;
* while it is live the admin reads the organization as a ``member`` with
  project role ``viewer`` on every project, through the org-qualified API and
  ``/orgs/{org}``; every non-GET request there is 403 "Step-in is read-only",
  except the two read-shaped POSTs; owner/admin reads stay closed;
* it is audited in the target organization (start and end), where its owner
  reads it; ``/auth/me`` and ``GET /orgs`` show it for the banner;
* it ends when ended, when it expires, or when the platform-admin flag goes;
  a natural expiry is recorded (``platform.step_in_end`` ``{"expired": true}``)
  once, the first time resolution or a listing sees it; an API key never
  steps in.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update

from tripl.api.deps import STEP_IN_READ_HANDLERS, STEP_IN_READ_ONLY
from tripl.models.domain_enums import OrganizationStatus
from tripl.models.platform_step_in import PlatformStepIn
from tripl.models.user import User
from tripl.services import platform_console_service
from tripl.tests._platform_world import (
    API,
    GLOBEX,
    GLOBEX_ID,
    SHOP,
    World,
    audit_rows,
    build_world,
    new_client,
    set_org_status,
)
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_rbac import iter_api_routes

PLATFORM = f"{API}/platform"
ORG = f"{API}/orgs/{GLOBEX}"
PROJECT = f"{ORG}/projects/{SHOP}"


@pytest.fixture
async def world() -> AsyncIterator[World]:
    built = await build_world()
    yield built
    await built.close()


async def _step_in(world: World, **body: Any) -> dict[str, Any]:
    payload = {"reason": "Support ticket 1234", **body}
    resp = await world.operator.post(f"{PLATFORM}/orgs/{GLOBEX}/step-in", json=payload)
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data


# ── opening one ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"reason": ""},
        {"reason": "  "},
        {"reason": "x" * 501},
        {"reason": "nul\x00"},
        {"reason": "ok", "ttl_minutes": 4},
        {"reason": "ok", "ttl_minutes": 241},
        {"ttl_minutes": 60},
    ],
)
async def test_a_step_in_needs_a_reason_and_a_bounded_ttl(
    world: World, body: dict[str, Any]
) -> None:
    resp = await world.operator.post(f"{PLATFORM}/orgs/{GLOBEX}/step-in", json=body)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_opening_one_defaults_to_an_hour(world: World) -> None:
    before = datetime.now(UTC)
    body = await _step_in(world)
    assert body["org_slug"] == GLOBEX
    assert body["active"] is True
    expires = datetime.fromisoformat(body["expires_at"])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    assert timedelta(minutes=59) < expires - before <= timedelta(minutes=61)


@pytest.mark.asyncio
async def test_only_a_platform_admin_steps_in(world: World) -> None:
    resp = await world.alice.post(f"{PLATFORM}/orgs/{GLOBEX}/step-in", json={"reason": "x"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_no_step_in_where_you_are_a_member_or_into_nothing(world: World) -> None:
    member = await world.operator.post(f"{PLATFORM}/orgs/default/step-in", json={"reason": "x"})
    assert member.status_code == 409
    assert member.json()["detail"] == platform_console_service.STEP_IN_AS_MEMBER
    missing = await world.operator.post(f"{PLATFORM}/orgs/nope/step-in", json={"reason": "x"})
    assert missing.status_code == 404
    await set_org_status(GLOBEX_ID, OrganizationStatus.deleting)
    deleting = await world.operator.post(f"{PLATFORM}/orgs/{GLOBEX}/step-in", json={"reason": "x"})
    assert deleting.status_code == 409
    assert deleting.json()["detail"] == platform_console_service.ORG_BEING_DELETED


# ── what it reads ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_without_a_step_in_the_org_does_not_exist_for_the_admin(world: World) -> None:
    for path in (f"{ORG}/projects", PROJECT, ORG):
        resp = await world.operator.get(path)
        assert resp.status_code == 404, (path, resp.text)


@pytest.mark.asyncio
async def test_a_step_in_reads_the_org_as_a_viewer(world: World) -> None:
    await _step_in(world)

    projects = await world.operator.get(f"{ORG}/projects")
    assert projects.status_code == 200, projects.text
    listed = {p["slug"]: p for p in projects.json()}
    assert set(listed) == {SHOP}
    assert listed[SHOP]["can_mutate"] is False
    for path in (PROJECT, f"{PROJECT}/event-types", f"{PROJECT}/branches", f"{ORG}/members"):
        resp = await world.operator.get(path)
        assert resp.status_code == 200, (path, resp.text)

    org = await world.operator.get(ORG)
    assert org.status_code == 200, org.text
    assert (org.json()["role"], org.json()["step_in"]) == ("member", True)

    # Owner/admin reads stay closed: a step-in is a member, not an admin.
    for path in (f"{ORG}/settings", f"{ORG}/audit", f"{ORG}/users/invitations"):
        resp = await world.operator.get(path)
        assert resp.status_code == 403, (path, resp.text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", f"{PROJECT}/event-types", {"name": "track", "display_name": "T"}),
        ("PATCH", PROJECT, {"name": "Renamed"}),
        ("DELETE", PROJECT, None),
        ("POST", f"{ORG}/projects", {"name": "New", "slug": "new"}),
        ("POST", f"{ORG}/me/api-keys", {"name": "k", "scope": "read"}),
        ("PATCH", ORG, {"name": "Renamed"}),
        ("DELETE", ORG, {"confirm_slug": GLOBEX}),
        ("PATCH", f"{ORG}/settings", {}),
        ("POST", f"{ORG}/users/invitations", {"email": "x@example.com", "role": "member"}),
        ("POST", f"{PROJECT}/ai/ask", {"question": "what is tracked?"}),
    ],
)
async def test_every_write_is_refused(
    world: World, method: str, path: str, body: dict[str, Any] | None
) -> None:
    await _step_in(world)
    resp = await world.operator.request(method, path, json=body)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == STEP_IN_READ_ONLY


@pytest.mark.asyncio
async def test_the_two_read_shaped_posts_are_allowed(world: World) -> None:
    await _step_in(world)
    for suffix in ("/anomalies/signals/query", "/events/window-metrics"):
        resp = await world.operator.post(f"{PROJECT}{suffix}", json={"event_ids": []})
        assert resp.status_code == 200, (suffix, resp.text)


def test_the_read_shaped_handlers_name_real_routes() -> None:
    handlers = {
        f"{route.endpoint.__module__}.{route.endpoint.__qualname__}"
        for _path, route in iter_api_routes()
    }
    assert handlers >= STEP_IN_READ_HANDLERS
    assert len(STEP_IN_READ_HANDLERS) == 2


@pytest.mark.asyncio
async def test_a_step_in_reads_a_suspended_org(world: World) -> None:
    await set_org_status(GLOBEX_ID, OrganizationStatus.suspended)
    await _step_in(world)
    assert (await world.operator.get(f"{ORG}/projects")).status_code == 200
    # The owner, a member, is refused.
    refused = await world.alice.get(f"{ORG}/projects")
    assert refused.status_code == 403


# ── visibility and audit ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_step_in_is_audited_in_the_target_org(world: World) -> None:
    body = await _step_in(world, ttl_minutes=30)
    (start,) = await audit_rows("platform.step_in")
    assert start.organization_id == GLOBEX_ID
    assert start.user_id == world.operator_id
    assert start.payload["reason"] == "Support ticket 1234"
    assert start.payload["ttl_minutes"] == 30

    ended = await world.operator.post(f"{PLATFORM}/step-ins/{body['id']}/end")
    assert ended.status_code == 200, ended.text
    assert ended.json()["active"] is False
    (end,) = await audit_rows("platform.step_in_end")
    assert end.organization_id == GLOBEX_ID

    # The target organization's owner reads both in their own feed.
    feed = await world.alice.get(f"{ORG}/audit", params={"limit": 50})
    assert feed.status_code == 200, feed.text
    actions = {item["action"] for item in feed.json()["items"]}
    assert {"platform.step_in", "platform.step_in_end"} <= actions


@pytest.mark.asyncio
async def test_me_and_the_org_list_show_the_live_step_in(world: World) -> None:
    body = await _step_in(world)
    me = (await world.operator.get(f"{API}/auth/me")).json()
    assert [(s["id"], s["org_slug"]) for s in me["active_step_ins"]] == [(body["id"], GLOBEX)]
    orgs = {o["slug"]: o for o in (await world.operator.get(f"{API}/orgs")).json()}
    assert orgs[GLOBEX]["step_in"] is True
    assert orgs[GLOBEX]["role"] == "member"
    assert orgs["default"]["step_in"] is False
    # Nobody else sees it.
    assert (await world.alice.get(f"{API}/auth/me")).json()["active_step_ins"] == []

    live = (await world.operator.get(f"{PLATFORM}/step-ins", params={"active": "true"})).json()
    assert [s["id"] for s in live] == [body["id"]]


# ── how it ends ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ending_closes_the_org_again(world: World) -> None:
    body = await _step_in(world)
    assert (await world.operator.post(f"{PLATFORM}/step-ins/{body['id']}/end")).status_code == 200
    assert (await world.operator.get(f"{ORG}/projects")).status_code == 404
    again = await world.operator.post(f"{PLATFORM}/step-ins/{body['id']}/end")
    assert again.status_code == 409
    assert again.json()["detail"] == platform_console_service.STEP_IN_ALREADY_ENDED
    assert (await world.operator.get(f"{API}/auth/me")).json()["active_step_ins"] == []
    finished = (await world.operator.get(f"{PLATFORM}/step-ins", params={"active": "false"})).json()
    assert [s["id"] for s in finished] == [body["id"]]


@pytest.mark.asyncio
async def test_another_admins_step_in_cannot_be_ended(world: World) -> None:
    body = await _step_in(world)
    async with TestSessionLocal() as session:
        await session.execute(
            update(User).where(User.id == world.bob_id).values(is_platform_admin=True)
        )
        await session.commit()
    resp = await world.bob.post(f"{PLATFORM}/step-ins/{body['id']}/end")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_an_expired_step_in_reads_nothing(world: World) -> None:
    await _step_in(world)
    async with TestSessionLocal() as session:
        await session.execute(
            update(PlatformStepIn).values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.commit()
    assert (await world.operator.get(f"{ORG}/projects")).status_code == 404
    assert (await world.operator.get(f"{API}/auth/me")).json()["active_step_ins"] == []


async def _expire_all() -> None:
    async with TestSessionLocal() as session:
        await session.execute(
            update(PlatformStepIn).values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.commit()


@pytest.mark.asyncio
async def test_natural_expiry_is_recorded_once_when_resolution_first_sees_it(
    world: World,
) -> None:
    body = await _step_in(world)
    await _expire_all()
    assert await audit_rows("platform.step_in_end") == []

    # Resolution sees it first: closed and audited as expired, in the target org.
    assert (await world.operator.get(f"{ORG}/projects")).status_code == 404
    (end,) = await audit_rows("platform.step_in_end")
    assert end.payload == {"step_in_id": body["id"], "expired": True}
    assert end.organization_id == GLOBEX_ID
    assert end.user_id == world.operator_id
    async with TestSessionLocal() as session:
        row = await session.get(PlatformStepIn, uuid.UUID(body["id"]))
        assert row is not None
        assert row.ended_at is not None
        assert row.ended_at == row.expires_at

    # Idempotent: later requests and listings do not record it again.
    assert (await world.operator.get(f"{ORG}/projects")).status_code == 404
    await world.operator.get(f"{API}/auth/me")
    await world.operator.get(f"{PLATFORM}/step-ins")
    assert len(await audit_rows("platform.step_in_end")) == 1
    # Ending it by hand now is a conflict: it has already ended.
    again = await world.operator.post(f"{PLATFORM}/step-ins/{body['id']}/end")
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_natural_expiry_is_recorded_when_a_listing_first_sees_it(world: World) -> None:
    body = await _step_in(world)
    await _expire_all()
    finished = (await world.operator.get(f"{PLATFORM}/step-ins", params={"active": "false"})).json()
    assert [(s["id"], s["ended_at"] is not None) for s in finished] == [(body["id"], True)]
    (end,) = await audit_rows("platform.step_in_end")
    assert end.payload == {"step_in_id": body["id"], "expired": True}


@pytest.mark.asyncio
async def test_losing_the_platform_admin_flag_ends_its_reach(world: World) -> None:
    await _step_in(world)
    async with TestSessionLocal() as session:
        await session.execute(
            update(User).where(User.id == world.operator_id).values(is_platform_admin=False)
        )
        await session.commit()
    assert (await world.operator.get(f"{ORG}/projects")).status_code == 404


@pytest.mark.asyncio
async def test_a_second_step_in_supersedes_the_first(world: World) -> None:
    first = await _step_in(world)
    second = await _step_in(world, reason="Follow-up")
    (ended,) = await audit_rows("platform.step_in_end")
    assert ended.payload == {"step_in_id": first["id"], "superseded": True}
    live = (await world.operator.get(f"{PLATFORM}/step-ins", params={"active": "true"})).json()
    assert [s["id"] for s in live] == [second["id"]]


@pytest.mark.asyncio
async def test_an_api_key_never_steps_in(world: World) -> None:
    await _step_in(world)
    key = await world.operator.post(f"{API}/me/api-keys", json={"name": "k", "scope": "read"})
    assert key.status_code == 201, key.text
    bearer = {"Authorization": f"Bearer {key.json()['token']}"}
    async with new_client() as bare:
        for path in (f"{ORG}/projects", ORG):
            resp = await bare.get(path, headers=bearer)
            assert resp.status_code == 404, (path, resp.text)
