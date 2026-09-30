"""The platform console API (F20 PR14): organizations, suspension, users.

* only a platform admin's browser session reaches ``/platform/...``: an org
  owner gets 403, and so does the platform admin's own API key;
* the organization list and detail carry metadata and counts, never project
  content; search escapes LIKE wildcards;
* suspend needs a reason (1..500, no NUL), refuses the default organization and
  a deleting one, is audited in the TARGET organization with the acting admin;
  unsuspend puts it back;
* the platform-admin flag: grant and revoke are audited at platform scope, you
  cannot revoke yourself, and the last platform admin cannot be revoked.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from tripl.models.domain_enums import OrganizationStatus
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.models.user import User
from tripl.services import audit_actions, platform_console_service
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

PLATFORM = f"{API}/platform"


@pytest.fixture
async def world() -> AsyncIterator[World]:
    built = await build_world()
    yield built
    await built.close()


# ── who reaches it ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_org_owner_is_not_a_platform_admin(world: World) -> None:
    for path in ("/orgs", f"/orgs/{GLOBEX}", "/users", "/step-ins"):
        resp = await world.alice.get(f"{PLATFORM}{path}")
        assert resp.status_code == 403, (path, resp.text)
        assert resp.json()["detail"] == "Platform admin required"
    refused = await world.alice.post(f"{PLATFORM}/orgs/{GLOBEX}/suspend", json={"reason": "x"})
    assert refused.status_code == 403


@pytest.mark.asyncio
async def test_the_platform_admins_api_key_is_refused(world: World) -> None:
    key = await world.operator.post(f"{API}/me/api-keys", json={"name": "k", "scope": "write"})
    assert key.status_code == 201, key.text
    bearer = {"Authorization": f"Bearer {key.json()['token']}"}
    # A cookie-less client, so only the Authorization header authenticates.
    async with new_client() as bare:
        resp = await bare.get(f"{PLATFORM}/orgs", headers=bearer)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Platform admin session required"


# ── organizations ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_org_list_carries_counts_and_owners(world: World) -> None:
    resp = await world.operator.get(f"{PLATFORM}/orgs")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_slug = {item["slug"]: item for item in body["items"]}
    assert body["total"] == 2
    assert set(by_slug) == {"default", GLOBEX}
    globex = by_slug[GLOBEX]
    assert globex["status"] == "active"
    assert globex["member_count"] == 1
    assert globex["project_count"] == 1
    assert globex["owner_emails"] == ["alice@example.com"]
    assert globex["suspended_at"] is None and globex["suspended_reason"] is None
    assert by_slug["default"]["is_default"] is True
    assert by_slug["default"]["owner_emails"] == ["operator@example.com"]


@pytest.mark.asyncio
async def test_org_list_search_filter_and_paging(world: World) -> None:
    found = (await world.operator.get(f"{PLATFORM}/orgs", params={"q": "GLOB"})).json()
    assert [item["slug"] for item in found["items"]] == [GLOBEX]
    assert found["total"] == 1
    # LIKE wildcards are literal: "%" matches no name.
    wild = (await world.operator.get(f"{PLATFORM}/orgs", params={"q": "%"})).json()
    assert wild == {"items": [], "total": 0}
    suspended = (
        await world.operator.get(f"{PLATFORM}/orgs", params={"status": "suspended"})
    ).json()
    assert suspended["total"] == 0
    page = (await world.operator.get(f"{PLATFORM}/orgs", params={"limit": 1, "offset": 1})).json()
    assert page["total"] == 2 and len(page["items"]) == 1
    bad = await world.operator.get(f"{PLATFORM}/orgs", params={"status": "gone"})
    assert bad.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["orgs", "users"])
async def test_a_nul_in_the_search_is_stripped_not_a_500(world: World, path: str) -> None:
    only_nul = await world.operator.get(f"{PLATFORM}/{path}", params={"q": "\x00"})
    assert only_nul.status_code == 200, only_nul.text
    laced = await world.operator.get(f"{PLATFORM}/{path}", params={"q": "gl\x00o"})
    assert laced.status_code == 200, laced.text
    if path == "orgs":
        assert [item["slug"] for item in laced.json()["items"]] == [GLOBEX]


@pytest.mark.asyncio
async def test_org_detail_is_metadata_only(world: World) -> None:
    resp = await world.operator.get(f"{PLATFORM}/orgs/{GLOBEX}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [(m["email"], m["role"]) for m in body["members"]] == [("alice@example.com", "owner")]
    assert [p["slug"] for p in body["projects"]] == [SHOP]
    assert set(body["projects"][0]) == {"slug", "name", "created_at"}
    missing = await world.operator.get(f"{PLATFORM}/orgs/nope")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Organization not found"


# ── suspension ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["", "   ", "a\x00b", "x" * 501])
async def test_suspend_requires_a_clean_reason(world: World, reason: str) -> None:
    resp = await world.operator.post(f"{PLATFORM}/orgs/{GLOBEX}/suspend", json={"reason": reason})
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_suspend_and_unsuspend_are_audited_in_the_target_org(world: World) -> None:
    suspended = await world.operator.post(
        f"{PLATFORM}/orgs/{GLOBEX}/suspend", json={"reason": "  unpaid invoice  "}
    )
    assert suspended.status_code == 200, suspended.text
    body = suspended.json()
    assert body["status"] == "suspended"
    assert body["suspended_reason"] == "unpaid invoice"
    assert body["suspended_at"] is not None

    listed = (await world.operator.get(f"{PLATFORM}/orgs", params={"status": "suspended"})).json()
    assert [item["slug"] for item in listed["items"]] == [GLOBEX]

    again = await world.operator.post(f"{PLATFORM}/orgs/{GLOBEX}/suspend", json={"reason": "x"})
    assert again.status_code == 409
    assert again.json()["detail"] == platform_console_service.ORG_ALREADY_SUSPENDED

    (row,) = await audit_rows("org.suspend")
    assert row.organization_id == GLOBEX_ID
    assert row.user_id == world.operator_id
    assert row.payload["reason"] == "unpaid invoice"

    restored = await world.operator.post(f"{PLATFORM}/orgs/{GLOBEX}/unsuspend")
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "active"
    assert restored.json()["suspended_reason"] is None
    (undo,) = await audit_rows("org.unsuspend")
    assert undo.organization_id == GLOBEX_ID
    assert undo.payload["suspended_reason"] == "unpaid invoice"

    not_suspended = await world.operator.post(f"{PLATFORM}/orgs/{GLOBEX}/unsuspend")
    assert not_suspended.status_code == 409
    assert not_suspended.json()["detail"] == platform_console_service.ORG_NOT_SUSPENDED


@pytest.mark.asyncio
async def test_the_default_org_cannot_be_suspended(world: World) -> None:
    resp = await world.operator.post(f"{PLATFORM}/orgs/default/suspend", json={"reason": "x"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == platform_console_service.DEFAULT_ORG_UNSUSPENDABLE


@pytest.mark.asyncio
async def test_a_deleting_org_cannot_be_suspended_or_unsuspended(world: World) -> None:
    await set_org_status(GLOBEX_ID, OrganizationStatus.deleting)
    for verb, body in (("suspend", {"reason": "x"}), ("unsuspend", None)):
        resp = await world.operator.post(f"{PLATFORM}/orgs/{GLOBEX}/{verb}", json=body)
        assert resp.status_code == 409, (verb, resp.text)
        assert resp.json()["detail"] == platform_console_service.ORG_BEING_DELETED
    # Still listed for the operator, with its status.
    listed = (await world.operator.get(f"{PLATFORM}/orgs", params={"status": "deleting"})).json()
    assert [item["slug"] for item in listed["items"]] == [GLOBEX]


# ── users and the platform-admin flag ────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_list_search_and_org_count(world: World) -> None:
    resp = await world.operator.get(f"{PLATFORM}/users")
    assert resp.status_code == 200, resp.text
    by_email = {item["email"]: item for item in resp.json()["items"]}
    assert resp.json()["total"] == 3
    assert by_email["operator@example.com"]["is_platform_admin"] is True
    assert by_email["alice@example.com"]["org_count"] == 2  # default + globex
    assert by_email["bob@example.com"]["org_count"] == 1
    assert by_email["bob@example.com"]["email_verified"] is True
    found = (await world.operator.get(f"{PLATFORM}/users", params={"q": "ALI"})).json()
    assert [item["email"] for item in found["items"]] == ["alice@example.com"]


@pytest.mark.asyncio
async def test_grant_and_revoke_are_audited_at_platform_scope(world: World) -> None:
    url = f"{PLATFORM}/users/{world.alice_id}/platform-admin"
    granted = await world.operator.post(url, json={"grant": True})
    assert granted.status_code == 200, granted.text
    assert granted.json()["is_platform_admin"] is True
    (grant_row,) = await audit_rows("platform.admin_grant")
    assert grant_row.organization_id is None
    assert grant_row.user_id == world.operator_id
    assert grant_row.target_id == world.alice_id

    # A no-op grant changes and audits nothing.
    assert (await world.operator.post(url, json={"grant": True})).status_code == 200
    assert len(await audit_rows("platform.admin_grant")) == 1

    # Alice now reaches the console.
    assert (await world.alice.get(f"{PLATFORM}/orgs")).status_code == 200

    revoked = await world.operator.post(url, json={"grant": False})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["is_platform_admin"] is False
    (revoke_row,) = await audit_rows("platform.admin_revoke")
    assert revoke_row.organization_id is None
    assert (await world.alice.get(f"{PLATFORM}/orgs")).status_code == 403


@pytest.mark.asyncio
async def test_you_cannot_revoke_yourself(world: World) -> None:
    resp = await world.operator.post(
        f"{PLATFORM}/users/{world.operator_id}/platform-admin", json={"grant": False}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == platform_console_service.CANNOT_REVOKE_SELF


@pytest.mark.asyncio
async def test_unknown_user_is_404(world: World) -> None:
    resp = await world.operator.post(
        f"{PLATFORM}/users/{uuid.uuid4()}/platform-admin", json={"grant": True}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == platform_console_service.USER_NOT_FOUND


@pytest.mark.asyncio
async def test_the_last_platform_admin_cannot_be_revoked(world: World) -> None:
    # Only reachable through a race over the API (the actor is an admin too),
    # so the rule is pinned on the service.
    async with TestSessionLocal() as session:
        alice = await session.get(User, world.alice_id)
        assert alice is not None
        with pytest.raises(platform_console_service.LastPlatformAdminError):
            await platform_console_service.set_platform_admin(
                session, world.operator_id, grant=False, actor=alice
            )
    async with TestSessionLocal() as session:
        operator = await session.get(User, world.operator_id)
        assert operator is not None and operator.is_platform_admin is True


# ── the audit filter ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_audit_filter_lists_the_platform_actions(world: World) -> None:
    resp = await world.operator.get(f"{API}/audit/actions")
    assert resp.status_code == 200, resp.text
    groups = {group["label"]: group["actions"] for group in resp.json()["workspace"]}
    assert set(groups["Platform"]) == {
        "org.suspend",
        "org.unsuspend",
        "platform.step_in",
        "platform.step_in_end",
        "platform.admin_grant",
        "platform.admin_revoke",
    }
    assert "org.suspend" in audit_actions.all_actions()


@pytest.mark.asyncio
async def test_the_default_org_lists_nothing_of_globex_in_its_console_counts(
    world: World,
) -> None:
    detail = (await world.operator.get(f"{PLATFORM}/orgs/default")).json()
    assert detail["id"] == str(DEFAULT_ORG_ID)
    assert SHOP not in {p["slug"] for p in detail["projects"]}
