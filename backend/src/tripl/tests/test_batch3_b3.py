"""Shadow candidate grain, duplicate breakdown columns, drift rescan cost.

Two of the three defects only ever *fail* on Postgres — both are duplicate
conflict keys inside one multi-row ``INSERT ... ON CONFLICT DO UPDATE``, which
Postgres refuses with a cardinality violation and SQLite silently resolves
last-write-wins. This suite runs on in-memory SQLite, so nothing here can make
the old code raise. The tests therefore pin what the collector BUILDS — the row
list, and the columns that reach the warehouse query — which is the same defect
seen one step earlier and is red on revert on any database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.core.analyzers.distribution_drift import compute_psi
from tripl.core.analyzers.event_generator import GenerationResult
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.shadow_event_candidate import SHADOW_STATUS_NEW
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.metrics.chunk_processing import _build_shadow_candidate_rows
from tripl.worker.tasks.metrics.metric_rows import (
    _collect_app_version_breakdown_rows,
    _collect_distribution_drift_rows,
    _collect_metric_breakdown_rows,
    _is_supported_configured_breakdown_column,
    _is_supported_metric_breakdown_column,
)

# --- shadow candidates are unique on (scan config, name) ---

_TYPE_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TYPE_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
_TYPE_C = uuid.UUID("33333333-3333-3333-3333-333333333333")

_T0 = datetime(2026, 1, 1, 8)
_T1 = datetime(2026, 1, 1, 9)
_T2 = datetime(2026, 1, 1, 10)
_T3 = datetime(2026, 1, 1, 11)


def test_shadow_rows_fold_one_identity_seen_under_two_event_types() -> None:
    project_id = uuid.uuid4()
    scan_config_id = uuid.uuid4()

    rows = _build_shadow_candidate_rows(
        {
            (_TYPE_A, "open"): [10, _T1, _T2],
            (_TYPE_B, "open"): [30, _T0, _T3],
            (_TYPE_A, "close"): [4, _T1, _T1],
        },
        project_id=project_id,
        scan_config_id=scan_config_id,
    )

    # The assertion that goes red on revert: the same input used to produce
    # three rows, two of them carrying the conflict key (config, "open").
    assert len(rows) == 2
    conflict_keys = {(row["scan_config_id"], row["event_name"]) for row in rows}
    assert len(conflict_keys) == len(rows)

    folded = next(row for row in rows if row["event_name"] == "open")
    assert folded["observed_count"] == 40
    assert folded["first_seen_at"] == _T0
    assert folded["last_seen_at"] == _T3
    # Attributed to the type that contributed most of the volume: that is what
    # the shadow inbox pre-fills when the candidate is accepted.
    assert folded["event_type_id"] == _TYPE_B
    assert folded["project_id"] == project_id
    assert folded["scan_config_id"] == scan_config_id
    assert folded["status"] == SHADOW_STATUS_NEW

    untouched = next(row for row in rows if row["event_name"] == "close")
    assert untouched["observed_count"] == 4
    assert untouched["event_type_id"] == _TYPE_A


def test_shadow_row_event_type_is_deterministic_when_volumes_tie() -> None:
    """Equal volume must not leave the stored type to dict iteration order."""
    first = _build_shadow_candidate_rows(
        {(_TYPE_A, "open"): [10, _T1, _T1], (_TYPE_C, "open"): [10, _T1, _T1]},
        project_id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
    )
    reversed_order = _build_shadow_candidate_rows(
        {(_TYPE_C, "open"): [10, _T1, _T1], (_TYPE_A, "open"): [10, _T1, _T1]},
        project_id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
    )

    assert len(first) == 1
    assert len(reversed_order) == 1
    assert first[0]["event_type_id"] == _TYPE_C
    assert reversed_order[0]["event_type_id"] == _TYPE_C
    assert first[0]["observed_count"] == 20


def test_shadow_row_without_an_event_type_still_folds() -> None:
    """A single-event-type scan may carry a NULL ``config.event_type_id``."""
    rows = _build_shadow_candidate_rows(
        {(None, "open"): [5, _T1, _T2], (_TYPE_A, "open"): [1, _T0, _T3]},
        project_id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
    )

    assert len(rows) == 1
    assert rows[0]["observed_count"] == 6
    assert rows[0]["first_seen_at"] == _T0
    assert rows[0]["last_seen_at"] == _T3
    # Volume wins over the unbound scope, which ranks as the empty string.
    assert rows[0]["event_type_id"] is None


def test_shadow_rows_keep_distinct_identities_apart() -> None:
    rows = _build_shadow_candidate_rows(
        {(_TYPE_A, "open"): [3, _T1, _T1], (_TYPE_A, "close"): [4, _T1, _T1]},
        project_id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
    )

    assert [row["event_name"] for row in rows] == ["open", "close"]
    assert [row["observed_count"] for row in rows] == [3, 4]


# --- the app version column is collected on its own path ---


def _breakdown_scan_config() -> ScanConfig:
    return ScanConfig(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        data_source_id=uuid.uuid4(),
        event_type_id=_TYPE_A,
        name="Structured",
        base_query="SELECT * FROM events",
        time_column="ts",
        event_type_column=None,
        app_version_column="app_version",
        platform_column="platform",
        event_name_format="{action}",
    )


_REGULAR_COLS = ["action", "app_version", "platform", "country"]
_REG_INDEX = {name: index for index, name in enumerate(_REGULAR_COLS)}


def test_configured_breakdown_predicate_rejects_only_the_version_column() -> None:
    config = _breakdown_scan_config()

    assert not _is_supported_configured_breakdown_column(
        config, column="app_version", regular_cols=_REGULAR_COLS
    )
    assert _is_supported_configured_breakdown_column(
        config, column="platform", regular_cols=_REGULAR_COLS
    )
    assert _is_supported_configured_breakdown_column(
        config, column="country", regular_cols=_REGULAR_COLS
    )
    assert not _is_supported_configured_breakdown_column(
        config, column="ts", regular_cols=_REGULAR_COLS
    )
    assert not _is_supported_configured_breakdown_column(
        config, column="not_a_column", regular_cols=_REGULAR_COLS
    )


def test_base_breakdown_predicate_still_accepts_the_version_column() -> None:
    """The guard against "fixing" this by tightening the base predicate.

    ``_collect_app_version_breakdown_rows`` tests itself with the BASE predicate.
    Rejecting the version column there would delete every app-version series in
    the product, silently — this test is what catches that.
    """
    config = _breakdown_scan_config()

    assert _is_supported_metric_breakdown_column(
        config, column="app_version", regular_cols=_REGULAR_COLS
    )
    assert _is_supported_metric_breakdown_column(
        config, column="platform", regular_cols=_REGULAR_COLS
    )


class _FakeBreakdownAdapter:
    """Records the GROUPING SETS columns asked for, and answers with fixed rows."""

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.breakdown_calls: list[list[str]] = []
        self._rows = rows

    def get_time_bucketed_breakdown_counts_multi(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        breakdown_columns: list[str],
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self.breakdown_calls.append(list(breakdown_columns))
        wanted = set(breakdown_columns)
        return (
            list(regular_columns),
            [],
            [row for row in self._rows if row[1] in wanted],
        )


def _collect_breakdowns(
    adapter: _FakeBreakdownAdapter,
    config: ScanConfig,
    single_result: GenerationResult,
) -> tuple[list[dict[str, object]], list[dict[str, object]], bool]:
    return _collect_metric_breakdown_rows(
        adapter=adapter,  # type: ignore[arg-type]
        config=config,
        interval_code="1h",
        regular_cols=_REGULAR_COLS,
        json_cols=[],
        json_value_path_map={},
        time_from=datetime(2026, 1, 1, 10),
        time_to=datetime(2026, 1, 1, 11),
        query_row_limit=1000,
        reg_index=_REG_INDEX,
        json_index={},
        n_reg=len(_REGULAR_COLS),
        gen_results={},
        single_result=single_result,
        et_by_name={},
    )


def _login_generation_result(breakdown_columns: list[str]) -> tuple[GenerationResult, Event]:
    login = Event(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        event_type_id=_TYPE_A,
        name="login",
        title="Login",
        metric_breakdown_columns=breakdown_columns,
    )
    return (
        GenerationResult(
            event_type_id=_TYPE_A,
            col_meta={"action": {"is_low": True}},
            events_by_name={"login": login},
        ),
        login,
    )


# (bucket, breakdown_column, breakdown_value, is_other, action, app_version,
#  platform, country, count)
_BREAKDOWN_ROWS: list[tuple[object, ...]] = [
    (datetime(2026, 1, 1, 10), "app_version", "2.2.0", False, "login", "2.2.0", "ios", "us", 10),
    (datetime(2026, 1, 1, 10), "app_version", "2.1.0", False, "login", "2.1.0", "ios", "us", 4),
    (datetime(2026, 1, 1, 10), "app_version", "Other", True, "login", "1.0.0", "ios", "us", 3),
    (datetime(2026, 1, 1, 10), "country", "us", False, "login", "2.2.0", "ios", "us", 17),
]

# The SAME source rows at the primary grain — (bucket, *regular columns, count),
# what ``get_time_bucketed_counts`` returns — which is where the app-version
# series is derived from instead of a second warehouse query. 10 + 4 + 3 = the
# 17 the country row above reports for the bucket.
_METRIC_ROWS: list[tuple[object, ...]] = [
    (datetime(2026, 1, 1, 10), "login", "2.2.0", "ios", "us", 10),
    (datetime(2026, 1, 1, 10), "login", "2.1.0", "ios", "us", 4),
    (datetime(2026, 1, 1, 10), "login", "1.0.0", "ios", "us", 3),
]


def _collect_version_breakdowns(
    config: ScanConfig, single_result: GenerationResult
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """The OTHER path into ``event_metric_breakdown``, called as chunk 1 calls it."""
    return _collect_app_version_breakdown_rows(
        config=config,
        regular_cols=_REGULAR_COLS,
        rows=_METRIC_ROWS,
        json_value_names=[],
        reg_index=_REG_INDEX,
        json_index={},
        n_reg=len(_REGULAR_COLS),
        gen_results={},
        single_result=single_result,
        et_by_name={},
    )


def test_an_event_breakdown_column_that_is_the_version_column_is_never_queried() -> None:
    config = _breakdown_scan_config()
    config.platform_column = None
    single_result, login = _login_generation_result(["app_version", "country"])
    adapter = _FakeBreakdownAdapter(_BREAKDOWN_ROWS)

    event_rows, type_rows, truncated = _collect_breakdowns(adapter, config, single_result)

    # Red on revert: the version column used to enter the generic GROUPING SETS
    # query alongside the legitimate one.
    assert adapter.breakdown_calls == [["country"]]
    assert not truncated
    assert {row["breakdown_column"] for row in event_rows} == {"country"}
    assert all(row["event_id"] == login.id for row in event_rows)
    assert type_rows == []

    # The duplicate-conflict-key assertion, made where the duplicate can exist.
    # Inside ``_collect_metric_breakdown_rows`` it cannot: those rows are built
    # from a dict keyed on the conflict key itself, so uniqueness there is a
    # property of the dict and holds on the defective code too. The two rows that
    # carry one key meet in ``process_chunk``, which extends the generic list
    # with the app-version list before the single multi-row upsert — so the
    # assertion has to span that concatenation, exactly as filed.
    version_event_rows, _version_type_rows = _collect_version_breakdowns(config, single_result)
    # Not vacuous: the version path really does emit rows for this bucket.
    assert {row["breakdown_column"] for row in version_event_rows} == {"app_version"}

    merged = [*event_rows, *version_event_rows]
    keys = [
        (
            row["event_id"],
            row["bucket"],
            row["breakdown_column"],
            row["breakdown_value"],
            row["is_other"],
        )
        for row in merged
    ]
    # ``uq_event_metric_breakdown_config_event_bucket_value``, spelled in full.
    # On Postgres the two ('app_version', bucket, '2.2.0', False) rows the two
    # paths used to produce aborted the whole INSERT with a cardinality
    # violation; SQLite merges them last-write-wins, so this list-level check is
    # what sees the defect here. The end-to-end proof, which persists the rows,
    # is ``test_collect_metrics_never_collects_the_version_column_as_a_generic_breakdown``
    # in test_metrics_tasks.py.
    assert len(keys) == len(set(keys))


def test_the_version_column_is_skipped_in_a_scan_level_breakdown_list_too() -> None:
    """Legacy scan-level rows predate ``check_scalar_columns_unreserved``."""
    config = _breakdown_scan_config()
    config.platform_column = None
    config.metric_breakdown_columns = ["app_version"]
    single_result, _login = _login_generation_result([])
    adapter = _FakeBreakdownAdapter(_BREAKDOWN_ROWS)

    event_rows, type_rows, _truncated = _collect_breakdowns(adapter, config, single_result)

    assert adapter.breakdown_calls == []
    assert event_rows == []
    assert type_rows == []


def test_the_platform_column_stays_collectable_from_an_event_list() -> None:
    """The demo project ships an event listing the platform column; it must work."""
    config = _breakdown_scan_config()
    single_result, login = _login_generation_result(["platform"])
    adapter = _FakeBreakdownAdapter(
        [
            (
                datetime(2026, 1, 1, 10),
                "platform",
                "ios",
                False,
                "login",
                "2.2.0",
                "ios",
                "us",
                7,
            )
        ]
    )

    event_rows, type_rows, _truncated = _collect_breakdowns(adapter, config, single_result)

    assert adapter.breakdown_calls == [["platform"]]
    assert [row["breakdown_column"] for row in event_rows] == ["platform"]
    # Scan-wide, so it is also stored per event type — one key each, never two.
    assert [row["breakdown_column"] for row in type_rows] == ["platform"]
    assert event_rows[0]["count"] == 7


async def _seed_versioned_scan(slug: str, *, app_version_column: str) -> None:
    async with TestSessionLocal() as session, session.begin():
        project = (await session.execute(select(Project).where(Project.slug == slug))).scalar_one()
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"wh-{slug}",
            db_type="clickhouse",
            host="localhost",
            port=9000,
            database_name="db",
            username="u",
            password_encrypted="x",
        )
        session.add(data_source)
        await session.flush()
        session.add(
            ScanConfig(
                id=uuid.uuid4(),
                project_id=project.id,
                data_source_id=data_source.id,
                event_type_id=None,
                name="scan",
                base_query="SELECT * FROM events",
                time_column="ts",
                event_type_column="event_type",
                app_version_column=app_version_column,
                platform_column="platform",
            )
        )


async def _setup_project(client: AsyncClient, slug: str) -> tuple[str, str]:
    await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    event_type_id = event_type.json()["id"]
    field = await client.post(
        f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields",
        json={
            "name": "screen",
            "display_name": "Screen",
            "field_type": "string",
            "is_required": True,
        },
    )
    return event_type_id, field.json()["id"]


@pytest.mark.asyncio
async def test_create_event_refuses_the_scans_app_version_column(client: AsyncClient) -> None:
    slug = "b3-create-version"
    event_type_id, field_id = await _setup_project(client, slug)
    await _seed_versioned_scan(slug, app_version_column="app_version")

    refused = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type_id,
            "name": "Home",
            "metric_breakdown_columns": ["app_version"],
            "field_values": [{"field_definition_id": field_id, "value": "home"}],
        },
    )
    assert refused.status_code == 422
    assert "app_version" in refused.json()["detail"]

    allowed = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type_id,
            "name": "Home",
            "metric_breakdown_columns": ["platform", "country"],
            "field_values": [{"field_definition_id": field_id, "value": "home"}],
        },
    )
    assert allowed.status_code == 201
    assert allowed.json()["metric_breakdown_columns"] == ["platform", "country"]


@pytest.mark.asyncio
async def test_bulk_create_names_the_item_holding_the_version_column(
    client: AsyncClient,
) -> None:
    slug = "b3-bulk-version"
    event_type_id, field_id = await _setup_project(client, slug)
    await _seed_versioned_scan(slug, app_version_column="app_version")

    resp = await client.post(
        f"/api/v1/projects/{slug}/events/bulk",
        json=[
            {
                "event_type_id": event_type_id,
                "name": "One",
                "field_values": [{"field_definition_id": field_id, "value": "one"}],
            },
            {
                "event_type_id": event_type_id,
                "name": "Two",
                "metric_breakdown_columns": ["app_version"],
                "field_values": [{"field_definition_id": field_id, "value": "two"}],
            },
        ],
    )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail.startswith("Event 2 of 2: ")
    assert "app_version" in detail


@pytest.mark.asyncio
async def test_update_event_refuses_a_newly_added_version_column(client: AsyncClient) -> None:
    slug = "b3-update-version"
    event_type_id, field_id = await _setup_project(client, slug)
    await _seed_versioned_scan(slug, app_version_column="app_version")

    created = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type_id,
            "name": "Home",
            "metric_breakdown_columns": ["country"],
            "field_values": [{"field_definition_id": field_id, "value": "home"}],
        },
    )
    assert created.status_code == 201
    event_id = created.json()["id"]

    refused = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}",
        json={"metric_breakdown_columns": ["country", "app_version"]},
    )
    assert refused.status_code == 422
    assert "app_version" in refused.json()["detail"]

    allowed = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}",
        json={"metric_breakdown_columns": ["country", "platform"]},
    )
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_update_event_grandfathers_a_stored_version_column(client: AsyncClient) -> None:
    """An unrelated edit must not be blocked by a column the form used to offer.

    The event form re-sends the whole breakdown list on every save. Refusing a
    stored value would make the user remove a column they did not touch before
    any edit could be saved; the collector skips it regardless.
    """
    slug = "b3-grandfather"
    event_type_id, field_id = await _setup_project(client, slug)

    created = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type_id,
            "name": "Home",
            "metric_breakdown_columns": ["app_version"],
            "field_values": [{"field_definition_id": field_id, "value": "home"}],
        },
    )
    assert created.status_code == 201, "no scan yet, so the column is not reserved"
    event_id = created.json()["id"]

    # The scan that reserves the column arrives afterwards.
    await _seed_versioned_scan(slug, app_version_column="app_version")

    resaved = await client.patch(
        f"/api/v1/projects/{slug}/events/{event_id}",
        json={"title": "Renamed", "metric_breakdown_columns": ["app_version"]},
    )
    assert resaved.status_code == 200
    assert resaved.json()["metric_breakdown_columns"] == ["app_version"]


# --- drift baselines are sliced, not rescanned ---

_DRIFT_REGULAR_COLS = ["country", "event_type"]
_DRIFT_REG_INDEX = {"country": 0, "event_type": 1}


class _FakeDriftAdapter:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def get_time_bucketed_breakdown_counts_multi(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        breakdown_columns: list[str],
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        assert values_limit is None
        return (list(regular_columns), [], list(self._rows))


def _drift_scan_config() -> ScanConfig:
    return ScanConfig(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        data_source_id=uuid.uuid4(),
        event_type_id=None,
        name="Drift",
        base_query="SELECT * FROM events",
        time_column="ts",
        event_type_column="event_type",
        distribution_drift_fields=["country"],
        baseline_window_buckets=2,
        min_history_buckets=2,
    )


_DRIFT_BASE = datetime(2026, 1, 1)


def _drift_bucket(hour: int) -> datetime:
    return _DRIFT_BASE + timedelta(hours=hour)


def _stored_drift_bucket(hour: int) -> datetime:
    """The bucket a drift row carries: the naive warehouse cell as aware UTC.

    The fake adapter hands back naive buckets, the shape a driver decodes, and
    the collector stores them through ``stored_bucket``.
    """
    return _drift_bucket(hour).replace(tzinfo=UTC)


def _drift_row(hour: int, value: str, event_type: str, count: int) -> tuple[object, ...]:
    # (bucket, field_name, field_value, is_json, country, event_type, count)
    return (_drift_bucket(hour), "country", value, False, value, event_type, count)


# One field, four event-type names of which "ghost" has no EventType, so it only
# ever reaches the scan-wide (NULL) scope. Deliberate edge cases, in order: a
# bucket before ``time_from`` that must still feed a baseline; a bucket at/after
# ``time_to`` that must be skipped; a scope with only one populated baseline
# bucket; a scope whose only predecessor falls outside the baseline window; and a
# value present in the current bucket but absent from the baseline.
_DRIFT_ROWS: list[tuple[object, ...]] = [
    _drift_row(8, "us", "login", 10),
    _drift_row(8, "de", "login", 10),
    _drift_row(8, "us", "purchase", 7),
    _drift_row(9, "us", "login", 10),
    _drift_row(9, "de", "login", 10),
    _drift_row(9, "us", "logout", 20),
    _drift_row(10, "us", "login", 40),
    _drift_row(10, "de", "login", 5),
    _drift_row(10, "us", "logout", 18),
    _drift_row(10, "de", "logout", 2),
    _drift_row(11, "us", "login", 30),
    _drift_row(11, "de", "login", 10),
    _drift_row(11, "us", "logout", 10),
    _drift_row(11, "de", "logout", 10),
    _drift_row(11, "us", "purchase", 9),
    _drift_row(11, "jp", "ghost", 4),
    _drift_row(12, "de", "logout", 12),
    _drift_row(12, "fr", "logout", 3),
    _drift_row(13, "us", "login", 5),
]

# Hand-derived (event_type_id, bucket hour, baseline map, current map) for every
# row the collector must emit, in the exact order it must emit them: scopes by
# ``(str(event_type_id or ""), field_name)`` — so the scan-wide scope first —
# then buckets ascending.
_EXPECTED_DRIFT: list[tuple[uuid.UUID | None, int, dict[str, int], dict[str, int]]] = [
    (None, 10, {"us": 10 + 7 + 10 + 20, "de": 10 + 10}, {"us": 40 + 18, "de": 5 + 2}),
    (
        None,
        11,
        {"us": 10 + 20 + 40 + 18, "de": 10 + 5 + 2},
        {"us": 30 + 10 + 9, "de": 10 + 10, "jp": 4},
    ),
    (
        None,
        12,
        {"us": 40 + 18 + 30 + 10 + 9, "de": 5 + 2 + 10 + 10, "jp": 4},
        {"de": 12, "fr": 3},
    ),
    (_TYPE_A, 10, {"us": 20, "de": 20}, {"us": 40, "de": 5}),
    (_TYPE_A, 11, {"us": 10 + 40, "de": 10 + 5}, {"us": 30, "de": 10}),
    (_TYPE_B, 11, {"us": 20 + 18, "de": 2}, {"us": 10, "de": 10}),
    (_TYPE_B, 12, {"us": 18 + 10, "de": 2 + 10}, {"de": 12, "fr": 3}),
]


def _drift_event_types() -> dict[str, EventType]:
    return {
        "login": EventType(id=_TYPE_A, name="login", display_name="Login"),
        "logout": EventType(id=_TYPE_B, name="logout", display_name="Logout"),
        "purchase": EventType(id=_TYPE_C, name="purchase", display_name="Purchase"),
    }


def _collect_drift(
    adapter: _FakeDriftAdapter,
    config: ScanConfig,
    et_by_name: dict[str, EventType],
) -> tuple[list[dict[str, object]], int, bool]:
    return _collect_distribution_drift_rows(
        adapter=adapter,  # type: ignore[arg-type]
        config=config,
        interval_code="1h",
        interval_delta=timedelta(hours=1),
        regular_cols=_DRIFT_REGULAR_COLS,
        json_cols=[],
        json_value_path_map={},
        time_from=datetime(2026, 1, 1, 10),
        time_to=datetime(2026, 1, 1, 13),
        query_row_limit=1000,
        reg_index=_DRIFT_REG_INDEX,
        et_by_name=et_by_name,
    )


def test_distribution_drift_rows_match_per_scope_baselines() -> None:
    """Behaviour pin for the rescan removal.

    Honest about what it is: the refactor is output-identical by construction,
    so this does NOT go red if it is reverted. It is the durable guard that the
    baseline window, the ``min_history_buckets`` rule and the row ORDER (which
    decides what a replay's delete pass touches) survive any later edit to this
    function. ``test_distribution_drift_baselines_are_not_rescanned`` below is
    the revert detector.
    """
    config = _drift_scan_config()
    rows, significant_count, truncated = _collect_drift(
        _FakeDriftAdapter(_DRIFT_ROWS), config, _drift_event_types()
    )

    assert not truncated
    assert len(rows) == len(_EXPECTED_DRIFT)

    expected_significant = 0
    for row, (event_type_id, hour, baseline, current) in zip(rows, _EXPECTED_DRIFT, strict=True):
        expected = compute_psi(baseline, current)
        where = f"{event_type_id} @ {hour}"
        assert row["event_type_id"] == event_type_id, where
        assert row["bucket"] == _stored_drift_bucket(hour), where
        assert row["field_name"] == "country", where
        assert row["scan_config_id"] == config.id, where
        assert row["baseline_total"] == expected.baseline_total, where
        assert row["current_total"] == expected.current_total, where
        # approx, not exact: both sides sum the same floats, but over a set whose
        # iteration order follows insertion, and the expectations here are keyed
        # by hand rather than in warehouse row order.
        assert row["psi"] == pytest.approx(expected.psi), where
        assert row["band"] == expected.band, where
        movers = cast(list[dict[str, object]], row["top_movers"])
        assert {mover["value"] for mover in movers} == {
            shift.value for shift in expected.top_movers
        }, where
        if expected.band == "significant":
            expected_significant += 1

    assert significant_count == expected_significant

    # The scan-wide scope is the sum of the typed ones plus the rows whose event
    # type is unknown to the plan.
    scan_wide = {row["bucket"]: row for row in rows if row["event_type_id"] is None}
    assert scan_wide[_stored_drift_bucket(10)]["current_total"] == 40 + 5 + 18 + 2


def test_distribution_drift_skips_a_scope_without_enough_history() -> None:
    """The two scopes the dataset deliberately starves must emit nothing."""
    rows, _significant, _truncated = _collect_drift(
        _FakeDriftAdapter(_DRIFT_ROWS), _drift_scan_config(), _drift_event_types()
    )

    emitted = {(row["event_type_id"], row["bucket"]) for row in rows}
    # Only bucket 09 is populated inside logout's baseline window at bucket 10.
    assert (_TYPE_B, _stored_drift_bucket(10)) not in emitted
    # purchase has one predecessor (bucket 08) and it falls outside the window.
    assert not any(row["event_type_id"] == _TYPE_C for row in rows)
    # Bucket 13 is at/after time_to.
    assert not any(row["bucket"] == _stored_drift_bucket(13) for row in rows)


_EQUALITY_CHECKS = [0]


class _CountingUUID(uuid.UUID):
    """A UUID that records every equality test made against it.

    The drift change is a pure-CPU one: same rows, same order, same numbers,
    less work — so no output can tell the two implementations apart and only the
    amount of work can. The old code filtered the whole flat count store per
    (scope, bucket) pair with ``row_event_type_id != event_type_id``, i.e. one
    comparison against this id for EVERY stored entry, every time. The new code
    only ever uses the id to look a scope up while ingesting. Counting the
    comparisons makes that a deterministic assertion instead of a wall-clock one,
    which on a slow CI box is the difference between a guard and a flake.

    Only ``__eq__`` is defined: ``object.__ne__`` delegates to it, so ``!=``
    is counted once too.
    """

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        _EQUALITY_CHECKS[0] += 1
        return super().__eq__(other)

    __hash__ = uuid.UUID.__hash__


def test_distribution_drift_baselines_are_not_rescanned() -> None:
    counting_type_id = _CountingUUID("44444444-4444-4444-4444-444444444444")
    # 30 buckets x 60 values under one event type: 20 in-window buckets, two
    # scopes (scan-wide and typed), 3600 stored count entries. The old nested
    # rescan is 2 x 20 x 3600 = 144,000 comparisons against this id; the new one
    # makes roughly one per ingested row.
    rows = [
        _drift_row(hour, f"v{value}", "login", value + 1)
        for hour in range(30)
        for value in range(60)
    ]
    config = _drift_scan_config()
    config.baseline_window_buckets = 3
    config.min_history_buckets = 2
    adapter = _FakeDriftAdapter(rows)

    _EQUALITY_CHECKS[0] = 0
    output_rows, _significant, _truncated = _collect_distribution_drift_rows(
        adapter=adapter,  # type: ignore[arg-type]
        config=config,
        interval_code="1h",
        interval_delta=timedelta(hours=1),
        regular_cols=_DRIFT_REGULAR_COLS,
        json_cols=[],
        json_value_path_map={},
        time_from=datetime(2026, 1, 1, 10),
        time_to=datetime(2026, 1, 2, 6),
        query_row_limit=10000,
        reg_index=_DRIFT_REG_INDEX,
        et_by_name={"login": EventType(id=counting_type_id, name="login", display_name="Login")},
    )
    equality_checks = _EQUALITY_CHECKS[0]

    # The work really did happen — otherwise the budget below is free to pass.
    assert len(output_rows) == 2 * 20
    assert equality_checks < 10 * len(rows), (
        f"{equality_checks} equality tests for {len(rows)} rows: the baseline "
        "split is rescanning the whole count store again"
    )
