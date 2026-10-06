"""The values a scan saw for one event field, when its breakdown rows disagreed.

One structured event fires on several screens; the scan collapses its rows onto
one event, which keeps the busiest row's ``page``. ``EventFieldObservation``
keeps the rest — with counts — so the event page can say "Seen with 2 values:
map/main (62%), spot/main (38%)" instead of letting the analyst read the one
stored value as "fires only on map/main".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers._event_field_observations import (
    OBSERVED_VALUE_LIMIT,
    OBSERVED_VALUE_MAX_LENGTH,
    build_distribution,
    tally_value,
)
from tripl.core.analyzers.cardinality import BreakdownAnalysis, CardinalityResult
from tripl.core.analyzers.event_generator import GenerationResult, generate_events
from tripl.models import Base
from tripl.models.event import Event, EventStatus
from tripl.models.event_field_observation import EventFieldObservation
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.services.event_field_observation_service import observed_values_payload
from tripl.services.plan_revision_service import build_plan_snapshot
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.conftest import TestSessionLocal

# --- the pure half ---------------------------------------------------------


def test_tally_sums_counts_per_value() -> None:
    seen: dict = {}
    tally_value(seen, ("tap", "screen"), "map/main", 40)
    tally_value(seen, ("tap", "screen"), "map/main", 22)
    tally_value(seen, ("tap", "screen"), "spot/main", 38)
    assert seen == {("tap", "screen"): {"map/main": 62, "spot/main": 38}}


def test_tally_an_unknown_count_poisons_that_value_only() -> None:
    seen: dict = {}
    tally_value(seen, ("tap", "screen"), "map/main", 40)
    tally_value(seen, ("tap", "screen"), "map/main", None)
    tally_value(seen, ("tap", "screen"), "spot/main", 38)
    assert seen[("tap", "screen")] == {"map/main": None, "spot/main": 38}


def test_distribution_is_busiest_first_with_unknown_counts_last() -> None:
    values, distinct, total = build_distribution({"b": 10, "a": 10, "c": 90, "z": None})
    assert [item["value"] for item in values] == ["c", "a", "b", "z"]
    assert distinct == 4
    # One unknown count makes the total unknown: shares off a partial sum lie.
    assert total is None


def test_distribution_caps_values_but_keeps_the_true_size() -> None:
    counts = {f"screen/{i:02d}": 100 - i for i in range(30)}
    values, distinct, total = build_distribution(counts)
    assert len(values) == OBSERVED_VALUE_LIMIT == 20
    assert values[0] == {"value": "screen/00", "count": 100}
    assert distinct == 30
    assert total == sum(counts.values())


def test_distribution_truncates_long_values() -> None:
    values, _, _ = build_distribution({"x" * 1000: 2, "y": 1})
    assert len(values[0]["value"]) == OBSERVED_VALUE_MAX_LENGTH


def test_payload_computes_shares_and_the_remainder_past_the_cap() -> None:
    row = EventFieldObservation(
        values=[{"value": "map/main", "count": 62}, {"value": "spot/main", "count": 30}],
        distinct_count=3,
        total_count=100,
        observed_at=datetime(2026, 10, 6, tzinfo=UTC),
    )
    payload = observed_values_payload(row)
    assert [v["share"] for v in payload["values"]] == [0.62, 0.30]
    assert payload["other_count"] == 8


def test_payload_without_counts_has_no_shares() -> None:
    row = EventFieldObservation(
        values=[{"value": "a", "count": None}, {"value": "b", "count": 4}],
        distinct_count=2,
        total_count=None,
        observed_at=datetime(2026, 10, 6, tzinfo=UTC),
    )
    payload = observed_values_payload(row)
    assert [v["share"] for v in payload["values"]] == [None, None]
    assert payload["other_count"] is None


# --- the generator ---------------------------------------------------------


@pytest.fixture
def sync_session():
    engine = create_engine("sqlite:///:memory:")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    session = sessionmaker(engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def plan(sync_session: Session):
    project = Project(id=uuid.uuid4(), name="Obs", slug="obs", description="")
    sync_session.add(project)
    sync_session.flush()
    et = EventType(
        id=uuid.uuid4(), project_id=project.id, name="se", display_name="SE", description=""
    )
    sync_session.add(et)
    sync_session.flush()
    fds = {
        name: FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=et.id,
            name=name,
            display_name=name.title(),
            field_type="string",
            order=order,
        )
        for order, name in enumerate(("screen", "action"))
    }
    sync_session.add_all(fds.values())
    sync_session.commit()
    return project, et, fds


def _analysis(rows: list[tuple]) -> BreakdownAnalysis:
    """``(screen, action[, count])`` rows; the trailing count is the adapter's ``_cnt``."""
    screens = sorted({row[0] for row in rows})
    actions = sorted({row[1] for row in rows})
    return BreakdownAnalysis(
        results={
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=len(screens),
                is_low=True,
                sample_values=screens,
            ),
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=len(actions),
                is_low=True,
                sample_values=actions,
            ),
        },
        rows=rows,
        reg_names=["screen", "action"],
        json_names=[],
    )


