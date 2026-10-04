"""Concurrency guard for branch search reindexing."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from tripl.services import search_service


async def test_reindex_locks_branch_before_building_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    branch_id = uuid.uuid4()
    calls: list[tuple[str, object]] = []

    class _Session:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, statement: object, params: dict[str, int]) -> None:
            calls.append((str(statement), params["key"]))

    async def fake_build(*_args: object) -> None:
        calls.append(("build", None))
        raise RuntimeError("stop after build starts")

    monkeypatch.setattr(search_service, "_build_documents", fake_build)

    with pytest.raises(RuntimeError, match="stop after build starts"):
        await search_service._reindex_branch_documents(
            _Session(),  # type: ignore[arg-type]
            project_id=uuid.uuid4(),
            branch_id=branch_id,
            slug="project",
        )

    assert calls == [
        ("SELECT pg_advisory_xact_lock(:key)", int.from_bytes(branch_id.bytes[:8], signed=True)),
        ("build", None),
    ]
