"""Per-organization settings: resolution, guards and the three surfaces (F20 PR9).

* resolution: org override -> operator override -> env, with
  ``ORG_SETTINGS_OPERATOR_FALLBACK`` deciding whether an organization without
  its own AI/SMTP inherits the operator's (``all``) or runs without (``none``);
* a secret is inherited only together with its endpoint (critique #13);
* a hosted organization's hosts must be public, at save and at use (#14);
* an organization's limits are clamped to the operator's (#15);
* an organization's read fails closed (#19);
* worker paths resolve the PROJECT's organization; account mail is the
  operator's relay whatever an organization set;
* ``/orgs/{org}/settings`` (owner/admin), ``/platform/settings`` (platform
  admin) and the legacy ``/settings`` (self-hosted: operator scope; hosted: the
  caller's own organization).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from tripl import crypto
from tripl.api.v1 import auth as auth_api
from tripl.config import settings
from tripl.main import app
from tripl.models import Base
from tripl.models.alert_destination import AlertDestination
from tripl.models.app_setting import SERVICE_SETTINGS_KEY, AppSetting
from tripl.models.audit_log import AuditLog
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.user import User
from tripl.services import app_settings_service, invitation_email, llm_service
from tripl.services.app_settings_service import (
    ORG_FIELDS,
    resolve_settings,
    settings_scope_for,
)
from tripl.tests._accounts import sign_up
from tripl.tests._members import add_org_member
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import alerts as alerts_task

API = "/api/v1"
PASSWORD = "Password123!"
ORG_A_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
ORG_B_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A real encryption key, and env values the tests can recognise."""
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "ai_api_key", "sk-env")
    monkeypatch.setattr(settings, "ai_base_url", "https://env-llm.example.com/v1")
    monkeypatch.setattr(settings, "smtp_host", "env-relay.example.com")
    monkeypatch.setattr(settings, "smtp_password", "env-smtp-secret")
    monkeypatch.setattr(settings, "smtp_from_address", "env@example.com")
    monkeypatch.setattr(settings, "org_settings_operator_fallback", "all")
    yield
    crypto._fernet.cache_clear()


def _enc(value: str) -> str:
    return crypto.encrypt_value(value)


def _operator() -> dict[str, Any]:
    return {
        "ai_base_url": "https://operator-llm.example.com/v1",
        "ai_api_key": _enc("sk-operator"),
        "ai_model": "operator-model",
        "ai_timeout_seconds": 20,
        "ai_max_output_tokens": 500,
        "scan_row_limit_default": 10_000,
        "smtp_host": "operator-relay.example.com",
        "smtp_password": _enc("operator-smtp-secret"),
        "smtp_from_address": "ops@example.com",
    }


# ── the resolution matrix (pure) ────────────────────────────────────────────


def test_the_operator_view_is_operator_over_env() -> None:
    resolved = resolve_settings(_operator(), None)
    assert resolved.values["ai_api_key"] == "sk-operator"
    assert resolved.sources["ai_api_key"] == "override"
    # Not overridden by the operator: env.
    assert resolved.values["ai_enabled"] is True
    assert resolved.sources["ai_enabled"] == "env"
    assert resolved.org_scope is None


def test_an_org_value_wins_and_is_badged_org() -> None:
    org = {"ai_model": "org-model", "ai_base_url": "https://org.example.com/v1"}
    org["ai_api_key"] = _enc("sk-org")
    resolved = resolve_settings(_operator(), org, org_scope=ORG_A_ID)
    assert resolved.values["ai_model"] == "org-model"
    assert resolved.values["ai_api_key"] == "sk-org"
    assert resolved.sources["ai_model"] == "org"
    assert resolved.overridden_fields == ("ai_api_key", "ai_base_url", "ai_model")


def test_fallback_all_inherits_the_operator_then_env() -> None:
    resolved = resolve_settings(_operator(), {}, org_scope=ORG_A_ID)
    assert resolved.values["ai_api_key"] == "sk-operator"
    assert resolved.sources["ai_api_key"] == "override"
    assert resolved.values["smtp_host"] == "operator-relay.example.com"
    # Neither the org nor the operator set it: env.
    assert resolved.values["ai_enabled"] is True
    assert resolved.sources["ai_enabled"] == "env"


def test_fallback_all_with_no_operator_override_inherits_env() -> None:
    resolved = resolve_settings({}, {}, org_scope=ORG_A_ID)
    assert resolved.values["ai_api_key"] == "sk-env"
    assert resolved.sources["ai_api_key"] == "env"


