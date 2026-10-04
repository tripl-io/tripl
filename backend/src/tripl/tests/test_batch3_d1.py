"""Schema-drift upsert regressions: type-name width and provenance (batch 3).

Two defects on the same write path, both in
``worker/tasks/metrics/schema_drift._upsert_schema_drifts``:

* the raw warehouse type name went into ``observed_type``
  (``String(128)``) untruncated. A labelled ClickHouse ``Enum8`` or a nested
  ``Map(String, Tuple(...))`` renders well past 128 characters, so on Postgres
  the drift upsert raised and took the whole catalog sync down with it. The
  Postgres DataError itself is unreachable here (the suite runs on SQLite, which
  ignores ``VARCHAR(n)``), so what is pinned is the invariant behind it: what
  lands in the row fits the column. That goes red on revert because SQLite
  happily stores the full 287-character string.
* the ON CONFLICT branches wrote ``scan_config_id =
  excluded.scan_config_id`` unconditionally, so a NULL provenance would blank
  the column that alerting (``signals.py``), detection reset
  (``detection_reset_service.py``) and demo pruning (``demo_runtime.py``) all
  filter on. That one is fully exercisable on SQLite — it is an upsert SET
  clause, not concurrency or FK enforcement. The Postgres branch is covered by
  compiling its statement, since CI has no Postgres.
"""

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.schema_drift import SchemaDrift
from tripl.services.schema_drift_service import _logical_type_from_observed
from tripl.worker.tasks.metrics import schema_drift as metrics_schema_drift

_OBSERVED_TYPE_WIDTH = cast(Any, SchemaDrift.__table__.c.observed_type.type).length


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch3_d1.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _create_scan_config(session: Session) -> ScanConfig:
    project = Project(
        id=uuid.uuid4(),
        name="Drift Project",
        slug=f"drift-{uuid.uuid4().hex[:8]}",
        description="",
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"Drift DS {uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    session.add_all([project, data_source])
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Structured Events",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
    )
    session.add(config)
    session.commit()
    return config


def _make_event_type_with_fields(
    session: Session,
    config: ScanConfig,
    *,
    fields: list[tuple[str, str]],
) -> EventType:
    et = EventType(
        id=uuid.uuid4(),
        project_id=config.project_id,
        name="drift_subject",
        display_name="Drift Subject",
        description="",
    )
    session.add(et)
    session.flush()
    for name, field_type in fields:
        session.add(
            FieldDefinition(
                id=uuid.uuid4(),
                event_type_id=et.id,
                name=name,
                display_name=name,
                field_type=field_type,
                is_required=False,
                description="",
            )
        )
    session.flush()
    session.refresh(et)
    return et


def _long_enum_type() -> str:
    """A labelled ClickHouse Enum8 as clickhouse-connect renders it: ~287 chars."""
    return "Enum8(" + ", ".join(f"'checkout_step_{i}' = {i}" for i in range(1, 13)) + ")"


def _long_map_type() -> str:
    """A nested Map whose full spec also runs past the column width."""
    inner = ", ".join(f"attribute_{i} String" for i in range(1, 12))
    return f"Map(String, Tuple({inner}))"


def _drift_row(session: Session, event_type_id: uuid.UUID, field_name: str) -> SchemaDrift:
    return session.execute(
        select(SchemaDrift).where(
            SchemaDrift.event_type_id == event_type_id,
            SchemaDrift.field_name == field_name,
        )
    ).scalar_one()


class _PostgresBind:
    class dialect:
        name = "postgresql"


class _CapturingSession:
    """Just enough Session for `_upsert_schema_drifts` to take its Postgres branch."""

    bind = _PostgresBind()

    def __init__(self) -> None:
        self.statements: list[object] = []

    def execute(self, statement: object) -> None:
        self.statements.append(statement)


def test_observed_type_bound_matches_the_schema_drift_column() -> None:
    """The truncation bound and the column width must not drift apart.

    Widening `schema_drifts.observed_type` without moving the constant would
    silently keep truncating at 128; narrowing it would start raising again.
    """
    assert metrics_schema_drift._OBSERVED_TYPE_MAX_LEN == _OBSERVED_TYPE_WIDTH


