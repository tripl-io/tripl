"""The three SMTP transports, and the settings that choose between them.

The defect these pin is not that a send failed — it is that the
wrong client STALLS rather than erroring. Speaking plaintext to an implicit-TLS
port leaves smtplib waiting for a greeting the server will never send in the
clear, so the symptom was a ten-second hang and a disconnect, which reads like a
network fault rather than a configuration one.

So the assertions here are about WHICH CLIENT gets constructed, not about
whether a send succeeded: a test that only checked "the mail went out" against a
fake would have passed on the broken code too.
"""

from __future__ import annotations

import uuid
from email.message import EmailMessage
from typing import Any, get_args

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from tripl.alerting_validation import validate_sender_address
from tripl.config import (
    SMTP_SECURITY_IMPLICIT_TLS,
    SMTP_SECURITY_MODES,
    SMTP_SECURITY_NONE,
    SMTP_SECURITY_STARTTLS,
    Settings,
    settings,
)
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.schemas.app_settings import SmtpSecurity
from tripl.services.app_settings_service import EmailConfig
from tripl.worker.tasks.alerts_channels import _send_email_message


def _fake_smtplib() -> tuple[Any, list[str]]:
    """A stand-in smtplib whose two clients each announce themselves by name."""
    events: list[str] = []

    def _client(label: str) -> type:
        class _Fake:
            def __init__(self, host: str, port: int, timeout: int = 0) -> None:
                events.append(f"{label}({host}:{port})")

            def __enter__(self) -> _Fake:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def starttls(self) -> None:
                events.append("starttls")

            def login(self, username: str, password: str) -> None:
                events.append("login")

            def send_message(self, msg: object) -> dict[str, object]:
                events.append("send")
                return {}

        return _Fake

    class _Module:
        SMTP = _client("SMTP")
        SMTP_SSL = _client("SMTP_SSL")

    return _Module, events


def _send(mode: str, port: int) -> list[str]:
    module, events = _fake_smtplib()
    _send_email_message(
        smtp_module=module,
        smtp_host="relay.example.com",
        smtp_port=port,
        smtp_username="apikey",
        smtp_password="secret",
        smtp_security=mode,
        from_address="alerts@example.com",
        recipients=["alice@example.com"],
        subject="Subject",
        body="Body",
    )
    return events


def test_starttls_connects_in_the_clear_and_then_upgrades() -> None:
    assert _send(SMTP_SECURITY_STARTTLS, 587) == [
        "SMTP(relay.example.com:587)",
        "starttls",
        "login",
        "send",
    ]


def test_plaintext_never_upgrades() -> None:
    assert _send(SMTP_SECURITY_NONE, 25) == [
        "SMTP(relay.example.com:25)",
        "login",
        "send",
    ]


def test_implicit_tls_uses_the_ssl_client_and_never_asks_for_starttls() -> None:
    """The whole bug, in one assertion.

    Before the fix this produced ``SMTP(...)`` followed by ``starttls`` — the
    plaintext client on an SMTPS port, which in production never reached
    ``starttls`` at all because the constructor was still blocked reading a
    greeting that arrives encrypted.
    """
    events = _send(SMTP_SECURITY_IMPLICIT_TLS, 465)

    assert events == ["SMTP_SSL(relay.example.com:465)", "login", "send"]
    assert "starttls" not in events
    assert not any(event.startswith("SMTP(") for event in events)


def test_the_schema_literal_names_exactly_the_modes_the_transport_dispatches_on() -> None:
    """A value the API accepts but the sender cannot act on would be stored happily.

    ``_send_email_message`` treats anything that is not ``implicit_tls`` as
    "plain client" and anything that is not ``starttls`` as "do not upgrade", so
    an unknown fourth mode would silently degrade to an unencrypted session
    rather than fail. The two lists have to stay equal.
    """
    assert set(get_args(SmtpSecurity)) == set(SMTP_SECURITY_MODES)


@pytest.mark.parametrize(
    ("use_tls", "expected"),
    [(True, SMTP_SECURITY_STARTTLS), (False, SMTP_SECURITY_NONE)],
)
def test_an_instance_that_never_heard_of_smtp_security_keeps_its_old_behaviour(
    use_tls: bool, expected: str
) -> None:
    assert Settings(smtp_use_tls=use_tls, smtp_security="").resolved_smtp_security() == expected


def test_an_explicit_mode_wins_over_the_deprecated_boolean() -> None:
    resolved = Settings(
        smtp_use_tls=False, smtp_security=SMTP_SECURITY_IMPLICIT_TLS
    ).resolved_smtp_security()

    assert resolved == SMTP_SECURITY_IMPLICIT_TLS


def test_a_misspelled_mode_is_refused_at_startup() -> None:
    """Rather than being read as one of the modes it resembles.

    ``SMTP_SECURITY=ssl`` is the natural typo, and silently treating it as
    "none" would downgrade a connection the operator asked to encrypt.
    """
    with pytest.raises(ValidationError, match="smtp_security must be one of"):
        Settings(smtp_security="ssl")


