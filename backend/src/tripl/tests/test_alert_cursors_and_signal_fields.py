"""Backend follow-ups for the frontend (#199, #205, #209).

- Keyset cursors on ``GET /alert-inbox`` and ``GET /alert-deliveries``.
- ``unit`` and ``detected_at`` on active signals.
- ``state`` on the demo cancel response.
- Every ``${var}`` token the demo plan seeds names a seeded variable.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from tripl.models.domain_enums import ProjectGenerationStatus
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.project import Project
from tripl.schemas.event_metric import MetricSignalResponse
from tripl.services._alerting_cursors import (
    decode_delivery_cursor,
    decode_inbox_cursor,
    encode_delivery_cursor,
    encode_inbox_cursor,
)
from tripl.services.demo.builders.plan import event_specs
from tripl.services.demo.builders.variables import _VARIABLE_SPECS
from tripl.services.metrics_insights_service import _attach_derived_fields
from tripl.services.metrics_service import _signal_from_anomaly
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_alerting import (
    _inbox_item,
    _seed_inbox_delivery,
    _seed_inbox_fixture,
    _seed_inbox_groups,
)

# ── cursors ─────────────────────────────────────────────────────────


def test_inbox_cursor_round_trips() -> None:
    key = (True, datetime(2026, 9, 25, 10, 0, tzinfo=UTC), str(uuid.uuid4()))
    assert decode_inbox_cursor(encode_inbox_cursor(key)) == key


def test_delivery_cursor_round_trips() -> None:
    created = datetime(2026, 9, 25, 10, 0, 0, 123456, tzinfo=UTC)
    delivery_id = uuid.uuid4()
    assert decode_delivery_cursor(encode_delivery_cursor(created, delivery_id)) == (
        created,
        delivery_id,
    )


@pytest.mark.parametrize("cursor", ["", "not-base64!!", "aW5ib3gxfDF8eHx5", "Zm9v"])
def test_malformed_cursor_is_a_422(cursor: str) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        decode_inbox_cursor(cursor)
    assert caught.value.status_code == 422


def test_cursor_of_one_list_is_refused_by_the_other() -> None:
    from fastapi import HTTPException

    delivery_cursor = encode_delivery_cursor(datetime.now(UTC), uuid.uuid4())
    with pytest.raises(HTTPException):
        decode_inbox_cursor(delivery_cursor)


@pytest.mark.asyncio
async def test_inbox_cursor_serves_a_row_that_sorted_down_past_the_seam(
    client: AsyncClient,
) -> None:
    """Acknowledging a page-1 incident drops it below every open one. With an
    offset, page 2 would then start one row late and never serve C; the cursor
    continues strictly after B, so C is served."""
    slug = "r3-inbox-cursor"
    a, b, c = await _seed_inbox_groups(client, slug, "R3 inbox cursor", count=3)
    base = f"/api/v1/projects/{slug}/alert-inbox"

    first = (await client.get(base, params={"limit": 2})).json()
    assert [item["correlation_group_id"] for item in first["items"]] == [str(a), str(b)]
    assert first["total"] == 3
    assert isinstance(first["next_cursor"], str)

    acted = await client.post(f"{base}/{a}/actions", json={"action": "acknowledge"})
    assert acted.status_code == 200, acted.text

    second = (await client.get(base, params={"limit": 2, "cursor": first["next_cursor"]})).json()
    served = [item["correlation_group_id"] for item in second["items"]]
    assert str(c) in served
    assert served[0] == str(c)
    assert second["next_cursor"] is None


@pytest.mark.asyncio
async def test_inbox_last_page_has_no_cursor_and_offset_still_works(
    client: AsyncClient,
) -> None:
    slug = "r3-inbox-offset"
    groups = await _seed_inbox_groups(client, slug, "R3 inbox offset", count=2)
    base = f"/api/v1/projects/{slug}/alert-inbox"
    page = (await client.get(base, params={"limit": 1, "offset": 1})).json()
    assert [item["correlation_group_id"] for item in page["items"]] == [str(groups[1])]
    assert page["next_cursor"] is None


@pytest.mark.asyncio
async def test_cursor_with_offset_is_a_422(client: AsyncClient) -> None:
    slug = "r3-inbox-both"
    await _seed_inbox_groups(client, slug, "R3 inbox both", count=2)
    first = (await client.get(f"/api/v1/projects/{slug}/alert-inbox", params={"limit": 1})).json()
    for path in ("alert-inbox", "alert-deliveries"):
        resp = await client.get(
            f"/api/v1/projects/{slug}/{path}",
            params={"cursor": first["next_cursor"], "offset": 1},
        )
        assert resp.status_code == 422, path


@pytest.mark.asyncio
async def test_inbox_bad_cursor_is_a_422(client: AsyncClient) -> None:
    slug = "r3-inbox-bad"
    await _seed_inbox_groups(client, slug, "R3 inbox bad", count=1)
    resp = await client.get(f"/api/v1/projects/{slug}/alert-inbox", params={"cursor": "junk"})
    assert resp.status_code == 422
    # A NUL is refused by the decoder too, never bound into a query (the
    # exemption test_text_filters.py records for `cursor`).
    for path in ("alert-inbox", "alert-deliveries"):
        laced = await client.get(f"/api/v1/projects/{slug}/{path}", params={"cursor": "a\x00b"})
        assert laced.status_code == 422, (path, laced.text)


@pytest.mark.asyncio
async def test_delivery_cursor_walks_every_row_once(client: AsyncClient) -> None:
    """Deliveries sharing one created_at are the case offset paging repeated and
    dropped rows on; the keyset walk must still serve each exactly once."""
    project = await client.post(
        "/api/v1/projects",
        json={"name": "R3 deliveries", "slug": "r3-deliveries", "description": ""},
    )
    project_id = uuid.UUID(project.json()["id"])
    scan_config_id, rule_ids, destination_id = await _seed_inbox_fixture(project_id)
    tied = datetime.now(UTC) - timedelta(hours=1)
    seeded = set()
    for index in range(5):
        delivery_id = await _seed_inbox_delivery(
            project_id,
            scan_config_id=scan_config_id,
            destination_id=destination_id,
            rule_id=rule_ids[0],
            # Three rows tie on created_at, two do not.
            created_at=tied if index < 3 else tied - timedelta(minutes=index),
            items=[
                _inbox_item(
                    scope_type="event",
                    bucket=tied,
                    percent_delta=100.0,
                    correlation_group_id=uuid.uuid4(),
                )
            ],
        )
        seeded.add(str(delivery_id))

    base = "/api/v1/projects/r3-deliveries/alert-deliveries"
    served: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        params: dict[str, str | int] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        page = (await client.get(base, params=params)).json()
        assert page["total"] == 5
        served.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(served) == 5
    assert set(served) == seeded


# ── unit and detected_at on signals ────────────────────────


@pytest.mark.asyncio
async def test_metric_signal_carries_its_unit_and_other_scopes_do_not(
    client: AsyncClient,
) -> None:
    project = await client.post(
        "/api/v1/projects",
        json={"name": "R3 unit", "slug": "r3-unit", "description": ""},
    )
    project_id = uuid.UUID(project.json()["id"])
    async with TestSessionLocal() as session:
        metric = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project_id,
            name="conversion_rate",
            display_name="Conversion Rate",
            kind="sql",
            config={},
            interval="1h",
            status="active",
            unit="%",
        )
        session.add(metric)
        await session.commit()

        def signal(scope_type: str, scope_ref: str) -> MetricSignalResponse:
            return MetricSignalResponse(
                scope_type=scope_type,
                scope_ref=scope_ref,
                state="latest",
                bucket=datetime.now(UTC),
                actual_count=4.0,
                expected_count=2.0,
                stddev=0.5,
                z_score=4.0,
                direction="spike",
            )

        attached = await _attach_derived_fields(
            session,
            [
                signal("metric", str(metric.id)),
                signal("project_total", "total"),
                signal("metric", "not-a-uuid"),
            ],
        )
    assert [item.unit for item in attached] == ["%", None, None]


def test_signal_reports_when_the_anomaly_was_detected() -> None:
    detected = datetime(2026, 9, 25, 12, 5, tzinfo=UTC)
    anomaly = MetricAnomaly(
        scan_config_id=uuid.uuid4(),
        scope_type="project_total",
        scope_ref="total",
        bucket=datetime(2026, 9, 25, 11, 0, tzinfo=UTC),
        actual_count=40.0,
        expected_count=10.0,
        stddev=2.0,
        z_score=15.0,
        direction="spike",
        created_at=detected,
    )
    assert _signal_from_anomaly(anomaly, state="latest").detected_at == detected


# ── cancel state ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_after_a_demo_just_finished_reports_finished(client: AsyncClient) -> None:
    me = (await client.get("/api/v1/auth/me")).json()
    async with TestSessionLocal() as session:
        session.add(
            Project(
                name="Demo Project",
                slug="r3-demo-finished",
                is_demo=True,
                generation_status=ProjectGenerationStatus.ready.value,
                created_by_user_id=uuid.UUID(me["id"]),
            )
        )
        await session.commit()

    resp = await client.post("/api/v1/projects/demo/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": False, "slug": "r3-demo-finished", "state": "finished"}


@pytest.mark.asyncio
async def test_cancel_with_only_an_old_demo_reports_none(client: AsyncClient) -> None:
    me = (await client.get("/api/v1/auth/me")).json()
    long_ago = datetime.now(UTC) - timedelta(days=2)
    async with TestSessionLocal() as session:
        session.add(
            Project(
                name="Demo Project",
                slug="r3-demo-old",
                is_demo=True,
                generation_status=ProjectGenerationStatus.ready.value,
                created_by_user_id=uuid.UUID(me["id"]),
                created_at=long_ago,
                updated_at=long_ago,
            )
        )
        await session.commit()

    resp = await client.post("/api/v1/projects/demo/cancel")
    assert resp.json() == {"cancelled": False, "slug": None, "state": "none"}


# ── demo template tokens ───────────────────────────────────────────

_TOKEN = re.compile(r"\$\{([^}]+)\}")


def test_every_demo_template_token_names_a_seeded_variable() -> None:
    seeded = {name for name, *_rest in _VARIABLE_SPECS}
    used = {
        token
        for spec in event_specs(datetime.now(UTC))
        for _field, value in spec.field_values
        for token in _TOKEN.findall(value)
    }
    assert used, "the demo plan should template at least one field value"
    assert used <= seeded, f"unseeded template variables: {sorted(used - seeded)}"
