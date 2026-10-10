"""Pre-launch (accounts and security): one account-mail sender, one role change,
one browser-session refusal.

* Password reset, verification and invitation mail, and the SMTP test send, all
  go out through ``alerts_channels.send_with_config`` from the operator relay's
  own sender (``services/account_mail.py``).
* ``PATCH /users/{id}`` and ``PATCH /orgs/{org}/members/{id}`` are one change:
  the same errors and the one audit action ``org.member_role_update``.
* ``deps.require_browser_session`` is the one "an API key may not do this"
  check, and every gate keeps the refusal text it documents.
"""

from __future__ import annotations

import inspect
import logging
import smtplib
import uuid
from typing import Any

import pytest
from fastapi import HTTPException, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tripl.api import deps
from tripl.api.v1 import _members, auth
from tripl.config import settings
from tripl.main import app
from tripl.models.audit_log import AuditLog
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.models.password_reset_token import PasswordResetToken
from tripl.services import account_mail, invitation_email
from tripl.services._email_test_send import TEST_SUBJECT, send_test_email
from tripl.services.app_settings_service import EmailConfig
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import alerts_channels

API = "/api/v1"
PASSWORD = "Password123!"

RELAY = EmailConfig(
    smtp_host="smtp.operator.example.com",
    smtp_port=587,
    smtp_username="tripl",
    smtp_password="secret",
    smtp_security="starttls",
    smtp_from_address="tripl <noreply@operator.example.com>",
)


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every message handed to the SMTP transport, instead of sending it."""
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(alerts_channels, "_send_email_message", lambda **kw: sent.append(kw))
    return sent


def _operator_relay(monkeypatch: pytest.MonkeyPatch, *, from_address: str) -> None:
    monkeypatch.setattr(settings, "smtp_host", "smtp.operator.example.com")
    monkeypatch.setattr(settings, "smtp_from_address", from_address)
    monkeypatch.setattr(settings, "app_base_url", "https://tripl.example.com")


# ── one account-mail sender ─────────────────────────────────────────────────


def test_every_account_mail_leaves_through_the_relay_under_its_own_sender(
    outbox: list[dict[str, Any]],
) -> None:
    account_mail.send_password_reset(
        recipient="a@example.com", reset_link="https://x/auth?reset_token=r", email_config=RELAY
    )
    account_mail.send_verification(
        recipient="b@example.com", verify_link="https://x/verify-email?token=v", email_config=RELAY
    )
    invitation_email.send(
        invitation_email.InvitationMail(
            recipient="c@example.com",
            organization_name="Acme",
            link="https://x/invite/i",
            email_config=RELAY,
        )
    )
    send_test_email(email_config=RELAY, recipient="d@example.com")

    assert [mail["recipients"] for mail in outbox] == [
        ["a@example.com"],
        ["b@example.com"],
        ["c@example.com"],
        ["d@example.com"],
    ]
    for mail in outbox:
        assert mail["smtp_module"] is smtplib
        assert mail["smtp_host"] == RELAY.smtp_host
        assert mail["smtp_security"] == RELAY.smtp_security
        assert mail["from_address"] == RELAY.smtp_from_address
    reset, verification, invitation, probe = outbox
    assert reset["subject"] == "Reset your tripl password"
    assert "https://x/auth?reset_token=r" in reset["body"]
    assert verification["subject"] == "Verify your tripl email address"
    assert "https://x/verify-email?token=v" in verification["body"]
    assert invitation["subject"] == "You're invited to Acme on tripl"
    assert "https://x/invite/i" in invitation["body"]
    assert probe["subject"] == TEST_SUBJECT


def test_account_mail_logs_a_relay_failure_and_the_smtp_test_reports_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(**_kw: Any) -> None:
        raise smtplib.SMTPAuthenticationError(535, b"authentication failed")

    monkeypatch.setattr(alerts_channels, "_send_email_message", refuse)

    with caplog.at_level(logging.ERROR, logger="tripl.services.account_mail"):
        # Best-effort: the request that queued it has already answered.
        account_mail.send_password_reset(
            recipient="a@example.com", reset_link="https://x", email_config=RELAY
        )
    assert "Failed to send password reset email" in caplog.text

    # The probe exists to report exactly this, so it raises instead.
    with pytest.raises(smtplib.SMTPAuthenticationError):
        send_test_email(email_config=RELAY, recipient="d@example.com")


async def test_operator_mail_is_the_operator_relay_and_its_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _operator_relay(monkeypatch, from_address="noreply@operator.example.com")
    async with TestSessionLocal() as session:
        mail = await account_mail.operator_mail(session)
    assert mail.config.smtp_host == "smtp.operator.example.com"
    assert mail.can_send is True
    assert mail.link("/invite/t") == "https://tripl.example.com/invite/t"
    assert (
        account_mail.password_reset_link(mail, "tok")
        == "https://tripl.example.com/auth?reset_token=tok"
    )

    monkeypatch.setattr(settings, "smtp_from_address", "")
    async with TestSessionLocal() as session:
        assert (await account_mail.operator_mail(session)).can_send is False


async def test_a_reset_request_mails_the_link_through_the_shared_transport(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, outbox: list[dict[str, Any]]
) -> None:
    _operator_relay(monkeypatch, from_address="noreply@operator.example.com")
    registered = await anon_client.post(
        f"{API}/auth/register", json={"email": "reset@example.com", "password": PASSWORD}
    )
    assert registered.status_code == 201, registered.text

    response = await anon_client.post(
        f"{API}/auth/password-reset/request", json={"email": "reset@example.com"}
    )

    assert response.status_code == 200
    assert response.json()["email_configured"] is True
    [mail] = outbox
    assert mail["recipients"] == ["reset@example.com"]
    assert mail["from_address"] == "noreply@operator.example.com"
    assert "https://tripl.example.com/auth?reset_token=" in mail["body"]


async def test_a_relay_without_a_sender_issues_no_reset_token(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, outbox: list[dict[str, Any]]
) -> None:
    """The case the senders' own blank-From: check used to catch, gated up front."""
    _operator_relay(monkeypatch, from_address="")
    await anon_client.post(
        f"{API}/auth/register", json={"email": "solo@example.com", "password": PASSWORD}
    )

    status = await anon_client.get(f"{API}/auth/status")
    response = await anon_client.post(
        f"{API}/auth/password-reset/request", json={"email": "solo@example.com"}
    )

    assert status.json()["email_configured"] is False
    assert response.json()["email_configured"] is False
    assert outbox == []
    async with TestSessionLocal() as session:
        assert (await session.scalars(select(PasswordResetToken))).all() == []


