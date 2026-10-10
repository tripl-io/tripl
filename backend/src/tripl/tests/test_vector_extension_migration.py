"""Migration d3f5a7c9e1b2 updates pgvector only when it can and needs to.

PostgreSQL checks that the caller owns the extension before it compares
versions, so an unconditional ``ALTER EXTENSION vector UPDATE`` failed every
install whose extensions an administrator created (a managed PostgreSQL, the
Helm chart's documented alternative), even with nothing to update. The suite
has no PostgreSQL, so the revision runs against a stand-in connection.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory


def _revision_module() -> ModuleType:
    backend_root = Path(__file__).resolve().parents[3]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    script = ScriptDirectory.from_config(config).get_revision("d3f5a7c9e1b2")
    assert script is not None
    module: ModuleType = script.module
    return module


class _Op:
    """Answers the version query with ``row`` and records what else runs."""

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self.row = row
        self.executed: list[str] = []

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(execute=lambda _statement: SimpleNamespace(first=lambda: self.row))

    def execute(self, statement: str) -> None:
        self.executed.append(statement)


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str, *args: object) -> None:
        self.warnings.append(message % args)


def _upgrade(
    monkeypatch: pytest.MonkeyPatch, row: tuple[Any, ...] | None, logger: _Logger | None = None
) -> _Op:
    module = _revision_module()
    op = _Op(row)
    monkeypatch.setattr(module, "op", op)
    monkeypatch.setattr(module, "logger", logger or _Logger())
    module.upgrade()
    return op


def test_an_owner_behind_the_shipped_version_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _upgrade(monkeypatch, ("0.8.0", "0.8.7", True)).executed == [
        "ALTER EXTENSION vector UPDATE"
    ]


def test_a_current_extension_is_left_alone_even_by_a_non_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _upgrade(monkeypatch, ("0.8.7", "0.8.7", False)).executed == []


def test_no_extension_is_nothing_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _upgrade(monkeypatch, None).executed == []


def test_a_non_owner_behind_the_shipped_version_warns_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = _Logger()

    op = _upgrade(monkeypatch, ("0.8.0", "0.8.7", False), logger)

    assert op.executed == []
    [message] = logger.warnings
    assert "0.8.0" in message
    assert "0.8.7" in message
    assert "ALTER EXTENSION vector UPDATE" in message


def test_the_warning_goes_to_alembics_console_logger() -> None:
    # alembic.ini routes "alembic" to the console; a server notice would be lost.
    assert _revision_module().logger.name == "alembic.runtime.migration"
