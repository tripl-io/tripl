"""Owner-issued invitations (tripl-jfm3.82).

The point of this flow is that a CLOSED instance can still onboard exactly the
people its owner named, so the tests that matter most assert redemption works
while ``registration_mode`` is ``disabled`` — and that nothing else does.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from tripl.config import REGISTRATION_DISABLED, settings
from tripl.main import app
from tripl.services import invitation_service

PASSWORD = "Password123!"


def _new_client() -> AsyncClient:
    """A second client with its own cookie jar, sharing the app and DB."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _invite(client: AsyncClient, email: str, role: str = "member"):
    return await client.post("/api/v1/users/invitations", json={"email": email, "role": role})


def _token_from(accept_path: str) -> str:
    return accept_path.rsplit("/", 1)[-1]


@pytest.mark.asyncio
async def test_owner_invites_and_the_link_is_returned_once(client: AsyncClient) -> None:
    resp = await _invite(client, "invitee@example.com")

    assert resp.status_code == 201
    body = resp.json()
    assert body["invitation"]["email"] == "invitee@example.com"
    # The organization role; ``member`` is the default, as for self sign-up.
    assert body["invitation"]["role"] == "member"
    assert body["invitation"]["is_expired"] is False
    assert body["accept_path"].startswith("/invite/")
    assert len(_token_from(body["accept_path"])) > 20

    # The listing must never hand out a way to redeem.
    listing = await client.get("/api/v1/users/invitations")
    assert listing.status_code == 200
    (row,) = listing.json()
    assert row["email"] == "invitee@example.com"
    assert "token" not in row
    assert "token_hash" not in row
    assert "accept_path" not in row