def test_upsert_schema_drifts_fits_a_long_warehouse_type_into_the_column(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        et = _make_event_type_with_fields(session, config, fields=[("payload", "string")])

        long_type = _long_enum_type()
        # Guard the fixture: if clickhouse type rendering ever gets shorter here,
        # this test would stop exercising the bug instead of failing loudly.
        assert len(long_type) > _OBSERVED_TYPE_WIDTH

        drift_items = metrics_schema_drift._diff_event_type_schema(
            et,
            [
                ColumnInfo(name="payload", type_name="String"),
                ColumnInfo(name="funnel_step", type_name=long_type),
            ],
            skip_columns=set(),
        )
        assert [(item["field_name"], item["drift_type"]) for item in drift_items] == [
            ("funnel_step", "new_field")
        ]
        # The diff helper still reports the true warehouse type; only the write
        # bounds it.
        assert drift_items[0]["observed_type"] == long_type

        metrics_schema_drift._upsert_schema_drifts(
            session,
            event_type_id=et.id,
            scan_config_id=config.id,
            drift_items=drift_items,
        )
        session.commit()

        row = _drift_row(session, et.id, "funnel_step")
        assert row.observed_type is not None
        assert len(row.observed_type) <= _OBSERVED_TYPE_WIDTH
        # Truncated from the tail, so the discriminating head survives, and
        # visibly rather than silently.
        assert row.observed_type.startswith("Enum8('checkout_step_1' = 1")
        assert row.observed_type.endswith("…")


def test_upsert_schema_drifts_bounds_a_long_type_changed_warehouse_type(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The other producer of a raw type name: `payload` declared string, observed Map."""
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        et = _make_event_type_with_fields(session, config, fields=[("payload", "string")])

        long_type = _long_map_type()
        assert len(long_type) > _OBSERVED_TYPE_WIDTH

        drift_items = metrics_schema_drift._diff_event_type_schema(
            et,
            [ColumnInfo(name="payload", type_name=long_type)],
            skip_columns=set(),
        )
        assert [(item["field_name"], item["drift_type"]) for item in drift_items] == [
            ("payload", "type_changed")
        ]

        metrics_schema_drift._upsert_schema_drifts(
            session,
            event_type_id=et.id,
            scan_config_id=config.id,
            drift_items=drift_items,
        )
        session.commit()

        row = _drift_row(session, et.id, "payload")
        assert row.observed_type is not None
        assert len(row.observed_type) <= _OBSERVED_TYPE_WIDTH
        assert row.observed_type.startswith("Map(String, Tuple(")
        # Accepting this drift must still resolve to the same logical field type
        # as the untruncated name would — the marker sits in the head.
        assert _logical_type_from_observed(row.observed_type) == "json"
        assert _logical_type_from_observed(long_type) == "json"


def test_upsert_schema_drifts_leaves_short_observed_types_untouched(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Contract-violation drifts carry a ~80-char summary that must survive whole."""
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        et = _make_event_type_with_fields(session, config, fields=[("payload", "string")])
        summary = "bad_rate=20.00%; max=1.00%; bad=200; total=1000"

        metrics_schema_drift._upsert_schema_drifts(
            session,
            event_type_id=et.id,
            scan_config_id=config.id,
            drift_items=[
                {
                    "field_name": "payload",
                    "drift_type": "required_null_violation",
                    "observed_type": summary,
                    "declared_type": "required",
                    "sample_value": None,
                }
            ],
        )
        session.commit()

        assert _drift_row(session, et.id, "payload").observed_type == summary


def test_upsert_schema_drifts_never_blanks_scan_config_provenance(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A re-upsert must not NULL the scan config the drift came from.

    Alerting, detection reset and demo pruning all filter on `scan_config_id`;
    a row that loses it is invisible to every one of them.
    """
    with sync_session_factory() as session:
        config = _create_scan_config(session)
        et = _make_event_type_with_fields(session, config, fields=[("payload", "string")])
        drift_items: list[dict[str, object]] = [
            {
                "field_name": "device_id",
                "drift_type": "new_field",
                "observed_type": "String",
                "declared_type": None,
                "sample_value": None,
            }
        ]

        metrics_schema_drift._upsert_schema_drifts(
            session,
            event_type_id=et.id,
            scan_config_id=config.id,
            drift_items=drift_items,
        )
        session.commit()
        first_detected_at = _drift_row(session, et.id, "device_id").detected_at

        # Deliberately exercising the SQL guard, not a supported call: the
        # parameter is typed non-optional so mypy rejects this at any real caller.
        metrics_schema_drift._upsert_schema_drifts(
            session,
            event_type_id=et.id,
            scan_config_id=cast(uuid.UUID, None),
            drift_items=drift_items,
        )
        session.commit()
        session.expire_all()

        row = _drift_row(session, et.id, "device_id")
        assert row.scan_config_id == config.id
        # coalesce(new, old) must not have frozen the rest of the row: the
        # detected_at refresh is the point of the upsert.
        assert row.detected_at > first_detected_at

        # The recovery direction: `ondelete="SET NULL"` can legitimately have
        # blanked provenance already, and a fresh scan must be able to restore
        # it. coalesce(old, new) would pin the NULL forever.
        row.scan_config_id = None
        session.commit()
        metrics_schema_drift._upsert_schema_drifts(
            session,
            event_type_id=et.id,
            scan_config_id=config.id,
            drift_items=drift_items,
        )
        session.commit()
        session.expire_all()

        assert _drift_row(session, et.id, "device_id").scan_config_id == config.id


def test_upsert_schema_drifts_postgres_branch_coalesces_provenance() -> None:
    """The Postgres branch is unreachable in CI, so compile it instead.

    This also proves the COALESCE renders at all for the pg dialect, and that
    the right-hand side is the existing row rather than the value being written.
    """
    session = _CapturingSession()

    metrics_schema_drift._upsert_schema_drifts(
        cast(Session, session),
        event_type_id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        drift_items=[
            {
                "field_name": "device_id",
                "drift_type": "new_field",
                "observed_type": "String",
                "declared_type": None,
                "sample_value": None,
            }
        ],
    )

    assert len(session.statements) == 1
    sql = str(cast(Any, session.statements[0]).compile(dialect=postgresql.dialect())).replace(
        "\n", " "
    )
    assert "ON CONFLICT ON CONSTRAINT uq_schema_drift_event_type_field_kind" in sql
    assert "scan_config_id = coalesce(excluded.scan_config_id, schema_drifts.scan_config_id)" in sql
    # Everything else still takes the incoming value — notably the detected_at refresh.
    assert "detected_at = excluded.detected_at" in sql
    assert "observed_type = excluded.observed_type" in sql
