"""Regression checks for the HTTP shell and auth hardening batch."""

from __future__ import annotations

import ast
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx
import pytest
from fastapi import FastAPI, File, Request, UploadFile
from httpx import AsyncClient
from sqlalchemy import select

from tripl import realtime
from tripl.config import Settings, settings
from tripl.middleware.body_limit import BodyLimitMiddleware
from tripl.middleware.security_headers import build_security_headers
from tripl.models.audit_log import AuditLog
from tripl.observability.metrics import settings_read_failures_total
from tripl.services import app_settings_service
from tripl.tests.conftest import TestSessionLocal


def test_startup_values_reject_invalid_http_fields() -> None:
    for field, value in (
        ("content_security_policy", "default-src 'self'\r\nX-Evil: yes"),
        ("request_id_header", "X Request ID"),
        ("session_cookie_name", "bad cookie"),
        ("smtp_from_address", "not-an-address"),
    ):
        with pytest.raises(ValueError):
            Settings.model_validate({**settings.model_dump(), field: value})


def test_production_cors_rejects_mixed_wildcard() -> None:
    candidate = Settings.model_validate(
        {**settings.model_dump(), "debug": False, "cors_allow_origins": "*, https://example.com"}
    )
    assert any("wildcard" in problem for problem in candidate.production_problems())


def test_default_csp_allows_figma_and_gcs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "serve_frontend", True)
    monkeypatch.setattr(settings, "content_security_policy", "")
    monkeypatch.setattr(settings, "photo_storage_backend", "gcs")
    csp = build_security_headers()["content-security-policy"]
    assert "frame-src https://www.figma.com https://embed.figma.com" in csp
    assert "img-src 'self' data: blob: https://storage.googleapis.com" in csp


@pytest.mark.asyncio
async def test_body_limit_rejects_declared_and_streamed_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "max_request_body_mb", 1)
    app = FastAPI()
    reached = []

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        reached.append(True)
        return {"size": len(await request.body())}

    @app.post("/api/v1/projects/demo/events/event-id/photos")
    async def photo(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    app.add_middleware(BodyLimitMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        declared = await client.post("/echo", content=b"x" * (1024 * 1024 + 1))
        assert reached == []  # Content-Length is refused before the app runs.

        async def chunks() -> AsyncIterator[bytes]:
            yield b"x" * (700 * 1024)
            yield b"x" * (400 * 1024)

        streamed = await client.post("/echo", content=chunks())
        photo_upload = await client.post(
            "/api/v1/projects/demo/events/event-id/photos", content=b"x" * (2 * 1024 * 1024)
        )
    assert declared.status_code == streamed.status_code == 413
    assert photo_upload.status_code == 200


@pytest.mark.asyncio
async def test_chunked_multipart_is_413_before_form_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "photo_max_size_mb", 1)
    app = FastAPI()

    @app.post("/api/v1/projects/demo/events/event-id/photos")
    async def upload(file: Annotated[UploadFile, File()]) -> dict[str, str]:
        return {"filename": file.filename or ""}

    app.add_middleware(BodyLimitMiddleware)
    boundary = "batch09"

    async def chunks() -> AsyncIterator[bytes]:
        yield (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="large.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode()
        for _ in range(40):
            yield b"x" * 65536
        yield f"\r\n--{boundary}--\r\n".encode()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/projects/demo/events/event-id/photos",
            content=chunks(),
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_realtime_subscribes_before_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    @asynccontextmanager
    async def subscribed(
        _project_id: uuid.UUID,
    ) -> AsyncIterator[AsyncIterator[dict[str, Any] | None]]:
        order.append("subscribe")

        async def messages() -> AsyncIterator[dict[str, Any] | None]:
            if False:
                yield None

        yield messages()

    async def replay(_project_id: uuid.UUID, _cursor: int | None) -> realtime.ResumePoint:
        order.append("replay")
        return realtime.ResumePoint(seq=0, epoch="e", replay=[])

    async def connected() -> bool:
        return False

    monkeypatch.setattr(realtime, "subscribed_messages", subscribed)
    monkeypatch.setattr(realtime, "read_resume_point", replay)
    frames = [
        frame
        async for frame in realtime.project_response_stream(
            project_id=uuid.uuid4(),
            slug="test",
            last_event_id=1,
            is_disconnected=connected,
            max_messages=0,
        )
    ]
    assert order == ["subscribe", "replay"]
    assert '"backend": "redis"' in frames[0]


def test_api_routers_do_not_build_sql_queries() -> None:
    root = Path(__file__).parents[1] / "api" / "v1"
    query_builders = {"select", "insert", "update", "delete"}
    session_methods = {"execute", "get", "scalar", "scalars", "stream"}
    offenders: list[str] = []
    for source in root.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            builds = isinstance(func, ast.Name) and func.id in query_builders
            runs = (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "session"
                and func.attr in session_methods
            )
            if builds or runs:
                offenders.append(f"{source.name}:{node.lineno}")
    assert offenders == []


@pytest.mark.asyncio
async def test_bearer_key_cannot_manage_keys(client: AsyncClient, anon_client: AsyncClient) -> None:
    created = await client.post("/api/v1/me/api-keys", json={"name": "agent", "scope": "write"})
    assert created.status_code == 201
    auth = {"Authorization": f"Bearer {created.json()['token']}"}
    successor = await anon_client.post(
        "/api/v1/me/api-keys", json={"name": "successor", "scope": "write"}, headers=auth
    )
    revoke = await anon_client.delete(f"/api/v1/me/api-keys/{created.json()['id']}", headers=auth)
    assert successor.status_code == revoke.status_code == 403


@pytest.mark.asyncio
async def test_settings_and_key_revocation_have_audit_rows(client: AsyncClient) -> None:
    updated = await client.patch(
        "/api/v1/settings", json={"security": {"rate_limit_enabled": False}}
    )
    assert updated.status_code == 200
    created = await client.post(
        "/api/v1/me/api-keys", json={"name": "batch09-audit-key", "scope": "read"}
    )
    assert created.status_code == 201
    revoked = await client.delete(f"/api/v1/me/api-keys/{created.json()['id']}")
    assert revoked.status_code == 204

    async with TestSessionLocal() as session:
        rows = await session.scalars(
            select(AuditLog).where(AuditLog.action.in_(["settings.update", "api_key.revoke"]))
        )
        recorded = list(rows)
    assert any(row.action == "settings.update" for row in recorded)
    assert any(
        row.action == "api_key.revoke" and row.target_name == "batch09-audit-key"
        for row in recorded
    )


def test_each_sync_settings_fallback_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed_read(_session: object) -> dict[str, object]:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(app_settings_service, "get_service_overrides_sync", failed_read)
    for section, getter in (
        ("ai", app_settings_service.get_ai_config_sync),
        ("email", app_settings_service.get_email_config_sync),
        ("runtime", app_settings_service.get_runtime_config_sync),
    ):
        counter = settings_read_failures_total.labels(section=section)
        before = counter._value.get()
        getter(object())  # type: ignore[arg-type]
        assert counter._value.get() == before + 1