def _scan(session: Session, plan, rows: list[tuple], **kwargs) -> GenerationResult:
    project, et, fds = plan
    result = generate_events(
        session,
        project.id,
        et.id,
        _analysis(rows),
        fds,
        event_name_format="{action}",
        **kwargs,
    )
    session.commit()
    return result


def _observations(session: Session) -> list[EventFieldObservation]:
    session.expire_all()
    return list(session.execute(select(EventFieldObservation)).scalars())


def _stored_screen(session: Session, plan) -> str:
    _, _, fds = plan
    return session.execute(
        select(EventFieldValue.value).where(EventFieldValue.field_definition_id == fds["screen"].id)
    ).scalar_one()


def test_collapsing_rows_record_the_distribution(sync_session: Session, plan) -> None:
    result = _scan(sync_session, plan, [("map/main", "tap", 62), ("spot/main", "tap", 38)])

    rows = _observations(sync_session)
    assert len(rows) == 1, "only the varying field gets a row; action had one value"
    row = rows[0]
    assert row.field_definition_id == plan[2]["screen"].id
    assert row.values == [
        {"value": "map/main", "count": 62},
        {"value": "spot/main", "count": 38},
    ]
    assert row.distinct_count == 2
    assert row.total_count == 100
    assert row.observed_at is not None
    assert result.field_observations_written == 1
    # The stored value is unchanged: the busiest row still wins.
    assert _stored_screen(sync_session, plan) == "map/main"
    # And the run summary's line keeps its wording.
    assert (
        "1 field(s) saw more than one value across collapsing rows; "
        "the busiest row won: tap.screen (2 values)"
    ) in result.details


def test_rows_without_counts_store_unknown_counts(sync_session: Session, plan) -> None:
    _scan(sync_session, plan, [("map/main", "tap"), ("spot/main", "tap")])
    (row,) = _observations(sync_session)
    assert {item["count"] for item in row.values} == {None}
    assert row.total_count is None


def test_the_last_observing_run_replaces_the_distribution(sync_session: Session, plan) -> None:
    _scan(sync_session, plan, [("map/main", "tap", 62), ("spot/main", "tap", 38)])
    _scan(sync_session, plan, [("map/main", "tap", 5), ("list/main", "tap", 7)])

    (row,) = _observations(sync_session)
    # Replaced, never summed: windows overlap and a sum would double-count.
    assert row.values == [{"value": "list/main", "count": 7}, {"value": "map/main", "count": 5}]
    assert row.total_count == 12


def test_a_field_that_stopped_varying_loses_its_row(sync_session: Session, plan) -> None:
    _scan(sync_session, plan, [("map/main", "tap", 62), ("spot/main", "tap", 38)])
    _scan(sync_session, plan, [("map/main", "tap", 62)])
    assert _observations(sync_session) == []


def test_an_archived_event_keeps_its_row(sync_session: Session, plan) -> None:
    _scan(sync_session, plan, [("map/main", "tap", 62), ("spot/main", "tap", 38)])
    event = sync_session.execute(select(Event)).scalar_one()
    event.status = EventStatus.archived
    sync_session.commit()

    _scan(sync_session, plan, [("map/main", "tap", 62)])
    (row,) = _observations(sync_session)
    assert row.distinct_count == 2


def test_an_authored_field_is_still_observed(sync_session: Session, plan) -> None:
    _scan(sync_session, plan, [("map/main", "tap", 62)])
    fv = sync_session.execute(
        select(EventFieldValue).where(EventFieldValue.field_definition_id == plan[2]["screen"].id)
    ).scalar_one()
    fv.value = "hand/set"
    fv.is_authored = True
    sync_session.commit()

    _scan(sync_session, plan, [("map/main", "tap", 62), ("spot/main", "tap", 38)])
    (row,) = _observations(sync_session)
    assert row.distinct_count == 2
    assert _stored_screen(sync_session, plan) == "hand/set"


