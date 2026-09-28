"""Operator SMTP and a captured verification mailbox, for the F20 sign-up tests.

Not a test module. :func:`install_mail_sink` configures the operator relay
(so ``email_can_send`` is true), captures every verification link
``api.v1.auth._send_verification_email`` would have mailed, and silences the
shared SMTP sender so nothing else (invitation mails) tries the network.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from tripl.config import settings
from tripl.main import app

API = "/api/v1"
PASSWORD = "Password123!"


def install_mail_sink(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    from tripl.api.v1 import auth as auth_api
    from tripl.worker.tasks import alerts_channels

    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from_address", "noreply@example.com")
    monkeypatch.setattr(settings, "app_base_url", "https://tripl.example.com")
    sent: list[dict[str, str]] = []

    def _capture(*, recipient: str, verify_link: str, email_config: Any) -> None:
        del email_config
        sent.append({"recipient": recipient, "verify_link": verify_link})

    monkeypatch.setattr(auth_api, "_send_verification_email", _capture)
    monkeypatch.setattr(alerts_channels, "_send_email_message", lambda **_kwargs: None)
    return sent


def token_from(mail: dict[str, str]) -> str:
    link = mail["verify_link"]
    assert link.startswith("https://tripl.example.com/verify-email?token="), link
    return link.split("token=", 1)[1]


def new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def hosted_sign_up(
    client: AsyncClient,
    email: str,
    *,
    org_slug: str,
    org_name: str = "Some Org",
    name: str | None = None,
) -> Any:
    """``POST /auth/register`` with the hosted form's organization fields."""
    payload: dict[str, Any] = {
        "email": email,
        "password": PASSWORD,
        "org_name": org_name,
        "org_slug": org_slug,
    }
    if name is not None:
        payload["name"] = name
    return await client.post(f"{API}/auth/register", json=payload)


async def confirm(client: AsyncClient, token: str) -> Any:
    return await client.post(f"{API}/auth/verify-email/confirm", json={"token": token})
