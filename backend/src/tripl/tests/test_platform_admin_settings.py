"""``/settings`` under organization roles and the platform admin (F20 PR4/PR9, GH #273).

* the settings admins are a platform admin, or an owner/admin of the default
  organization — on this self-hosted instance the default organization's scope
  IS the operator's (PR9, critique #17), so its admins' mail and AI values are
  the instance's;
* a write touching an operator-only field (``OPERATOR_FIELDS``) takes a platform
  admin: an org admin gets 403 and nothing is written, even when the operator
  field rides along with org fields;
* on that operator alias the credential groups (SMTP relay, AI endpoint, key
  and model) still take a platform admin: the relay carries every user's
  password-reset mail;
* the ``system``, ``security``, ``storage`` and ``observability`` blocks are
  ``None`` for everyone but a platform admin, on reads and on write answers
  alike;
* a platform admin with no membership still operates the instance, but sees no
  project and reads every data source's connection redacted.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from tripl.api import deps
from tripl.main import app
from tripl.models.organization import OrganizationMember
from tripl.models.project_member import ProjectMember
from tripl.services import app_settings_service
from tripl.tests._members import add_org_member
from tripl.tests.conftest import TestSessionLocal

PASSWORD = "Password123!"


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _register(client: AsyncClient, name: str) -> uuid.UUID:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": f"{name}@example.com", "password": PASSWORD, "name": name},
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


class Stand:
    def __init__(self) -> None:
        self.operator = _new_client()
        self.admin = _new_client()
        self.member = _new_client()
        self.operator_id = uuid.uuid4()
        self.admin_id = uuid.uuid4()


@pytest.fixture
async def stand() -> AsyncIterator[Stand]:
    """The first account (default-org owner AND platform admin), an org admin, a member."""
    s = Stand()
    s.operator_id = await _register(s.operator, "operator")
    s.admin_id = await _register(s.admin, "orgadmin")
    await _register(s.member, "member")
    async with TestSessionLocal() as session:
        await add_org_member(session, s.admin_id, "admin")
    yield s
    for client in (s.operator, s.admin, s.member):
        await client.aclose()


# ── the field classification ────────────────────────────────────────────────

# Written out, not derived: a new settings field must be classified on purpose.
# F20 PR9 (owner decision 4): mail, AI chat and the row-limit defaults are an
# organization's own, and PR10 adds the search embeddings (switch, provider,
# model, key; the endpoint too, which is env-only for the operator and so not
# an editable field at all). Storage (PR11) stays the operator's.
_ORG_FIELDS = {
    "scan_row_limit_default",
    "metrics_row_limit_default",
    "smtp_host",
    "smtp_port",
    "smtp_username",
    "smtp_password",
    "smtp_security",
    "smtp_from_address",
    "ai_enabled",
    "ai_base_url",
    "ai_model",
    "ai_api_key",
    "ai_timeout_seconds",
    "ai_max_output_tokens",
    "describe_system_prompt",
    "ask_system_prompt",
    "alert_explanation_system_prompt",
    "search_embeddings_enabled",
    "search_embedding_provider",
    "search_embedding_model",
    "search_embedding_api_key",
}


def test_every_editable_field_is_classified_exactly_once() -> None:
    operator = app_settings_service.OPERATOR_FIELDS
    assert operator.isdisjoint(_ORG_FIELDS)
    assert operator | _ORG_FIELDS == app_settings_service.EDITABLE_FIELDS
    # The org-only field: an organization's own embedding endpoint (PR10).
    assert _ORG_FIELDS | {"search_embedding_base_url"} == app_settings_service.ORG_FIELDS
    assert "search_embedding_base_url" not in app_settings_service.EDITABLE_FIELDS
    assert set(app_settings_service.SECURITY_FIELDS) <= operator
    assert set(app_settings_service.OBSERVABILITY_FIELDS) <= operator
    # Still one shared value per instance: every storage field (PR11), which
    # routes every organization's photos.
    assert set(app_settings_service.STORAGE_FIELDS) <= operator
    assert {
        "app_base_url",
        "registration_mode",
        "photo_local_dir",
        "gcs_photo_credentials_path",
        "photo_max_size_mb",
    } <= operator


def test_touches_operator_fields() -> None:
    assert app_settings_service.touches_operator_fields({"registration_mode": "open"})
    assert app_settings_service.touches_operator_fields(
        {"smtp_host": "smtp.example.com", "photo_local_dir": "/tmp"}
    )
    assert not app_settings_service.touches_operator_fields({"smtp_host": "smtp.example.com"})
    assert not app_settings_service.touches_operator_fields(
        {"scan_row_limit_default": 10, "ai_model": "m", "ai_api_key": "k"}
    )
    assert app_settings_service.touches_operator_fields({"photo_allowed_mime": "image/png"})
    assert not app_settings_service.touches_operator_fields({})


# ── who reads and who writes ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_system_is_only_for_the_platform_admin(stand: Stand) -> None:
    operator = await stand.operator.get("/api/v1/settings")
    assert operator.status_code == 200, operator.text
    assert operator.json()["system"] is not None

    for section in ("security", "storage", "observability"):
        assert operator.json()[section] is not None

    admin = await stand.admin.get("/api/v1/settings")
    assert admin.status_code == 200, admin.text
    assert admin.json()["system"] is None
    # The operator's infrastructure is withheld from an org admin...
    for section in ("security", "storage", "observability"):
        assert admin.json()[section] is None
    assert admin.json()["ai"]["search_embedding_base_url"] == ""
    assert not any(key.startswith("storage.") for key in admin.json()["sources"])
    # ...the organization's own values are there.
    assert admin.json()["email"] == operator.json()["email"]

    written = await stand.admin.patch(
        "/api/v1/settings", json={"runtime": {"scan_row_limit_default": 4321}}
    )
    assert written.status_code == 200, written.text
    assert written.json()["system"] is None
    assert written.json()["security"] is None
    assert written.json()["runtime"]["scan_row_limit_default"] == 4321


@pytest.mark.parametrize(
    "payload",
    [
        {"runtime": {"app_base_url": "https://evil.example.com"}},
        {"security": {"session_ttl_hours": 1}},
        {"observability": {"log_level": "DEBUG"}},
        {"storage": {"photo_local_dir": "/etc"}},
        {"storage": {"gcs_photo_credentials_path": "/etc/shadow"}},
        {"storage": {"photo_max_size_mb": 4096}},
        {"storage": {"gcs_photo_bucket": "attacker-bucket"}},
        {"storage": {"gcs_photo_public": True}},
        {"storage": {"photo_allowed_mime": "image/png"}},
        # The operator's embedding endpoint group and switch (PR10): the stand's
        # organization is the operator alias, so these are every inheriting
        # organization's search provider and need a platform admin.
        {"ai": {"search_embedding_api_key": "sk-x"}},
        {"ai": {"search_embeddings_enabled": True}},
        # An operator field riding along with an org field refuses the whole write.
        {"ai": {"ai_model": "some-model"}, "security": {"registration_mode": "open"}},
    ],
)
@pytest.mark.asyncio
async def test_org_admin_cannot_write_operator_fields(
    stand: Stand, payload: dict[str, Any]
) -> None:
    before = await stand.operator.get("/api/v1/settings")
    for method in ("PATCH", "PUT"):
        refused = await stand.admin.request(method, "/api/v1/settings", json=payload)
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"] == deps.PLATFORM_ADMIN_REQUIRED
    after = await stand.operator.get("/api/v1/settings")
    assert after.json()["overridden_fields"] == before.json()["overridden_fields"]


@pytest.mark.parametrize(
    "payload",
    [
        {"runtime": {"scan_row_limit_default": 1234}},
        {"ai": {"ai_timeout_seconds": 5, "describe_system_prompt": "Be brief."}},
        {"ai": {"ai_enabled": False}},
    ],
)
@pytest.mark.asyncio
async def test_org_admin_writes_org_fields(stand: Stand, payload: dict[str, Any]) -> None:
    for method in ("PATCH", "PUT"):
        written = await stand.admin.request(method, "/api/v1/settings", json=payload)
        assert written.status_code == 200, written.text


@pytest.mark.parametrize(
    "payload",
    [
        {"email": {"smtp_host": "smtp.attacker.example.com"}},
        {"email": {"smtp_password": "x"}},
        {"ai": {"ai_base_url": "https://llm.example.com/v1", "ai_api_key": "sk-org"}},
        {"ai": {"ai_model": "some-model"}},
        # A credential field riding along with a scalar refuses the whole write.
        {"runtime": {"scan_row_limit_default": 99}, "email": {"smtp_host": "x.example.com"}},
    ],
)
@pytest.mark.asyncio
async def test_org_admin_cannot_rewrite_the_operator_relay_or_ai_endpoint(
    stand: Stand, payload: dict[str, Any]
) -> None:
    """Self-hosted: the default organization IS the operator scope, whose SMTP
    relay carries every account's password-reset mail and whose AI endpoint
    other organizations inherit — a platform admin's to change, by either route."""
    before = await stand.operator.get("/api/v1/settings")
    for method in ("PATCH", "PUT"):
        for path in ("/api/v1/settings", "/api/v1/orgs/default/settings"):
            body = payload
            if path.startswith("/api/v1/orgs") and "runtime" in payload:
                body = {**payload, "limits": payload["runtime"]}
                del body["runtime"]
            refused = await stand.admin.request(method, path, json=body)
            assert refused.status_code == 403, (path, refused.text)
            assert refused.json()["detail"] == deps.PLATFORM_ADMIN_REQUIRED
    after = await stand.operator.get("/api/v1/settings")
    assert after.json()["overridden_fields"] == before.json()["overridden_fields"]
    # The platform admin may.
    written = await stand.operator.patch(
        "/api/v1/orgs/default/settings", json={"email": {"smtp_host": "relay.example.com"}}
    )
    assert written.status_code == 200, written.text


