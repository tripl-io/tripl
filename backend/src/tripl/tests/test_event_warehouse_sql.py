"""GET /events/{id}/warehouse-sql: the event's rows, from the scan's own query.

The pure builder is pinned per dialect on in-memory rows; the route is pinned
end to end for the cases that depend on lookup (no scan, a branch copy).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from tripl.core.adapters.measure_validator import SqlDialect
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.scan_config import ScanConfig
from tripl.services.event_warehouse_sql_service import NO_SCAN_REASON, build_event_warehouse_sql
from tripl.tests.conftest import TestSessionLocal

NOW = datetime(2026, 10, 15, 12, 30, 45, tzinfo=UTC)


def _config(**overrides: object) -> ScanConfig:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "name": "Events",
        "base_query": "SELECT * FROM analytics.events;",
        "event_type_id": None,
        "event_type_column": "event",
        "time_column": "ts",
        "event_name_format": "{category}:{action}",
        "event_group_rules": [],
    }
    values.update(overrides)
    return ScanConfig(**values)


def _event(fields: dict[str, str], *, types: dict[str, str] | None = None, **kw: object) -> Event:
    event_type = EventType(id=uuid.uuid4(), name="se", display_name="se")
    event = Event(
        id=uuid.uuid4(),
        event_type_id=event_type.id,
        name=str(kw.get("name", "x")),
        source_name=kw.get("source_name", "x"),
    )
    event.event_type = event_type
    event.field_values = [
        EventFieldValue(
            value=value,
            field_definition=FieldDefinition(
                name=name,
                display_name=name,
                field_type=(types or {}).get(name, "string"),
            ),
        )
        for name, value in fields.items()
    ]
    return event


def test_postgres_filters_on_type_and_name_format_columns_only() -> None:
    event = _event({"category": "weather", "action": "show", "label": "widget"})

    out = build_event_warehouse_sql(_config(), event, dialect=SqlDialect.postgres, now=NOW)

    assert out.exact is True
    assert out.notes == []
    assert out.sql == (
        "SELECT *\n"
        "FROM (\nSELECT * FROM analytics.events\n) AS scan\n"
        "WHERE \"event\" = 'se'\n"
        "  AND \"category\" = 'weather'\n"
        "  AND \"action\" = 'show'\n"
        "  AND \"ts\" >= TIMESTAMPTZ '2026-10-14 12:30:00.000000+00:00'\n"
        "LIMIT 100"
    )
    # label is not in the name format: rows of one event may differ there.
    assert "label" not in out.sql


def test_clickhouse_quotes_with_backticks_and_pins_utc() -> None:
    event = _event({"category": "o'brien", "action": "show"})

    out = build_event_warehouse_sql(_config(), event, dialect=SqlDialect.clickhouse, now=NOW)

    assert out.sql is not None
    assert "`event` = 'se'" in out.sql
    assert "`category` = 'o\\'brien'" in out.sql or "`category` = 'o''brien'" in out.sql
    assert "parseDateTime64BestEffort('2026-10-14 12:30:00" in out.sql


def test_bigquery_casts_a_numeric_looking_value_and_says_what_it_assumed() -> None:
    event = _event({"category": "42", "action": "show"})

    out = build_event_warehouse_sql(_config(), event, dialect=SqlDialect.bigquery, now=NOW)

    assert out.sql is not None
    assert "CAST(`category` AS STRING) = '42'" in out.sql
    assert "TIMESTAMP '2026-10-14 12:30:00" in out.sql
    assert out.exact is True
    assert any("TIMESTAMP column" in note for note in out.notes)


def test_a_variable_token_is_left_out_and_still_exact() -> None:
    event = _event({"category": "${screen}", "action": "show"})

    out = build_event_warehouse_sql(_config(), event, dialect=SqlDialect.postgres, now=NOW)

    assert out.exact is True
    assert out.sql is not None
    assert '"category"' not in out.sql


def test_text_mixed_with_a_token_is_left_out_and_named() -> None:
    event = _event({"category": "page_${id}", "action": "show"})

    out = build_event_warehouse_sql(_config(), event, dialect=SqlDialect.postgres, now=NOW)

    assert out.exact is False
    assert out.sql is not None
    assert '"category" =' not in out.sql
    assert any("category" in note for note in out.notes)
    assert out.sql.startswith("-- ")


def test_a_group_rule_event_filters_on_its_type_only() -> None:
    rule = {
        "name": "Any show",
        "conditions": [{"field": "action", "pattern": "^show"}],
        "condition_logic": "all",
    }
    event = _event(
        {"category": "weather", "action": "/^show/"}, name="Any show", source_name="Any show"
    )

    out = build_event_warehouse_sql(
        _config(event_group_rules=[rule]), event, dialect=SqlDialect.postgres, now=NOW
    )

    assert out.exact is False
    assert out.sql is not None
    assert "\"event\" = 'se'" in out.sql
    assert '"category" =' not in out.sql
    assert '"action" =' not in out.sql
    assert any("action ~ /^show/" in note for note in out.notes)


def test_without_a_name_format_every_field_identifies_the_event() -> None:
    event = _event(
        {"category": "weather", "count": "3", "flag": "True", "empty": ""},
        types={"count": "number", "flag": "boolean"},
    )

    out = build_event_warehouse_sql(
        _config(event_name_format=None), event, dialect=SqlDialect.postgres, now=NOW
    )

    assert out.sql is not None
    assert "\"category\" = 'weather'" in out.sql
    assert '"count" = 3' in out.sql
    assert '"flag" = TRUE' in out.sql
    assert '("empty" IS NULL OR "empty" = \'\')' in out.sql


def test_a_single_type_scan_needs_no_type_filter_and_no_time_column_no_window() -> None:
    event = _event({"action": "show"})

    out = build_event_warehouse_sql(
        _config(event_type_id=uuid.uuid4(), event_name_format="{action}", time_column=None),
        event,
        dialect=SqlDialect.postgres,
        now=NOW,
    )

    assert out.sql is not None
    assert '"event"' not in out.sql
    assert "TIMESTAMPTZ" not in out.sql
    assert "\"action\" = 'show'" in out.sql


# --- the route ---------------------------------------------------------------


async def _project_with_event(client: AsyncClient, slug: str) -> tuple[uuid.UUID, str, str]:
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "se", "display_name": "SE"}
    )
    assert et.status_code == 201, et.text
    et_id = et.json()["id"]
    field = await client.post(
        f"/api/v1/projects/{slug}/event-types/{et_id}/fields",
        json={"name": "action", "display_name": "action", "field_type": "string"},
    )
    assert field.status_code == 201, field.text
    ev = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": et_id,
            "name": "show",
            "field_values": [{"field_definition_id": field.json()["id"], "value": "show"}],
        },
    )
    assert ev.status_code == 201, ev.text
    return project_id, et_id, ev.json()["id"]


async def _bind_scan(project_id: uuid.UUID, event_type_id: str) -> None:
    async with TestSessionLocal() as session:
        source = DataSource(
            id=uuid.uuid4(),
            name=f"DS {uuid.uuid4().hex[:8]}",
            db_type="postgres",
            host="localhost",
            port=5432,
            database_name="wh",
            username="reader",
            password_encrypted="",
        )
        session.add(source)
        session.add(
            ScanConfig(
                id=uuid.uuid4(),
                data_source_id=source.id,
                project_id=project_id,
                event_type_id=uuid.UUID(event_type_id),
                name="Web events",
                base_query="SELECT * FROM events",
                time_column="ts",
                event_name_format="{action}",
                cardinality_threshold=100,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_no_scan_answers_200_with_a_reason(client: AsyncClient) -> None:
    _, _, event_id = await _project_with_event(client, "wsql-none")

    resp = await client.get(f"/api/v1/projects/wsql-none/events/{event_id}/warehouse-sql")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sql"] is None
    assert body["reason"] == NO_SCAN_REASON


@pytest.mark.asyncio
async def test_a_bound_scan_and_its_branch_copy_answer_the_same_query(
    client: AsyncClient,
) -> None:
    project_id, et_id, event_id = await _project_with_event(client, "wsql-bound")
    await _bind_scan(project_id, et_id)

    main = await client.get(f"/api/v1/projects/wsql-bound/events/{event_id}/warehouse-sql")
    assert main.status_code == 200, main.text
    body = main.json()
    assert body["dialect"] == "postgres"
    assert body["scan_config_name"] == "Web events"
    assert body["exact"] is True
    assert "\"action\" = 'show'" in body["sql"]

    branch = await client.post("/api/v1/projects/wsql-bound/branches", json={"name": "b1"})
    assert branch.status_code == 201, branch.text
    branch_id = branch.json()["id"]
    events = await client.get(f"/api/v1/projects/wsql-bound/events?branch={branch_id}")
    assert events.status_code == 200, events.text
    copy_id = next(item["id"] for item in events.json()["items"] if item["name"] == "show")
    assert copy_id != event_id

    copied = await client.get(
        f"/api/v1/projects/wsql-bound/events/{copy_id}/warehouse-sql?branch={branch_id}"
    )
    assert copied.status_code == 200, copied.text
    assert copied.json()["scan_config_name"] == "Web events"
    assert "\"action\" = 'show'" in copied.json()["sql"]
