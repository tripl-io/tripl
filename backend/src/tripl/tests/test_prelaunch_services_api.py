"""Pre-launch clean-up of the services and API lane, second pass.

* A request number the database cannot bind is a 422, not a 500: the shared
  ``Offset`` on every paged list, ``INT32`` bounds on body integers stored in an
  ``INTEGER`` column, and the duplicates cursor's strict parse.
* The notification bell's cursor is a tagged cursor like the alert lists'.
* The ``/properties`` routes read as properties in the API reference.
* The binary routes say so in the OpenAPI document.
* Every slug lookup answers a missing project with the one documented 404.
* The shared helpers the copies were folded into: ``chunked``, ``enum_text``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import extensions
from tripl.main import app
from tripl.models.domain_enums import MetricKind, enum_text
from tripl.schemas.alerting import AlertRuleCreate, AlertRuleUpdate
from tripl.schemas.event_type import EventTypeCreate, EventTypeUpdate
from tripl.schemas.fact_table import FactTableUpdate
from tripl.schemas.field_definition import FieldDefinitionCreate, FieldDefinitionUpdate
from tripl.schemas.integers import INT32_MAX
from tripl.schemas.meta_field import MetaFieldCreate, MetaFieldUpdate
from tripl.schemas.metric_definition import MetricDefinitionUpdate, SqlMetricCreate
from tripl.schemas.pagination import MAX_OFFSET
from tripl.schemas.project_anomaly_settings import ProjectAnomalySettingsUpdate
from tripl.schemas.scan_config import (
    ScanConfigCreate,
    ScanConfigPreviewRequest,
    ScanConfigUpdate,
    ScanDryRunRequest,
)
from tripl.services import (
    metric_series_service,
    metrics_service,
    plan_revision_service,
    schema_drift_service,
)
from tripl.services._alerting_cursors import (
    decode_notification_cursor,
    encode_delivery_cursor,
    encode_inbox_cursor,
    encode_notification_cursor,
)
from tripl.services._id_chunks import chunked
from tripl.services.project_lookup import PROJECT_NOT_FOUND
from tripl.tests.conftest import TestSessionLocal


async def _create_project(client: AsyncClient, slug: str) -> None:
    resp = await client.post(
        "/api/v1/projects", json={"name": slug.upper(), "slug": slug, "description": ""}
    )
    assert resp.status_code == 201, resp.text


def _error_locs(body: dict[str, Any]) -> list[list[Any]]:
    return [list(error["loc"]) for error in body["detail"]]


# ── Numbers the database cannot bind ─────────────────────────────────────────


async def test_an_offset_past_the_cap_is_a_422_and_the_cap_itself_pages(
    client: AsyncClient,
) -> None:
    await _create_project(client, "bounds")
    # Twenty digits: past a bigint, which asyncpg refused in the driver (a 500).
    too_far = await client.get("/api/v1/projects/bounds/events", params={"offset": str(10**19)})
    assert too_far.status_code == 422, too_far.text
    assert ["query", "offset"] in _error_locs(too_far.json())

    at_cap = await client.get("/api/v1/projects/bounds/events", params={"offset": MAX_OFFSET})
    assert at_cap.status_code == 200, at_cap.text
    assert at_cap.json()["items"] == []


def test_every_paged_list_caps_its_offset() -> None:
    if extensions.extensions():
        pytest.skip("an extension adds routes of its own")
    spec = app.openapi()
    offsets = [
        (f"{method.upper()} {path}", param["schema"])
        for path, item in spec["paths"].items()
        for method, operation in item.items()
        for param in operation.get("parameters", [])
        if param["name"] == "offset" and param["in"] == "query"
    ]
    assert offsets
    assert [route for route, schema in offsets if schema.get("maximum") != MAX_OFFSET] == []


async def test_a_body_order_past_an_integer_column_is_a_422(client: AsyncClient) -> None:
    await _create_project(client, "bounds-body")
    url = "/api/v1/projects/bounds-body/event-types"
    too_big = await client.post(
        url, json={"name": "too_big", "display_name": "Too big", "order": INT32_MAX + 1}
    )
    assert too_big.status_code == 422, too_big.text
    assert ["body", "order"] in _error_locs(too_big.json())

    largest = await client.post(
        url, json={"name": "largest", "display_name": "Largest", "order": INT32_MAX}
    )
    assert largest.status_code == 201, largest.text
    assert largest.json()["order"] == INT32_MAX


def _integer_maximum(model: type[BaseModel], field: str) -> Any:
    schema = model.model_json_schema()["properties"][field]
    branches = schema.get("anyOf", [schema])
    (integer,) = [branch for branch in branches if branch.get("type") == "integer"]
    return integer.get("maximum")


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (EventTypeCreate, "order"),
        (EventTypeUpdate, "order"),
        (FieldDefinitionCreate, "order"),
        (FieldDefinitionUpdate, "order"),
        (MetaFieldCreate, "order"),
        (MetaFieldUpdate, "order"),
        (SqlMetricCreate, "order"),
        (SqlMetricCreate, "breakdown_values_limit"),
        (MetricDefinitionUpdate, "order"),
        (MetricDefinitionUpdate, "breakdown_values_limit"),
        (FactTableUpdate, "order"),
        (AlertRuleCreate, "cooldown_minutes"),
        (AlertRuleUpdate, "cooldown_minutes"),
        (ProjectAnomalySettingsUpdate, "baseline_window_buckets"),
        (ProjectAnomalySettingsUpdate, "min_history_buckets"),
        (ProjectAnomalySettingsUpdate, "min_expected_count"),
        (ScanConfigCreate, "scan_lookback_hours"),
        (ScanConfigCreate, "cardinality_threshold"),
        (ScanConfigCreate, "scan_row_limit"),
        (ScanConfigCreate, "metrics_row_limit"),
        (ScanConfigCreate, "metric_breakdown_values_limit"),
        (ScanConfigUpdate, "scan_lookback_hours"),
        (ScanConfigUpdate, "cardinality_threshold"),
        (ScanConfigUpdate, "scan_row_limit"),
        (ScanConfigUpdate, "metrics_row_limit"),
        (ScanConfigUpdate, "metric_breakdown_values_limit"),
        (ScanConfigPreviewRequest, "scan_lookback_hours"),
        (ScanDryRunRequest, "scan_lookback_hours"),
        (ScanDryRunRequest, "cardinality_threshold"),
    ],
)
def test_body_integers_stored_in_an_integer_column_are_bounded(
    model: type[BaseModel], field: str
) -> None:
    assert _integer_maximum(model, field) == INT32_MAX


@pytest.mark.parametrize("cursor", ["²", "1" * 10, "-1", " 1", "１"])
async def test_a_duplicates_cursor_that_is_not_an_offset_is_a_422(
    client: AsyncClient, cursor: str
) -> None:
    await _create_project(client, "dup-cursor")
    resp = await client.get("/api/v1/projects/dup-cursor/duplicates", params={"cursor": cursor})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "Invalid cursor"


async def test_a_duplicates_cursor_offset_still_pages(client: AsyncClient) -> None:
    await _create_project(client, "dup-offset")
    resp = await client.get("/api/v1/projects/dup-offset/duplicates", params={"cursor": "0"})
    assert resp.status_code == 200, resp.text


# ── The notification bell's cursor ───────────────────────────────────────────


def test_a_notification_cursor_round_trips_in_utc() -> None:
    notification_id = uuid.uuid4()
    naive = datetime(2026, 10, 9, 12, 30)
    at, ident = decode_notification_cursor(encode_notification_cursor(naive, notification_id))
    assert (at, ident) == (naive.replace(tzinfo=UTC), notification_id)
    assert at.tzinfo is not None


# A fixed id: parametrize values are collected once per xdist worker, and a
# random one would make each worker see different test ids.
_FIXED_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


@pytest.mark.parametrize(
    "foreign",
    [
        encode_delivery_cursor(datetime(2026, 10, 9, tzinfo=UTC), _FIXED_ID),
        encode_inbox_cursor((True, datetime(2026, 10, 9, tzinfo=UTC), str(_FIXED_ID))),
        "not-a-cursor",
    ],
    ids=["delivery", "inbox", "garbage"],
)
def test_another_lists_cursor_is_not_a_notification_cursor(foreign: str) -> None:
    with pytest.raises(HTTPException) as exc:
        decode_notification_cursor(foreign)
    assert exc.value.status_code == 422


async def test_the_bell_refuses_an_inbox_cursor(client: AsyncClient) -> None:
    await _create_project(client, "bell-cursor")
    inbox = encode_inbox_cursor((True, datetime(2026, 10, 9, tzinfo=UTC), str(uuid.uuid4())))
    resp = await client.get("/api/v1/me/notifications", params={"cursor": inbox})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "Invalid cursor"


# ── The API reference ────────────────────────────────────────────────────────


def test_property_routes_read_as_properties_in_the_reference() -> None:
    spec = app.openapi()
    current = [
        operation
        for path, item in spec["paths"].items()
        if path.startswith("/api/v1/projects/{slug}/properties")
        for operation in item.values()
    ]
    legacy = [
        operation
        for path, item in spec["paths"].items()
        if path.startswith("/api/v1/projects/{slug}/variables")
        for operation in item.values()
    ]
    assert current
    assert len(current) == len(legacy)
    for operation in current:
        assert "ariable" not in operation["summary"], operation["summary"]
        assert "  " not in operation["summary"], operation["summary"]
        assert "variable" not in operation["operationId"].split("_api_v1_")[0]
        assert not operation.get("deprecated")
    summaries = {operation["summary"] for operation in current}
    assert {"List Properties", "Create Property", "Clear Property Values"} <= summaries
    ids = [operation["operationId"] for operation in current + legacy]
    assert len(ids) == len(set(ids))
    # The deprecated twins keep the old word.
    assert all(operation["deprecated"] for operation in legacy)
    assert spec["paths"]["/api/v1/projects/{slug}/variables"]["get"]["summary"] == "List Variables"


def test_binary_routes_describe_bytes_not_json() -> None:
    spec = app.openapi()
    binary = {"type": "string", "format": "binary"}
    photo = spec["paths"]["/api/v1/projects/{slug}/events/{event_id}/photos/{photo_id}/file"]
    responses = photo["get"]["responses"]
    assert set(responses["200"]["content"]) == {"image/*", "application/octet-stream"}
    assert all(body["schema"] == binary for body in responses["200"]["content"].values())
    assert "204" in responses

    export = spec["paths"]["/api/v1/projects/{slug}/docs/export"]["get"]["responses"]["200"]
    assert export["content"]["application/zip"]["schema"] == binary
    assert "application/json" in export["content"]


# ── One 404 for a missing project ────────────────────────────────────────────

_MISSING = "no-such-project"
_LOOKUPS: list[Callable[[AsyncSession], Awaitable[object]]] = [
    lambda session: metrics_service.get_event_metrics(session, _MISSING, uuid.uuid4()),
    lambda session: metric_series_service.get_metric_series(session, _MISSING, uuid.uuid4()),
    lambda session: plan_revision_service.list_revisions(session, _MISSING),
    lambda session: schema_drift_service.list_drifts_for_event_type(
        session, _MISSING, uuid.uuid4()
    ),
]


@pytest.mark.parametrize("lookup", _LOOKUPS)
async def test_a_missing_project_gets_the_documented_404(
    lookup: Callable[[AsyncSession], Awaitable[object]],
) -> None:
    async with TestSessionLocal() as session:
        with pytest.raises(HTTPException) as exc:
            await lookup(session)
    assert exc.value.status_code == 404
    assert exc.value.detail == PROJECT_NOT_FOUND


# ── Shared helpers ───────────────────────────────────────────────────────────


def test_chunked_slices_at_the_size_it_is_given() -> None:
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(chunked(list(range(2500)))) == [
        list(range(0, 1000)),
        list(range(1000, 2000)),
        list(range(2000, 2500)),
    ]
    assert list(chunked([])) == []


def test_enum_text_reads_a_member_and_its_string_alike() -> None:
    assert enum_text(MetricKind.event_composition) == "event_composition"
    assert enum_text("event_composition") == "event_composition"