def test_fallback_none_disables_ai_and_smtp_but_keeps_scalars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "org_settings_operator_fallback", "none")
    resolved = resolve_settings(_operator(), {}, org_scope=ORG_A_ID)
    assert resolved.values["ai_api_key"] == ""
    assert resolved.values["ai_base_url"] == ""
    assert resolved.values["smtp_host"] == ""
    assert resolved.values["smtp_password"] == ""
    assert resolved.sources["ai_api_key"] == "disabled"
    assert resolved.sources["smtp_host"] == "disabled"
    assert llm_service.is_enabled(app_settings_service.ai_config_for(resolved)) is False
    assert not app_settings_service.email_can_send(app_settings_service.email_config_for(resolved))
    # Non-secret scalars still fall back.
    assert resolved.values["scan_row_limit_default"] == 10_000
    assert resolved.values["ai_timeout_seconds"] == 20
    # The operator view is untouched by the policy.
    assert resolve_settings(_operator(), None).values["ai_api_key"] == "sk-operator"


def test_fallback_none_still_runs_an_org_with_its_own_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "org_settings_operator_fallback", "none")
    org = {
        "ai_base_url": "https://org.example.com/v1",
        "ai_api_key": _enc("sk-org"),
        "ai_model": "m",
    }
    resolved = resolve_settings(_operator(), org, org_scope=ORG_A_ID)
    config = app_settings_service.ai_config_for(resolved)
    assert config.ai_api_key == "sk-org"
    assert llm_service.is_enabled(config) is True


# ── secrets travel with their endpoint (critique #13) ───────────────────────


def test_an_org_endpoint_never_receives_the_operator_ai_key() -> None:
    resolved = resolve_settings(
        _operator(), {"ai_base_url": "https://attacker.example.com/v1"}, org_scope=ORG_A_ID
    )
    assert resolved.values["ai_base_url"] == "https://attacker.example.com/v1"
    assert resolved.values["ai_api_key"] == ""
    assert resolved.sources["ai_api_key"] == "default"
    # The model is part of the group: not the operator's either.
    assert resolved.values["ai_model"] != "operator-model"


def test_an_org_key_alone_does_not_ride_the_operator_endpoint() -> None:
    resolved = resolve_settings(_operator(), {"ai_api_key": _enc("sk-org")}, org_scope=ORG_A_ID)
    assert resolved.values["ai_api_key"] == "sk-org"
    assert resolved.values["ai_base_url"] != "https://operator-llm.example.com/v1"


def test_an_org_smtp_host_never_receives_the_operator_password() -> None:
    resolved = resolve_settings(
        _operator(), {"smtp_host": "relay.attacker.example.com"}, org_scope=ORG_A_ID
    )
    assert resolved.values["smtp_host"] == "relay.attacker.example.com"
    assert resolved.values["smtp_password"] == ""
    assert resolved.values["smtp_from_address"] == ""


def test_an_undecryptable_org_secret_is_empty_not_inherited() -> None:
    resolved = resolve_settings(
        _operator(),
        {"ai_base_url": "https://org.example.com/v1", "ai_api_key": "not-a-token"},
        org_scope=ORG_A_ID,
    )
    assert resolved.values["ai_api_key"] == ""


# ── operator ceilings (critique #15) ────────────────────────────────────────


def test_org_limits_are_clamped_to_the_operator() -> None:
    org = {
        "scan_row_limit_default": 999_999,
        "ai_timeout_seconds": 600,
        "ai_max_output_tokens": 100,
    }
    resolved = resolve_settings(_operator(), org, org_scope=ORG_A_ID)
    assert resolved.values["scan_row_limit_default"] == 10_000
    assert resolved.values["ai_timeout_seconds"] == 20
    # Lowering is always allowed.
    assert resolved.values["ai_max_output_tokens"] == 100


# ── scope aliasing (critique #17) ───────────────────────────────────────────


def test_the_self_hosted_default_org_is_the_operator_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert settings_scope_for(None) is None
    assert settings_scope_for(DEFAULT_ORG_ID) is None
    assert settings_scope_for(ORG_A_ID) == ORG_A_ID
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    assert settings_scope_for(DEFAULT_ORG_ID) == DEFAULT_ORG_ID


# ── SSRF at use time (critique #14) ─────────────────────────────────────────


