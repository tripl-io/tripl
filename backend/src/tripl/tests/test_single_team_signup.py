"""Sign-up on a single-team instance, and the tenancy policy it runs under.

Community's policy (``tripl.tenancy``) is one team's instance: the first
account owns the default organization and is a platform admin, later ones join
it as members, every account is verified at creation and nothing is gated,
only a platform admin creates organizations. A multi-tenant service is an
extension's (tripl Enterprise), and ``DEPLOYMENT_MODE=hosted`` without one
refuses to start.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl import extensions, tenancy
from tripl.config import settings
from tripl.models.email_verification_token import EmailVerificationToken
from tripl.models.organization import Organization
from tripl.tests._tenancy import POLICY, use_multi_tenant
from tripl.tests._verification_mail import API, PASSWORD, install_mail_sink, new_client
from tripl.tests.conftest import TestSessionLocal

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def clients() -> AsyncIterator[list[AsyncClient]]:
    made = [new_client() for _ in range(2)]
    try:
        yield made
    finally:
        for client in made:
            await client.aclose()


async def test_register_joins_the_default_organization_and_is_never_gated(
    clients: list[AsyncClient],
) -> None:
    root, second = clients
    first = await root.post(
        f"{API}/auth/register",
        # Organization fields are ignored on a single-team instance.
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
    # Every account is verified at creation; nothing enforces it.
    assert later.json()["email_verified"] is True
    assert later.json()["role"] == "member"
    assert (await second.get(f"{API}/projects")).status_code == 200
    async with TestSessionLocal() as session:
        assert (
            await session.scalar(select(Organization.id).where(Organization.slug == "not-a-slug"))
        ) is None


async def test_only_a_platform_admin_creates_organizations(clients: list[AsyncClient]) -> None:
    root, member = clients
    for client, email in ((root, "root@example.com"), (member, "member@example.com")):
        resp = await client.post(
            f"{API}/auth/register", json={"email": email, "password": PASSWORD}
        )
        assert resp.status_code == 201, resp.text
    refused = await member.post(f"{API}/orgs", json={"slug": "nope", "name": "Nope"})
    assert refused.status_code == 403
    assert (await root.post(f"{API}/orgs", json={"slug": "yes", "name": "Yes"})).status_code == 201


async def test_verification_is_a_no_op(
    clients: list[AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = install_mail_sink(monkeypatch)
    monkeypatch.setattr(settings, "platform_admin_emails", ["member@example.com"])
    owner, member = clients
    await owner.post(
        f"{API}/auth/register", json={"email": "owner@example.com", "password": PASSWORD}
    )
    await member.post(
        f"{API}/auth/register", json={"email": "member@example.com", "password": PASSWORD}
    )
    assert (await member.post(f"{API}/auth/verify-email/request")).status_code == 204
    me = (await member.get(f"{API}/auth/me")).json()
    assert sent == []
    async with TestSessionLocal() as session:
        assert (await session.scalars(select(EmailVerificationToken))).all() == []
    assert me["email_verified"] is True
    # PLATFORM_ADMIN_EMAILS is a hosted-instance rule.
    assert me["is_platform_admin"] is False


async def test_status_follows_the_policy(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    single = (await anon_client.get(f"{API}/auth/status")).json()
    assert single["deployment_mode"] == "self_hosted"
    assert single["email_verification_required"] is False
    assert single["has_users"] is False

    use_multi_tenant(monkeypatch)
    many = (await anon_client.get(f"{API}/auth/status")).json()
    assert many["deployment_mode"] == "hosted"
    assert many["email_verification_required"] is True
    # Never reveals an empty multi-tenant instance.
    assert many["has_users"] is True


async def test_hosted_without_an_extension_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    with extensions.override_extensions([]):
        assert tenancy.policy().multi_tenant is False
        with pytest.raises(RuntimeError, match="needs an extension"):
            tenancy.check_deployment_mode()
    monkeypatch.setattr(settings, "deployment_mode", "self_hosted")
    with extensions.override_extensions([]):
        tenancy.check_deployment_mode()


async def test_an_extension_supplies_the_multi_tenant_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Hosting(extensions.Extension):
        def tenancy(self) -> tenancy.TenancyPolicy | None:
            return POLICY

    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    with extensions.override_extensions([extensions.Extension(), Hosting()]):
        assert tenancy.policy() is POLICY
        tenancy.check_deployment_mode()
