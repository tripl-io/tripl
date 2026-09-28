"""Hosted sign-up (F20, GH #273).

With ``DEPLOYMENT_MODE=hosted``, ``POST /auth/register`` takes the new
organization too: the account creates and owns it, joins no other one, starts
unverified and is never a platform admin. The hosted gate then refuses the
account everywhere outside ``/auth`` until it verifies its address. Joining an
existing organization is what invitations are for, and accepting one while
signed in needs a verified address; redeeming one into a new account leaves it
unverified and mails a link. ``PLATFORM_ADMIN_EMAILS`` is granted only by a
confirmed verification link. A self-hosted instance is unchanged, except that
every account there is recorded as verified at creation.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.config import REGISTRATION_DISABLED, settings
from tripl.models.audit_log import AuditLog
from tripl.models.email_verification_token import EmailVerificationToken
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.user import User
from tripl.services import email_verification_service
from tripl.tests._verification_mail import (
    API,
    PASSWORD,
    confirm,
    hosted_sign_up,
    install_mail_sink,
    new_client,
    token_from,
)
from tripl.tests.conftest import TestSessionLocal

pytestmark = pytest.mark.asyncio

NOT_VERIFIED = email_verification_service.EMAIL_NOT_VERIFIED_MESSAGE


@pytest.fixture
def mail(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """A hosted instance whose operator relay can send; the verification mailbox."""
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    return install_mail_sink(monkeypatch)


@pytest.fixture
async def clients() -> AsyncIterator[list[AsyncClient]]:
    made = [new_client() for _ in range(3)]
    try:
        yield made
    finally:
        for client in made:
            await client.aclose()


async def _user(email: str) -> User | None:
    async with TestSessionLocal() as session:
        user: User | None = await session.scalar(select(User).where(User.email == email))
    return user


async def _memberships(user_id: uuid.UUID) -> dict[uuid.UUID, str]:
    async with TestSessionLocal() as session:
        rows = await session.execute(
            select(OrganizationMember.organization_id, OrganizationMember.role).where(
                OrganizationMember.user_id == user_id
            )
        )
    return {org_id: str(role) for org_id, role in rows.all()}


async def _signed_up(client: AsyncClient, email: str, slug: str) -> None:
    resp = await hosted_sign_up(client, email, org_slug=slug)
    assert resp.status_code == 201, resp.text


# ── registration ─────────────────────────────────────────────────────────────


async def test_hosted_sign_up_creates_the_org_and_its_owner(
    mail: list[dict[str, str]], clients: list[AsyncClient]
) -> None:
    [client, *_] = clients
    resp = await hosted_sign_up(
        client, "founder@example.com", org_slug="founders", org_name=" Founders Inc "
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["orgs"] == [
        {"slug": "founders", "name": "Founders Inc", "role": "owner", "status": "active"}
    ]
    assert body["role"] == "owner"
    assert body["email_verified"] is False
    assert body["is_platform_admin"] is False
    assert "tripl_session=" in resp.headers["set-cookie"]

    async with TestSessionLocal() as session:
        org_id = await session.scalar(
            select(Organization.id).where(Organization.slug == "founders")
        )
        [audit] = (
            await session.scalars(select(AuditLog).where(AuditLog.action == "org.create"))
        ).all()
    assert org_id is not None
    # Its own organization, and not the default one.
    assert await _memberships(uuid.UUID(body["id"])) == {org_id: "owner"}
    assert DEFAULT_ORG_ID not in await _memberships(uuid.UUID(body["id"]))
    assert audit.organization_id == org_id

    # The verification link went to the address, through the operator relay.
    assert [m["recipient"] for m in mail] == ["founder@example.com"]
    token_from(mail[0])


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"org_name": "Only A Name"},
        {"org_slug": "only-a-slug"},
        {"org_name": "   ", "org_slug": "blank-name"},
        {"org_name": "Reserved", "org_slug": "default"},
        {"org_name": "Bad", "org_slug": "Bad Slug"},
    ],
)
async def test_hosted_sign_up_needs_valid_org_fields(
    mail: list[dict[str, str]], anon_client: AsyncClient, extra: dict[str, str]
) -> None:
    resp = await anon_client.post(
        f"{API}/auth/register",
        json={"email": "incomplete@example.com", "password": PASSWORD, **extra},
    )
    assert resp.status_code == 422, resp.text
    assert await _user("incomplete@example.com") is None
    assert mail == []


async def test_a_racing_duplicate_email_is_409_not_500(
    mail: list[dict[str, str]], clients: list[AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tripl.services import auth_service

    first, second, _ = clients
    await _signed_up(first, "race@example.com", "race-one")

    # A concurrent sign-up that passed the pre-check before the other committed:
    # the unique constraint is what catches it.
    async def _nobody(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(auth_service, "_get_user_by_email", _nobody)
    again = await hosted_sign_up(second, "race@example.com", org_slug="race-two")
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == "User with this email already exists"
    async with TestSessionLocal() as session:
        orphan = await session.scalar(
            select(Organization.id).where(Organization.slug == "race-two")
        )
        users = (await session.scalars(select(User).where(User.email == "race@example.com"))).all()
    assert orphan is None
    assert len(users) == 1
    assert len(mail) == 1


async def test_a_taken_slug_is_409_and_creates_nothing(
    mail: list[dict[str, str]], clients: list[AsyncClient]
) -> None:
    first, second, _ = clients
    await _signed_up(first, "one@example.com", "taken")
    again = await hosted_sign_up(second, "two@example.com", org_slug="taken")
    assert again.status_code == 409, again.text
    assert await _user("two@example.com") is None
    assert len(mail) == 1


async def test_registration_disabled_closes_hosted_sign_up(
    mail: list[dict[str, str]], anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No first-user bootstrap on a hosted instance: empty, and still closed.
    monkeypatch.setattr(settings, "registration_mode", REGISTRATION_DISABLED)
    resp = await hosted_sign_up(anon_client, "first@example.com", org_slug="first")
    assert resp.status_code == 403, resp.text
    assert await _user("first@example.com") is None
    status = (await anon_client.get(f"{API}/auth/status")).json()
    assert status["registration_enabled"] is False


async def test_hosted_sign_up_needs_working_email(
    mail: list[dict[str, str]], anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "smtp_host", "")
    resp = await hosted_sign_up(anon_client, "nomail@example.com", org_slug="nomail")
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == "Email delivery is not configured"
    assert await _user("nomail@example.com") is None
    async with TestSessionLocal() as session:
        nomail = await session.scalar(select(Organization.id).where(Organization.slug == "nomail"))
    assert nomail is None


# ── the gate ─────────────────────────────────────────────────────────────────


async def test_an_unverified_account_reaches_only_auth_routes(
    mail: list[dict[str, str]], anon_client: AsyncClient
) -> None:
    await _signed_up(anon_client, "gated@example.com", "gated")

    projects = await anon_client.get(f"{API}/projects")
    assert projects.status_code == 403
    assert projects.json()["detail"] == NOT_VERIFIED
    org_path = await anon_client.get(f"{API}/orgs/gated/projects")
    assert org_path.status_code == 403
    assert (await anon_client.get(f"{API}/orgs")).status_code == 403

    me = await anon_client.get(f"{API}/auth/me")
    assert me.status_code == 200
    assert me.json()["email_verified"] is False

    assert (await confirm(anon_client, token_from(mail[0]))).status_code == 204
    assert (await anon_client.get(f"{API}/projects")).status_code == 200
    assert (await anon_client.get(f"{API}/auth/me")).json()["email_verified"] is True


async def test_platform_admin_emails_are_granted_on_verification_not_sign_up(
    mail: list[dict[str, str]], anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "platform_admin_emails", ["operator@example.com"])
    resp = await hosted_sign_up(anon_client, "Operator@Example.com", org_slug="ops")
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_platform_admin"] is False
    user = await _user("operator@example.com")
    assert user is not None and user.is_platform_admin is False

    assert (await confirm(anon_client, token_from(mail[0]))).status_code == 204
    user = await _user("operator@example.com")
    assert user is not None and user.is_platform_admin is True


# ── creating more organizations ──────────────────────────────────────────────


async def test_a_verified_hosted_account_creates_organizations(
    mail: list[dict[str, str]], anon_client: AsyncClient
) -> None:
    await _signed_up(anon_client, "maker@example.com", "maker")
    unverified = await anon_client.post(f"{API}/orgs", json={"slug": "second", "name": "Second"})
    assert unverified.status_code == 403
    assert unverified.json()["detail"] == NOT_VERIFIED

    assert (await confirm(anon_client, token_from(mail[0]))).status_code == 204
    created = await anon_client.post(f"{API}/orgs", json={"slug": "second", "name": "Second"})
    assert created.status_code == 201, created.text
    assert created.json()["role"] == "owner"
    user = await _user("maker@example.com")
    assert user is not None and user.is_platform_admin is False


async def test_a_self_hosted_member_still_cannot_create_organizations(
    clients: list[AsyncClient],
) -> None:
    root, member, _ = clients
    for client, email in ((root, "root@example.com"), (member, "member@example.com")):
        resp = await client.post(
            f"{API}/auth/register", json={"email": email, "password": PASSWORD}
        )
        assert resp.status_code == 201, resp.text
    refused = await member.post(f"{API}/orgs", json={"slug": "nope", "name": "Nope"})
    assert refused.status_code == 403
    assert (await root.post(f"{API}/orgs", json={"slug": "yes", "name": "Yes"})).status_code == 201


# ── self-hosted is unchanged ─────────────────────────────────────────────────


async def test_self_hosted_register_is_unchanged_and_never_gated(
    clients: list[AsyncClient],
) -> None:
    root, second, _ = clients
    first = await root.post(
        f"{API}/auth/register",
        # Organization fields are ignored on a self-hosted instance.
        json={
            "email": "boot@example.com",
            "password": PASSWORD,
            "org_name": "Ignored",
            "org_slug": "Not A Slug",
        },
    )
    assert first.status_code == 201, first.text
    assert first.json()["is_platform_admin"] is True
    assert first.json()["email_verified"] is True
    assert [(org["slug"], org["role"]) for org in first.json()["orgs"]] == [("default", "owner")]

    later = await second.post(
        f"{API}/auth/register", json={"email": "later@example.com", "password": PASSWORD}
    )
    assert later.status_code == 201, later.text
    # Every self-hosted account is verified at creation; nothing enforces it.
    assert later.json()["email_verified"] is True
    assert later.json()["role"] == "member"
    assert (await second.get(f"{API}/projects")).status_code == 200
    async with TestSessionLocal() as session:
        assert (
            await session.scalar(select(Organization.id).where(Organization.slug == "not-a-slug"))
        ) is None


# ── /auth/status ─────────────────────────────────────────────────────────────


async def test_status_in_both_modes(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    self_hosted = (await anon_client.get(f"{API}/auth/status")).json()
    assert self_hosted["deployment_mode"] == "self_hosted"
    assert self_hosted["email_verification_required"] is False
    assert self_hosted["has_users"] is False

    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    hosted = (await anon_client.get(f"{API}/auth/status")).json()
    assert hosted["deployment_mode"] == "hosted"
    assert hosted["email_verification_required"] is True
    # Never reveals an empty hosted instance.
    assert hosted["has_users"] is True
    assert hosted["registration_enabled"] is True


# ── invitations ──────────────────────────────────────────────────────────────


async def _invite(owner: AsyncClient, org_slug: str, email: str) -> str:
    created = await owner.post(
        f"{API}/orgs/{org_slug}/users/invitations", json={"email": email, "role": "member"}
    )
    assert created.status_code == 201, created.text
    return str(created.json()["accept_path"]).rsplit("/", 1)[-1]


async def test_accepting_while_signed_in_needs_a_verified_address(
    mail: list[dict[str, str]], clients: list[AsyncClient]
) -> None:
    owner, joiner, _ = clients
    await _signed_up(owner, "host@example.com", "host-co")
    assert (await confirm(owner, token_from(mail[-1]))).status_code == 204
    token = await _invite(owner, "host-co", "joiner@example.com")

    await _signed_up(joiner, "joiner@example.com", "joiner-co")
    refused = await joiner.post(f"{API}/auth/invitations/{token}/accept", json={})
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Verify your email address before accepting an invitation."

    assert (await confirm(joiner, token_from(mail[-1]))).status_code == 204
    accepted = await joiner.post(f"{API}/auth/invitations/{token}/accept", json={})
    assert accepted.status_code == 200, accepted.text
    assert {"slug": "host-co", "name": "Some Org", "role": "member"} in accepted.json()["orgs"]


async def test_a_new_account_from_an_invitation_starts_unverified_and_gets_a_link(
    mail: list[dict[str, str]], clients: list[AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Listed, and still not granted by the invitation: the inviter held the link.
    monkeypatch.setattr(settings, "platform_admin_emails", ["invitee@example.com"])
    owner, invitee, _ = clients
    await _signed_up(owner, "boss@example.com", "boss-co")
    assert (await confirm(owner, token_from(mail[-1]))).status_code == 204
    token = await _invite(owner, "boss-co", "invitee@example.com")

    joined = await invitee.post(
        f"{API}/auth/invitations/{token}/accept", json={"password": PASSWORD}
    )
    assert joined.status_code == 201, joined.text
    assert joined.json()["email_verified"] is False
    assert joined.json()["is_platform_admin"] is False
    assert [org["slug"] for org in joined.json()["orgs"]] == ["boss-co"]
    assert [m["recipient"] for m in mail][-1] == "invitee@example.com"
    gated = await invitee.get(f"{API}/projects")
    assert gated.status_code == 403
    assert gated.json()["detail"] == NOT_VERIFIED

    # Confirmed from the invitee's own session: verified, and the listed
    # address becomes a platform admin now.
    assert (await confirm(invitee, token_from(mail[-1]))).status_code == 204
    assert (await invitee.get(f"{API}/projects")).status_code == 200
    user = await _user("invitee@example.com")
    assert user is not None
    assert user.email_verified_at is not None
    assert user.is_platform_admin is True


async def test_a_self_hosted_invitation_account_is_verified_at_creation(
    clients: list[AsyncClient],
) -> None:
    owner, invitee, _ = clients
    boot = await owner.post(
        f"{API}/auth/register", json={"email": "root@example.com", "password": PASSWORD}
    )
    assert boot.status_code == 201, boot.text
    token = await _invite(owner, "default", "guest@example.com")
    joined = await invitee.post(
        f"{API}/auth/invitations/{token}/accept", json={"password": PASSWORD}
    )
    assert joined.status_code == 201, joined.text
    assert joined.json()["email_verified"] is True
    async with TestSessionLocal() as session:
        assert (await session.scalars(select(EmailVerificationToken))).all() == []
