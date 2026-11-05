"""The data migration that decodes JSON-text values a replay stored in contexts."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "0d3e4cd8a66d_variable_values_decode_json_text.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_variable_values_json_text", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(rows: list[dict[str, object]]) -> dict[uuid.UUID, tuple[list[object], int]]:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "variable_values",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_column", sa.String(255)),
        sa.Column("observed_count", sa.Integer()),
        sa.Column("values", sa.JSON()),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert(), rows)
        with Operations.context(MigrationContext.configure(conn)):
            _migration().upgrade()
        return {
            row.id: (row.values, row.observed_count) for row in conn.execute(sa.select(table)).all()
        }


def test_a_json_context_keeps_one_rendering_of_each_string() -> None:
    row_id = uuid.uuid4()
    stored = _run(
        [
            {
                "id": row_id,
                "source_column": "properties.referrer",
                "observed_count": 10,
                "values": ["deeplink", "direct", "search", '"search"', '"direct"', '"push"'],
            }
        ]
    )

    # ``push`` was only ever seen quoted: decoded, not dropped.
    assert stored[row_id] == (["deeplink", "direct", "search", "push"], 8)


def test_a_regular_column_and_a_clean_json_context_are_left_alone() -> None:
    regular = uuid.uuid4()
    clean = uuid.uuid4()
    stored = _run(
        [
            {
                "id": regular,
                "source_column": "title",
                "observed_count": 2,
                "values": ['"quoted"', "quoted"],
            },
            {
                "id": clean,
                "source_column": "properties.plan",
                "observed_count": 40,
                "values": ["free", "pro", "42", "true"],
            },
        ]
    )

    assert stored[regular] == (['"quoted"', "quoted"], 2)
    assert stored[clean] == (["free", "pro", "42", "true"], 40)


def test_observed_count_never_drops_below_the_values_kept() -> None:
    row_id = uuid.uuid4()
    stored = _run(
        [
            {
                "id": row_id,
                "source_column": "properties.referrer",
                "observed_count": 2,
                "values": ["search", '"search"', '"push"', '"home_feed"'],
            }
        ]
    )

    assert stored[row_id] == (["search", "push", "home_feed"], 3)