def test_a_run_told_not_to_record_leaves_rows_alone(sync_session: Session, plan) -> None:
    _scan(sync_session, plan, [("map/main", "tap", 62), ("spot/main", "tap", 38)])
    # A fallback-window tick that saw one value must not delete the full scan's row.
    _scan(sync_session, plan, [("map/main", "tap", 3)], record_observations=False)
    (row,) = _observations(sync_session)
    assert row.total_count == 100

    # Nor write one where there was none.
    _scan(sync_session, plan, [("map/main", "tap", 62)])
    _scan(
        sync_session,
        plan,
        [("map/main", "tap", 1), ("spot/main", "tap", 2)],
        record_observations=False,
    )
    assert _observations(sync_session) == []


def test_an_identity_cut_by_max_events_writes_no_row(sync_session: Session, plan) -> None:
    # Ascending within the identity, so the first row creates the event and the
    # second meets the cap. The event is new and its tally holds one value, so
    # the cut writes nothing — there is no complete row to protect yet.
    result = _scan(
        sync_session,
        plan,
        [("map/main", "tap", 62), ("spot/main", "tap", 38)],
        max_events=1,
    )
    assert "Reached max_events limit (1)" in result.details
    assert _observations(sync_session) == []


def test_an_event_past_the_max_events_cap_keeps_its_row(sync_session: Session, plan) -> None:
    _scan(sync_session, plan, [("map/main", "tap", 62), ("spot/main", "tap", 38)])

    # A new identity takes the one creation the cap allows; the existing
    # event's row comes after the break, so the scan never reached it.
    result = _scan(
        sync_session,
        plan,
        [("list/main", "swipe", 5), ("map/main", "tap", 1)],
        max_events=1,
    )
    assert "Reached max_events limit (1)" in result.details
    (row,) = _observations(sync_session)
    assert row.distinct_count == 2
    assert row.total_count == 100


def test_sync_catalog_forwards_the_record_flag(sync_session: Session, plan) -> None:
    """A tick on the collector's fallback window passes ``record_observations=False``."""
    from tripl.models.data_source import DataSource
    from tripl.models.scan_config import ScanConfig
    from tripl.worker.tasks.metrics.catalog_sync import sync_catalog

    project, et, _ = plan
    source = DataSource(
        id=uuid.uuid4(),
        name="wh",
        db_type="clickhouse",
        host="localhost",
        port=9000,
        database_name="db",
        username="u",
    )
    sync_session.add(source)
    sync_session.flush()
    config = ScanConfig(
        id=uuid.uuid4(),
        project_id=project.id,
        data_source_id=source.id,
        name="scan",
        base_query="SELECT 1",
        event_type_id=et.id,
    )
    sync_session.add(config)
    sync_session.commit()

    class _Adapter:
        def get_json_path_samples(self, *args: object, **kwargs: object):
            return {}

    seen: list[bool] = []

    def _fake_generate(*args: object, record_observations: bool = True, **kwargs: object):
        seen.append(record_observations)
        return GenerationResult()

    window = (datetime(2026, 10, 6, tzinfo=UTC), datetime(2026, 10, 6, 1, tzinfo=UTC))
    for flag in (True, False):
        sync_catalog(
            sync_session,
            adapter=_Adapter(),
            config=config,
            columns=[ColumnInfo("screen", "String"), ColumnInfo("action", "String")],
            skip_cols=set(),
            json_value_path_map={},
            scan_row_limit=1000,
            metrics_row_limit=1000,
            time_from_dt=window[0],
            time_to_dt=window[1],
            catalog_scan_window=window,
            is_replay=False,
            analyze_cardinality_fn=lambda *a, **k: _analysis([("map/main", "tap", 1)]),
            analyze_cardinality_grouped_fn=lambda *a, **k: ([], {}),
            generate_events_fn=_fake_generate,
            record_field_observations=flag,
        )
    assert seen == [True, False]


def test_the_collector_records_only_on_a_declared_window() -> None:
    """``collect_metrics`` passes ``catalog_window_declared``, not ``True``.

    Read from the source because the flag is computed mid-task, after the
    adapter connects; running the task to observe it would need a warehouse.
    """
    import inspect

    from tripl.worker.tasks.metrics import tasks

    assert "record_field_observations=catalog_window_declared" in inspect.getsource(tasks)


# --- the API ---------------------------------------------------------------