@pytest.mark.asyncio
async def test_platform_admin_writes_operator_fields(stand: Stand) -> None:
    written = await stand.operator.patch(
        "/api/v1/settings", json={"observability": {"otel_service_name": "tripl-test"}}
    )
    assert written.status_code == 200, written.text
    assert written.json()["observability"]["otel_service_name"] == "tripl-test"


@pytest.mark.asyncio
async def test_org_admin_reaches_the_org_settings_side_routes(stand: Stand) -> None:
    assert (await stand.admin.get("/api/v1/settings/ai")).status_code == 200
    assert (await stand.admin.get("/api/v1/settings/ai/defaults")).status_code == 200


@pytest.mark.asyncio
async def test_a_plain_member_gets_only_the_public_limits(stand: Stand) -> None:
    for path in ("/api/v1/settings", "/api/v1/settings/ai", "/api/v1/settings/ai/defaults"):
        assert (await stand.member.get(path)).status_code == 403, path
    refused = await stand.member.patch(
        "/api/v1/settings", json={"email": {"smtp_host": "smtp.example.com"}}
    )
    assert refused.status_code == 403
    assert (await stand.member.get("/api/v1/settings/photo-limits")).status_code == 200
    assert (await stand.member.get("/api/v1/settings/row-limits")).status_code == 200


