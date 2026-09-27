"""Event health loader and routes (F15, #268) against the DB fixtures.

* contract: a violation lowers only the matching expectation; snoozed and
  expired drifts are ignored;
* signals: an open, significant event-scope signal counts until it gets a verdict;
* the catalog sort "least healthy first", its paging, and the 400 on a branch;
* the routes: empty project, 422 on the id bounds, 404 off the scored
  population, membership (non-member 404, viewer allowed).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from tripl.models.data_source import DataSource
from tripl.models.event import Event, EventStatus
from tripl.models.event_metric import EventMetric
from tripl.models.field_definition import FieldDefinition
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.scan_config import ScanConfig
from tripl.models.schema_drift import SchemaDrift
from tripl.models.signal_triage import SignalTriage
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal


async def _post(client: AsyncClient, url: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post(url, json=body)
    assert resp.status_code in (200, 201), resp.text
    data = resp.json()
    assert isinstance(data, dict)
    return data


async def _project(client: AsyncClient, slug: str) -> tuple[uuid.UUID, str, uuid.UUID]:
    project = await _post(client, "/api/v1/projects", {"name": slug, "slug": slug})
    base = f"/api/v1/projects/{slug}"
    event_type = await _post(client, f"{base}/event-types", {"name": "track", "display_name": "T"})
    return uuid.UUID(project["id"]), base, uuid.UUID(event_type["id"])


async def _event(
    client: AsyncClient,
    base: str,
    event_type_id: uuid.UUID,
    name: str,
    *,
    status: EventStatus,
    **values: Any,
) -> uuid.UUID:
    created = await _post(
        client, f"{base}/events", {"event_type_id": str(event_type_id), "name": name}
    )
    event_id = uuid.UUID(created["id"])
    async with TestSessionLocal() as session:
        await session.execute(
            update(Event).where(Event.id == event_id).values(status=status.value, **values)
        )
        await session.commit()
    return event_id


async def _scan_config(
    project_id: uuid.UUID, event_type_id: uuid.UUID, *, detection: bool = False
) -> uuid.UUID:
    async with TestSessionLocal() as session:
        data_source = DataSource(
            name=f"wh-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=9000,
            database_name="db",
            username="u",
            password_encrypted="",
        )
        session.add(data_source)
        await session.flush()
        config = ScanConfig(
            project_id=project_id,
            data_source_id=data_source.id,
            event_type_id=event_type_id,
            name="checkout events",
            base_query="SELECT time, event_name FROM events",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
            anomaly_detection_enabled=detection,
        )
        session.add(config)
        await session.commit()
        return config.id


def _component(health: dict[str, Any], key: str) -> dict[str, Any]:
    return next(c for c in health["components"] if c["key"] == key)


@pytest.mark.asyncio
async def test_empty_project(client: AsyncClient) -> None:
    await _post(client, "/api/v1/projects", {"name": "hs-empty", "slug": "hs-empty"})
    base = "/api/v1/projects/hs-empty"

    project = await client.get(f"{base}/health")
    assert project.status_code == 200, project.text
    body = project.json()
    assert body["score"] is None
    assert body["grade"] is None
    assert body["scored_events"] == 0
    assert body["worst"] == []
    assert body["trend"] == []
    assert body["previous_score"] is None
    assert [c["key"] for c in body["component_averages"]] == [
        "implemented_seen",
        "contract",
        "drifts",
        "signals",
        "freshness",
        "documentation",
    ]
    assert all(c["value"] is None and c["applies_count"] == 0 for c in body["component_averages"])

    types = await client.get(f"{base}/health/event-types")
    assert types.status_code == 200, types.text
    assert types.json() == {"items": []}

    events = await client.get(f"{base}/health/events", params={"ids": str(uuid.uuid4())})
    assert events.status_code == 200, events.text
    assert events.json()["items"] == []


@pytest.mark.asyncio
async def test_ids_bounds_are_validated(client: AsyncClient) -> None:
    await _post(client, "/api/v1/projects", {"name": "hs-bounds", "slug": "hs-bounds"})
    base = "/api/v1/projects/hs-bounds"
    none = await client.get(f"{base}/health/events")
    assert none.status_code == 422
    too_many = await client.get(
        f"{base}/health/events", params=[("ids", str(uuid.uuid4())) for _ in range(151)]
    )
    assert too_many.status_code == 422
    bad_trend = await client.get(f"{base}/health", params={"trend_days": 0})
    assert bad_trend.status_code == 422


@pytest.mark.asyncio
async def test_event_health_404_off_the_scored_population(client: AsyncClient) -> None:
    _project_id, base, event_type_id = await _project(client, "hs-404")
    archived = await _event(client, base, event_type_id, "gone", status=EventStatus.archived)
    missing = await client.get(f"{base}/events/{uuid.uuid4()}/health")
    assert missing.status_code == 404
    gone = await client.get(f"{base}/events/{archived}/health")
    assert gone.status_code == 404
    batch = await client.get(f"{base}/health/events", params={"ids": str(archived)})
    assert batch.json()["items"] == []


@pytest.mark.asyncio
async def test_contract_violation_counts_only_active_matching_drifts(client: AsyncClient) -> None:
    project_id, base, event_type_id = await _project(client, "hs-contract")
    event_id = await _event(
        client,
        base,
        event_type_id,
        "checkout_completed",
        status=EventStatus.live,
        last_seen_at=datetime.now(UTC),
        description="Fires on the receipt screen",
    )
    # Contract applies only to an event a scan covers.
    await _scan_config(project_id, event_type_id)
    now = datetime.now(UTC)
    async with TestSessionLocal() as session:
        session.add_all(
            [
                # amount: required + range -> 2 expectations.
                FieldDefinition(
                    event_type_id=event_type_id,
                    name="amount",
                    display_name="Amount",
                    field_type="number",
                    is_required=True,
                    contract_min_value=0.0,
                ),
                # plan: enum with options -> 1 expectation.
                FieldDefinition(
                    event_type_id=event_type_id,
                    name="plan",
                    display_name="Plan",
                    field_type="enum",
                    enum_options=["free", "pro"],
                ),
                # A plain optional string: no expectation.
                FieldDefinition(
                    event_type_id=event_type_id,
                    name="note",
                    display_name="Note",
                    field_type="string",
                ),
            ]
        )
        session.add_all(
            [
                SchemaDrift(
                    event_type_id=event_type_id,
                    field_name="amount",
                    drift_type="range_violation",
                    status="open",
                    detected_at=now,
                ),
                # Snoozed into the future: not active.
                SchemaDrift(
                    event_type_id=event_type_id,
                    field_name="plan",
                    drift_type="enum_violation",
                    status="snoozed",
                    snoozed_until=now + timedelta(days=3),
                    detected_at=now,
                ),
                # Outside the retention window: expired.
                SchemaDrift(
                    event_type_id=event_type_id,
                    field_name="amount",
                    drift_type="required_null_violation",
                    status="open",
                    detected_at=now - timedelta(days=45),
                ),
            ]
        )
        await session.commit()

    resp = await client.get(f"{base}/events/{event_id}/health")
    assert resp.status_code == 200, resp.text
    health = resp.json()
    contract = _component(health, "contract")
    assert contract["applies"] is True
    assert contract["counts"] == {"violated": 1, "total": 3}
    assert contract["detail"] == "1 of 3 contract rules failing: amount (range)"
    assert contract["value"] == pytest.approx(2 / 3, abs=1e-3)
    # The covering scan makes drifts apply; it has no detection and no schedule,
    # so signals and freshness stay excluded.
    assert set(health["excluded"]) == {"signals", "freshness"}
    assert health["renormalized"] is True
    assert project_id  # the event belongs to the seeded project


@pytest.mark.asyncio
async def test_verdicted_signal_is_not_counted(client: AsyncClient) -> None:
    project_id, base, event_type_id = await _project(client, "hs-signals")
    event_id = await _event(
        client,
        base,
        event_type_id,
        "checkout_completed",
        status=EventStatus.live,
        last_seen_at=datetime.now(UTC),
    )
    config_id = await _scan_config(project_id, event_type_id, detection=True)
    bucket = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    async with TestSessionLocal() as session:
        session.add(
            EventMetric(scan_config_id=config_id, event_id=event_id, bucket=bucket, count=100)
        )
        session.add(
            MetricAnomaly(
                scan_config_id=config_id,
                scope_type="event",
                scope_ref=str(event_id),
                event_id=event_id,
                event_type_id=event_type_id,
                bucket=bucket,
                actual_count=100,
                expected_count=20,
                stddev=5,
                z_score=16,
                direction="spike",
            )
        )
        await session.commit()

    before = (await client.get(f"{base}/events/{event_id}/health")).json()
    signals = _component(before, "signals")
    assert signals["applies"] is True
    assert signals["counts"] == {"open": 1}
    assert signals["detail"] == "1 signal needs a verdict"

    async with TestSessionLocal() as session:
        session.add(
            SignalTriage(
                project_id=project_id,
                scan_config_id=config_id,
                scope_type="event",
                scope_ref=str(event_id),
                action="false_positive",
                bucket=bucket,
            )
        )
        await session.commit()

    after = (await client.get(f"{base}/events/{event_id}/health")).json()
    assert _component(after, "signals")["counts"] == {"open": 0}
    assert _component(after, "signals")["value"] == 1.0
    assert after["score"] > before["score"]


@pytest.mark.asyncio
async def test_health_sort_orders_least_healthy_first(client: AsyncClient) -> None:
    _project_id, base, event_type_id = await _project(client, "hs-sort")
    # 0: live, never seen, no description, no owner.
    await _event(client, base, event_type_id, "a_dead", status=EventStatus.live)
    # 50: draft, documentation only, description but no owner.
    await _event(
        client, base, event_type_id, "b_draft", status=EventStatus.draft, description="Planned"
    )
    # 81: live and seen (25 of 40) plus half the documentation (7.5 of 15).
    await _event(
        client,
        base,
        event_type_id,
        "c_live",
        status=EventStatus.live,
        description="Live",
        last_seen_at=datetime.now(UTC),
    )

    everything = await client.get(f"{base}/events", params={"order_by": "health"})
    assert everything.status_code == 200, everything.text
    assert [item["name"] for item in everything.json()["items"]] == ["a_dead", "b_draft", "c_live"]

    page = await client.get(
        f"{base}/events", params={"order_by": "health", "offset": 1, "limit": 1}
    )
    assert page.status_code == 200, page.text
    assert [item["name"] for item in page.json()["items"]] == ["b_draft"]
    assert page.json()["total"] == 3

    ids = [item["id"] for item in everything.json()["items"]]
    scores = await client.get(f"{base}/health/events", params=[("ids", i) for i in ids])
    assert [item["score"] for item in scores.json()["items"]] == [0, 50, 81]

    types = (await client.get(f"{base}/health/event-types")).json()["items"]
    assert len(types) == 1
    assert types[0]["event_type_id"] == str(event_type_id)
    assert types[0]["scored_events"] == 3
    assert types[0]["score"] == round((0 + 50 + 81) / 3)
    assert [brief["name"] for brief in types[0]["worst"]] == ["a_dead", "b_draft", "c_live"]

    project = (await client.get(f"{base}/health")).json()
    assert project["scored_events"] == 3
    assert project["unhealthy_count"] == 1
    assert project["warning_count"] == 1
    assert project["healthy_count"] == 1
    assert project["worst"][0]["name"] == "a_dead"


@pytest.mark.asyncio
async def test_health_sort_is_rejected_on_a_branch(client: AsyncClient) -> None:
    _project_id, base, event_type_id = await _project(client, "hs-branch")
    await _event(client, base, event_type_id, "only", status=EventStatus.live)
    branch = await _post(client, f"{base}/branches", {"name": "feature"})
    resp = await client.get(f"{base}/events", params={"order_by": "health", "branch": branch["id"]})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Health sort is available on the main plan"
    # The other orders still work on the branch.
    ok = await client.get(f"{base}/events", params={"order_by": "catalog", "branch": branch["id"]})
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_non_member_gets_404_and_viewer_can_read(client: AsyncClient) -> None:
    _project_id, base, event_type_id = await _project(client, "hs-private")
    event_id = await _event(client, base, event_type_id, "private", status=EventStatus.live)
    await client.post("/api/v1/auth/logout")
    registered = await client.post(
        "/api/v1/auth/register",
        json={"email": "health-stranger@example.com", "password": "Password123!", "name": "S"},
    )
    assert registered.status_code == 201, registered.text
    urls = [
        f"{base}/health",
        f"{base}/health/event-types",
        f"{base}/health/events?ids={event_id}",
        f"{base}/events/{event_id}/health",
    ]
    for url in urls:
        resp = await client.get(url)
        assert resp.status_code == 404, (url, resp.text)
        assert resp.json()["detail"] == "Project not found"

    await add_member_by_slug("hs-private", "health-stranger@example.com", "viewer")
    for url in urls:
        resp = await client.get(url)
        assert resp.status_code == 200, (url, resp.text)
