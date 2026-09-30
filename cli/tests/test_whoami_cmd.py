"""``tripl whoami`` end to end, through ``main([...])`` against a fake instance (F20 PR6)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tripl_cli.cli import main

from .conftest import FakeInstance

_ME: dict[str, Any] = {
    "id": "uid-1",
    "email": "agent@example.com",
    "name": "Agent",
    "role": "admin",
    "is_platform_admin": False,
    "orgs": [
        {"slug": "acme", "name": "Acme", "role": "admin"},
        {"slug": "default", "name": "Default organization", "role": "member"},
    ],
    "org": "acme",
    "api_key_scope": "write",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


def test_whoami_prints_the_user_the_key_scope_and_the_org(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.auth(payload=_ME)
    assert main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert "user:  Agent <agent@example.com>" in out
    assert "key:   write, reaches the whole organization" in out
    assert "org:   acme (role: admin)" in out
    # One read, no writes.
    assert [call.request.url.path for call in tripl_api.router.calls] == ["/api/v1/auth/me"]


def test_whoami_json_is_one_document(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.auth(payload=_ME)
    assert main(["whoami", "--json"]) == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["command"] == "whoami"
    assert document["reach"] == "instance"
    assert document["access"] == "write"
    assert document["user"] == {"id": "uid-1", "email": "agent@example.com", "name": "Agent"}
    assert (document["org"], document["role"]) == ("acme", "admin")
    assert document["orgs"] == ["acme", "default"]
    assert document["instance"]["api_key_scope"] == "instance"
    # The human lines went to stderr.
    assert "org:   acme" in captured.err


def test_an_older_instance_without_the_new_keys_falls_back_to_the_key_prefix(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default fake ``/auth/me`` predates ``org`` and ``api_key_scope``."""
    assert main(["whoami", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    # conftest's key is ``tk_r_...``.
    assert document["access"] == "read"
    assert document["org"] is None


def test_a_project_bound_key_is_reported_not_failed(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.auth(status=403, payload={"detail": "API key is scoped to a single project"})
    assert main(["whoami", "--json"]) == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert (document["reach"], document["user"], document["org"]) == ("project", None, None)
    assert "bound to one project" in captured.err


def test_a_rejected_key_exits_one(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.auth(status=401, payload={"detail": "Invalid or expired API key"})
    assert main(["whoami"]) == 1
    assert "401" in capsys.readouterr().err