def test_the_auth_router_keeps_the_names_tests_replace() -> None:
    """Both editions' tests capture links by replacing these two names."""
    assert set(inspect.signature(auth._send_password_reset_email).parameters) == {
        "recipient",
        "reset_link",
        "email_config",
    }
    assert set(inspect.signature(auth._send_verification_email).parameters) == {
        "recipient",
        "verify_link",
        "email_config",
    }


# ── one role change ─────────────────────────────────────────────────────────


async def _audit(action: str) -> list[AuditLog]:
    async with TestSessionLocal() as session:
        return list(
            (await session.scalars(select(AuditLog).where(AuditLog.action == action))).all()
        )


async def test_both_role_routes_answer_alike_and_file_one_action(client: AsyncClient) -> None:
    owner_id = (await client.get(f"{API}/auth/me")).json()["id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob:
        joined = await bob.post(
            f"{API}/auth/register", json={"email": "bob@example.com", "password": PASSWORD}
        )
        assert joined.status_code == 201, joined.text
        bob_id = joined.json()["id"]

    by_users = await client.patch(f"{API}/users/{bob_id}", json={"role": "admin"})
    by_orgs = await client.patch(f"{API}/orgs/default/members/{bob_id}", json={"role": "member"})
    assert (by_users.status_code, by_users.json()["role"]) == (200, "admin")
    assert (by_orgs.status_code, by_orgs.json()["role"]) == (200, "member")

    rows = await _audit("org.member_role_update")
    assert len(rows) == 2
    assert {(row.payload["old_role"], row.payload["new_role"]) for row in rows} == {
        ("member", "admin"),
        ("admin", "member"),
    }
    assert {row.organization_id for row in rows} == {DEFAULT_ORG_ID}
    assert await _audit("user.role_update") == []

    stranger = uuid.uuid4()
    for url in (f"{API}/users/{stranger}", f"{API}/orgs/default/members/{stranger}"):
        missing = await client.patch(url, json={"role": "admin"})
        assert (missing.status_code, missing.json()["detail"]) == (404, _members.MEMBER_NOT_FOUND)
    for url in (f"{API}/users/{owner_id}", f"{API}/orgs/default/members/{owner_id}"):
        last = await client.patch(url, json={"role": "admin"})
        assert (last.status_code, last.json()["detail"]) == (400, _members.LAST_OWNER)


# ── one browser-session refusal ─────────────────────────────────────────────


def _request(**state: Any) -> Request:
    return Request({"type": "http", "headers": [], "state": state})


def test_require_browser_session_refuses_any_key_with_the_gates_own_text() -> None:
    deps.require_browser_session(_request())  # a cookie session passes

    for scope in ("read", "write"):
        with pytest.raises(HTTPException) as refused:
            deps.require_browser_session(_request(api_key_scope=scope))
        assert (refused.value.status_code, refused.value.detail) == (
            403,
            "A browser session is required",
        )
    with pytest.raises(HTTPException) as owner:
        deps.require_browser_session(_request(api_key_scope="write"), deps.OWNER_SESSION_REQUIRED)
    assert owner.value.detail == "Owner session required"


async def test_keys_get_the_same_refusal_texts_as_before(client: AsyncClient) -> None:
    minted = await client.post(f"{API}/me/api-keys", json={"name": "agent", "scope": "write"})
    assert minted.status_code == 201, minted.text
    bearer = {"Authorization": f"Bearer {minted.json()['token']}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as agent:
        resend = await agent.post(f"{API}/auth/verify-email/request", headers=bearer)
        another_key = await agent.post(
            f"{API}/me/api-keys", json={"name": "x", "scope": "read"}, headers=bearer
        )
        org_rename = await agent.patch(
            f"{API}/orgs/default", json={"name": "Renamed"}, headers=bearer
        )

    assert (resend.status_code, resend.json()["detail"]) == (403, "A browser session is required")
    assert (another_key.status_code, another_key.json()["detail"]) == (
        403,
        "API key management requires a user session",
    )
    assert (org_rename.status_code, org_rename.json()["detail"]) == (
        403,
        "Owner session required",
    )
