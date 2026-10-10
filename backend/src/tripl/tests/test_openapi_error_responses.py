"""The contract documents the refusals every protected route can answer.

``api.v1.router`` mounts each protected router through ``_include_protected``,
which declares 401 and 403 with the shared ``ErrorResponse`` body, and the 404
of a missing project (or a non-member) on routers under ``/projects/{slug}``.
Before that the document listed nothing but 422s, so a generated client typed
no error at all.
"""

from __future__ import annotations

from typing import Any

from tripl.main import app

_ERROR_REF = "#/components/schemas/ErrorResponse"


def _responses(spec: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    responses: dict[str, Any] = spec["paths"][path][method]["responses"]
    return responses


def _documents_error(responses: dict[str, Any], code: str) -> bool:
    body = responses.get(code, {}).get("content", {}).get("application/json", {})
    return bool(body.get("schema", {}).get("$ref") == _ERROR_REF)


def test_the_error_body_is_a_named_component() -> None:
    schema = app.openapi()["components"]["schemas"]["ErrorResponse"]
    assert "detail" in schema["properties"]
    assert schema["required"] == ["detail"]


def test_project_routes_document_sign_in_permission_and_the_project_404() -> None:
    spec = app.openapi()
    for path, method in (
        ("/api/v1/projects/{slug}/events", "get"),
        ("/api/v1/projects/{slug}/events/{event_id}", "patch"),
        ("/api/v1/projects/{slug}", "get"),
        ("/api/v1/activity/projects/{slug}", "get"),
    ):
        responses = _responses(spec, path, method)
        for code in ("401", "403", "404"):
            assert _documents_error(responses, code), (method, path, code)


def test_account_and_organization_routes_document_sign_in_and_permission_only() -> None:
    spec = app.openapi()
    for path, method in (
        ("/api/v1/me/api-keys", "get"),
        ("/api/v1/me/notifications", "get"),
        ("/api/v1/orgs", "get"),
        ("/api/v1/platform/settings", "patch"),
    ):
        responses = _responses(spec, path, method)
        assert _documents_error(responses, "401"), (method, path)
        assert _documents_error(responses, "403"), (method, path)
        # The 404 is the project gate's; these routes have no project to miss.
        assert "404" not in responses, (method, path)


def test_sign_in_routes_document_no_session_refusal() -> None:
    spec = app.openapi()
    for path in ("/api/v1/auth/login", "/api/v1/auth/register"):
        responses = _responses(spec, path, "post")
        assert not _documents_error(responses, "401"), path
        assert not _documents_error(responses, "403"), path


def test_a_routes_own_response_wins_over_the_shared_ones() -> None:
    # chart_annotations declares its own 200 next to the 201; the shared
    # refusals are added beside it, not instead of it.
    responses = _responses(app.openapi(), "/api/v1/projects/{slug}/annotations", "post")
    assert {"200", "201", "401", "403", "404"} <= set(responses)
