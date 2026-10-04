"""Worker regression checks for project-wide variable retirement."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.scan_config import ScanConfig
from tripl.models.variable import Variable
from tripl.tests.test_metrics_tasks import (
    _create_scan_config,
    _seed_scan_created_variable,
)
from tripl.worker import variable_sweep


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch12_worker.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def test_sibling_config_without_lookback_defers_project_wide_scalar_retirement(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        bounded = _create_scan_config(session, with_event_type=True)
        bounded.scan_lookback_hours = 24
        sibling = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=bounded.data_source_id,
            project_id=bounded.project_id,
            event_type_id=bounded.event_type_id,
            name="Sibling interval scan",
            base_query=bounded.base_query,
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add(sibling)
        scalar = _seed_scan_created_variable(
            session, bounded, source_name="campaign", column_type="string"
        )
        scalar_id = scalar.id
        project_id = bounded.project_id
        session.commit()

    with sync_session_factory() as session:
        assert (
            variable_sweep.retire_unused_variables(session, project_id=project_id, branch_id=None)
            == 0
        )
        assert session.get(Variable, scalar_id) is not None
        session.get(ScanConfig, sibling.id).scan_lookback_hours = 24
        session.commit()
        assert (
            variable_sweep.retire_unused_variables(session, project_id=project_id, branch_id=None)
            == 1
        )
        assert session.get(Variable, scalar_id) is None
