"""Sign a test account up, in either deployment mode. Not a test module.

``POST /auth/register`` on a hosted instance creates an organization and an
UNVERIFIED account (F20 hosted sign-up), which the hosted gate then refuses
everywhere outside ``/auth``. Fixtures that build a hosted world and place
members into organizations themselves want what hosted registration used to
give them instead: a plain member of the default organization, never a
platform admin — and verified, so the gate lets them through. On a hosted
instance :func:`sign_up` writes exactly that account and signs the client in
through ``/auth/login``; self-hosted it is ``/auth/register``, unchanged.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient

from tripl import tenancy
from tripl.auth_utils import hash_password, normalize_email
from tripl.models.domain_enums import OrganizationRole
from tripl.models.organization import DEFAULT_ORG_ID, OrganizationMember
from tripl.models.user import User
from tripl.tests.conftest import TestSessionLocal

API = "/api/v1"


async def sign_up(client: AsyncClient, *, email: str, password: str, name: str) -> uuid.UUID:
    """Create ``email``'s account, sign ``client`` in, and return the user's id."""
    if not tenancy.multi_tenant():
        resp = await client.post(
            f"{API}/auth/register", json={"email": email, "password": password, "name": name}
        )
        assert resp.status_code == 201, resp.text
        return uuid.UUID(resp.json()["id"])

    async with TestSessionLocal() as session:
        user = User(
            email=normalize_email(email),
            name=name,
            password_hash=hash_password(password),
            is_platform_admin=False,
            email_verified_at=datetime.now(UTC),
        )
        session.add(user)
        await session.flush()
        session.add(
            OrganizationMember(
                organization_id=DEFAULT_ORG_ID,
                user_id=user.id,
                role=OrganizationRole.member.value,
            )
        )
        await session.commit()
        user_id = user.id
    resp = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return user_id
