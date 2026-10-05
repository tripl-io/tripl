"""The extension points (``tripl.extensions``): dispatch, defaults, and how the app answers.

A server with no extension must behave as Community: every hook a no-op. An
installed extension is reached only through these hooks, so each dispatch is
pinned here with a fake extension, and the bundled enterprise extension is
checked to be the one loaded by default.
"""

import uuid
from collections.abc import Mapping
from typing import Any

import pytest
from httpx import AsyncClient
from starlette.responses import PlainTextResponse, Response

from tripl import extensions
from tripl.extensions import ErrorKind, Extension, GateRefused, override_extensions
from tripl.models import Base


class _Recorder(Extension):
    """Records every hook call; refuses sessions when told to."""

    name = "recorder"

    def __init__(self, counts: Mapping[str, int] | None = None, *, refuse: bool = False) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.counts = dict(counts or {})
        self.refuse = refuse

    async def org_session_gate(self, request, session, user, org, *, role=None) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(("org_session_gate", org.id))
        if self.refuse:
            raise GateRefused("Sign in elsewhere", extra={"sign_in": "/elsewhere"})

    async def on_member_removed(self, session, org_id, user_id, *, by_admin) -> Mapping[str, int]:  # type: ignore[no-untyped-def]
        self.calls.append(("on_member_removed", org_id, user_id, by_admin))
        return self.counts

    async def on_owner_demoted(self, session, org_id, user_id) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(("on_owner_demoted", org_id, user_id))

    async def on_membership_restored(self, session, org_id, user_id) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(("on_membership_restored", org_id, user_id))

    async def on_org_deleting(self, session, org_id) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(("on_org_deleting", org_id))

    async def on_group_change(self, session, org_id, group_id, *, added=(), removed=()) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(("on_group_change", org_id, group_id, tuple(added), tuple(removed)))

    async def on_audit_recorded(self, session, entry, org_id) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(("on_audit_recorded", org_id))


class _OwnsZzz(Extension):
    """Answers errors under ``/api/v1/zzz`` in a format of its own."""

    name = "owns-zzz"

    def error_response(
        self,
        path: str,
        kind: ErrorKind,
        status_code: int,
        detail: str,
        headers: Mapping[str, str] | None = None,
    ) -> Response | None:
        if not path.startswith("/api/v1/zzz"):
            return None
        return PlainTextResponse(f"{kind}:{status_code}", status_code=status_code)


def test_bundled_enterprise_extension_is_loaded_by_default() -> None:
    assert [e.name for e in extensions.extensions()] == ["bundled-enterprise"]


def test_bundled_models_are_in_the_metadata() -> None:
    assert {"org_audit_webhooks", "audit_webhook_outbox"} <= set(Base.metadata.tables)


async def test_no_extension_makes_every_hook_a_no_op() -> None:
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    with override_extensions([]):
        assert await extensions.on_member_removed(None, org_id, user_id, by_admin=True) == {}  # type: ignore[arg-type]
        await extensions.on_owner_demoted(None, org_id, user_id)  # type: ignore[arg-type]
        await extensions.on_membership_restored(None, org_id, user_id)  # type: ignore[arg-type]
        await extensions.on_org_deleting(None, org_id)  # type: ignore[arg-type]
        await extensions.on_group_change(None, org_id, uuid.uuid4(), added=[user_id])  # type: ignore[arg-type]
        assert extensions.error_response("/api/v1/x", "http", 404, "Not Found") is None


async def test_lifecycle_hooks_reach_every_extension_in_order() -> None:
    first, second = _Recorder({"a": 1, "shared": 1}), _Recorder({"b": 2, "shared": 5})
    org_id, user_id, group_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with override_extensions([first, second]):
        counts = await extensions.on_member_removed(None, org_id, user_id, by_admin=False)  # type: ignore[arg-type]
        await extensions.on_owner_demoted(None, org_id, user_id)  # type: ignore[arg-type]
        await extensions.on_membership_restored(None, org_id, user_id)  # type: ignore[arg-type]
        await extensions.on_org_deleting(None, org_id)  # type: ignore[arg-type]
        await extensions.on_group_change(None, org_id, group_id, removed=[user_id])  # type: ignore[arg-type]
    # Counts merge; a later extension's key wins.
    assert counts == {"a": 1, "b": 2, "shared": 5}
    expected = [
        ("on_member_removed", org_id, user_id, False),
        ("on_owner_demoted", org_id, user_id),
        ("on_membership_restored", org_id, user_id),
        ("on_org_deleting", org_id),
        ("on_group_change", org_id, group_id, (), (user_id,)),
    ]
    assert first.calls == expected
    assert second.calls == expected


async def test_a_refusing_gate_is_answered_with_its_extra_fields(client: AsyncClient) -> None:
    gate = _Recorder(refuse=True)
    with override_extensions([gate]):
        resp = await client.get("/api/v1/projects")
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Sign in elsewhere", "sign_in": "/elsewhere"}
    assert gate.calls[0][0] == "org_session_gate"


async def test_a_passing_gate_lets_the_request_through(client: AsyncClient) -> None:
    gate = _Recorder()
    with override_extensions([gate]):
        resp = await client.get("/api/v1/projects")
    assert resp.status_code == 200
    assert gate.calls


async def test_audit_rows_reach_the_audit_hook(client: AsyncClient) -> None:
    recorder = _Recorder()
    with override_extensions([recorder]):
        resp = await client.post("/api/v1/projects", json={"name": "Hooked", "slug": "hooked"})
    assert resp.status_code == 201, resp.text
    assert any(call[0] == "on_audit_recorded" for call in recorder.calls)


async def test_an_extension_owns_the_error_format_of_its_paths(client: AsyncClient) -> None:
    with override_extensions([_OwnsZzz()]):
        owned = await client.get("/api/v1/zzz/nothing-here")
        other = await client.get("/api/v1/nothing-here")
    assert owned.status_code == 404
    assert owned.text == "http:404"
    assert other.status_code == 404
    assert other.json() == {"detail": "Not Found"}


def test_an_entry_point_that_is_not_an_extension_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Point:
        name = "bogus"

        def load(self) -> object:
            return object()

    monkeypatch.setattr(extensions, "_BUNDLED", ())
    monkeypatch.setattr(extensions, "entry_points", lambda group: [_Point()])
    with pytest.raises(TypeError, match="not a tripl.extensions.Extension"):
        extensions._load()
