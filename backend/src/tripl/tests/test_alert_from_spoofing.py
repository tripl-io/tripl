"""An email destination's From: override and whose relay carries it (critique #16).

* through the OPERATOR's relay (an organization without its own SMTP) the
  override is ignored and the relay's configured sender goes out;
* through the organization's OWN relay the override is honoured;
* the self-hosted default organization IS the operator scope: unchanged;
* the destination Test button follows the same rule;
* an organization that enters the OPERATOR's own SMTP host does not own that
  relay: the override is still ignored;
* at save time on a hosted instance, an override is refused unless the
  organization runs its own relay (even for a verified SSO domain, since send
  time would ignore it), so save and send apply one rule.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from tripl import crypto
from tripl.config import settings
from tripl.models import Base
from tripl.models.alert_destination import AlertDestination
from tripl.models.app_setting import SERVICE_SETTINGS_KEY, AppSetting
from tripl.models.org_sso import OrgSsoDomain
from tripl.models.organization import DEFAULT_ORG_ID, Organization
from tripl.models.project import Project
from tripl.services import _alerting_test_send, app_settings_service
from tripl.services._alerting_from_policy import assert_from_override_allowed
from tripl.services.app_settings_service import email_sender_for, resolve_settings
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import alerts as alerts_task
from tripl.worker.tasks import alerts_channels

OWN_RELAY_ORG = uuid.UUID("00000000-0000-0000-0000-00000000c0a1")
INHERITING_ORG = uuid.UUID("00000000-0000-0000-0000-00000000c0b2")
OPERATOR_SENDER = "ops@example.com"
OWN_SENDER = "alerts@acme-own.example.com"
SPOOFED = "Payments Team <billing@victim-bank.example.org>"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    monkeypatch.setattr(settings, "smtp_host", "env-relay.example.com")
    monkeypatch.setattr(settings, "smtp_from_address", "env@example.com")
    monkeypatch.setattr(settings, "org_settings_operator_fallback", "all")
    yield
    crypto._fernet.cache_clear()


@pytest.fixture
def hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "hosted")


def _operator() -> dict[str, Any]:
    return {
        "smtp_host": "operator-relay.example.com",
        "smtp_password": crypto.encrypt_value("operator-smtp-secret"),
        "smtp_from_address": OPERATOR_SENDER,
    }


def _own_relay() -> dict[str, Any]:
    return {"smtp_host": "acme-relay.example.com", "smtp_from_address": OWN_SENDER}


# ── the flag and the rule (pure) ────────────────────────────────────────────


def test_operator_view_allows_the_override() -> None:
    config = app_settings_service.email_config_for(resolve_settings(_operator(), None))
    assert config.from_override_allowed is True
    assert email_sender_for(SPOOFED, config) == SPOOFED


def test_an_inherited_operator_relay_ignores_the_override(hosted: None) -> None:
    resolved = resolve_settings(_operator(), {}, org_scope=INHERITING_ORG)
    config = app_settings_service.email_config_for(resolved)
    assert config.smtp_host == "operator-relay.example.com"
    assert config.from_override_allowed is False
    assert email_sender_for(SPOOFED, config) == OPERATOR_SENDER


def test_an_own_relay_honours_the_override(hosted: None) -> None:
    resolved = resolve_settings(_operator(), _own_relay(), org_scope=OWN_RELAY_ORG)
    config = app_settings_service.email_config_for(resolved)
    assert config.smtp_host == "acme-relay.example.com"
    assert config.from_override_allowed is True
    assert email_sender_for("Acme <x@acme-own.example.com>", config) == (
        "Acme <x@acme-own.example.com>"
    )


def test_no_override_uses_the_configured_sender() -> None:
    config = app_settings_service.email_config_for(resolve_settings(_operator(), None))
    assert email_sender_for(None, config) == OPERATOR_SENDER
    assert email_sender_for("", config) == OPERATOR_SENDER


def test_the_disabled_config_never_allows_an_override() -> None:
    assert app_settings_service.disabled_email_config().from_override_allowed is False


@pytest.mark.parametrize(
    "claimed_host",
    ["operator-relay.example.com", "Operator-Relay.Example.COM", "operator-relay.example.com."],
)
def test_the_operators_own_host_is_not_an_own_relay(hosted: None, claimed_host: str) -> None:
    resolved = resolve_settings(
        _operator(),
        {"smtp_host": claimed_host, "smtp_from_address": OWN_SENDER},
        org_scope=OWN_RELAY_ORG,
    )
    assert "smtp_host" in resolved.guarded_hosts
    assert app_settings_service.relay_is_scope_owned(resolved) is False
    config = app_settings_service._email_config_of(resolved)
    assert config.from_override_allowed is False
    assert email_sender_for(SPOOFED, config) == OWN_SENDER


def test_a_different_host_is_still_an_own_relay(hosted: None) -> None:
    resolved = resolve_settings(_operator(), _own_relay(), org_scope=OWN_RELAY_ORG)
    assert resolved.operator_smtp_host == "operator-relay.example.com"
    assert app_settings_service.relay_is_scope_owned(resolved) is True


# ── delivery, digest and test send (sync, real resolution) ──────────────────


@pytest.fixture
def sync_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'from_spoofing.db'}")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                Organization(id=OWN_RELAY_ORG, slug="acme", name="Acme"),
                Organization(id=INHERITING_ORG, slug="globex", name="Globex"),
            ]
        )
        session.commit()
        session.add(AppSetting(key=SERVICE_SETTINGS_KEY, value=_operator(), organization_id=None))
        session.add(
            AppSetting(key=SERVICE_SETTINGS_KEY, value=_own_relay(), organization_id=OWN_RELAY_ORG)
        )
        session.commit()
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _email_destination(session: Session, org_id: uuid.UUID, slug: str) -> AlertDestination:
    project = Project(name=slug.title(), slug=slug, organization_id=org_id)
    session.add(project)
    session.commit()
    return AlertDestination(
        project_id=project.id,
        type="email",
        name="Ops",
        email_recipients="oncall@example.com",
        email_from_address=SPOOFED,
    )


class _CapturingSmtplib:
    """A stand-in ``smtplib`` that records every message instead of sending it."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        outer = self

        class _Conn:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def __enter__(self) -> _Conn:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

            def starttls(self) -> None:
                return None

            def login(self, *_args: object) -> None:
                return None

            def send_message(self, msg: EmailMessage) -> dict[str, Any]:
                outer.sent.append(msg)
                return {}

        self.SMTP = _Conn
        self.SMTP_SSL = _Conn


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> _CapturingSmtplib:
    fake = _CapturingSmtplib()
    monkeypatch.setattr(alerts_task, "smtplib", fake)
    return fake