@pytest.mark.asyncio
async def test_a_platform_admin_key_cannot_touch_settings(stand: Stand) -> None:
    minted = await stand.operator.post(
        "/api/v1/me/api-keys", json={"name": "ops", "scope": "write"}
    )
    assert minted.status_code == 201, minted.text
    headers = {"Authorization": f"Bearer {minted.json()['token']}"}
    async with _new_client() as bearer:
        assert (await bearer.get("/api/v1/settings", headers=headers)).status_code == 403
        refused = await bearer.patch(
            "/api/v1/settings",
            json={"observability": {"otel_service_name": "x"}},
            headers=headers,
        )
        assert refused.status_code == 403


# ── the platform admin holds nothing inside an organization ────────────────


@pytest.mark.asyncio
async def test_platform_admin_without_membership(stand: Stand) -> None:
    project = await stand.operator.post(
        "/api/v1/projects", json={"name": "Private", "slug": "private"}
    )
    assert project.status_code == 201, project.text
    source = await stand.admin.post(
        "/api/v1/data-sources",
        json={
            "name": "wh",
            "db_type": "clickhouse",
            "host": "clickhouse.internal.example.com",
            "port": 9440,
            "database_name": "analytics",
            "username": "tripl_ro",
            "password": "hunter2",
        },
    )
    assert source.status_code == 201, source.text
    async with TestSessionLocal() as session:
        for model in (OrganizationMember, ProjectMember):
            await session.execute(delete(model).where(model.user_id == stand.operator_id))
        await session.commit()

    assert (await stand.operator.get("/api/v1/projects/private")).status_code == 404
    assert (await stand.operator.get("/api/v1/projects")).json() == []
    listed = await stand.operator.get("/api/v1/data-sources")
    assert listed.status_code == 200, listed.text
    assert [row["host"] for row in listed.json()] == [""]
    assert "internal.example.com" not in listed.text

    # ...while the org admin, who is not a platform admin, reads it unredacted.
    admin_view = await stand.admin.get("/api/v1/data-sources")
    assert [row["host"] for row in admin_view.json()] == ["clickhouse.internal.example.com"]

    settings = await stand.operator.get("/api/v1/settings")
    assert settings.status_code == 200, settings.text
    assert settings.json()["system"] is not None
    operator = await stand.operator.patch(
        "/api/v1/settings", json={"security": {"session_ttl_hours": 24}}
    )
    assert operator.status_code == 200, operator.text
