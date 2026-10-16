"""The demo window clamp, wired: a real collection over a demo source honours it.

``test_demo_v5_runtime`` pins ``clamp_demo_window_start`` as a function. These
drive the collector itself (``execute_collect_metrics``, the body of
``collect_metrics``) over a ``demo`` source on the in-memory engine, so they
fail if the collector stops calling it. The in-memory engine answers any hour,
older than the retention too, so nothing but that call moves a window:

* without it a replay reaching past the warehouse's horizon keeps the start it
  asked for, and the summary's ``time_from`` says so;
* without it a window wholly older than the horizon is collected — as zeros on
  PostgreSQL, whose dropped partitions hold nothing — instead of refused.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

# Imported first and for its side effect: the worker task package is
# import-order sensitive (see test_demo_v5_runtime).
import tripl.worker.celery_app  # noqa: F401
from tripl.config import settings
from tripl.core.bucketing import to_utc
from tripl.core.demo_warehouse import EVENTS_SCAN_SQL
from tripl.core.demo_warehouse import status as warehouse_status_module
from tripl.models import Base
from tripl.models.data_source import DataSource, DBType
from tripl.models.event_metric import EventMetric
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.worker.tasks._errors import ScanError
from tripl.worker.tasks.metrics import demo_window
from tripl.worker.tasks.metrics import tasks as metrics_tasks

_HOUR = timedelta(hours=1)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'demo_window_wiring.db'}")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _memory_warehouse(monkeypatch: MonkeyPatch) -> Iterator[None]:
    """The in-memory engine, deterministically: no connect attempt anywhere."""
    monkeypatch.setattr(settings, "demo_warehouse_mode", "memory")
    warehouse_status_module.clear_cache()
    yield
    warehouse_status_module.clear_cache()


def _demo_config(factory: sessionmaker[Session]) -> uuid.UUID:
    """A demo project's source and scan config, as the demo builder writes them."""
    tag = uuid.uuid4().hex[:8]
    with factory() as session:
        project = Project(
            id=uuid.uuid4(),
            name=f"Window wiring {tag}",
            slug=f"window-wiring-{tag}",
            description="",
            is_demo=True,
        )
        session.add(project)
        session.flush()
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Window wiring DS {tag}",
            db_type=DBType.demo.value,
            host="demo-warehouse",
            port=0,
            database_name="demo",
            username="",
            password_encrypted="",
            project_id=project.id,
        )
        session.add(data_source)
        session.flush()
        config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            name=f"Demo scan {tag}",
            base_query=EVENTS_SCAN_SQL,
            time_column="event_time",
            event_type_column="event_type",
            event_name_format="{event_name}",
            interval="1h",
            replay_chunk_interval="1d",
            auto_create_events=False,
        )
        session.add(config)
        session.commit()
        return config.id


def _horizon() -> datetime:
    return demo_window.demo_warehouse_horizon(datetime.now(UTC))


def test_a_replay_reaching_past_the_retention_starts_at_the_horizon(
    sync_session_factory: sessionmaker[Session],
) -> None:
    config_id = _demo_config(sync_session_factory)
    before = _horizon()
    with sync_session_factory() as session:
        summary = metrics_tasks.execute_collect_metrics(
            session,
            str(config_id),
            time_from=(before - 2 * _HOUR).isoformat(),
            time_to=(before + 3 * _HOUR).isoformat(),
            skip_detection=True,
        )
    # The horizon moves with the clock: an hour turning mid-test moves it by one.
    after = _horizon()

    assert summary["mode"] == metrics_tasks.METRICS_REPLAY_MODE
    assert to_utc(datetime.fromisoformat(str(summary["time_from"]))) in {before, after}
    with sync_session_factory() as session:
        [job] = session.scalars(select(ScanJob).where(ScanJob.scan_config_id == config_id)).all()
        assert job.status == ScanJobStatus.completed.value


def test_a_window_the_warehouse_no_longer_holds_is_refused_not_stored(
    sync_session_factory: sessionmaker[Session],
) -> None:
    config_id = _demo_config(sync_session_factory)
    horizon = _horizon()
    with sync_session_factory() as session, pytest.raises(ScanError, match="holds data from"):
        metrics_tasks.execute_collect_metrics(
            session,
            str(config_id),
            time_from=(horizon - 6 * _HOUR).isoformat(),
            time_to=(horizon - 2 * _HOUR).isoformat(),
            skip_detection=True,
        )

    with sync_session_factory() as session:
        [job] = session.scalars(select(ScanJob).where(ScanJob.scan_config_id == config_id)).all()
        assert job.status == ScanJobStatus.failed.value
        assert job.error_message is not None and "holds data from" in job.error_message
        stored = session.scalar(
            select(func.count())
            .select_from(EventMetric)
            .where(EventMetric.scan_config_id == config_id)
        )
        assert stored == 0