def _send_resolved(session: Session, destination: AlertDestination) -> None:
    config, recipients, from_address = alerts_task._resolve_email_context(session, destination)
    alerts_task._send_email_message(
        smtp_host=config.smtp_host,
        smtp_port=config.smtp_port,
        smtp_username=config.smtp_username,
        smtp_password=config.smtp_password,
        smtp_security=config.smtp_security,
        from_address=from_address,
        recipients=recipients,
        subject="alert",
        body="body",
    )


def test_operator_relay_sends_under_the_operator_sender(
    hosted: None, sync_factory: sessionmaker[Session], smtp: _CapturingSmtplib
) -> None:
    with sync_factory() as session:
        destination = _email_destination(session, INHERITING_ORG, "globex-app")
        _send_resolved(session, destination)
    assert [msg["From"] for msg in smtp.sent] == [OPERATOR_SENDER]


def test_own_relay_sends_under_the_override(
    hosted: None, sync_factory: sessionmaker[Session], smtp: _CapturingSmtplib
) -> None:
    with sync_factory() as session:
        destination = _email_destination(session, OWN_RELAY_ORG, "acme-app")
        _send_resolved(session, destination)
    assert [msg["From"] for msg in smtp.sent] == [SPOOFED]


def test_self_hosted_default_org_keeps_the_override(
    sync_factory: sessionmaker[Session], smtp: _CapturingSmtplib
) -> None:
    assert settings.deployment_mode == "self_hosted"
    with sync_factory() as session:
        destination = _email_destination(session, DEFAULT_ORG_ID, "default-app")
        _send_resolved(session, destination)
    assert [msg["From"] for msg in smtp.sent] == [SPOOFED]


def test_the_digest_path_ignores_the_override_on_the_operator_relay(
    hosted: None, sync_factory: sessionmaker[Session]
) -> None:
    sent: list[dict[str, Any]] = []
    with sync_factory() as session:
        destination = _email_destination(session, INHERITING_ORG, "globex-digest")
        project = session.get(Project, destination.project_id)
        assert project is not None
        config = app_settings_service.get_email_config_for_project_sync(session, project.id)
        alerts_channels._send_digest_to_destination(
            destination=destination,
            message="digest",
            project=project,
            email_config=config,
            send_slack_message=lambda *_a, **_k: None,
            send_email_message=lambda **kwargs: sent.append(kwargs),
        )
    assert [call["from_address"] for call in sent] == [OPERATOR_SENDER]


def _test_target(org_id: uuid.UUID) -> _alerting_test_send._TestTarget:
    return _alerting_test_send._TestTarget(
        destination_id=None,
        destination_type="email",
        destination_name="Ops",
        message="test",
        webhook_url=None,
        bot_token=None,
        chat_id=None,
        target_url=None,
        webhook_header_name=None,
        webhook_header_value=None,
        email_recipients="oncall@example.com",
        email_from_address=SPOOFED,
        jira_base_url=None,
        jira_auth_email=None,
        jira_api_token=None,
        jira_project_key=None,
        jira_issue_type=None,
        linear_api_key=None,
        linear_team_id=None,
        linear_state_id=None,
        linear_label_ids=None,
        organization_id=org_id,
    )