def test_llm_service_refuses_a_private_org_host_at_use_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    org = {
        "ai_base_url": "http://127.0.0.1:11434/v1",
        "ai_api_key": _enc("sk-org"),
        "ai_model": "m",
    }
    config = app_settings_service.ai_config_for(
        resolve_settings(_operator(), org, org_scope=ORG_A_ID)
    )
    assert config.host_guard is True
    posted: list[str] = []
    monkeypatch.setattr(
        llm_service, "_post_chat_completions", lambda url, *_a: (posted.append(url), None)[1]
    )
    assert llm_service.complete("s", "u", config=config) is None
    assert posted == []


def test_self_hosted_operator_hosts_may_be_private(monkeypatch: pytest.MonkeyPatch) -> None:
    operator = {**_operator(), "ai_base_url": "http://localhost:11434/v1"}
    config = app_settings_service.ai_config_for(resolve_settings(operator, None))
    assert config.host_guard is False
    monkeypatch.setattr(
        llm_service,
        "_post_chat_completions",
        lambda *_a: ('{"choices": [{"message": {"content": "ok"}}]}', None),
    )
    assert llm_service.complete("s", "u", config=config) == "ok"


def test_a_private_org_smtp_host_is_dropped_at_use_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    resolved = resolve_settings(
        _operator(),
        {"smtp_host": "10.0.0.5", "smtp_from_address": "a@example.com"},
        org_scope=ORG_A_ID,
    )
    assert app_settings_service.email_config_for(resolved).smtp_host == ""


# ── sync getters: fail closed, the project's organization ───────────────────


@pytest.fixture
def sync_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'org_settings.db'}")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        # ``create_all`` seeds the default organization; add a second one.
        session.add(Organization(id=ORG_A_ID, slug="alpha", name="Alpha"))
        session.commit()
        session.add(AppSetting(key=SERVICE_SETTINGS_KEY, value=_operator(), organization_id=None))
        session.add(
            AppSetting(
                key=SERVICE_SETTINGS_KEY,
                value={
                    "ai_base_url": "https://alpha-llm.example.com/v1",
                    "ai_api_key": _enc("sk-alpha"),
                    "ai_model": "alpha-model",
                    "smtp_host": "alpha-relay.example.com",
                    "smtp_from_address": "alpha@example.com",
                },
                organization_id=ORG_A_ID,
            )
        )
        session.commit()
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_org_scope_sync_reads_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_session: object) -> dict[str, object]:
        raise RuntimeError("db down")

    monkeypatch.setattr(app_settings_service, "get_service_overrides_sync", boom)
    ai = app_settings_service.get_ai_config_sync(object(), org_id=ORG_A_ID)  # type: ignore[arg-type]
    assert ai.ai_api_key == ""
    assert llm_service.is_enabled(ai) is False
    email = app_settings_service.get_email_config_sync(object(), org_id=ORG_A_ID)  # type: ignore[arg-type]
    assert email.smtp_host == ""
    assert email.smtp_password == ""
    with pytest.raises(RuntimeError):
        app_settings_service.get_runtime_config_sync(object(), org_id=ORG_A_ID)  # type: ignore[arg-type]
    # The operator scope still degrades to env, as before.
    assert app_settings_service.get_ai_config_sync(object(), org_id=None).ai_api_key == "sk-env"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_org_scope_async_reads_raise_rather_than_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(_session: object) -> dict[str, object]:
        raise RuntimeError("db down")

    monkeypatch.setattr(app_settings_service, "get_service_overrides", boom)
    async with TestSessionLocal() as session:
        with pytest.raises(RuntimeError):
            await app_settings_service.get_ai_config(session, org_id=ORG_A_ID)


def test_worker_ai_uses_the_projects_organization(sync_factory: sessionmaker[Session]) -> None:
    with sync_factory() as session:
        alpha = Project(name="A", slug="a", organization_id=ORG_A_ID)
        default = Project(name="D", slug="d", organization_id=DEFAULT_ORG_ID)
        session.add_all([alpha, default])
        session.commit()
        a_config = app_settings_service.get_ai_config_for_project_sync(session, alpha.id)
        d_config = app_settings_service.get_ai_config_for_project_sync(session, default.id)
        unknown = app_settings_service.get_ai_config_for_project_sync(session, uuid.uuid4())
    assert a_config.ai_api_key == "sk-alpha"
    assert a_config.ai_model == "alpha-model"
    # Self-hosted default organization == operator scope.
    assert d_config.ai_api_key == "sk-operator"
    # An unknown project gets no AI, not the operator's.
    assert unknown.ai_api_key == ""


