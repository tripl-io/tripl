"""CRUD + scope gating for user-issued API keys.

The fixture-authenticated client is the session-cookie path; once an API key
is issued, requests use ``Authorization: Bearer <token>`` and bypass cookies.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from tripl.models.api_key import ApiKey
from tripl.models.organization import OrganizationMember
from tripl.models.user import User
from tripl.services import api_key_service
from tripl.services.api_key_service import API_KEY_TOUCH_INTERVAL_SECONDS, _hash_token
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal


async def _issue_key(
    client: AsyncClient,
    *,
    name: str = "agent",
    scope: str = "read",
    expires_in_days: int | None = None,
    project_slug: str | None = None,
) -> tuple[str, str]:
    payload: dict[str, object] = {"name": name, "scope": scope}
    if expires_in_days is not None:
        payload["expires_in_days"] = expires_in_days
    if project_slug is not None:
        payload["project_slug"] = project_slug
    resp = await client.post("/api/v1/me/api-keys", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["id"], body["token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@pytest.mark.asyncio
async def test_create_returns_raw_token_once_and_list_only_shows_prefix(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    """POST yields the raw token exactly once; subsequent listings expose
    only the prefix + metadata."""
    _key_id, token = await _issue_key(client, name="scripted-agent", scope="read")
    assert token.startswith("tk_r_")

    listing = await client.get("/api/v1/me/api-keys")
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1
    only = rows[0]
    assert only["name"] == "scripted-agent"
    assert only["scope"] == "read"
    assert only["revoked_at"] is None
    # No raw token leaked on GET — the prefix is non-secret and OK to display.
    assert "token" not in only
    assert only["key_prefix"].startswith("tk_r_")
    assert len(only["key_prefix"]) < len(token)


@pytest.mark.asyncio
async def test_read_key_can_call_get_endpoints(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    """A read-scope key authenticates GET endpoints without a session cookie."""
    await client.post("/api/v1/projects", json={"name": "P", "slug": "agent-read"})
    _key_id, token = await _issue_key(client, scope="read")

    resp = await anon_client.get("/api/v1/projects/agent-read/event-types", headers=_bearer(token))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_read_key_cannot_call_write_endpoints(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    """A read-scope key is rejected (403) on editor surfaces."""
    await client.post("/api/v1/projects", json={"name": "P", "slug": "agent-block"})
    _key_id, token = await _issue_key(client, scope="read")

    resp = await anon_client.post(
        "/api/v1/projects/agent-block/event-types",
        json={"name": "track", "display_name": "Track"},
        headers=_bearer(token),
    )
    assert resp.status_code == 403
    assert "read-only" in resp.json()["detail"].lower()

    event_resp = await anon_client.post(
        "/api/v1/projects/agent-block/events",
        json={
            "event_type_id": "00000000-0000-0000-0000-000000000000",
            "name": "Blocked event",
        },
        headers=_bearer(token),
    )
    assert event_resp.status_code == 403

    scan_preview = await anon_client.post(
        "/api/v1/projects/agent-block/scans/preview",
        json={
            "data_source_id": "00000000-0000-0000-0000-000000000000",
            "base_query": "select 1",
        },
        headers=_bearer(token),
    )
    assert scan_preview.status_code == 403


@pytest.mark.asyncio
async def test_read_key_cannot_manage_api_keys(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    _key_id, token = await _issue_key(client, scope="read")

    create = await anon_client.post(
        "/api/v1/me/api-keys",
        json={"name": "escalate", "scope": "write"},
        headers=_bearer(token),
    )
    assert create.status_code == 403


@pytest.mark.asyncio
async def test_non_member_can_create_read_key_but_not_write_key(anon_client: AsyncClient) -> None:
    """A write key needs membership of the organization it is bound to (F20 PR4).

    Replaces the instance-viewer rule: there is no instance viewer any more, a
    plain org member mints write keys (what the key may write is still decided
    per project by the project role), and an account that is not a member of
    the request's organization gets 403 for write scope.
    """
    for email in ("api-owner@example.com", "api-member@example.com"):
        registered = await anon_client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "Password123!", "name": email},
        )
        assert registered.status_code == 201, registered.text
        if email == "api-owner@example.com":
            await anon_client.post("/api/v1/auth/logout")

    member_write = await anon_client.post(
        "/api/v1/me/api-keys", json={"name": "member-write", "scope": "write"}
    )
    assert member_write.status_code == 201, member_write.text

    async with TestSessionLocal() as session:
        user_id = await session.scalar(
            select(User.id).where(User.email == "api-member@example.com")
        )
        await session.execute(
            delete(OrganizationMember).where(OrganizationMember.user_id == user_id)
        )
        await session.commit()

    read_key = await anon_client.post(
        "/api/v1/me/api-keys",
        json={"name": "outsider-read", "scope": "read"},
    )
    assert read_key.status_code == 201, read_key.text

    write_key = await anon_client.post(
        "/api/v1/me/api-keys",
        json={"name": "outsider-write", "scope": "write"},
    )
    assert write_key.status_code == 403, write_key.text


@pytest.mark.asyncio
async def test_owner_read_key_cannot_update_roles(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    _key_id, token = await _issue_key(client, scope="read")
    users = await anon_client.get("/api/v1/users", headers=_bearer(token))
    assert users.status_code == 200
    owner_id = users.json()[0]["id"]

    update = await anon_client.patch(
        f"/api/v1/users/{owner_id}",
        json={"role": "member"},
        headers=_bearer(token),
    )
    assert update.status_code == 403


@pytest.mark.asyncio
async def test_write_key_can_call_write_endpoints(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    """A write-scope key passes editor gating (subject to user role)."""
    await client.post("/api/v1/projects", json={"name": "P", "slug": "agent-write"})
    _key_id, token = await _issue_key(client, scope="write")

    resp = await anon_client.post(
        "/api/v1/projects/agent-write/event-types",
        json={"name": "checkout", "display_name": "Checkout"},
        headers=_bearer(token),
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "checkout"


@pytest.mark.asyncio
async def test_write_key_cannot_call_owner_only_endpoints(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    await client.post("/api/v1/projects", json={"name": "P", "slug": "owner-key-block"})
    users = await client.get("/api/v1/users")
    assert users.status_code == 200
    owner_id = users.json()[0]["id"]
    _key_id, token = await _issue_key(client, scope="write")

    role_update = await anon_client.patch(
        f"/api/v1/users/{owner_id}",
        json={"role": "editor"},
        headers=_bearer(token),
    )
    assert role_update.status_code == 403
    assert "owner session" in role_update.json()["detail"].lower()

    project_delete = await anon_client.delete(
        "/api/v1/projects/owner-key-block",
        headers=_bearer(token),
    )
    assert project_delete.status_code == 403
    assert "owner session" in project_delete.json()["detail"].lower()


@pytest.mark.asyncio
async def test_revoked_key_is_rejected(anon_client: AsyncClient, client: AsyncClient) -> None:
    key_id, token = await _issue_key(client, scope="read")
    revoke = await client.delete(f"/api/v1/me/api-keys/{key_id}")
    assert revoke.status_code == 204

    after = await anon_client.get("/api/v1/projects", headers=_bearer(token))
    assert after.status_code == 401


@pytest.mark.asyncio
async def test_invalid_bearer_returns_401(anon_client: AsyncClient) -> None:
    """A garbage bearer token must NOT silently fall back to anonymous —
    that would let an attacker probe for endpoints under fake auth."""
    resp = await anon_client.get(
        "/api/v1/projects", headers=_bearer("tk_r_definitely-not-a-real-key")
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_expires_in_days_validation(client: AsyncClient) -> None:
    """expires_in_days must be positive (or absent)."""
    bad = await client.post(
        "/api/v1/me/api-keys",
        json={"name": "k", "scope": "read", "expires_in_days": 0},
    )
    assert bad.status_code == 422

    good = await client.post(
        "/api/v1/me/api-keys",
        json={"name": "k", "scope": "read", "expires_in_days": 30},
    )
    assert good.status_code == 201
    assert good.json()["expires_at"] is not None


@pytest.mark.asyncio
async def test_project_scoped_key_reaches_only_its_project(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    """A key bound to one project authenticates that project's routes and is
    answers 404 "Project not found" on any other project, like an unknown slug,
    so the key is no oracle for which slugs exist."""
    await client.post("/api/v1/projects", json={"name": "A", "slug": "scoped-a"})
    await client.post("/api/v1/projects", json={"name": "B", "slug": "scoped-b"})
    _key_id, token = await _issue_key(client, scope="read", project_slug="scoped-a")

    ok = await anon_client.get("/api/v1/projects/scoped-a/event-types", headers=_bearer(token))
    assert ok.status_code == 200

    denied = await anon_client.get("/api/v1/projects/scoped-b/event-types", headers=_bearer(token))
    assert denied.status_code == 404
    assert denied.json()["detail"] == "Project not found"

    unknown = await anon_client.get(
        "/api/v1/projects/scoped-nowhere/event-types", headers=_bearer(token)
    )
    assert unknown.status_code == 404
    assert unknown.json() == denied.json()


@pytest.mark.asyncio
async def test_project_scoped_key_rejected_on_instance_routes(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    """A project-bound key can't reach routes that lack a project slug (the
    instance-wide surfaces like the project list or user directory)."""
    await client.post("/api/v1/projects", json={"name": "A", "slug": "scoped-only"})
    _key_id, token = await _issue_key(client, scope="read", project_slug="scoped-only")

    listing = await anon_client.get("/api/v1/projects", headers=_bearer(token))
    assert listing.status_code == 403
    assert "scoped to a single project" in listing.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_key_with_unknown_project_is_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/me/api-keys",
        json={"name": "k", "scope": "read", "project_slug": "does-not-exist"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unscoped_key_keeps_cross_project_access(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    """Keys minted without a project_slug retain the legacy cross-project reach
    and the response reports a null project_id."""
    await client.post("/api/v1/projects", json={"name": "A", "slug": "unscoped-a"})
    await client.post("/api/v1/projects", json={"name": "B", "slug": "unscoped-b"})
    create = await client.post("/api/v1/me/api-keys", json={"name": "wide", "scope": "read"})
    assert create.status_code == 201
    body = create.json()
    assert body["project_id"] is None
    token = body["token"]

    for slug in ("unscoped-a", "unscoped-b"):
        resp = await anon_client.get(f"/api/v1/projects/{slug}/event-types", headers=_bearer(token))
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_verify_and_touch_throttles_last_used_writes(client: AsyncClient) -> None:
    """A second auth within the touch interval must NOT issue a new commit/update:
    ``last_used_at`` is left unchanged to avoid write amplification on the hot path."""
    from tripl.tests.conftest import TestSessionLocal

    _key_id, token = await _issue_key(client, scope="read")

    async with TestSessionLocal() as session:
        row = await api_key_service.verify_and_touch(session, token)
        assert row is not None
        first_used = row.last_used_at
        assert first_used is not None

    # Second auth well within the interval: no write, timestamp preserved.
    async with TestSessionLocal() as session:
        commits = 0
        real_commit = session.commit

        async def counting_commit() -> None:
            nonlocal commits
            commits += 1
            await real_commit()

        session.commit = counting_commit  # type: ignore[method-assign]
        row = await api_key_service.verify_and_touch(session, token)
        assert row is not None
        assert commits == 0
        # Unchanged within the interval (the plain DateTime column may drop
        # tzinfo on the SQLite round-trip, so compare on UTC-normalized values).
        assert _as_utc(row.last_used_at) == first_used

    # Backdate beyond the interval: the next auth refreshes the timestamp.
    async with TestSessionLocal() as session:
        row = await session.scalar(select(ApiKey).where(ApiKey.key_hash == _hash_token(token)))
        assert row is not None
        row.last_used_at = datetime.now(UTC) - timedelta(seconds=API_KEY_TOUCH_INTERVAL_SECONDS + 5)
        await session.commit()

    async with TestSessionLocal() as session:
        commits = 0
        real_commit = session.commit

        async def counting_commit2() -> None:
            nonlocal commits
            commits += 1
            await real_commit()

        session.commit = counting_commit2  # type: ignore[method-assign]
        row = await api_key_service.verify_and_touch(session, token)
        assert row is not None
        assert commits == 1
        assert _as_utc(row.last_used_at) > first_used


@pytest.mark.asyncio
async def test_user_cannot_revoke_another_users_key(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    """API keys are per-user — even a second account in the same instance
    can't see or revoke them."""
    key_id, _token = await _issue_key(client, scope="read")

    # Register a second user via the anon client and use it instead.
    register = await anon_client.post(
        "/api/v1/auth/register",
        json={
            "email": "other@example.com",
            "password": "Password123!",
            "name": "Other",
        },
    )
    assert register.status_code == 201

    revoke = await anon_client.delete(f"/api/v1/me/api-keys/{key_id}")
    assert revoke.status_code == 404


