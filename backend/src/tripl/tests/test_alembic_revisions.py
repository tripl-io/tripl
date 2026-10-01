"""Invariants for the single baseline Alembic revision and its model contract."""

import re
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

_BASELINE_REVISION = "a1c3e5f7b9d2"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _script() -> ScriptDirectory:
    backend_root = _backend_root()
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    return ScriptDirectory.from_config(config)


def test_alembic_has_exactly_one_baseline_revision() -> None:
    script = _script()
    assert script.get_heads() == [_BASELINE_REVISION]
    assert script.get_bases() == [_BASELINE_REVISION]
    assert script.get_revision(_BASELINE_REVISION).down_revision is None
    assert len(list(script.walk_revisions())) == 1


def test_reported_head_matches_the_migration_graph() -> None:
    from tripl.services import migration_status_service

    migration_status_service.head_revision.cache_clear()
    try:
        assert migration_status_service.head_revision() == _script().get_heads()[0]
    finally:
        migration_status_service.head_revision.cache_clear()


def test_native_enum_model_columns_use_the_same_types_in_baseline_sql() -> None:
    import tripl.models  # noqa: F401  (populates Base.metadata)
    from tripl.models.base import Base

    sql = (_backend_root() / "alembic" / "baseline_schema.sql").read_text()
    native_enums = {
        (table.name, column.name): column.type.name
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, sa.Enum) and column.type.native_enum
    }
    assert native_enums

    tables = {
        name: match.group(1)
        for name in {table for table, _column in native_enums}
        if (match := re.search(rf"(?ms)^CREATE TABLE public\.{name} \(\n(.*?)^\);", sql))
    }
    assert set(tables) == {table for table, _column in native_enums}
    mismatches = [
        f"{table}.{column}: expected public.{enum}"
        for (table, column), enum in native_enums.items()
        if re.search(
            rf'(?m)^    "?{re.escape(column)}"? public\.{re.escape(enum)}\b', tables[table]
        )
        is None
    ]
    assert mismatches == []


def test_not_null_timestamp_columns_have_server_defaults() -> None:
    import tripl.models  # noqa: F401  (populates Base.metadata)
    from tripl.models.base import Base

    for table in Base.metadata.tables.values():
        for column in table.columns:
            if column.name in ("created_at", "updated_at") and column.nullable is False:
                assert column.server_default is not None, (
                    f"{table.name}.{column.name} is NOT NULL but has no server_default"
                )
