"""The ``per_distinct_user`` series preview never quotes a failure to connect.

Every other preview runner builds its adapter through
``metric_preview_service._connect``, which marks a failure there as a failure
to connect and words it by kind. The distinct-user denominator is the
collector's own query (``metric_collect._collect_distinct_user_series``), which
built and probed its adapter itself, so a driver's text naming the warehouse
host and port reached an editor whenever no statement hint matched it. The
preview now hands the collector ``_connect_and_probe``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

import tripl.core.adapters.registry as adapter_registry
from tripl.core.adapters.base import ColumnInfo
from tripl.models.domain_enums import ScanInterval
from tripl.models.event_metric import EventMetric
from tripl.models.scan_config import ScanConfig
from tripl.tests.conftest import TestSessionLocal

# psycopg's wording for a database that does not exist: it names the host and
# the port, and matches none of the hints a statement failure is read with.
_DRIVER_TEXT = 'connection to server at "10.0.0.5", port 5432 failed: FATAL: database "dw" missing'


class _Adapter:
    def __init__(
        self,
        *,
        probe_error: Exception | None = None,
        query_error: Exception | None = None,
    ) -> None:
        self.probe_error = probe_error
        self.query_error = query_error
        self.closed = False
        self.queried = False

    def test_connection(self) -> bool:
        if self.probe_error is not None:
            raise self.probe_error
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return [ColumnInfo(name=name, type_name="String") for name in ("ts", "user_id")]

    def get_time_bucketed_aggregate(self, *args: object, **kwargs: object) -> tuple:
        self.queried = True
        if self.query_error is not None:
            raise self.query_error
        hour = datetime(2026, 9, 1, 9, tzinfo=UTC)
        return ["bucket", "value"], [], [(hour, 2)]

    def close(self) -> None:
        self.closed = True


@pytest.fixture
async def stand(client: AsyncClient) -> dict[str, str]:
    """A project, a warehouse bound to it, an event and one hour of its counts."""
    project = await client.post(
        "/api/v1/projects", json={"name": "Distinct Mask", "slug": "distinct-mask"}
    )
    assert project.status_code == 201, project.text
    slug = project.json()["slug"]
    source = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Masked CH",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "test_db",
        },
    )
    assert source.status_code == 201, source.text
    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "pv", "display_name": "Page View"}
    )
    assert event_type.status_code == 201, event_type.text
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type.json()["id"], "name": "signup"},
    )
    assert event.status_code == 201, event.text
    async with TestSessionLocal() as session:
        grid = ScanConfig(
            project_id=uuid.UUID(project.json()["id"]),
            data_source_id=uuid.UUID(source.json()["id"]),
            name="distinct-grid",
            base_query="SELECT ts, user_id FROM events",
            time_column="ts",
            interval=ScanInterval.h1,
        )
        session.add(grid)
        await session.flush()
        session.add(
            EventMetric(
                scan_config_id=grid.id,
                event_id=uuid.UUID(event.json()["id"]),
                event_type_id=None,
                bucket=datetime(2026, 9, 1, 9, tzinfo=UTC),
                count=4,
            )
        )
        await session.commit()
    return {"slug": slug, "event_id": event.json()["id"]}


async def _preview(client: AsyncClient, stand: dict[str, str]) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{stand['slug']}/metrics/series-preview",
        json={
            "kind": "event_composition",
            "composition": "per_distinct_user",
            "numerator_event_id": stand["event_id"],
            "user_id_column": "user_id",
        },
    )
    assert resp.status_code == 200, resp.text
    body: dict = resp.json()
    return body


def _build_with(monkeypatch: pytest.MonkeyPatch, outcome: _Adapter | Exception) -> None:
    def _build(_ds: object) -> _Adapter:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(adapter_registry, "build_adapter", _build)


async def test_a_failure_while_building_the_adapter_is_not_quoted(
    client: AsyncClient, stand: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_with(monkeypatch, RuntimeError(_DRIVER_TEXT))

    error = (await _preview(client, stand))["error"]

    assert error
    assert "10.0.0.5" not in error
    assert "5432" not in error


async def test_a_failed_connection_probe_is_not_quoted_and_closes_the_adapter(
    client: AsyncClient, stand: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _Adapter(probe_error=RuntimeError(_DRIVER_TEXT))
    _build_with(monkeypatch, adapter)

    error = (await _preview(client, stand))["error"]

    assert error
    assert "10.0.0.5" not in error
    assert adapter.closed is True
    assert adapter.queried is False


async def test_the_engines_verdict_on_the_query_is_still_shown(
    client: AsyncClient, stand: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _Adapter(query_error=RuntimeError("Unrecognized name: author_id at [1:8]"))
    _build_with(monkeypatch, adapter)

    error = (await _preview(client, stand))["error"]

    assert error == "Unrecognized name: author_id at [1:8]"
    assert adapter.closed is True


async def test_a_working_warehouse_still_previews(
    client: AsyncClient, stand: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _Adapter()
    _build_with(monkeypatch, adapter)

    body = await _preview(client, stand)

    assert body["error"] is None
    assert [point["value"] for point in body["points"]] == [2.0]
    assert adapter.closed is True