@pytest.mark.asyncio
async def test_invitation_works_on_a_closed_instance(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole reason this flow exists: onboarding without opening the door."""
    monkeypatch.setattr(settings, "registration_mode", REGISTRATION_DISABLED)

    minted = await _invite(client, "closed-invitee@example.com", role="admin")
    assert minted.status_code == 201
    token = _token_from(minted.json()["accept_path"])

    async with _new_client() as invitee:
        # Self-service signup is refused...
        refused = await invitee.post(
            "/api/v1/auth/register",
            json={"email": "closed-invitee@example.com", "password": PASSWORD},
        )
        assert refused.status_code == 403

        # ...but the named invitation still works.
        accepted = await invitee.post(
            f"/api/v1/auth/invitations/{token}/accept",
            json={"password": PASSWORD, "name": "Invited Person"},
        )
        assert accepted.status_code == 201
        assert accepted.json()["email"] == "closed-invitee@example.com"
        # The role is the one the OWNER chose, not a default and not the
        # invitee's choice.
        assert accepted.json()["role"] == "admin"

        # And they are signed in already.
        me = await invitee.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["email"] == "closed-invitee@example.com"


@pytest.mark.asyncio
async def test_accepting_the_same_invitation_twice_is_refused(client: AsyncClient) -> None:
    token = _token_from((await _invite(client, "once@example.com")).json()["accept_path"])

    async with _new_client() as first:
        assert (
            await first.post(
                f"/api/v1/auth/invitations/{token}/accept", json={"password": PASSWORD}
            )
        ).status_code == 201

    async with _new_client() as second:
        replay = await second.post(
            f"/api/v1/auth/invitations/{token}/accept", json={"password": PASSWORD}
        )
        assert replay.status_code == 400
        assert "invalid, expired, or already used" in replay.json()["detail"]


@pytest.mark.asyncio
async def test_unknown_and_expired_tokens_are_indistinguishable(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected redemption must not reveal WHY it was rejected."""
    # Mint one that is already past its window, rather than reaching into the
    # DB to backdate a row: this exercises the real expiry arithmetic.
    monkeypatch.setattr(invitation_service, "INVITATION_TTL_HOURS", -1)
    minted = await _invite(client, "stale@example.com")
    assert minted.status_code == 201
    token = _token_from(minted.json()["accept_path"])

    async with _new_client() as invitee:
        expired = await invitee.post(
            f"/api/v1/auth/invitations/{token}/accept", json={"password": PASSWORD}
        )
        unknown = await invitee.post(
            "/api/v1/auth/invitations/not-a-real-token/accept", json={"password": PASSWORD}
        )

    assert expired.status_code == unknown.status_code == 400
    assert expired.json()["detail"] == unknown.json()["detail"]


@pytest.mark.asyncio
async def test_revoking_an_invitation_kills_its_link(client: AsyncClient) -> None:
    minted = await _invite(client, "revoked@example.com")
    token = _token_from(minted.json()["accept_path"])
    invitation_id = minted.json()["invitation"]["id"]

    revoke = await client.delete(f"/api/v1/users/invitations/{invitation_id}")
    assert revoke.status_code == 204

    async with _new_client() as invitee:
        dead = await invitee.post(
            f"/api/v1/auth/invitations/{token}/accept", json={"password": PASSWORD}
        )
    assert dead.status_code == 400


@pytest.mark.asyncio
async def test_reinviting_supersedes_the_previous_link(client: AsyncClient) -> None:
    """Only the newest link works, matching how password resets behave."""
    first = _token_from((await _invite(client, "resent@example.com")).json()["accept_path"])
    second = _token_from((await _invite(client, "resent@example.com")).json()["accept_path"])
    assert first != second

    async with _new_client() as invitee:
        stale = await invitee.post(
            f"/api/v1/auth/invitations/{first}/accept", json={"password": PASSWORD}
        )
        assert stale.status_code == 400

        fresh = await invitee.post(
            f"/api/v1/auth/invitations/{second}/accept", json={"password": PASSWORD}
        )
        assert fresh.status_code == 201

    listing = await client.get("/api/v1/users/invitations")
    assert listing.json() == []


@pytest.mark.asyncio
async def test_cannot_invite_an_address_that_already_has_an_account(client: AsyncClient) -> None:
    clash = await _invite(client, "test@example.com")

    assert clash.status_code == 409
    assert "already has an account" in clash.json()["detail"]


@pytest.mark.asyncio
async def test_preview_shows_who_the_invitation_is_for(client: AsyncClient) -> None:
    token = _token_from(
        (await _invite(client, "preview@example.com", role="admin")).json()["accept_path"]
    )

    async with _new_client() as anon:
        preview = await anon.get(f"/api/v1/auth/invitations/{token}")

    assert preview.status_code == 200
    assert preview.json()["email"] == "preview@example.com"
    assert preview.json()["role"] == "admin"
    # Discloses nothing beyond the invitation itself.
    assert set(preview.json()) == {"email", "role", "expires_at"}


@pytest.mark.asyncio
async def test_only_an_owner_may_invite(client: AsyncClient) -> None:
    """A plain member must not be able to mint accounts — that is an owner/admin power."""
    token = _token_from((await _invite(client, "an-editor@example.com")).json()["accept_path"])

    async with _new_client() as editor:
        assert (
            await editor.post(
                f"/api/v1/auth/invitations/{token}/accept", json={"password": PASSWORD}
            )
        ).status_code == 201

        assert (await _invite(editor, "downstream@example.com")).status_code == 403
        assert (await editor.get("/api/v1/users/invitations")).status_code == 403


@pytest.mark.asyncio
async def test_invitation_requires_authentication_at_all(client: AsyncClient) -> None:
    del client
    async with _new_client() as anon:
        assert (await _invite(anon, "nobody@example.com")).status_code == 401


@pytest.mark.asyncio
async def test_instance_roles_are_not_an_invitation_vocabulary(client: AsyncClient) -> None:
    """``editor`` / ``viewer`` were instance roles; an invitation carries an org role (422)."""
    for legacy in ("editor", "viewer"):
        assert (await _invite(client, f"{legacy}@example.com", role=legacy)).status_code == 422


@pytest.mark.asyncio
async def test_only_an_owner_may_invite_an_owner(client: AsyncClient) -> None:
    """An org admin invites members and admins, but not owners."""
    token = _token_from(
        (await _invite(client, "an-admin@example.com", role="admin")).json()["accept_path"]
    )
    async with _new_client() as admin:
        accepted = await admin.post(
            f"/api/v1/auth/invitations/{token}/accept", json={"password": PASSWORD}
        )
        assert accepted.status_code == 201, accepted.text
        assert (await _invite(admin, "fine@example.com", role="admin")).status_code == 201
        refused = await _invite(admin, "boss@example.com", role="owner")
        assert refused.status_code == 403, refused.text
        assert "owner" in refused.json()["detail"].lower()

    assert (await _invite(client, "boss@example.com", role="owner")).status_code == 201
