# ruff: noqa: F811  (the fixtures imported from test_org_sso are redefined as parameters)
"""SCIM 2.0 provisioning: tokens, authentication and ``/Users`` (F20, GH #273).

* the tokens are an OWNER's (an admin and a member get 403, a stranger 404, an
  API key 403), shown once, stored as a keyed digest, and stop working when
  revoked;
* ``/scim/v2/{org}`` admits only a SCIM token of THAT organization: a session
  cookie and an API key are 401, a token of another organization 404, a
  suspended organization 403 — all in the SCIM error format;
* provisioning creates an account only in a verified SSO domain, links an
  existing one only there or when it is already a member (no existence
  oracle), without touching its password or name (verifying the address only
  in a verified domain), filters and pages;
* a former owner's tokens stop working; a removal by an owner or admin is not
  undone by the provider; a POST after a DELETE brings the account back;
  hostile paging and bodies, unknown paths and failed authentication answer
  SCIM errors, never a 500;
* ``active`` false (Okta's string ``"False"``, Azure AD's boolean) and DELETE
  deprovision: the membership, the org's API keys and SSO identities go, the
  account stays; re-activation brings the membership back; the last owner is
  409; another organization's users are invisible; every write is audited with
  no user and the token's prefix.

Groups and the admin-group mapping: ``test_scim_groups.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select, update

from tripl.config import settings
from tripl.models.api_key import ApiKey
from tripl.models.domain_enums import OrganizationStatus
from tripl.models.org_scim import OrgScimToken, ScimUserLink
from tripl.models.org_sso import OrgSsoDomain, SsoMembershipBlock, UserSsoIdentity
from tripl.models.organization import OrganizationMember
from tripl.models.user import User
from tripl.tests._members import add_org_member
from tripl.tests._platform_world import set_org_status
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_org_sso import (  # noqa: F401 - fixtures
    ACME,
    API,
    DOMAIN,
    PASSWORD,
    People,
    _audit,
    _new_client,
    _user_id,
    acme,
    people,
)

SCIM = f"/scim/v2/{ACME}"
TOKENS_URL = f"{API}/orgs/{ACME}/scim/tokens"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


# ── helpers ─────────────────────────────────────────────────────────────────


async def verify_domain(org_id: uuid.UUID, domain: str = DOMAIN) -> None:
    """A verified SSO domain of the organization, without the DNS dance."""
    async with TestSessionLocal() as session:
        session.add(
            OrgSsoDomain(
                organization_id=org_id,
                domain=domain,
                verification_token=uuid.uuid4().hex,
                verified_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def mint_token(owner: AsyncClient, org: str = ACME) -> str:
    resp = await owner.post(f"{API}/orgs/{org}/scim/tokens")
    assert resp.status_code == 201, resp.text
    token: str = resp.json()["token"]
    return token


async def scim(
    token: str | None,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    params: dict[str, Any] | None = None,
    base: str = SCIM,
) -> Response:
    headers = {"Content-Type": "application/scim+json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with _new_client() as client:
        return await client.request(
            method, f"{base}{path}", json=body, params=params, headers=headers
        )


def user_body(email: str, **extra: Any) -> dict[str, Any]:
    return {"schemas": [USER_SCHEMA], "userName": email, "active": True, **extra}


def patch(*operations: dict[str, Any]) -> dict[str, Any]:
    return {"schemas": [PATCH_SCHEMA], "Operations": list(operations)}


def _active(value: object) -> dict[str, Any]:
    return {"op": "replace", "path": "active", "value": value}


async def provision(token: str, email: str, **extra: Any) -> dict[str, Any]:
    resp = await scim(token, "POST", "/Users", user_body(email, **extra))
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def org_role(org_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
    async with TestSessionLocal() as session:
        role = await session.scalar(
            select(OrganizationMember.role).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id == user_id,
            )
        )
        return None if role is None else str(role)


async def load_user(email: str) -> User:
    async with TestSessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == email))
        assert user is not None
        return user


def assert_scim_error(resp: Response, status: int, scim_type: str | None = None) -> None:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/scim+json")
    body = resp.json()
    assert body["schemas"] == [ERROR_SCHEMA]
    assert body["status"] == str(status)
    if scim_type is not None:
        assert body["scimType"] == scim_type


@pytest.fixture
async def token(people: People, acme: uuid.UUID) -> str:
    await verify_domain(acme)
    return await mint_token(people["root"])


# ── tokens ──────────────────────────────────────────────────────────────────


async def test_tokens_are_an_owners_shown_once_and_revocable(
    people: People, acme: uuid.UUID
) -> None:
    root = people["root"]
    created = await root.post(TOKENS_URL)
    assert created.status_code == 201, created.text
    raw = created.json()["token"]
    assert raw.startswith("tripl_scim_")
    assert raw.startswith(created.json()["prefix"])

    listed = await root.get(TOKENS_URL)
    assert listed.status_code == 200, listed.text
    [row] = listed.json()
    assert row["prefix"] == created.json()["prefix"]
    assert row["created_by_email"] == "root@example.com"
    assert raw not in listed.text
    async with TestSessionLocal() as session:
        stored = await session.scalar(select(OrgScimToken))
        assert stored is not None
        assert stored.token_hash != raw and raw not in stored.token_hash

    for name in ("bob", "mia"):
        assert (await people[name].get(TOKENS_URL)).status_code == 403
        assert (await people[name].post(TOKENS_URL)).status_code == 403
    assert (await people["stranger"].get(TOKENS_URL)).status_code == 404
    key = await root.post(
        f"{API}/orgs/{ACME}/me/api-keys", json={"name": "agent", "scope": "write"}
    )
    assert key.status_code == 201, key.text
    async with _new_client() as bearer:
        by_key = await bearer.post(
            TOKENS_URL, headers={"Authorization": f"Bearer {key.json()['token']}"}
        )
    assert by_key.status_code == 403, by_key.text

    assert (await scim(raw, "GET", "/Users")).status_code == 200
    revoked = await root.delete(f"{TOKENS_URL}/{row['id']}")
    assert revoked.status_code == 204, revoked.text
    assert_scim_error(await scim(raw, "GET", "/Users"), 401)
    assert (await root.get(TOKENS_URL)).json()[0]["revoked_at"] is not None

    [create_row] = await _audit("org.scim.token_create")
    [revoke_row] = await _audit("org.scim.token_revoke")
    assert create_row.user_id == people.ids["root"]
    assert revoke_row.payload == {"token_prefix": row["prefix"]}
    assert raw not in str(create_row.payload)


async def test_sessions_and_api_keys_are_refused(people: People, token: str) -> None:
    root = people["root"]
    # A signed-in browser: its cookie is not a SCIM credential.
    by_cookie = await root.get(f"{SCIM}/Users")
    assert_scim_error(by_cookie, 401)
    assert "Bearer" in by_cookie.headers["www-authenticate"]
    key = await root.post(
        f"{API}/orgs/{ACME}/me/api-keys", json={"name": "agent", "scope": "write"}
    )
    assert_scim_error(await scim(key.json()["token"], "GET", "/Users"), 401)
    assert_scim_error(await scim(None, "GET", "/Users"), 401)
    assert_scim_error(await scim("tripl_scim_not-a-token", "GET", "/Users"), 401)


async def test_a_token_of_another_org_is_404(people: People, token: str) -> None:
    root = people["root"]
    other = await root.post(f"{API}/orgs", json={"slug": "globex", "name": "Globex"})
    assert other.status_code == 201, other.text
    globex_token = await mint_token(root, "globex")

    assert_scim_error(await scim(globex_token, "GET", "/Users"), 404)
    assert_scim_error(await scim(token, "GET", "/Users", base="/scim/v2/globex"), 404)
    assert_scim_error(await scim(token, "GET", "/Users", base="/scim/v2/nope"), 404)
    ok = await scim(globex_token, "GET", "/Users", base="/scim/v2/globex")
    assert ok.status_code == 200, ok.text


async def test_a_suspended_org_is_403(acme: uuid.UUID, token: str) -> None:
    await set_org_status(acme, OrganizationStatus.suspended)
    assert_scim_error(await scim(token, "GET", "/Users"), 403)


async def test_service_provider_config_and_discovery(token: str) -> None:
    resp = await scim(token, "GET", "/ServiceProviderConfig")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/scim+json")
    body = resp.json()
    assert body["patch"] == {"supported": True}
    assert body["bulk"]["supported"] is False
    assert body["filter"] == {"supported": True, "maxResults": 200}
    assert body["sort"] == {"supported": False}
    assert body["etag"] == {"supported": False}
    assert body["changePassword"] == {"supported": False}

    types = (await scim(token, "GET", "/ResourceTypes")).json()
    assert {item["id"] for item in types["Resources"]} == {"User", "Group"}
    schemas = (await scim(token, "GET", "/Schemas")).json()
    assert USER_SCHEMA in {item["id"] for item in schemas["Resources"]}


# ── provisioning ────────────────────────────────────────────────────────────


async def test_create_a_user_in_a_verified_domain(
    people: People, acme: uuid.UUID, token: str
) -> None:
    email = f"nina@{DOMAIN}"
    resp = await scim(
        token,
        "POST",
        "/Users",
        user_body(
            email.upper(),
            externalId="okta-nina",
            name={"givenName": "Nina", "familyName": "Ivanova"},
        ),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["userName"] == email
    assert body["active"] is True
    assert body["externalId"] == "okta-nina"
    assert body["name"]["givenName"] == "Nina"
    assert resp.headers["location"].endswith(f"{SCIM}/Users/{body['id']}")

    user = await load_user(email)
    assert str(user.id) == body["id"]
    assert user.email_verified_at is not None
    assert user.is_platform_admin is False
    assert user.name == "Nina Ivanova"
    assert await org_role(acme, user.id) == "member"
    async with _new_client() as client:
        login = await client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
        assert login.status_code == 401

    [row] = await _audit("org.scim.user_provision")
    assert row.user_id is None
    assert row.organization_id == acme
    assert row.payload["via"] == "scim"
    assert row.payload["token_prefix"] == token[: len("tripl_scim_") + 6]

    again = await scim(token, "POST", "/Users", user_body(email))
    assert_scim_error(again, 409, "uniqueness")


async def test_create_in_an_unverified_domain_is_400(token: str) -> None:
    resp = await scim(token, "POST", "/Users", user_body("eve@elsewhere.example.com"))
    assert_scim_error(resp, 400, "invalidValue")
    assert resp.json()["detail"] == "email domain not verified for this organization"
    assert await _user_id("eve@elsewhere.example.com") is None
    assert_scim_error(await scim(token, "POST", "/Users", {"active": True}), 400)


async def test_linking_an_existing_account_touches_no_password_or_name(
    people: People, acme: uuid.UUID, token: str
) -> None:
    # carol@acme.example.com: an existing account in a verified domain, not a member.
    before = await load_user(f"carol@{DOMAIN}")
    resp = await scim(
        token,
        "POST",
        "/Users",
        user_body(f"carol@{DOMAIN}", name={"formatted": "Someone Else"}),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] == str(before.id)
    after = await load_user(f"carol@{DOMAIN}")
    assert after.name == before.name
    assert after.password_hash == before.password_hash
    assert await org_role(acme, after.id) == "member"
    async with _new_client() as client:
        login = await client.post(
            f"{API}/auth/login", json={"email": f"carol@{DOMAIN}", "password": PASSWORD}
        )
        assert login.status_code == 200, login.text
    [row] = await _audit("org.scim.user_link")
    assert row.payload["via"] == "scim"


async def test_an_unverified_account_is_verified_only_in_a_verified_domain(
    people: People, acme: uuid.UUID, token: str
) -> None:
    await people.register("dave", "dave@elsewhere.example.com")
    await people.register("sam", f"sam@{DOMAIN}")
    async with TestSessionLocal() as session:
        for email in ("dave@elsewhere.example.com", f"sam@{DOMAIN}"):
            user = await session.scalar(select(User).where(User.email == email))
            assert user is not None
            user.email_verified_at = None
        await session.commit()

    # Outside the verified domains an existing account is not linked at all
    # (below): add dave to the organization first, as an invitation would.
    async with TestSessionLocal() as session:
        await add_org_member(session, people.ids["dave"], "member", org_id=acme)
    await provision(token, "dave@elsewhere.example.com")
    dave = await load_user("dave@elsewhere.example.com")
    assert dave.email_verified_at is None
    assert await org_role(acme, dave.id) == "member"

    await provision(token, f"sam@{DOMAIN}")
    sam = await load_user(f"sam@{DOMAIN}")
    assert sam.email_verified_at is not None
    # Nobody had proved the address: the squatter's password stops working.
    async with _new_client() as client:
        login = await client.post(
            f"{API}/auth/login", json={"email": f"sam@{DOMAIN}", "password": PASSWORD}
        )
        assert login.status_code == 401


async def test_filter_and_pagination(people: People, acme: uuid.UUID, token: str) -> None:
    for name in ("u1", "u2", "u3"):
        await provision(token, f"{name}@{DOMAIN}", externalId=f"ext-{name}")

    found = await scim(token, "GET", "/Users", params={"filter": f'userName eq "U2@{DOMAIN}"'})
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["totalResults"] == 1
    assert body["Resources"][0]["userName"] == f"u2@{DOMAIN}"

    by_external = await scim(token, "GET", "/Users", params={"filter": 'externalId eq "ext-u3"'})
    assert [r["userName"] for r in by_external.json()["Resources"]] == [f"u3@{DOMAIN}"]
    by_email = await scim(
        token,
        "GET",
        "/Users",
        params={"filter": f'emails[type eq "work"].value eq "u1@{DOMAIN}"'},
    )
    assert by_email.json()["totalResults"] == 1
    none = await scim(token, "GET", "/Users", params={"filter": 'userName eq "ghost@x.example"'})
    assert none.json()["totalResults"] == 0
    assert_scim_error(
        await scim(token, "GET", "/Users", params={"filter": 'title co "x"'}), 400, "invalidFilter"
    )

    everyone = (await scim(token, "GET", "/Users")).json()
    total = everyone["totalResults"]
    # root, bob, mia (members) and the three provisioned.
    assert total == 6
    first = (await scim(token, "GET", "/Users", params={"startIndex": 1, "count": 2})).json()
    assert first["itemsPerPage"] == 2 and first["startIndex"] == 1
    assert first["totalResults"] == total
    rest = (await scim(token, "GET", "/Users", params={"startIndex": 3, "count": 10})).json()
    assert rest["itemsPerPage"] == total - 2
    ids = [r["id"] for r in first["Resources"]] + [r["id"] for r in rest["Resources"]]
    assert len(set(ids)) == total


# ── deprovisioning ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "operation",
    [
        # Okta: a path-less replace (its older connector sends the string).
        {"op": "replace", "value": {"active": "False"}},
        # Azure AD: a path, a capitalised op, a boolean.
        {"op": "Replace", "path": "active", "value": False},
    ],
    ids=["okta", "azure"],
)
async def test_active_false_deprovisions(
    people: People, acme: uuid.UUID, token: str, operation: dict[str, Any]
) -> None:
    mia = people["mia"]
    mia_id = people.ids["mia"]
    key = await mia.post(f"{API}/orgs/{ACME}/me/api-keys", json={"name": "mia", "scope": "read"})
    assert key.status_code == 201, key.text
    async with TestSessionLocal() as session:
        session.add(
            UserSsoIdentity(
                user_id=mia_id,
                organization_id=acme,
                issuer="https://idp.example.com",
                subject="mia-sub",
            )
        )
        await session.commit()

    resp = await scim(token, "PATCH", f"/Users/{mia_id}", patch(operation))
    assert resp.status_code == 200, resp.text
    assert resp.json()["active"] is False

    assert await org_role(acme, mia_id) is None
    async with TestSessionLocal() as session:
        stored_key = await session.scalar(select(ApiKey).where(ApiKey.name == "mia"))
        assert stored_key is not None and stored_key.revoked_at is not None
        assert await session.get(User, mia_id) is not None
        assert (
            await session.scalar(select(UserSsoIdentity).where(UserSsoIdentity.user_id == mia_id))
        ) is None
        link = await session.scalar(select(ScimUserLink).where(ScimUserLink.user_id == mia_id))
        assert link is not None and link.active is False
    # Still visible to the provider, inactive.
    read = await scim(token, "GET", f"/Users/{mia_id}")
    assert read.status_code == 200 and read.json()["active"] is False
    [row] = await _audit("org.scim.user_deactivate")
    assert row.user_id is None
    assert row.payload["changes"]["api_keys"] == 1
    assert row.payload["changes"]["sso_identities"] == 1


async def test_reactivation_brings_the_membership_back(
    people: People, acme: uuid.UUID, token: str
) -> None:
    mia_id = people.ids["mia"]
    off = await scim(token, "PATCH", f"/Users/{mia_id}", patch(_active(False)))
    assert off.status_code == 200, off.text
    blocked = select(SsoMembershipBlock).where(SsoMembershipBlock.user_id == mia_id)
    async with TestSessionLocal() as session:
        assert await session.scalar(blocked) is not None

    on = await scim(token, "PATCH", f"/Users/{mia_id}", patch(_active("True")))
    assert on.status_code == 200, on.text
    assert on.json()["active"] is True
    assert await org_role(acme, mia_id) == "member"
    async with TestSessionLocal() as session:
        assert await session.scalar(blocked) is None
    assert len(await _audit("org.scim.user_reactivate")) == 1


async def test_delete_deprovisions_and_keeps_the_account(
    people: People, acme: uuid.UUID, token: str
) -> None:
    created = await provision(token, f"leo@{DOMAIN}")
    gone = await scim(token, "DELETE", f"/Users/{created['id']}")
    assert gone.status_code == 204, gone.text
    user = await load_user(f"leo@{DOMAIN}")
    assert await org_role(acme, user.id) is None
    read = await scim(token, "GET", f"/Users/{created['id']}")
    assert read.status_code == 200 and read.json()["active"] is False
    [row] = await _audit("org.scim.user_deactivate")
    assert row.payload["deleted"] is True


async def test_the_last_owner_cannot_be_deprovisioned(
    people: People, acme: uuid.UUID, token: str
) -> None:
    root_id = people.ids["root"]
    resp = await scim(token, "PATCH", f"/Users/{root_id}", patch(_active(False)))
    assert_scim_error(resp, 409, "mutability")
    assert await org_role(acme, root_id) == "owner"
    assert_scim_error(await scim(token, "DELETE", f"/Users/{root_id}"), 409, "mutability")


async def test_username_cannot_change(people: People, token: str) -> None:
    mia_id = people.ids["mia"]
    resp = await scim(
        token,
        "PATCH",
        f"/Users/{mia_id}",
        patch({"op": "replace", "path": "userName", "value": "other@example.com"}),
    )
    assert_scim_error(resp, 400, "mutability")
    same = await scim(
        token,
        "PUT",
        f"/Users/{mia_id}",
        user_body(f"mia@{DOMAIN}", externalId="azure-mia", displayName="Mia M"),
    )
    assert same.status_code == 200, same.text
    assert same.json()["externalId"] == "azure-mia"
    # mia's address is in a verified domain: the account's name follows.
    assert (await load_user(f"mia@{DOMAIN}")).name == "Mia M"


async def test_other_organizations_users_are_invisible(people: People, token: str) -> None:
    stranger_id = people.ids["stranger"]
    assert_scim_error(await scim(token, "GET", f"/Users/{stranger_id}"), 404)
    assert_scim_error(
        await scim(
            token,
            "PATCH",
            f"/Users/{stranger_id}",
            patch(_active(False)),
        ),
        404,
    )
    assert_scim_error(await scim(token, "DELETE", f"/Users/{stranger_id}"), 404)
    assert_scim_error(await scim(token, "GET", "/Users/not-a-uuid"), 404)
    listing = (await scim(token, "GET", "/Users", params={"count": 200})).json()
    assert "stranger@example.com" not in {r["userName"] for r in listing["Resources"]}
    filtered = await scim(
        token, "GET", "/Users", params={"filter": 'userName eq "stranger@example.com"'}
    )
    assert filtered.json()["totalResults"] == 0


# ── review repairs ──────────────────────────────────────────────────────────


async def test_an_existing_account_outside_the_verified_domains_is_not_linked(
    people: People, acme: uuid.UUID, token: str
) -> None:
    """No platform-wide existence oracle, no stranger pulled into the organization."""
    unknown = await scim(token, "POST", "/Users", user_body("ghost@example.com"))
    known = await scim(token, "POST", "/Users", user_body("stranger@example.com", displayName="X"))
    assert_scim_error(known, 400, "invalidValue")
    assert known.json() == unknown.json()
    stranger_id = people.ids["stranger"]
    assert await org_role(acme, stranger_id) is None
    async with TestSessionLocal() as session:
        link = await session.scalar(select(ScimUserLink).where(ScimUserLink.user_id == stranger_id))
        assert link is None
    assert_scim_error(await scim(token, "GET", f"/Users/{stranger_id}"), 404)

    # A member in an unverified domain is already visible: linking it is fine
    # and leaves its role alone.
    bob = await scim(token, "POST", "/Users", user_body("bob@example.com"))
    assert bob.status_code == 201, bob.text
    assert await org_role(acme, people.ids["bob"]) == "admin"


async def _make_owner(org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    async with TestSessionLocal() as session:
        await session.execute(
            update(OrganizationMember)
            .where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id == user_id,
            )
            .values(role="owner")
        )
        await session.commit()


async def _token_revoked(raw_prefix: str) -> bool:
    async with TestSessionLocal() as session:
        row = await session.scalar(select(OrgScimToken).where(OrgScimToken.prefix == raw_prefix))
        assert row is not None
        return row.revoked_at is not None


def _prefix(raw: str) -> str:
    return raw[: len("tripl_scim_") + 6]


async def test_removing_an_owner_revokes_their_tokens(
    people: People, acme: uuid.UUID, token: str
) -> None:
    await _make_owner(acme, people.ids["bob"])
    bobs = await mint_token(people["bob"])
    assert (await scim(bobs, "GET", "/Users")).status_code == 200

    removed = await people["root"].delete(f"{API}/orgs/{ACME}/members/{people.ids['bob']}")
    assert removed.status_code == 200, removed.text
    assert_scim_error(await scim(bobs, "GET", "/Users"), 401)
    assert await _token_revoked(_prefix(bobs))
    # root's own token is untouched.
    assert (await scim(token, "GET", "/Users")).status_code == 200
    [row] = await _audit("org.scim.token_revoke")
    assert row.user_id is None and row.organization_id == acme
    assert row.payload == {"token_prefix": _prefix(bobs), "reason": "creator_no_longer_owner"}
    [removal] = await _audit("org.member_remove")
    assert removal.payload["scim_tokens"] == 1


async def test_demoting_an_owner_revokes_their_tokens(
    people: People, acme: uuid.UUID, token: str
) -> None:
    await _make_owner(acme, people.ids["bob"])
    bobs = await mint_token(people["bob"])
    demoted = await people["root"].patch(
        f"{API}/orgs/{ACME}/members/{people.ids['bob']}", json={"role": "admin"}
    )
    assert demoted.status_code == 200, demoted.text
    assert await _token_revoked(_prefix(bobs))
    assert_scim_error(await scim(bobs, "GET", "/Users"), 401)


async def test_transferring_ownership_revokes_the_old_owners_tokens(
    people: People, acme: uuid.UUID, token: str
) -> None:
    moved = await people["root"].post(
        f"{API}/orgs/{ACME}/transfer-ownership", json={"user_id": str(people.ids["bob"])}
    )
    assert moved.status_code == 200, moved.text
    assert await _token_revoked(_prefix(token))
    assert_scim_error(await scim(token, "GET", "/Users"), 401)


async def test_a_token_whose_creator_is_no_longer_an_owner_is_refused(
    people: People, acme: uuid.UUID, token: str
) -> None:
    """The backstop: a role change that did not revoke still leaves the token dead."""
    await _make_owner(acme, people.ids["bob"])
    async with TestSessionLocal() as session:
        await session.execute(
            update(OrganizationMember)
            .where(
                OrganizationMember.organization_id == acme,
                OrganizationMember.user_id == people.ids["root"],
            )
            .values(role="admin")
        )
        await session.commit()
    assert not await _token_revoked(_prefix(token))
    assert_scim_error(await scim(token, "GET", "/Users"), 401)


async def test_a_manual_removal_is_not_undone_by_scim(
    people: People, acme: uuid.UUID, token: str
) -> None:
    nina = await provision(token, f"nina@{DOMAIN}")
    nina_id = uuid.UUID(nina["id"])
    removed = await people["root"].delete(f"{API}/orgs/{ACME}/members/{nina_id}")
    assert removed.status_code == 200, removed.text
    read = await scim(token, "GET", f"/Users/{nina_id}")
    assert read.status_code == 200 and read.json()["active"] is False

    # A PUT without ``active`` is not a re-activation.
    body = {"schemas": [USER_SCHEMA], "userName": f"nina@{DOMAIN}", "displayName": "Nina"}
    put = await scim(token, "PUT", f"/Users/{nina_id}", body)
    assert put.status_code == 200, put.text
    assert put.json()["active"] is False
    # Asking for it explicitly is refused, and the SSO block stays.
    again = await scim(token, "PATCH", f"/Users/{nina_id}", patch(_active(True)))
    assert_scim_error(again, 409, "mutability")
    assert_scim_error(await scim(token, "POST", "/Users", user_body(f"nina@{DOMAIN}")), 409)
    assert await org_role(acme, nina_id) is None
    blocked = select(SsoMembershipBlock).where(SsoMembershipBlock.user_id == nina_id)
    async with TestSessionLocal() as session:
        assert await session.scalar(blocked) is not None

    # An unlinked member removed by hand: a POST does not pull them back either.
    mia = await people["root"].delete(f"{API}/orgs/{ACME}/members/{people.ids['mia']}")
    assert mia.status_code == 200, mia.text
    assert_scim_error(
        await scim(token, "POST", "/Users", user_body(f"mia@{DOMAIN}")), 409, "mutability"
    )
    assert await org_role(acme, people.ids["mia"]) is None

    # Back in through an invitation: the provider manages them again.
    async with TestSessionLocal() as session:
        await add_org_member(session, nina_id, "member", org_id=acme)
    off = await scim(token, "PATCH", f"/Users/{nina_id}", patch(_active(False)))
    assert off.status_code == 200, off.text
    on = await scim(token, "PATCH", f"/Users/{nina_id}", patch(_active(True)))
    assert on.status_code == 200, on.text
    assert await org_role(acme, nina_id) == "member"


async def test_put_without_active_leaves_a_deprovisioned_user_inactive(
    acme: uuid.UUID, token: str
) -> None:
    leo = await provision(token, f"leo@{DOMAIN}")
    off = await scim(token, "PATCH", f"/Users/{leo['id']}", patch(_active(False)))
    assert off.status_code == 200, off.text
    body = {"schemas": [USER_SCHEMA], "userName": f"leo@{DOMAIN}", "externalId": "okta-leo"}
    put = await scim(token, "PUT", f"/Users/{leo['id']}", body)
    assert put.status_code == 200, put.text
    assert put.json()["active"] is False
    assert await org_role(acme, uuid.UUID(leo["id"])) is None


async def test_post_after_delete_brings_the_same_account_back(acme: uuid.UUID, token: str) -> None:
    leo = await provision(token, f"leo@{DOMAIN}")
    assert (await scim(token, "DELETE", f"/Users/{leo['id']}")).status_code == 204
    again = await scim(token, "POST", "/Users", user_body(f"leo@{DOMAIN}", externalId="okta-2"))
    assert again.status_code == 201, again.text
    assert again.json()["id"] == leo["id"]
    assert again.json()["active"] is True
    assert again.json()["externalId"] == "okta-2"
    assert await org_role(acme, uuid.UUID(leo["id"])) == "member"
    assert len(await _audit("org.scim.user_reactivate")) == 1


async def test_hostile_paging_and_bodies_are_400_not_500(token: str) -> None:
    huge = await scim(token, "GET", "/Users", params={"startIndex": str(2**70)})
    assert huge.status_code == 200, huge.text
    assert huge.json()["Resources"] == []
    groups = await scim(token, "GET", "/Groups", params={"startIndex": str(2**70)})
    assert groups.status_code == 200, groups.text

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/scim+json"}
    async with _new_client() as client:
        brackets = await client.post(f"{SCIM}/Users", content=b"[" * 50_000, headers=headers)
        assert_scim_error(brackets, 400, "invalidSyntax")
        nested: dict[str, Any] = {"userName": f"deep@{DOMAIN}"}
        for _ in range(200):
            nested = {"x": nested}
        deep = await client.post(
            f"{SCIM}/Users", json={"userName": "a@b.example", "n": nested}, headers=headers
        )
        assert_scim_error(deep, 400, "invalidSyntax")


async def test_failed_authentication_is_rate_limited_per_address(
    token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tripl.middleware.rate_limit import SCIM_AUTH_FAILURE_RATE_LIMIT_PER_MINUTE

    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    for _ in range(SCIM_AUTH_FAILURE_RATE_LIMIT_PER_MINUTE):
        assert_scim_error(await scim("tripl_scim_bogus", "GET", "/Users"), 401)
    limited = await scim("tripl_scim_bogus", "GET", "/Users")
    assert_scim_error(limited, 429)
    assert limited.headers["retry-after"]
    # A valid token never draws on the failure bucket.
    assert (await scim(token, "GET", "/Users")).status_code == 200


async def test_unknown_paths_and_methods_answer_in_the_scim_format(
    token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for method, path in (("GET", "/Bulk"), ("POST", "/Me"), ("DELETE", "/ServiceProviderConfig")):
        assert_scim_error(await scim(token, method, path), 404)
    assert_scim_error(await scim(None, "GET", "/Bulk"), 404)

    monkeypatch.setattr(settings, "max_request_body_mb", 1)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/scim+json"}
    async with _new_client() as client:
        too_big = await client.post(
            f"{SCIM}/Users", content=b" " * (1024 * 1024 + 1), headers=headers
        )
    assert_scim_error(too_big, 413)