def test_alert_email_uses_the_destinations_organization_relay(
    sync_factory: sessionmaker[Session],
) -> None:
    with sync_factory() as session:
        alpha = Project(name="A", slug="a", organization_id=ORG_A_ID)
        session.add(alpha)
        session.commit()
        destination = AlertDestination(
            project_id=alpha.id,
            type="email",
            name="Ops",
            email_recipients="ops@example.com",
        )
        config, recipients, from_address = alerts_task._resolve_email_context(session, destination)
    assert config.smtp_host == "alpha-relay.example.com"
    # The org's own group: no operator password was attached to its relay.
    assert config.smtp_password == ""
    assert recipients == ["ops@example.com"]
    assert from_address == "alpha@example.com"


def test_row_limits_follow_the_projects_organization(
    sync_factory: sessionmaker[Session],
) -> None:
    with sync_factory() as session:
        row = session.scalar(select(AppSetting).where(AppSetting.organization_id == ORG_A_ID))
        assert row is not None
        row.value = {**row.value, "scan_row_limit_default": 500}
        session.commit()
        org = app_settings_service.get_runtime_config_sync(session, org_id=ORG_A_ID)
        operator = app_settings_service.get_runtime_config_sync(session)
    assert org.scan_row_limit_default == 500
    assert operator.scan_row_limit_default == 10_000
    assert org.app_base_url == operator.app_base_url


# ── HTTP surfaces ───────────────────────────────────────────────────────────


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _register(client: AsyncClient, name: str) -> uuid.UUID:
    # Hosted: a verified default-org member, as hosted sign-up used to make.
    return await sign_up(client, email=f"{name}@example.com", password=PASSWORD, name=name)


async def _move_to_org(user_id: uuid.UUID, org_id: uuid.UUID, role: str) -> None:
    async with TestSessionLocal() as session:
        await session.execute(
            delete(OrganizationMember).where(
                OrganizationMember.user_id == user_id,
                OrganizationMember.organization_id == DEFAULT_ORG_ID,
            )
        )
        await session.commit()
        await add_org_member(session, user_id, role, org_id=org_id)


class Hosted:
    """A hosted instance: the platform operator, org A (admin, member), org B (owner)."""

    def __init__(self) -> None:
        self.operator = _new_client()
        self.a_admin = _new_client()
        self.a_member = _new_client()
        self.b_owner = _new_client()

    def clients(self) -> list[AsyncClient]:
        return [self.operator, self.a_admin, self.a_member, self.b_owner]


@pytest.fixture
async def hosted(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Hosted]:
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    async with TestSessionLocal() as session:
        session.add_all(
            [
                Organization(id=ORG_A_ID, slug="alpha", name="Alpha"),
                Organization(id=ORG_B_ID, slug="bravo", name="Bravo"),
            ]
        )
        session.add(AppSetting(key=SERVICE_SETTINGS_KEY, value=_operator(), organization_id=None))
        await session.commit()
    h = Hosted()
    try:
        operator_id = await _register(h.operator, "operator")
        a_admin = await _register(h.a_admin, "alpha-admin")
        a_member = await _register(h.a_member, "alpha-member")
        b_owner = await _register(h.b_owner, "bravo-owner")
        async with TestSessionLocal() as session:
            op = await session.get(User, operator_id)
            assert op is not None
            op.is_platform_admin = True
            await session.commit()
        await _move_to_org(operator_id, ORG_B_ID, "member")
        await _move_to_org(a_admin, ORG_A_ID, "admin")
        await _move_to_org(a_member, ORG_A_ID, "member")
        await _move_to_org(b_owner, ORG_B_ID, "owner")
        yield h
    finally:
        for client in h.clients():
            await client.aclose()


