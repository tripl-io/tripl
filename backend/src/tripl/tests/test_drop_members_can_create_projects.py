"""``organizations.members_can_create_projects`` is gone (migration f7b9d1e3a5c7).

Nothing ever read the column, so it suggested a project-creation setting that
did not exist. The suite has no PostgreSQL, so the revision runs against a
stand-in ``op`` that records the calls.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from tripl.models.organization import Organization, default_organization_values

_COLUMN = "members_can_create_projects"


def _revision_module() -> ModuleType:
    backend_root = Path(__file__).resolve().parents[3]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    script = ScriptDirectory.from_config(config).get_revision("f7b9d1e3a5c7")
    assert script is not None
    module: ModuleType = script.module
    return module


class _Op:
    def __init__(self) -> None:
        self.dropped: list[tuple[str, str]] = []
        self.added: list[tuple[str, Any]] = []

    def drop_column(self, table: str, column: str) -> None:
        self.dropped.append((table, column))

    def add_column(self, table: str, column: Any) -> None:
        self.added.append((table, column))


def test_the_model_and_the_default_org_row_no_longer_carry_the_column() -> None:
    assert _COLUMN not in Organization.__table__.c
    assert _COLUMN not in default_organization_values()


def test_upgrade_drops_the_column(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _revision_module()
    op = _Op()
    monkeypatch.setattr(module, "op", op)
    module.upgrade()
    assert op.dropped == [("organizations", _COLUMN)]
    assert op.added == []


def test_downgrade_restores_the_column_with_its_old_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _revision_module()
    op = _Op()
    monkeypatch.setattr(module, "op", op)
    module.downgrade()
    assert op.dropped == []
    [(table, column)] = op.added
    assert table == "organizations"
    assert column.name == _COLUMN
    assert isinstance(column.type, sa.Boolean)
    assert column.nullable is False
    assert column.server_default is not None
