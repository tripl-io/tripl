"""A chosen Event type names a scan's events on every path, column or not.

The scan form saves both an Event type and an Event type column when a user
picks a column and then a fixed Event type, and tells them the column then
names nothing. The manual run and the dry run agreed; the scheduled collection,
a replay and the metric-row collectors let the column win, so every tick filed
the volume under event types named after the column's values. All of them now
ask ``worker.utils.scan_naming``.

A config that names its events neither way now fails a scheduled run with the
curated message instead of "Scan failed due to an internal error.".
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import tripl.worker.celery_app  # noqa: F401
from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.cardinality import BreakdownAnalysis, CardinalityResult
from tripl.core.analyzers.event_generator import GenerationResult
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.worker.tasks._errors import NO_EVENT_NAMING_MSG, ScanError, user_facing_error
from tripl.worker.tasks.metrics.catalog_sync import sync_catalog
from tripl.worker.tasks.metrics.generation import _load_existing_generation_results
from tripl.worker.tasks.metrics.metric_rows import (
    _collect_app_version_breakdown_rows,
    _collect_distribution_drift_rows,
    _collect_metric_breakdown_rows,
)
from tripl.worker.utils.scan_naming import group_column_index, scan_group_column

_BOUND = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_COLUMN_TYPE = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_WINDOW = (datetime(2026, 10, 6, tzinfo=UTC), datetime(2026, 10, 6, 1, tzinfo=UTC))


# --------------------------------------------------------------------------- #
# The resolver
# --------------------------------------------------------------------------- #


def test_a_chosen_event_type_beats_a_leftover_column() -> None:
    both = ScanConfig(event_type_id=_BOUND, event_type_column="screen")
    grouped = ScanConfig(event_type_id=None, event_type_column="screen")
    neither = ScanConfig(event_type_id=None, event_type_column=None)
    blank = ScanConfig(event_type_id=None, event_type_column="")

    assert scan_group_column(both) is None
    assert scan_group_column(grouped) == "screen"
    assert scan_group_column(neither) is None
    assert scan_group_column(blank) is None

    reg_index = {"action": 0, "screen": 1}
    assert group_column_index(both, reg_index) is None
    assert group_column_index(grouped, reg_index) == 1
    assert group_column_index(ScanConfig(event_type_column="missing"), reg_index) is None


# --------------------------------------------------------------------------- #
# Scheduled catalog sync and replay
# --------------------------------------------------------------------------- #


@pytest.fixture
def sync_session() -> Iterator[Session]:
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


def _seed_bound_scan(session: Session) -> tuple[Project, EventType, ScanConfig]:
    """A scan bound to one Event type that ALSO carries an Event type column."""
    project = Project(id=uuid.uuid4(), name="Naming", slug="naming", description="")
    session.add(project)
    session.flush()
    event_type = EventType(
        id=uuid.uuid4(), project_id=project.id, name="se", display_name="SE", description=""
    )
    session.add(event_type)
    session.flush()
    session.add_all(
        FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=event_type.id,
            name=name,
            display_name=name.title(),
            field_type="string",
            order=order,
        )
        for order, name in enumerate(("screen", "action"))
    )
    source = DataSource(
        id=uuid.uuid4(),
        name="wh",
        db_type="clickhouse",
        host="localhost",
        port=9000,
        database_name="db",
        username="u",
    )
    session.add(source)
    session.flush()
    config = ScanConfig(
        id=uuid.uuid4(),
        project_id=project.id,
        data_source_id=source.id,
        name="scan",
        base_query="SELECT 1",
        event_type_id=event_type.id,
        event_type_column="screen",
    )
    session.add(config)
    session.commit()
    return project, event_type, config


def _analysis() -> BreakdownAnalysis:
    return BreakdownAnalysis(
        results={
            name: CardinalityResult(
                column=ColumnInfo(name, "String"),
                count=1,
                is_low=True,
                sample_values=[value],
            )
            for name, value in (("screen", "map/main"), ("action", "tap"))
        },
        rows=[("map/main", "tap", 1)],
        reg_names=["screen", "action"],
        json_names=[],
    )


class _Adapter:
    def get_json_path_samples(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {}


def _sync(
    session: Session,
    config: ScanConfig,
    *,
    grouped_calls: list[object],
    generate_calls: list[tuple[tuple[object, ...], dict[str, object]]],
) -> None:
    def _grouped(*args: object, **kwargs: object) -> tuple[list[str], dict[str, object]]:
        grouped_calls.append(kwargs.get("group_column"))
        return [], {}

    def _generate(*args: object, **kwargs: object) -> GenerationResult:
        generate_calls.append((args, kwargs))
        return GenerationResult()

    sync_catalog(
        session,
        adapter=_Adapter(),  # type: ignore[arg-type]
        config=config,
        columns=[ColumnInfo("screen", "String"), ColumnInfo("action", "String")],
        skip_cols={"screen"},
        json_value_path_map={},
        scan_row_limit=1000,
        metrics_row_limit=1000,
        time_from_dt=_WINDOW[0],
        time_to_dt=_WINDOW[1],
        catalog_scan_window=_WINDOW,
        is_replay=False,
        analyze_cardinality_fn=lambda *a, **k: _analysis(),
        analyze_cardinality_grouped_fn=_grouped,
        generate_events_fn=_generate,
    )


def test_the_scheduled_sync_files_rows_under_the_chosen_event_type(sync_session: Session) -> None:
    project, event_type, config = _seed_bound_scan(sync_session)
    grouped_calls: list[object] = []
    generate_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    _sync(sync_session, config, grouped_calls=grouped_calls, generate_calls=generate_calls)

    assert grouped_calls == [], "the column must not group a scan bound to an Event type"
    assert len(generate_calls) == 1
    args, kwargs = generate_calls[0]
    assert args[2] == event_type.id
    # Still reserved, so it is neither catalogued nor reported as a plan gap.
    assert kwargs["event_type_column"] == "screen"
    names = sync_session.execute(
        select(EventType.name).where(EventType.project_id == project.id)
    ).scalars()
    assert list(names) == ["se"]


def test_a_scheduled_run_that_names_nothing_says_so(sync_session: Session) -> None:
    _project, _event_type, config = _seed_bound_scan(sync_session)
    config.event_type_id = None
    config.event_type_column = None

    with pytest.raises(ScanError) as excinfo:
        _sync(sync_session, config, grouped_calls=[], generate_calls=[])

    assert str(excinfo.value) == NO_EVENT_NAMING_MSG
    assert user_facing_error(excinfo.value) == f"Scan failed: {NO_EVENT_NAMING_MSG}"


def test_replay_loads_the_chosen_event_type_not_every_type(sync_session: Session) -> None:
    project, event_type, config = _seed_bound_scan(sync_session)
    # A second main-plan type, the kind past column-first ticks created.
    sync_session.add(
        EventType(
            id=uuid.uuid4(),
            project_id=project.id,
            name="map/main",
            display_name="map/main",
            description="",
        )
    )
    sync_session.commit()

    gen_results, single_result = _load_existing_generation_results(
        sync_session,
        config=config,
        columns=[ColumnInfo("screen", "String"), ColumnInfo("action", "String")],
    )

    assert gen_results == {}
    assert single_result is not None
    # The column stays out of the event's identity on the single-type path.
    assert set(single_result.col_meta) == {"action"}


# --------------------------------------------------------------------------- #
# Metric-row collectors
# --------------------------------------------------------------------------- #

_REGULAR_COLS = ["action", "app_version", "platform", "country"]
_REG_INDEX = {name: index for index, name in enumerate(_REGULAR_COLS)}
_BUCKET = datetime(2026, 1, 1, 10)


def _bound_metric_config(**overrides: object) -> ScanConfig:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "data_source_id": uuid.uuid4(),
        "event_type_id": _BOUND,
        "name": "Bound",
        "base_query": "SELECT * FROM events",
        "time_column": "ts",
        # Left over from when the scan was grouped; names nothing now.
        "event_type_column": "country",
        "app_version_column": "app_version",
        "platform_column": None,
        "event_name_format": "{action}",
    }
    fields.update(overrides)
    return ScanConfig(**fields)


def _login() -> tuple[GenerationResult, Event]:
    login = Event(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        event_type_id=_BOUND,
        name="login",
        title="Login",
        metric_breakdown_columns=[],
    )
    return (
        GenerationResult(
            event_type_id=_BOUND,
            col_meta={"action": {"is_low": True}},
            events_by_name={"login": login},
        ),
        login,
    )


# What a column-first lookup would find: a type named after the column value.
_COLUMN_TYPES = {"us": EventType(id=_COLUMN_TYPE, name="us", display_name="us")}


def test_app_version_rows_go_to_the_chosen_event_type() -> None:
    single_result, login = _login()
    event_rows, type_rows = _collect_app_version_breakdown_rows(
        config=_bound_metric_config(),
        regular_cols=_REGULAR_COLS,
        rows=[
            (_BUCKET, "login", "2.2.0", "ios", "us", 10),
            (_BUCKET, "login", "2.1.0", "ios", "us", 4),
        ],
        json_value_names=[],
        reg_index=_REG_INDEX,
        json_index={},
        n_reg=len(_REGULAR_COLS),
        gen_results={},
        single_result=single_result,
        et_by_name=_COLUMN_TYPES,
    )

    assert sorted(row["count"] for row in event_rows) == [4, 10]
    assert {row["event_id"] for row in event_rows} == {login.id}
    assert {row["event_type_id"] for row in type_rows} == {_BOUND}


class _BreakdownAdapter:
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
        wanted = set(breakdown_columns)
        return list(regular_columns), [], [row for row in self._rows if row[1] in wanted]


def test_breakdown_rows_go_to_the_chosen_event_type() -> None:
    single_result, login = _login()
    adapter = _BreakdownAdapter(
        [(_BUCKET, "platform", "ios", False, "login", "2.2.0", "ios", "us", 7)]
    )

    event_rows, type_rows, truncated = _collect_metric_breakdown_rows(
        adapter=adapter,  # type: ignore[arg-type]
        config=_bound_metric_config(platform_column="platform"),
        interval_code="1h",
        regular_cols=_REGULAR_COLS,
        json_cols=[],
        json_value_path_map={},
        time_from=_BUCKET,
        time_to=_BUCKET + timedelta(hours=1),
        query_row_limit=1000,
        reg_index=_REG_INDEX,
        json_index={},
        n_reg=len(_REGULAR_COLS),
        gen_results={},
        single_result=single_result,
        et_by_name=_COLUMN_TYPES,
    )

    assert not truncated
    assert [(row["event_id"], row["count"]) for row in event_rows] == [(login.id, 7)]
    assert {row["event_type_id"] for row in type_rows} == {_BOUND}


def test_distribution_drift_scopes_to_the_chosen_event_type() -> None:
    # (bucket, field_name, field_value, is_json, country, event_type, count)
    rows: list[tuple[object, ...]] = [
        (datetime(2026, 1, 1, hour), "country", value, False, value, "login", count)
        for hour in (8, 9, 10)
        for value, count in (("us", 10 + hour), ("de", 5))
    ]
    config = _bound_metric_config(
        event_type_column="event_type",
        app_version_column=None,
        distribution_drift_fields=["country"],
        baseline_window_buckets=2,
        min_history_buckets=2,
    )

    output, _significant, _truncated = _collect_distribution_drift_rows(
        adapter=_BreakdownAdapter(rows),  # type: ignore[arg-type]
        config=config,
        interval_code="1h",
        interval_delta=timedelta(hours=1),
        regular_cols=["country", "event_type"],
        json_cols=[],
        json_value_path_map={},
        time_from=datetime(2026, 1, 1, 10),
        time_to=datetime(2026, 1, 1, 11),
        query_row_limit=1000,
        reg_index={"country": 0, "event_type": 1},
        et_by_name={"login": EventType(id=_COLUMN_TYPE, name="login", display_name="Login")},
    )

    scopes = {row["event_type_id"] for row in output}
    assert scopes == {None, _BOUND}
