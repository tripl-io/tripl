"""Email verification tokens and endpoints (F20 hosted sign-up, GH #273).

``POST /auth/verify-email/request`` mails the signed-in account a fresh,
single-use, 24-hour link (superseding every earlier one); ``POST
/auth/verify-email/confirm`` redeems it, from a session of that same account
(401 without one), and signs every other session of the account out. Unknown,
expired and used tokens — and a token of another account — are one uniform
400. A confirmed password reset proves the address too (and revokes the
account's API keys), but only a confirmed link grants ``PLATFORM_ADMIN_EMAILS``.
Only the HMAC digest of a token is stored. Self-hosted, every account is
verified at creation and a request is a no-op.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from tripl.auth_utils import hash_session_token
from tripl.config import settings
from tripl.models.api_key import ApiKey
from tripl.models.email_verification_token import EmailVerificationToken
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

REQUEST_URL = f"{API}/auth/verify-email/request"
INVALID = email_verification_service.INVALID_VERIFICATION_MESSAGE


@pytest.fixture
def mail(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    return install_mail_sink(monkeypatch)


async def _signed_up(client: AsyncClient, email: str = "verify@example.com") -> None:
    resp = await hosted_sign_up(client, email, org_slug=email.split("@", 1)[0])
    assert resp.status_code == 201, resp.text


async def _tokens() -> list[EmailVerificationToken]:
    async with TestSessionLocal() as session:
        return list((await session.scalars(select(EmailVerificationToken))).all())


async def _user(email: str) -> User:
    async with TestSessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == email))
    assert user is not None
    return user


async def test_confirm_verifies_and_is_single_use(
    mail: list[dict[str, str]], anon_client: AsyncClient
) -> None:
    await _signed_up(anon_client)
    token = token_from(mail[0])
    [row] = await _tokens()
    # Only the keyed digest is stored.
    assert row.token_hash == hash_session_token(token)
    assert token not in row.token_hash
    assert row.expires_at - datetime.now(UTC) > timedelta(hours=23)

    assert (await confirm(anon_client, token)).status_code == 204
    assert (await _user("verify@example.com")).email_verified_at is not None
    [used] = await _tokens()
    assert used.used_at is not None

    replay = await confirm(anon_client, token)
    assert replay.status_code == 400
    assert replay.json()["detail"] == INVALID


async def test_unknown_and_expired_tokens_are_the_same_400(
    mail: list[dict[str, str]], anon_client: AsyncClient
) -> None:
    await _signed_up(anon_client)
    unknown = await confirm(anon_client, "not-a-real-token")
    assert (unknown.status_code, unknown.json()["detail"]) == (400, INVALID)

    async with TestSessionLocal() as session:
        past = datetime.now(UTC) - timedelta(seconds=1)
        await session.execute(update(EmailVerificationToken).values(expires_at=past))
        await session.commit()
    expired = await confirm(anon_client, token_from(mail[0]))
    assert (expired.status_code, expired.json()["detail"]) == (400, INVALID)
    assert (await _user("verify@example.com")).email_verified_at is None


async def test_confirm_needs_a_session(
    mail: list[dict[str, str]], anon_client: AsyncClient
) -> None:
    await _signed_up(anon_client)
    async with new_client() as elsewhere:
        refused = await confirm(elsewhere, token_from(mail[0]))
    assert refused.status_code == 401
    assert refused.json()["detail"] == "Sign in to confirm your email address."
    assert (await _user("verify@example.com")).email_verified_at is None
    # Not consumed: the account holder can still use it.
    assert (await confirm(anon_client, token_from(mail[0]))).status_code == 204


async def test_confirm_from_another_accounts_session_is_400_and_leaves_the_token(
    mail: list[dict[str, str]], anon_client: AsyncClient
) -> None:
    await _signed_up(anon_client, "alpha@example.com")
    async with new_client() as other:
        await _signed_up(other, "beta@example.com")
        refused = await confirm(other, token_from(mail[0]))
        assert (refused.status_code, refused.json()["detail"]) == (400, INVALID)
    alpha = await _user("alpha@example.com")
    assert alpha.email_verified_at is None
    assert (await _user("beta@example.com")).email_verified_at is None
    [alpha_token] = [t for t in await _tokens() if t.user_id == alpha.id]
    assert alpha_token.used_at is None

    assert (await confirm(anon_client, token_from(mail[0]))).status_code == 204
    assert (await _user("alpha@example.com")).email_verified_at is not None


async def test_confirm_signs_every_other_session_out(
    mail: list[dict[str, str]], anon_client: AsyncClient
) -> None:
    await _signed_up(anon_client)
    async with new_client() as second:
        login = await second.post(
            f"{API}/auth/login", json={"email": "verify@example.com", "password": PASSWORD}
        )
        assert login.status_code == 200, login.text
        assert (await second.get(f"{API}/auth/me")).status_code == 200

        assert (await confirm(anon_client, token_from(mail[0]))).status_code == 204
        assert (await second.get(f"{API}/auth/me")).status_code == 401
    # The confirming session stays.
    me = await anon_client.get(f"{API}/auth/me")
    assert me.status_code == 200
    assert me.json()["email_verified"] is True


async def test_a_resend_supersedes_the_earlier_link(
    mail: list[dict[str, str]], anon_client: AsyncClient
) -> None:
    await _signed_up(anon_client)
    assert (await anon_client.post(REQUEST_URL)).status_code == 204
    assert [m["recipient"] for m in mail] == ["verify@example.com"] * 2
    assert len(await _tokens()) == 1

    stale = await confirm(anon_client, token_from(mail[0]))
    assert stale.status_code == 400
    assert (await confirm(anon_client, token_from(mail[1]))).status_code == 204


async def test_request_is_a_no_op_once_verified(
    mail: list[dict[str, str]], anon_client: AsyncClient
) -> None:
    await _signed_up(anon_client)
    assert (await confirm(anon_client, token_from(mail[0]))).status_code == 204
    assert (await anon_client.post(REQUEST_URL)).status_code == 204
    assert len(mail) == 1


async def test_request_is_503_without_working_email(
    mail: list[dict[str, str]], anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _signed_up(anon_client)
    monkeypatch.setattr(settings, "smtp_from_address", "")
    resp = await anon_client.post(REQUEST_URL)
    assert resp.status_code == 503
    assert len(mail) == 1


async def test_request_needs_a_session(anon_client: AsyncClient) -> None:
    assert (await anon_client.post(REQUEST_URL)).status_code == 401


def _capture_resets(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from tripl.api.v1 import auth as auth_api

    resets: list[str] = []

    def _capture(*, recipient: str, reset_link: str, email_config: Any) -> None:
        del recipient, email_config
        resets.append(reset_link.split("reset_token=", 1)[1])

    monkeypatch.setattr(auth_api, "_send_password_reset_email", _capture)
    return resets


async def _reset_password(client: AsyncClient, resets: list[str], email: str) -> None:
    requested = await client.post(f"{API}/auth/password-reset/request", json={"email": email})
    assert requested.status_code == 200
    confirmed = await client.post(
        f"{API}/auth/password-reset/confirm",
        json={"token": resets[-1], "new_password": "Another-Password9"},
    )
    assert confirmed.status_code == 200, confirmed.text


async def test_a_confirmed_password_reset_verifies_the_address_but_grants_no_admin(
    mail: list[dict[str, str]], anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    resets = _capture_resets(monkeypatch)
    monkeypatch.setattr(settings, "platform_admin_emails", ["verify@example.com"])
    await _signed_up(anon_client)
    await _reset_password(anon_client, resets, "verify@example.com")
    user = await _user("verify@example.com")
    assert user.email_verified_at is not None
    # Only a confirmed verification link grants PLATFORM_ADMIN_EMAILS.
    assert user.is_platform_admin is False


async def test_a_confirmed_password_reset_revokes_the_accounts_api_keys(
    mail: list[dict[str, str]], anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    resets = _capture_resets(monkeypatch)
    await _signed_up(anon_client)
    assert (await confirm(anon_client, token_from(mail[0]))).status_code == 204
    minted = await anon_client.post(f"{API}/me/api-keys", json={"name": "k", "scope": "write"})
    assert minted.status_code == 201, minted.text
    key = minted.json()["token"]
    async with new_client() as bearer:
        headers = {"Authorization": f"Bearer {key}"}
        assert (await bearer.get(f"{API}/projects", headers=headers)).status_code == 200

        await _reset_password(anon_client, resets, "verify@example.com")
        assert (await bearer.get(f"{API}/projects", headers=headers)).status_code == 401
    async with TestSessionLocal() as session:
        [row] = (await session.scalars(select(ApiKey))).all()
    assert row.revoked_at is not None


async def test_an_api_key_of_an_unverified_account_is_refused(
    mail: list[dict[str, str]], anon_client: AsyncClient
) -> None:
    await _signed_up(anon_client)
    assert (await confirm(anon_client, token_from(mail[0]))).status_code == 204
    minted = await anon_client.post(f"{API}/me/api-keys", json={"name": "k", "scope": "write"})
    assert minted.status_code == 201, minted.text
    key = minted.json()["token"]
    async with TestSessionLocal() as session:
        await session.execute(update(User).values(email_verified_at=None))
        await session.commit()

    async with new_client() as bearer:
        headers = {"Authorization": f"Bearer {key}"}
        refused = await bearer.get(f"{API}/projects", headers=headers)
        assert refused.status_code == 403
        assert refused.json()["detail"] == email_verification_service.EMAIL_NOT_VERIFIED_MESSAGE
        # Nor may a key ask for a link: that is the account holder's, in a browser.
        assert (await bearer.post(REQUEST_URL, headers=headers)).status_code == 403


async def test_self_hosted_verifies_every_account_at_creation(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = install_mail_sink(monkeypatch)
    monkeypatch.setattr(settings, "platform_admin_emails", ["member@example.com"])
    first = await anon_client.post(
        f"{API}/auth/register", json={"email": "owner@example.com", "password": PASSWORD}
    )
    assert first.json()["email_verified"] is True
    async with new_client() as member:
        joined = await member.post(
            f"{API}/auth/register", json={"email": "member@example.com", "password": PASSWORD}
        )
        assert joined.json()["email_verified"] is True
        # Nothing to verify self-hosted: a request is a no-op.
        assert (await member.post(REQUEST_URL)).status_code == 204
        me = (await member.get(f"{API}/auth/me")).json()
    assert sent == []
    assert await _tokens() == []
    assert me["email_verified"] is True
    # PLATFORM_ADMIN_EMAILS is a hosted-instance rule.
    assert me["is_platform_admin"] is False