@pytest.mark.parametrize(
    ("org_id", "expected"),
    [(INHERITING_ORG, OPERATOR_SENDER), (OWN_RELAY_ORG, SPOOFED)],
)
def test_the_test_send_follows_the_same_rule(
    hosted: None,
    sync_factory: sessionmaker[Session],
    smtp: _CapturingSmtplib,
    monkeypatch: pytest.MonkeyPatch,
    org_id: uuid.UUID,
    expected: str,
) -> None:
    monkeypatch.setattr(app_settings_service, "_open_sync_session", sync_factory)
    _alerting_test_send._send_email(_test_target(org_id))
    assert [msg["From"] for msg in smtp.sent] == [expected]


# ── save-time validation (hosted) ───────────────────────────────────────────


async def _seed_hosted_orgs() -> None:
    async with TestSessionLocal() as session:
        session.add_all(
            [
                Organization(id=OWN_RELAY_ORG, slug="acme", name="Acme"),
                Organization(id=INHERITING_ORG, slug="globex", name="Globex"),
            ]
        )
        await session.commit()
        session.add_all(
            [
                AppSetting(key=SERVICE_SETTINGS_KEY, value=_operator(), organization_id=None),
                AppSetting(
                    key=SERVICE_SETTINGS_KEY, value=_own_relay(), organization_id=OWN_RELAY_ORG
                ),
                OrgSsoDomain(
                    organization_id=INHERITING_ORG,
                    domain="globex-verified.example.com",
                    verification_token="t" * 32,
                    verified_at=datetime.now(UTC),
                ),
                OrgSsoDomain(
                    organization_id=INHERITING_ORG,
                    domain="globex-pending.example.com",
                    verification_token="u" * 32,
                    verified_at=None,
                ),
            ]
        )
        await session.commit()


@pytest.mark.asyncio
async def test_hosted_save_refuses_an_unverified_domain_on_the_operator_relay(
    hosted: None,
) -> None:
    await _seed_hosted_orgs()
    async with TestSessionLocal() as session:
        for value in (SPOOFED, "alerts@globex-pending.example.com"):
            with pytest.raises(HTTPException) as info:
                await assert_from_override_allowed(session, INHERITING_ORG, value)
            assert info.value.status_code == 422


@pytest.mark.asyncio
async def test_hosted_save_refuses_a_verified_domain_on_the_operator_relay(
    hosted: None,
) -> None:
    """Send time would ignore it (the operator's relay), so the save refuses it too."""
    await _seed_hosted_orgs()
    async with TestSessionLocal() as session:
        with pytest.raises(HTTPException) as info:
            await assert_from_override_allowed(
                session, INHERITING_ORG, "Globex Alerts <alerts@Globex-Verified.example.com>"
            )
        assert info.value.status_code == 422


def test_a_verified_domain_on_the_operator_relay_is_ignored_at_send(
    hosted: None, sync_factory: sessionmaker[Session], smtp: _CapturingSmtplib
) -> None:
    """The send half of the same rule: a verified domain does not unlock the override."""
    with sync_factory() as session:
        session.add(
            OrgSsoDomain(
                organization_id=INHERITING_ORG,
                domain="globex-verified.example.com",
                verification_token="t" * 32,
                verified_at=datetime.now(UTC),
            )
        )
        session.commit()
        destination = _email_destination(session, INHERITING_ORG, "globex-verified")
        destination.email_from_address = "alerts@globex-verified.example.com"
        _send_resolved(session, destination)
    assert [msg["From"] for msg in smtp.sent] == [OPERATOR_SENDER]


@pytest.mark.asyncio
async def test_hosted_save_refuses_the_operators_host_claimed_as_own(hosted: None) -> None:
    await _seed_hosted_orgs()
    async with TestSessionLocal() as session:
        session.add(
            AppSetting(
                key=SERVICE_SETTINGS_KEY,
                value={"smtp_host": "OPERATOR-relay.example.com."},
                organization_id=INHERITING_ORG,
            )
        )
        await session.commit()
        with pytest.raises(HTTPException) as info:
            await assert_from_override_allowed(session, INHERITING_ORG, SPOOFED)
        assert info.value.status_code == 422


@pytest.mark.asyncio
async def test_hosted_save_accepts_any_domain_on_an_own_relay(hosted: None) -> None:
    await _seed_hosted_orgs()
    async with TestSessionLocal() as session:
        await assert_from_override_allowed(session, OWN_RELAY_ORG, SPOOFED)


@pytest.mark.asyncio
async def test_hosted_save_accepts_no_override(hosted: None) -> None:
    await _seed_hosted_orgs()
    async with TestSessionLocal() as session:
        await assert_from_override_allowed(session, INHERITING_ORG, None)
        await assert_from_override_allowed(session, INHERITING_ORG, "")


@pytest.mark.asyncio
async def test_hosted_save_refuses_a_project_without_an_organization(hosted: None) -> None:
    async with TestSessionLocal() as session:
        with pytest.raises(HTTPException):
            await assert_from_override_allowed(session, None, SPOOFED)


@pytest.mark.asyncio
async def test_self_hosted_save_is_unchanged() -> None:
    assert settings.deployment_mode == "self_hosted"
    async with TestSessionLocal() as session:
        await assert_from_override_allowed(session, DEFAULT_ORG_ID, SPOOFED)
