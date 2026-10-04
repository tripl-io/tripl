"""Retention of operational rows in regular projects."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.worker.tasks import maintenance


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'retention.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _scan_config(session: Session) -> ScanConfig:
    project = Project(id=uuid.uuid4(), name="Regular", slug="regular", description="")
    source = DataSource(
        id=uuid.uuid4(),
        name="Warehouse",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    config = ScanConfig(
        id=uuid.uuid4(),
        project_id=project.id,
        data_source_id=source.id,
        name="Scans",
        base_query="SELECT time FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
    )
    session.add_all([project, source, config])
    session.commit()
    return config


def test_cleanup_scan_jobs_keeps_active_and_recent_jobs(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maintenance, "_get_sync_session", session_factory)
    monkeypatch.setattr(maintenance.settings, "scan_job_retention_days", 30)
    now = datetime.now(UTC)
    with session_factory() as session:
        config = _scan_config(session)
        rows = [
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                status=status,
                created_at=now - timedelta(days=age),
                updated_at=now - timedelta(days=updated_age),
                completed_at=(
                    now - timedelta(days=completed_age) if completed_age is not None else None
                ),
            )
            for status, age, updated_age, completed_age in [
                (ScanJobStatus.completed.value, 40, 31, 31),
                (ScanJobStatus.failed.value, 40, 40, None),
                (ScanJobStatus.completed.value, 1, 1, 1),
                (ScanJobStatus.pending.value, 40, 40, None),
                (ScanJobStatus.running.value, 40, 40, None),
                # A long-running scan must not disappear just after completion.
                (ScanJobStatus.completed.value, 40, 1, 1),
                # Legacy terminal jobs with no completed_at use updated_at.
                (ScanJobStatus.cancelled.value, 40, 1, None),
            ]
        ]
        session.add_all(rows)
        session.commit()

    result = maintenance.cleanup_scan_jobs.run()

    assert result["deleted"] == 2
    with session_factory() as session:
        remaining = session.scalars(select(ScanJob)).all()
    assert {row.id for row in remaining} == {row.id for row in rows[2:]}


def test_cleanup_distribution_drifts_uses_band_specific_horizons(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maintenance, "_get_sync_session", session_factory)
    monkeypatch.setattr(maintenance.settings, "distribution_drift_retention_days", 90)
    monkeypatch.setattr(maintenance.settings, "distribution_drift_minor_retention_days", 30)
    now = datetime.now(UTC)
    with session_factory() as session:
        config = _scan_config(session)
        rows = [
            DistributionDrift(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                field_name=f"field_{index}",
                bucket=now - timedelta(days=age),
                psi=0.2,
                band=band,
                baseline_total=100,
                current_total=100,
            )
            for index, (band, age) in enumerate(
                [
                    ("stable", 31),
                    ("minor", 31),
                    ("significant", 91),
                    ("significant", 31),
                    ("minor", 1),
                ]
            )
        ]
        session.add_all(rows)
        session.commit()

    result = maintenance.cleanup_distribution_drifts.run()

    assert result["deleted"] == 3
    with session_factory() as session:
        remaining = session.scalars(select(DistributionDrift)).all()
    assert {row.id for row in remaining} == {rows[3].id, rows[4].id}