@pytest.mark.asyncio
async def test_a_fresh_instance_reports_the_mode_as_a_default_not_a_delivery(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The badge has to say "Default" when nothing delivered the value.

    ``smtp_security``'s class default is the empty string — "ask the deprecated
    boolean" — while what the operator is shown is the answer, ``starttls``.
    Comparing the two naively badges an untouched instance "Env" and credits a
    variable nobody set.

    Both fields are pinned so the assertion does not depend on whatever the
    developer's shell happens to export, the way the AI-section test does.
    """
    monkeypatch.setattr(settings, "smtp_security", "")
    monkeypatch.setattr(settings, "smtp_use_tls", True)

    response = await client.get("/api/v1/settings")
    assert response.status_code == 200
    payload = response.json()

    assert payload["email"]["smtp_security"] == SMTP_SECURITY_STARTTLS
    assert payload["sources"]["email.smtp_security"] == "default"
    # The replaced boolean is gone from the contract entirely, not merely hidden.
    assert "smtp_use_tls" not in payload["email"]


@pytest.mark.asyncio
async def test_the_smtp_test_names_the_missing_from_address(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank sender is the failure that leaves no trace anywhere else.

    The transport never sees it: ``_send_password_reset_email`` returns early, so
    a token is minted, the mail is dropped, and the requester is still told a
    link is on its way.
    """
    monkeypatch.setattr(settings, "smtp_from_address", "")
    await client.patch(
        "/api/v1/settings",
        json={"email": {"smtp_host": "relay.example.com"}},
    )

    response = await client.post("/api/v1/settings/email/test", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "From:" in body["message"]


def test_a_display_name_sender_is_accepted_because_real_delivery_accepts_it() -> None:
    """A diagnostic must not fail a configuration that actually delivers.

    ``validate_email_address`` refuses ``Tripl <no-reply@x>``, while ``EmailMessage``
    takes it, so the strict helper refused a From: that delivers on every fire. The
    alert-destination test hit that first; the alert and digest SEND
    paths carried the same strictness until tripl-0zpq.29, which is the defect that
    mattered — a diagnostic reporting success and the real send then failing.
    Five callers now share this one helper, which checks only the address part:
    the two send paths (worker/tasks/alerts.py, worker/tasks/alerts_channels.py),
    the two test sends (services/_alerting_test_send.py, services/_email_test_send.py)
    and the settings schema that saves the value (schemas/app_settings.py).

    The original string comes back, display name intact — normalising it away
    would silently drop what the operator configured.
    """
    assert (
        validate_sender_address("Tripl Alerts <no-reply@example.com>")
        == "Tripl Alerts <no-reply@example.com>"
    )
    assert validate_sender_address("no-reply@example.com") == "no-reply@example.com"


def test_a_sender_with_no_at_sign_is_refused_with_a_readable_reason() -> None:
    """What a bare string actually costs, since header injection is not on the table.

    ``EmailMessage.__setitem__`` raises on a linefeed or carriage return, so a
    newline cannot reach the wire. A value with no @-sign serialises happily and
    comes back as an opaque relay error instead.
    """
    with pytest.raises(ValueError, match="not a usable address|@-sign"):
        validate_sender_address("not-an-address")


def test_the_alert_destination_test_send_accepts_a_display_name_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half of tripl-q9o6 that lives outside the shared helper.

    Pinning the CALL SITE, not just the validator: the defect was that this path
    used the strict helper, so a test asserting only ``validate_sender_address``
    would stay green if someone put ``validate_email_address`` back here.
    """
    # celery_app FIRST, deliberately: ``alerts`` and ``celery_app`` import each
    # other (alerts -> celery_app -> metrics -> alerts), and the cycle only
    # resolves when celery_app is the module that starts loading — which is what
    # the worker itself does. Entering from ``alerts`` raises ImportError on a
    # partially initialized module.
    import tripl.worker.celery_app  # noqa: F401
    from tripl.services import _alerting_test_send, app_settings_service
    from tripl.worker.tasks import alerts

    sent: dict[str, object] = {}
    monkeypatch.setattr(
        app_settings_service,
        "get_email_config_sync",
        lambda *a, **k: EmailConfig(
            smtp_host="relay.example.com",
            smtp_port=587,
            smtp_username="",
            smtp_password="",
            smtp_security=SMTP_SECURITY_STARTTLS,
            smtp_from_address="",
        ),
    )
    monkeypatch.setattr(alerts, "_send_email_message", lambda **kw: sent.update(kw))

    target = _alerting_test_send._TestTarget(
        destination_id=uuid.uuid4(),
        destination_type="email",
        destination_name="Ops",
        message="body",
        webhook_url=None,
        bot_token=None,
        chat_id=None,
        target_url=None,
        webhook_header_name=None,
        webhook_header_value=None,
        email_recipients="ops@example.com",
        email_from_address="Tripl Alerts <no-reply@example.com>",
        jira_base_url=None,
        jira_auth_email=None,
        jira_api_token=None,
        jira_project_key=None,
        jira_issue_type=None,
        linear_api_key=None,
        linear_team_id=None,
        linear_state_id=None,
        linear_label_ids=None,
        organization_id=DEFAULT_ORG_ID,
    )

    _alerting_test_send._send_email(target)

    assert sent["from_address"] == "Tripl Alerts <no-reply@example.com>"


@pytest.mark.parametrize("injected", ["ok@example.com\nBcc: evil@example.com", "a@b.c\r\nX: y"])
def test_a_newline_in_a_header_cannot_reach_the_wire(injected: str) -> None:
    """Pinning the property the validation deliberately does NOT rest on."""
    message = EmailMessage()

    with pytest.raises(ValueError, match="linefeed or carriage return"):
        message["From"] = injected


@pytest.mark.asyncio
async def test_the_smtp_test_refuses_before_opening_a_socket_with_no_host(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "smtp_host", "")

    response = await client.post("/api/v1/settings/email/test", json={})

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "message": "No SMTP host is configured, so nothing can be sent.",
    }