@pytest.mark.asyncio
async def test_org_settings_read_shows_sources_inherited_and_ceilings(hosted: Hosted) -> None:
    resp = await hosted.a_admin.get(f"{API}/orgs/alpha/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["organization"] == "alpha"
    assert body["scope"] == "organization"
    assert body["operator_fallback"] == "all"
    assert body["ai"]["ai_api_key_configured"] is True
    assert body["sources"]["ai.ai_api_key"] == "override"
    assert body["inherited"]["email"]["smtp_host"] == "operator-relay.example.com"
    assert body["ceilings"]["scan_row_limit_default"] == 10_000
    assert "sk-operator" not in resp.text
    assert "operator-smtp-secret" not in resp.text


@pytest.mark.asyncio
async def test_org_settings_write_is_scoped_audited_and_secret_safe(hosted: Hosted) -> None:
    resp = await hosted.a_admin.patch(
        f"{API}/orgs/alpha/settings",
        json={
            "ai": {
                "ai_base_url": "https://alpha-llm.example.com/v1",
                "ai_api_key": "sk-alpha",
                "ai_model": "alpha-model",
            },
            "limits": {"scan_row_limit_default": 777},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sources"]["ai.ai_api_key"] == "org"
    assert body["limits"]["scan_row_limit_default"] == 777
    assert "sk-alpha" not in resp.text

    async with TestSessionLocal() as session:
        rows = (await session.scalars(select(AppSetting))).all()
        by_scope = {row.organization_id: row.value for row in rows}
        assert by_scope[ORG_A_ID]["ai_api_key"] != "sk-alpha"
        # The operator document is untouched; org B has no row at all.
        assert "scan_row_limit_default" in by_scope[None]
        assert by_scope[None]["scan_row_limit_default"] == 10_000
        assert ORG_B_ID not in by_scope
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "settings.update"))
        assert audit is not None
        assert audit.organization_id == ORG_A_ID
        assert audit.payload["scope"] == "organization"

    # Org B still runs on the operator's values.
    b = await hosted.b_owner.get(f"{API}/orgs/bravo/settings")
    assert b.json()["ai"]["ai_model"] == "operator-model"


@pytest.mark.parametrize(
    "payload",
    [
        {"security": {"registration_mode": "open"}},
        {"runtime": {"app_base_url": "https://evil.example.com"}},
        {"ai": {"search_embedding_api_key": "sk-x"}},
        # The storage server paths stay the operator's (PR11, critique #12);
        # an SVG allow-list is refused too, in test_org_photo_storage.py.
        {"storage": {"photo_local_dir": "/etc"}},
        {"storage": {"gcs_photo_credentials_path": "/etc/shadow"}},
    ],
)
@pytest.mark.asyncio
async def test_org_settings_refuse_operator_fields(hosted: Hosted, payload: dict[str, Any]) -> None:
    resp = await hosted.a_admin.patch(f"{API}/orgs/alpha/settings", json=payload)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_org_settings_refuse_private_hosts_when_hosted(hosted: Hosted) -> None:
    for payload in (
        {"ai": {"ai_base_url": "http://169.254.169.254/latest"}},
        {"email": {"smtp_host": "127.0.0.1"}},
        {"email": {"smtp_host": "10.1.2.3"}},
    ):
        resp = await hosted.a_admin.patch(f"{API}/orgs/alpha/settings", json=payload)
        assert resp.status_code == 422, (payload, resp.text)
    async with TestSessionLocal() as session:
        assert await app_settings_service.get_org_overrides(session, ORG_A_ID) == {}


@pytest.mark.asyncio
async def test_org_settings_refuse_limits_above_the_operator(hosted: Hosted) -> None:
    resp = await hosted.a_admin.patch(
        f"{API}/orgs/alpha/settings", json={"ai": {"ai_timeout_seconds": 21}}
    )
    assert resp.status_code == 422
    assert "20" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_org_settings_permissions(hosted: Hosted) -> None:
    # A member reads the row limits only.
    assert (await hosted.a_member.get(f"{API}/orgs/alpha/settings")).status_code == 403
    limits = await hosted.a_member.get(f"{API}/orgs/alpha/settings/row-limits")
    assert limits.status_code == 200
    assert limits.json()["scan_row_limit_default"] == 10_000
    assert (await hosted.a_member.patch(f"{API}/orgs/alpha/settings", json={})).status_code == 403
    # Another organization's owner: the organization does not exist for them.
    for method, path in (
        ("GET", "/orgs/alpha/settings"),
        ("PATCH", "/orgs/alpha/settings"),
        ("GET", "/orgs/alpha/settings/row-limits"),
        ("POST", "/orgs/alpha/settings/ai/test"),
        ("POST", "/orgs/alpha/settings/email/test"),
    ):
        kwargs: dict[str, Any] = {"json": {}} if method != "GET" else {}
        resp = await hosted.b_owner.request(method, f"{API}{path}", **kwargs)
        assert resp.status_code == 404, (path, resp.text)
    # A platform admin gets nothing inside an organization from the flag.
    assert (await hosted.operator.get(f"{API}/orgs/alpha/settings")).status_code == 404


@pytest.mark.asyncio
async def test_org_probes_use_the_organizations_config(
    hosted: Hosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def fake_complete(*_args: object, config: Any, **_kwargs: object) -> str:
        seen.append(config.ai_api_key)
        return "ok"

    monkeypatch.setattr(llm_service, "complete", fake_complete)
    await hosted.a_admin.patch(
        f"{API}/orgs/alpha/settings",
        json={
            "ai": {
                "ai_base_url": "https://alpha-llm.example.com/v1",
                "ai_api_key": "sk-alpha",
            }
        },
    )
    resp = await hosted.a_admin.post(f"{API}/orgs/alpha/settings/ai/test", json={})
    assert resp.json()["ok"] is True
    b = await hosted.b_owner.post(f"{API}/orgs/bravo/settings/ai/test", json={})
    assert b.json()["ok"] is True
    assert seen == ["sk-alpha", "sk-operator"]


@pytest.mark.asyncio
async def test_platform_settings_are_platform_admin_only(hosted: Hosted) -> None:
    for client in (hosted.a_admin, hosted.b_owner):
        assert (await client.get(f"{API}/platform/settings")).status_code == 403
        assert (
            await client.patch(
                f"{API}/platform/settings", json={"security": {"registration_mode": "open"}}
            )
        ).status_code == 403
    resp = await hosted.operator.get(f"{API}/platform/settings")
    assert resp.status_code == 200
    assert resp.json()["system"] is not None
    written = await hosted.operator.patch(
        f"{API}/platform/settings", json={"email": {"smtp_host": "new-relay.example.com"}}
    )
    assert written.status_code == 200, written.text
    assert written.json()["email"]["smtp_host"] == "new-relay.example.com"
    async with TestSessionLocal() as session:
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "settings.update"))
        assert audit is not None
        assert audit.organization_id is None
        assert audit.payload["scope"] == "platform"


