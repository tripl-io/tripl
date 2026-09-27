"""``/settings`` under organization roles and the platform admin (F20 PR4, GH #273).

* the settings admins are a platform admin, or an owner/admin of the default
  organization (whose values the instance scope is until PR9);
* a write touching an operator-only field (``OPERATOR_FIELDS``) takes a platform
  admin: an org admin gets 403 and nothing is written, even when the operator
  field rides along with org fields;
* the ``system`` block is ``None`` for everyone but a platform admin, on reads
  and on write answers alike;
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
_ORG_FIELDS = {
    "scan_row_limit_default",
    "metrics_row_limit_default",
    "photo_allowed_mime",
    "ai_enabled",
    "ai_model",
    "ai_timeout_seconds",
    "ai_max_output_tokens",
    "describe_system_prompt",
    "ask_system_prompt",
    "alert_explanation_system_prompt",
    "search_embeddings_enabled",
}


def test_every_editable_field_is_classified_exactly_once() -> None:
    operator = app_settings_service.OPERATOR_FIELDS
    assert operator.isdisjoint(_ORG_FIELDS)
    assert operator | _ORG_FIELDS == app_settings_service.EDITABLE_FIELDS
    assert set(app_settings_service.SECURITY_FIELDS) <= operator
    assert set(app_settings_service.OBSERVABILITY_FIELDS) <= operator
    # One shared value per instance until PR9: whoever sets these routes every
    # organization's mail, photos and plan text.
    assert set(app_settings_service.EMAIL_FIELDS) <= operator
    assert {"ai_base_url", "ai_api_key", "search_embedding_api_key"} <= operator
    assert {"photo_storage_backend", "gcs_photo_bucket", "gcs_photo_public"} <= operator
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
    assert app_settings_service.touches_operator_fields({"smtp_host": "smtp.example.com"})
    assert not app_settings_service.touches_operator_fields(
        {"scan_row_limit_default": 10, "ai_model": "m"}
    )
    assert not app_settings_service.touches_operator_fields({})


# ── who reads and who writes ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_system_is_only_for_the_platform_admin(stand: Stand) -> None:
    operator = await stand.operator.get("/api/v1/settings")
    assert operator.status_code == 200, operator.text
    assert operator.json()["system"] is not None

    admin = await stand.admin.get("/api/v1/settings")
    assert admin.status_code == 200, admin.text
    assert admin.json()["system"] is None
    # Everything else of the read is there for the org admin.
    assert admin.json()["email"] == operator.json()["email"]

    written = await stand.admin.patch(
        "/api/v1/settings", json={"runtime": {"scan_row_limit_default": 4321}}
    )
    assert written.status_code == 200, written.text
    assert written.json()["system"] is None
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
        {"email": {"smtp_host": "smtp.example.com"}},
        {"ai": {"ai_base_url": "https://llm.evil.example.com"}},
        {"ai": {"search_embedding_api_key": "sk-x"}},
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
        {"ai": {"ai_model": "some-model"}},
        {"runtime": {"scan_row_limit_default": 1234}},
        {"storage": {"photo_allowed_mime": "image/png"}},
    ],
)
@pytest.mark.asyncio
async def test_org_admin_writes_org_fields(stand: Stand, payload: dict[str, Any]) -> None:
    for method in ("PATCH", "PUT"):
        written = await stand.admin.request(method, "/api/v1/settings", json=payload)
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
