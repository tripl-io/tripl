"""A scheduled tick reads its window once when the catalog and the chunk agree.

The catalog sync's breakdown and the metrics chunk's bucketed counts are the same
GROUP BY over the same columns, the second with a time bucket. When the catalog
window is the one collection chunk, the first is the second summed over the
bucket, so the tick folds instead of querying twice (``shared_breakdown``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.worker.tasks.metrics import tasks as metrics
from tripl.worker.tasks.metrics.shared_breakdown import (
    PrefetchedBreakdownAdapter,
    TickRows,
    fold_buckets,
)

T0 = datetime(2026, 1, 1, 10, tzinfo=UTC)
T1 = datetime(2026, 1, 1, 11, tzinfo=UTC)
T2 = datetime(2026, 1, 1, 12, tzinfo=UTC)


# ── fold_buckets ─────────────────────────────────────────────────────────────


def test_fold_sums_each_combination_over_the_buckets_and_ranks_by_count() -> None:
    rows: list[tuple[object, ...]] = [
        (T0, "page_view", ["a", "b"], 5),
        (T0, "click", ["x"], 7),
        (T1, "page_view", ["a", "b"], 4),
        (T1, "signup", [], 1),
    ]

    assert fold_buckets(rows) == [
        ("page_view", ["a", "b"], 9),
        ("click", ["x"], 7),
        ("signup", [], 1),
    ]


def test_fold_keeps_json_values_and_nulls_as_distinct_cells() -> None:
    rows: list[tuple[object, ...]] = [
        (T0, "e", None, '"1"', 2),
        (T1, "e", None, '"2"', 3),
        (T1, "e", None, '"1"', 1),
    ]

    assert fold_buckets(rows) == [("e", None, '"1"', 3), ("e", None, '"2"', 3)]


# ── PrefetchedBreakdownAdapter ──────────────────────────────────────────────


class _Warehouse:
    def __init__(self) -> None:
        self.breakdowns = 0

    def get_full_breakdown(self, *_args: object, **_kwargs: object) -> object:
        self.breakdowns += 1
        return ["event"], [], [], [("from the warehouse", 1)]

    def describe(self) -> str:
        return "the real adapter"


def _tick(rows: list[tuple[object, ...]]) -> TickRows:
    return TickRows(
        base_query="SELECT * FROM events",
        regular_columns=("event",),
        json_columns=(),
        json_value_paths=(),
        time_column="time",
        time_from=T0,
        time_to=T2,
        json_value_names=[],
        rows=rows,
    )


def test_the_matching_breakdown_is_folded_and_limited_without_a_query() -> None:
    warehouse = _Warehouse()
    wrapped = PrefetchedBreakdownAdapter(
        warehouse,  # type: ignore[arg-type]
        _tick([(T0, "a", 1), (T0, "b", 5), (T1, "a", 2)]),
    )

    out = wrapped.get_full_breakdown(
        "SELECT * FROM events", ["event"], [], {}, "time", T0, T2, limit=1
    )

    assert out == (["event"], [], [], [("b", 5)])
    assert wrapped.served
    assert warehouse.breakdowns == 0
    assert wrapped.describe() == "the real adapter"


@pytest.mark.parametrize(
    "change",
    [
        {"base_query": "SELECT * FROM other"},
        {"regular_columns": ["event", "page"]},
        {"json_value_paths": {"props": ["plan"]}},
        {"time_from": T1},
        {"time_column": None},
    ],
)
def test_any_other_breakdown_goes_to_the_warehouse(change: dict[str, object]) -> None:
    warehouse = _Warehouse()
    wrapped = PrefetchedBreakdownAdapter(warehouse, _tick([(T0, "a", 1)]))  # type: ignore[arg-type]
    args: dict[str, object] = {
        "base_query": "SELECT * FROM events",
        "regular_columns": ["event"],
        "json_columns": [],
        "json_value_paths": {},
        "time_column": "time",
        "time_from": T0,
        "time_to": T2,
    } | change

    wrapped.get_full_breakdown(**args)  # type: ignore[arg-type]

    assert warehouse.breakdowns == 1
    assert not wrapped.served


# ── collect_metrics: how many times the warehouse is read ────────────────────


@pytest.fixture(autouse=True)
def _source_never_late(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "is_holding", lambda freshness: False)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'shared_breakdown.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


class _CountingWarehouse:
    """Two events (an event name and a screen) over two hours; counts the reads."""

    def __init__(self) -> None:
        self.breakdowns = 0
        self.bucketed = 0

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return [
            ColumnInfo(name="time", type_name="DateTime"),
            ColumnInfo(name="event_name", type_name="String"),
            ColumnInfo(name="screen", type_name="String"),
        ]

    def get_full_breakdown(
        self, *_args: object, **_kwargs: object
    ) -> tuple[list[str], list[str], list[str], list[tuple[object, ...]]]:
        self.breakdowns += 1
        return ["event_name", "screen"], [], [], [("page_view", "home", 9), ("click", "home", 7)]

    def get_time_bucketed_counts(
        self, *_args: object, **_kwargs: object
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self.bucketed += 1
        rows: list[tuple[object, ...]] = [
            (T0, "page_view", "home", 5),
            (T0, "click", "home", 7),
            (T1, "page_view", "home", 4),
        ]
        return ["event_name", "screen"], [], rows

    def close(self) -> None:
        return None


def _config(session: Session, *, lookback_hours: int | None) -> ScanConfig:
    project = Project(id=uuid.uuid4(), name="P", slug=f"p-{uuid.uuid4().hex[:8]}", description="")
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"DS {uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Events",
        base_query="SELECT time, event_name, screen FROM events",
        time_column="time",
        event_type_column="event_name",
        cardinality_threshold=100,
        interval="1h",
        scan_lookback_hours=lookback_hours,
    )
    session.add_all([project, data_source, config])
    session.commit()
    return config


def _run(
    factory: sessionmaker[Session], monkeypatch: MonkeyPatch, *, lookback_hours: int | None
) -> tuple[_CountingWarehouse, ScanConfig]:
    with factory() as session:
        config = _config(session, lookback_hours=lookback_hours)
    warehouse = _CountingWarehouse()
    monkeypatch.setattr(metrics, "_get_sync_session", factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: warehouse)
    monkeypatch.setattr(metrics, "_resolve_collection_window", lambda *a, **k: (T0, T2, False))
    monkeypatch.setattr(metrics, "_widen_for_held_buckets", lambda *a, time_from, **k: time_from)
    metrics.collect_metrics.run(str(config.id))
    return warehouse, config


def test_without_a_declared_lookback_the_tick_reads_the_warehouse_once(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    warehouse, config = _run(sync_session_factory, monkeypatch, lookback_hours=None)

    assert (warehouse.bucketed, warehouse.breakdowns) == (1, 0)
    with sync_session_factory() as session:
        events = session.scalars(select(Event).where(Event.project_id == config.project_id)).all()
        event_counts = sorted(
            session.scalars(
                select(EventMetric.count).where(
                    EventMetric.scan_config_id == config.id, EventMetric.event_id.is_not(None)
                )
            )
        )
    # The catalog was built from the folded rows and the metrics from the same
    # rows: one event per name, and each bucket's count written against it.
    assert len(events) == 2
    assert event_counts == [4, 5, 7]


def test_a_declared_lookback_keeps_its_own_catalog_read(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    warehouse, _config = _run(sync_session_factory, monkeypatch, lookback_hours=24)

    assert (warehouse.bucketed, warehouse.breakdowns) == (1, 1)