@pytest.mark.asyncio
async def test_minting_a_project_key_requires_membership(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    """A key bound to a project the caller cannot see is refused as if the project
    did not exist: minting must not confirm a hidden slug."""
    await client.post("/api/v1/projects", json={"name": "Hidden", "slug": "key-hidden"})

    await anon_client.post("/api/v1/auth/logout")
    register = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "key-editor@example.com", "password": "Password123!", "name": "Ed"},
    )
    assert register.status_code == 201

    refused = await anon_client.post(
        "/api/v1/me/api-keys",
        json={"name": "k", "scope": "read", "project_slug": "key-hidden"},
    )
    assert refused.status_code == 404, refused.text

    await add_member_by_slug("key-hidden", "key-editor@example.com", "viewer")
    minted = await anon_client.post(
        "/api/v1/me/api-keys",
        json={"name": "k", "scope": "read", "project_slug": "key-hidden"},
    )
    assert minted.status_code == 201, minted.text


@pytest.mark.asyncio
async def test_unscoped_key_of_a_non_owner_reaches_only_member_projects(
    anon_client: AsyncClient, client: AsyncClient
) -> None:
    """An unscoped key acts with its user's membership: no project beyond it."""
    await client.post("/api/v1/projects", json={"name": "A", "slug": "member-a"})
    await client.post("/api/v1/projects", json={"name": "B", "slug": "member-b"})

    await anon_client.post("/api/v1/auth/logout")
    register = await anon_client.post(
        "/api/v1/auth/register",
        json={"email": "wide-editor@example.com", "password": "Password123!", "name": "Ed"},
    )
    assert register.status_code == 201
    await add_member_by_slug("member-a", "wide-editor@example.com", "editor")
    _key_id, token = await _issue_key(anon_client, scope="read")
    await anon_client.post("/api/v1/auth/logout")

    ok = await anon_client.get("/api/v1/projects/member-a/event-types", headers=_bearer(token))
    assert ok.status_code == 200, ok.text
    hidden = await anon_client.get("/api/v1/projects/member-b/event-types", headers=_bearer(token))
    assert hidden.status_code == 404, hidden.text

    listing = await anon_client.get("/api/v1/projects", headers=_bearer(token))
    assert listing.status_code == 200, listing.text
    assert {item["slug"] for item in listing.json()} == {"member-a"}