async def _seed_event(client: AsyncClient, slug: str) -> tuple[str, str, str]:
    await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "se", "display_name": "SE"}
    )
    et_id = et.json()["id"]
    field = await client.post(
        f"/api/v1/projects/{slug}/event-types/{et_id}/fields",
        json={"name": "page", "display_name": "Page", "field_type": "string"},
    )
    field_id = field.json()["id"]
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": et_id,
            "name": "windbar_tap",
            "field_values": [{"field_definition_id": field_id, "value": "map/main"}],
        },
    )
    assert event.status_code == 201
    return et_id, field_id, event.json()["id"]


async def _observe(event_id: str, field_id: str) -> None:
    async with TestSessionLocal() as session, session.begin():
        event = await session.get(Event, uuid.UUID(event_id))
        assert event is not None
        session.add(
            EventFieldObservation(
                project_id=event.project_id,
                event_id=event.id,
                field_definition_id=uuid.UUID(field_id),
                values=[{"value": "map/main", "count": 62}, {"value": "spot/main", "count": 38}],
                distinct_count=2,
                total_count=100,
                observed_at=datetime(2026, 10, 5, 9, tzinfo=UTC),
            )
        )


@pytest.mark.asyncio
async def test_event_detail_carries_the_observed_values(client: AsyncClient) -> None:
    _, field_id, event_id = await _seed_event(client, "obs-detail")
    await _observe(event_id, field_id)

    detail = await client.get(f"/api/v1/projects/obs-detail/events/{event_id}")
    assert detail.status_code == 200
    observed = detail.json()["field_values"][0]["observed_values"]
    assert observed["distinct_count"] == 2
    assert observed["total_count"] == 100
    assert observed["other_count"] == 0
    assert [(v["value"], v["share"]) for v in observed["values"]] == [
        ("map/main", 0.62),
        ("spot/main", 0.38),
    ]

    # Lists do not pay for it.
    listed = await client.get("/api/v1/projects/obs-detail/events")
    assert listed.json()["items"][0]["field_values"][0]["observed_values"] is None


@pytest.mark.asyncio
async def test_saving_the_form_keeps_the_observation(client: AsyncClient) -> None:
    """The form save deletes and re-inserts every field value; the row is elsewhere."""
    _, field_id, event_id = await _seed_event(client, "obs-save")
    await _observe(event_id, field_id)

    saved = await client.patch(
        f"/api/v1/projects/obs-save/events/{event_id}",
        json={"field_values": [{"field_definition_id": field_id, "value": "spot/main"}]},
    )
    assert saved.status_code == 200
    observed = saved.json()["field_values"][0]["observed_values"]
    assert observed is not None and observed["distinct_count"] == 2

    again = await client.get(f"/api/v1/projects/obs-save/events/{event_id}")
    assert again.json()["field_values"][0]["observed_values"] == observed


@pytest.mark.asyncio
async def test_a_branch_copy_reads_its_main_twins_observation(client: AsyncClient) -> None:
    _, field_id, event_id = await _seed_event(client, "obs-branch")
    branch = await client.post("/api/v1/projects/obs-branch/branches", json={"name": "feat"})
    assert branch.status_code == 201
    branch_id = branch.json()["id"]
    # Observed AFTER the branch was cut: a copy would be stale, a read-through is not.
    await _observe(event_id, field_id)

    listed = await client.get(f"/api/v1/projects/obs-branch/events?branch={branch_id}")
    copy_id = listed.json()["items"][0]["id"]
    assert copy_id != event_id
    detail = await client.get(f"/api/v1/projects/obs-branch/events/{copy_id}?branch={branch_id}")
    assert detail.status_code == 200
    observed = detail.json()["field_values"][0]["observed_values"]
    assert observed is not None
    assert observed["observed_at"].startswith("2026-10-05")
    assert [v["value"] for v in observed["values"]] == ["map/main", "spot/main"]

    # Nothing was written for the branch row.
    async with TestSessionLocal() as session:
        own = (
            await session.execute(
                select(EventFieldObservation).where(
                    EventFieldObservation.event_id == uuid.UUID(copy_id)
                )
            )
        ).all()
    assert own == []


@pytest.mark.asyncio
async def test_observations_stay_out_of_the_plan_snapshot(client: AsyncClient) -> None:
    """Scan-observed, like ``VariableValue``: a branch diff must not move on a scan."""
    _, field_id, event_id = await _seed_event(client, "obs-snap")
    async with TestSessionLocal() as session:
        event = await session.get(Event, uuid.UUID(event_id))
        assert event is not None
        project_id = event.project_id
        before = await build_plan_snapshot(session, project_id)

    await _observe(event_id, field_id)

    async with TestSessionLocal() as session:
        after = await build_plan_snapshot(session, project_id)
    assert after == before
