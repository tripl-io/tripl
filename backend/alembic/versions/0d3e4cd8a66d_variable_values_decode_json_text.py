"""variable values: one rendering of a JSON string

A replay sampled a JSON-path variable two ways and rendered the same cell
differently. The JSON sampler decoded it (``search``); the per-row walk kept the
adapter's JSON text (``"search"``: Postgres casts the extract with ``::text``,
ClickHouse wraps it in ``toJSONString``). Every context a replay touched ended
up listing each string value twice, once with quotes. The walk now decodes the
value (``tripl.worker.tasks.metrics.generation._row_value_for_variable``), and
this migration repairs the rows already written.

Only contexts whose ``source_column`` is a JSON address (``column.path``) are
rewritten; a regular column's value never went through JSON text. In each, a
value that is a JSON string literal is replaced by the string it encodes, and
the list is de-duplicated in its stored order. ``observed_count`` loses the
duplicates it counted, never dropping below the number of values kept.

A JSON string whose own text is a quoted string (the warehouse holding
``"\\"x\\""``) is indistinguishable here from the bug's output and is decoded
too; no scan can render that value differently from this migration's result.

Downgrade is a no-op: the duplicates are not worth restoring.

Revision ID: 0d3e4cd8a66d
Revises: f2b4d6a8c0e3
Create Date: 2026-10-19 14:00:00.000000

"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0d3e4cd8a66d"
down_revision: str | None = "f2b4d6a8c0e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_variable_values = sa.table(
    "variable_values",
    sa.column("id", sa.Uuid()),
    sa.column("source_column", sa.String()),
    sa.column("observed_count", sa.Integer()),
    sa.column("values", sa.JSON()),
)


def _decoded(values: list[object]) -> list[object]:
    """``values`` with each JSON string literal decoded, de-duplicated in order."""
    out: list[object] = []
    seen: set[object] = set()
    for value in values:
        if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                decoded = value
            if isinstance(decoded, str):
                value = decoded
        key = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def upgrade() -> None:
    bind = op.get_bind()
    # A quoted value is stored as ``"\"search\""``, so the serialised list holds
    # a backslash before a quote; rows without one have nothing to repair.
    candidates = bind.execute(
        sa.select(
            _variable_values.c.id,
            _variable_values.c.observed_count,
            _variable_values.c["values"],
        ).where(
            _variable_values.c.source_column.like("%.%"),
            sa.cast(_variable_values.c["values"], sa.Text).like('%\\"%', escape="!"),
        )
    ).all()
    for row_id, observed_count, values in candidates:
        if not isinstance(values, list):
            continue
        repaired = _decoded(values)
        if repaired == values:
            continue
        removed = len(values) - len(repaired)
        bind.execute(
            sa.update(_variable_values)
            .where(_variable_values.c.id == row_id)
            .values(
                {
                    "values": repaired,
                    "observed_count": max(len(repaired), (observed_count or 0) - removed),
                }
            )
        )


def downgrade() -> None:
    pass