@pytest.mark.asyncio
async def test_hosted_legacy_settings_act_in_the_callers_own_org(hosted: Hosted) -> None:
    resp = await hosted.a_admin.patch(
        f"{API}/settings", json={"runtime": {"scan_row_limit_default": 321}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["runtime"]["scan_row_limit_default"] == 321
    assert resp.json()["sources"]["runtime.scan_row_limit_default"] == "org"
    async with TestSessionLocal() as session:
        assert (await app_settings_service.get_org_overrides(session, ORG_A_ID)) == {
            "scan_row_limit_default": 321
        }
        operator = await app_settings_service.get_service_overrides(session)
        assert operator["scan_row_limit_default"] == 10_000
    # Org B's legacy view does not see it.
    b = await hosted.b_owner.get(f"{API}/settings")
    assert b.status_code == 200
    assert b.json()["runtime"]["scan_row_limit_default"] == 10_000
    # A member of A is not a settings admin.
    assert (await hosted.a_member.get(f"{API}/settings")).status_code == 403
    # The org-scope checks apply through the legacy path too.
    private = await hosted.a_admin.patch(
        f"{API}/settings", json={"email": {"smtp_host": "10.0.0.1"}}
    )
    assert private.status_code == 422


@pytest.mark.asyncio
async def test_account_mail_uses_the_operator_relay(
    hosted: Hosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    await hosted.a_admin.patch(
        f"{API}/orgs/alpha/settings",
        json={
            "email": {
                "smtp_host": "alpha-relay.example.com",
                "smtp_from_address": "a@example.com",
            }
        },
    )
    sent: list[str] = []

    def capture(*, recipient: str, reset_link: str, email_config: Any) -> None:
        sent.append(email_config.smtp_host)

    monkeypatch.setattr(auth_api, "_send_password_reset_email", capture)
    resp = await _new_client().post(
        f"{API}/auth/password-reset/request", json={"email": "alpha-admin@example.com"}
    )
    assert resp.status_code == 200
    assert sent == ["operator-relay.example.com"]
    async with TestSessionLocal() as session:
        mail = await invitation_email.prepare(
            session,
            recipient="new@example.com",
            organization_id=ORG_A_ID,
            accept_path="/invite/x",
        )
    assert mail is not None
    assert mail.email_config.smtp_host == "operator-relay.example.com"


# ── self-hosted: the default organization is the operator scope ─────────────


@pytest.mark.asyncio
async def test_self_hosted_default_org_settings_write_the_operator_scope(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp = await client.patch(
        f"{API}/orgs/default/settings",
        json={
            "email": {
                "smtp_host": "team-relay.example.com",
                "smtp_from_address": "t@example.com",
            }
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope"] == "operator"
    async with TestSessionLocal() as session:
        operator = await app_settings_service.get_service_overrides(session)
        assert operator["smtp_host"] == "team-relay.example.com"
        assert (
            await session.scalar(select(AppSetting).where(AppSetting.organization_id.is_not(None)))
        ) is None
    # Password-reset mail follows what was set there (critique #17).
    sent: list[str] = []

    def capture(*, recipient: str, reset_link: str, email_config: Any) -> None:
        sent.append(email_config.smtp_host)

    monkeypatch.setattr(auth_api, "_send_password_reset_email", capture)
    await client.post(f"{API}/auth/password-reset/request", json={"email": "test@example.com"})
    assert sent == ["team-relay.example.com"]


def test_org_fields_are_what_the_owner_decided() -> None:
    assert {"smtp_host", "smtp_password", "ai_base_url", "ai_api_key"} <= ORG_FIELDS
    # PR10: each organization's own vector space; the width stays the operator's.
    assert {"search_embedding_api_key", "search_embedding_base_url"} <= ORG_FIELDS
    assert "search_embedding_dimensions" not in ORG_FIELDS
    # PR11: photo storage, without the server paths.
    assert {"photo_storage_backend", "gcs_photo_bucket", "gcs_photo_credentials_json"} <= ORG_FIELDS
    assert {"photo_local_dir", "gcs_photo_credentials_path"}.isdisjoint(ORG_FIELDS)
    assert "app_base_url" not in ORG_FIELDS


# ── review repairs: operator credentials, redirects, redaction, audit feeds ─


def test_a_self_hosted_non_default_org_host_is_guarded() -> None:
    """Only the default organization is the operator's own team when self-hosted."""
    assert settings.deployment_mode == "self_hosted"
    resolved = resolve_settings(
        _operator(),
        {"ai_base_url": "http://10.0.0.5/v1", "ai_api_key": _enc("k"), "smtp_host": "127.0.0.1"},
        org_scope=ORG_A_ID,
    )
    assert app_settings_service.ai_config_for(resolved).host_guard is True
    assert app_settings_service.email_config_for(resolved).smtp_host == ""


@pytest.mark.asyncio
async def test_self_hosted_other_org_refuses_private_hosts_on_save(client: AsyncClient) -> None:
    async with TestSessionLocal() as session:
        session.add(Organization(id=ORG_A_ID, slug="alpha", name="Alpha"))
        await session.commit()
        user = await session.scalar(select(User).where(User.email == "test@example.com"))
        assert user is not None
        await add_org_member(session, user.id, "admin", org_id=ORG_A_ID)
    for payload in (
        {"ai": {"ai_base_url": "http://10.0.0.5/v1"}},
        {"email": {"smtp_host": "127.0.0.1"}},
    ):
        resp = await client.patch(f"{API}/orgs/alpha/settings", json=payload)
        assert resp.status_code == 422, (payload, resp.text)
    async with TestSessionLocal() as session:
        assert await app_settings_service.get_org_overrides(session, ORG_A_ID) == {}


def test_ai_completions_never_follow_a_redirect() -> None:
    """A public endpoint answering 302 -> an internal address is not followed."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    hits: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            hits.append((self.command, self.path))
            if self.path.startswith("/v1/"):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port}/internal")
                self.end_headers()
                return
            body = b'{"choices": [{"message": {"content": "internal"}}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self._answer()

        def do_GET(self) -> None:  # noqa: N802
            self._answer()

        def log_message(self, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # The operator's config (no use-time host check) so the first hop is
        # allowed; the redirect refusal applies to every AI completion.
        operator = {**_operator(), "ai_base_url": f"http://127.0.0.1:{port}/v1"}
        config = app_settings_service.ai_config_for(resolve_settings(operator, None))
        assert llm_service.complete("s", "u", config=config) is None
    finally:
        server.shutdown()
        server.server_close()
    assert hits == [("POST", "/v1/chat/completions")]


@pytest.mark.asyncio
async def test_org_view_withholds_the_operators_smtp_username(hosted: Hosted) -> None:
    written = await hosted.operator.patch(
        f"{API}/platform/settings", json={"email": {"smtp_username": "AKIAOPERATORKEYID"}}
    )
    assert written.status_code == 200, written.text
    assert written.json()["email"]["smtp_username"] == "AKIAOPERATORKEYID"
    resp = await hosted.a_admin.get(f"{API}/orgs/alpha/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email"]["smtp_username"] == ""
    assert body["inherited"]["email"]["smtp_username"] == ""
    # Told THAT it inherits the relay, not the operator's login to it.
    assert body["inherited"]["email"]["smtp_host"] == "operator-relay.example.com"
    assert "AKIAOPERATORKEYID" not in resp.text
    # Its own username is its own to see.
    own = await hosted.a_admin.patch(
        f"{API}/orgs/alpha/settings",
        json={"email": {"smtp_host": "relay.example.com", "smtp_username": "alpha-user"}},
    )
    assert own.status_code == 200, own.text
    assert own.json()["email"]["smtp_username"] == "alpha-user"


@pytest.mark.asyncio
async def test_hosted_legacy_settings_withhold_operator_infrastructure(hosted: Hosted) -> None:
    tenant = await hosted.a_admin.get(f"{API}/settings")
    assert tenant.status_code == 200, tenant.text
    body = tenant.json()
    for section in ("system", "security", "storage", "observability"):
        assert body[section] is None, section
    assert body["ai"]["search_embedding_base_url"] == ""
    assert not any(key.startswith(("security.", "storage.")) for key in body["sources"])
    operator = await hosted.operator.get(f"{API}/settings")
    assert operator.status_code == 200, operator.text
    for section in ("system", "security", "storage", "observability"):
        assert operator.json()[section] is not None, section


@pytest.mark.asyncio
async def test_legacy_settings_file_default_org_fields_in_its_feed(client: AsyncClient) -> None:
    """Self-hosted: the default organization's own values, written through the
    legacy route, land in its audit feed as through ``/orgs/{org}/settings``."""
    resp = await client.patch(
        f"{API}/settings",
        json={
            "runtime": {"scan_row_limit_default": 55},
            "observability": {"otel_service_name": "tripl-audit"},
        },
    )
    assert resp.status_code == 200, resp.text
    async with TestSessionLocal() as session:
        rows = (
            await session.scalars(select(AuditLog).where(AuditLog.action == "settings.update"))
        ).all()
    feeds = {row.organization_id: row.payload["changed_fields"] for row in rows}
    assert feeds == {
        DEFAULT_ORG_ID: ["scan_row_limit_default"],
        None: ["otel_service_name"],
    }


@pytest.mark.asyncio
async def test_ask_plan_sends_the_bound_organizations_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from tripl.middleware.org_context import OrgRef, bound_org
    from tripl.services import ai_service

    async with TestSessionLocal() as session:
        session.add(Organization(id=ORG_A_ID, slug="alpha", name="Alpha"))
        await session.commit()
        session.add(AppSetting(key=SERVICE_SETTINGS_KEY, value=_operator(), organization_id=None))
        session.add(
            AppSetting(
                key=SERVICE_SETTINGS_KEY,
                value={
                    "ai_base_url": "https://alpha-llm.example.com/v1",
                    "ai_api_key": _enc("sk-alpha"),
                },
                organization_id=ORG_A_ID,
            )
        )
        await session.commit()

    async def no_hits(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(items=[], semantic_used=False)

    seen: list[str] = []

    def fake_complete(*_args: object, config: Any, **_kwargs: object) -> str:
        seen.append(config.ai_api_key)
        return "answer"

    monkeypatch.setattr(ai_service.search_service, "search_project", no_hits)
    monkeypatch.setattr(llm_service, "complete", fake_complete)
    async with TestSessionLocal() as session:
        with bound_org(OrgRef(id=ORG_A_ID, slug="alpha")):
            await ai_service.ask_plan(session, "any", "q?", None)
        with bound_org(OrgRef(id=DEFAULT_ORG_ID, slug="default")):
            await ai_service.ask_plan(session, "any", "q?", None)
    # Alpha's own key; the self-hosted default organization runs the operator's.
    assert seen == ["sk-alpha", "sk-operator"]


@pytest.mark.asyncio
async def test_email_available_means_any_of_the_users_organizations(
    hosted: Hosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Notification preferences are per user across organizations; each
    organization mails through its own relay, so email is available when any
    one of them can send."""
    monkeypatch.setattr(settings, "org_settings_operator_fallback", "none")
    alone = await hosted.a_admin.get(f"{API}/me/notification-prefs")
    assert alone.status_code == 200, alone.text
    assert alone.json()["email_available"] is False
    async with TestSessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == "alpha-admin@example.com"))
        assert user is not None
        await add_org_member(session, user.id, "member", org_id=ORG_B_ID)
        session.add(
            AppSetting(
                key=SERVICE_SETTINGS_KEY,
                value={
                    "smtp_host": "bravo-relay.example.com",
                    "smtp_from_address": "b@example.com",
                },
                organization_id=ORG_B_ID,
            )
        )
        await session.commit()
    # Read from alpha (no relay): bravo will still email this user.
    both = await hosted.a_admin.get(f"{API}/orgs/alpha/me/notification-prefs")
    assert both.status_code == 200, both.text
    assert both.json()["email_available"] is True
