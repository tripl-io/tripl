"""Alert-rule integration for variable value drifts."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.alerting_matching import (
    SCOPE_VARIABLE_VALUE_DRIFT,
    DriftAlertCandidate,
    rule_matches_anomaly,
)
from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers._variable_value_drift import detect_variable_value_drifts
from tripl.core.analyzers.cardinality import BreakdownAnalysis, CardinalityResult
from tripl.core.analyzers.event_generator import generate_events
from tripl.models import Base
from tripl.models.alert_rule import AlertRule
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.variable import Variable
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.worker.tasks.metrics.catalog_sync import sync_catalog
from tripl.worker.tasks.metrics.signals import _get_active_variable_value_drift_candidates


def _build_rule(**overrides: object) -> AlertRule:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "destination_id": uuid.uuid4(),
        "name": "Value drift rule",
        "enabled": True,
        "include_project_total": True,
        "include_event_types": True,
        "include_events": True,
        "include_schema_drifts": False,
        "include_distribution_drifts": False,
        "include_release_regressions": False,
        "notify_on_spike": True,
        "notify_on_drop": True,
        "min_percent_delta": 0.0,
        "min_absolute_delta": 0,
        "min_expected_count": 0,
        "cooldown_minutes": 60,
        "message_template": None,
        "items_template": None,
        "message_format": "plain",
    }
    defaults.update(overrides)
    rule = AlertRule(**defaults)
    rule.filters = []
    return rule


def _candidate() -> DriftAlertCandidate:
    return DriftAlertCandidate(
        id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        scope_type=SCOPE_VARIABLE_VALUE_DRIFT,
        scope_ref=str(uuid.uuid4()),
        event_id=uuid.uuid4(),
        event_type_id=None,
        bucket=datetime(2026, 7, 10, 12, tzinfo=UTC),
        direction="spike",
        actual_count=2,
        expected_count=0,
        drift_field="variant",
        drift_type="value_drift",
        sample_value="x, y",
    )


def test_rule_gate_requires_include_variable_value_drifts():
    candidate = _candidate()
    assert rule_matches_anomaly(_build_rule(), candidate) is False
    enabled = _build_rule(
        include_variable_value_drifts=True,
        min_percent_delta=999,
        min_absolute_delta=999,
        min_expected_count=999,
    )
    assert rule_matches_anomaly(enabled, candidate) is True


@pytest.fixture
def sync_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_candidates_built_from_active_drifts(sync_session: Session):
    project = Project(id=uuid.uuid4(), name="P", slug="vvd-alerts", description="")
    sync_session.add(project)
    sync_session.flush()
    data_source = DataSource(
        id=uuid.uuid4(),
        name="wh",
        db_type="clickhouse",
        host="localhost",
        port=9000,
        database_name="db",
        username="u",
        password_encrypted="x",
    )
    sync_session.add(data_source)
    sync_session.flush()
    config = ScanConfig(
        id=uuid.uuid4(),
        project_id=project.id,
        data_source_id=data_source.id,
        name="scan",
        base_query="SELECT * FROM events",
    )
    event_type = EventType(
        id=uuid.uuid4(), project_id=project.id, name="pv", display_name="PV", description=""
    )
    sync_session.add_all([config, event_type])
    sync_session.flush()
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=event_type.id,
        name="Onboarding",
        description="",
        order=0,
    )
    variable = Variable(
        id=uuid.uuid4(),
        project_id=project.id,
        name="variant",
        variable_type="string",
        description="",
    )
    sync_session.add_all([event, variable])
    sync_session.flush()

    active = VariableValueDrift(
        project_id=project.id,
        variable_id=variable.id,
        event_id=event.id,
        scan_config_id=config.id,
        observed_values=["x", "y"],
        detected_at=datetime.now(UTC),
    )
    snoozed_future = VariableValueDrift(
        project_id=project.id,
        variable_id=variable.id,
        event_id=event.id,
        scan_config_id=config.id,
        observed_values=["z"],
        status="snoozed",
        snoozed_until=datetime.now(UTC) + timedelta(days=5),
        detected_at=datetime.now(UTC),
    )
    # uq is (variable_id, event_id) — use a second event for the snoozed row.
    event2 = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=event_type.id,
        name="Checkout",
        description="",
        order=1,
    )
    sync_session.add(event2)
    sync_session.flush()
    snoozed_future.event_id = event2.id
    sync_session.add_all([active, snoozed_future])
    sync_session.commit()

    candidates = _get_active_variable_value_drift_candidates(sync_session, config)

    assert len(candidates) == 1
    candidate = next(iter(candidates.values()))
    assert candidate.scope_type == SCOPE_VARIABLE_VALUE_DRIFT
    assert candidate.scope_ref == str(active.id)
    assert candidate.event_id == event.id
    assert candidate.drift_field == "variant"
    assert candidate.drift_type == "value_drift"
    assert candidate.sample_value == "x, y"


class _NoContractAdapter:
    """Stands in for the warehouse, which this path never reaches.

    ``sync_catalog`` only queries the adapter to validate field contracts, and
    the fields seeded below declare none, so it returns before touching it.
    """


_SCAN_COLUMNS = [ColumnInfo("screen", "String"), ColumnInfo("variant_id", "String")]


def _variant_analysis() -> BreakdownAnalysis:
    """One named screen carrying a high-cardinality column, i.e. a variable."""
    return BreakdownAnalysis(
        results={
            "screen": CardinalityResult(
                column=_SCAN_COLUMNS[0],
                count=1,
                is_low=True,
                sample_values=["home"],
            ),
            "variant_id": CardinalityResult(
                column=_SCAN_COLUMNS[1],
                count=3,
                is_low=False,
                sample_values=["1", "2", "3"],
            ),
        },
        rows=[("home", "1"), ("home", "2"), ("home", "3")],
        reg_names=["screen", "variant_id"],
        json_names=[],
    )


def test_drift_detected_by_collection_is_stamped_with_the_scan_config(sync_session: Session):
    """Detection and dispatch must agree on provenance.

    Seeding drift rows by hand is what hid this: alert dispatch filters on
    ``scan_config_id``, so a row the collector writes without one is detected
    and then never alerted. Drive the real path instead.
    """
    project = Project(id=uuid.uuid4(), name="P", slug="vvd-collect", description="")
    sync_session.add(project)
    sync_session.flush()
    data_source = DataSource(
        id=uuid.uuid4(),
        name="wh",
        db_type="clickhouse",
        host="localhost",
        port=9000,
        database_name="db",
        username="u",
        password_encrypted="x",
    )
    sync_session.add(data_source)
    sync_session.flush()
    event_type = EventType(
        id=uuid.uuid4(), project_id=project.id, name="pv", display_name="PV", description=""
    )
    sync_session.add(event_type)
    sync_session.flush()
    config = ScanConfig(
        id=uuid.uuid4(),
        project_id=project.id,
        data_source_id=data_source.id,
        event_type_id=event_type.id,
        name="scan",
        base_query="SELECT * FROM events",
    )
    sync_session.add_all(
        [
            config,
            FieldDefinition(
                id=uuid.uuid4(),
                event_type_id=event_type.id,
                name="screen",
                display_name="Screen",
                field_type="string",
                order=0,
            ),
            FieldDefinition(
                id=uuid.uuid4(),
                event_type_id=event_type.id,
                name="variant_id",
                display_name="Variant",
                field_type="string",
                order=1,
            ),
            Variable(
                id=uuid.uuid4(),
                project_id=project.id,
                name="variant",
                variable_type="string",
                description="",
                allowed_values=["1"],
                bindings=["variant_id"],
            ),
        ]
    )
    sync_session.commit()

    now = datetime.now(UTC)
    sync_catalog(
        sync_session,
        adapter=_NoContractAdapter(),
        config=config,
        columns=_SCAN_COLUMNS,
        skip_cols=set(),
        json_value_path_map={},
        scan_row_limit=50000,
        metrics_row_limit=50000,
        time_from_dt=now - timedelta(hours=1),
        time_to_dt=now,
        catalog_scan_window=None,
        is_replay=False,
        analyze_cardinality_fn=lambda *args, **kwargs: _variant_analysis(),
        analyze_cardinality_grouped_fn=lambda *args, **kwargs: ([], {}),
        generate_events_fn=generate_events,
    )
    sync_session.commit()

    drift = sync_session.execute(select(VariableValueDrift)).scalar_one()
    assert drift.scan_config_id == config.id
    assert drift.observed_values == ["2", "3"]

    candidates = _get_active_variable_value_drift_candidates(sync_session, config)
    assert len(candidates) == 1
    assert next(iter(candidates.values())).scope_ref == str(drift.id)


def test_rescan_without_a_scan_config_keeps_the_existing_attribution(sync_session: Session):
    """Provenance is written once and never downgraded."""
    project = Project(id=uuid.uuid4(), name="P", slug="vvd-keep", description="")
    sync_session.add(project)
    sync_session.flush()
    data_source = DataSource(
        id=uuid.uuid4(),
        name="wh",
        db_type="clickhouse",
        host="localhost",
        port=9000,
        database_name="db",
        username="u",
        password_encrypted="x",
    )
    sync_session.add(data_source)
    sync_session.flush()
    event_type = EventType(
        id=uuid.uuid4(), project_id=project.id, name="pv", display_name="PV", description=""
    )
    config = ScanConfig(
        id=uuid.uuid4(),
        project_id=project.id,
        data_source_id=data_source.id,
        name="scan",
        base_query="SELECT * FROM events",
    )
    sync_session.add_all([event_type, config])
    sync_session.flush()
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=event_type.id,
        name="Onboarding",
        description="",
        order=0,
    )
    variable = Variable(
        id=uuid.uuid4(),
        project_id=project.id,
        name="variant",
        variable_type="string",
        description="",
        allowed_values=["a"],
    )
    field_definition = FieldDefinition(
        id=uuid.uuid4(),
        event_type_id=event_type.id,
        name="screen",
        display_name="Screen",
        field_type="string",
        order=0,
    )
    sync_session.add_all([event, variable, field_definition])
    sync_session.commit()

    def _detect(scan_config_id: uuid.UUID | None, values: list[str]) -> None:
        detect_variable_value_drifts(
            sync_session,
            project_id=project.id,
            branch_id=variable.branch_id,
            scan_config_id=scan_config_id,
            contexts={
                (variable.id, event.id, field_definition.id): {
                    "variable_id": variable.id,
                    "event_id": event.id,
                    "field_definition_id": field_definition.id,
                    "source_column": "screen",
                    "value_kind": "low",
                    "observed_count": len(values),
                    "values": values,
                }
            },
        )
        sync_session.commit()

    _detect(config.id, ["a", "x"])
    _detect(None, ["a", "x", "z"])

    # The upsert bypasses the ORM identity map — expire before re-reading.
    sync_session.expire_all()
    drift = sync_session.execute(select(VariableValueDrift)).scalar_one()
    assert drift.observed_values == ["x", "z"]
    assert drift.scan_config_id == config.id

    # A second config scanning the same pair takes the row over. Detection and
    # dispatch run in one task per config, so the run that saw the drift has to
    # be the one whose alert query matches it; freezing the first writer would
    # strand the row on a config that may never dispatch again.
    other_config = ScanConfig(
        id=uuid.uuid4(),
        project_id=project.id,
        data_source_id=data_source.id,
        name="second scan",
        base_query="SELECT * FROM events",
    )
    sync_session.add(other_config)
    sync_session.commit()

    _detect(other_config.id, ["a", "x", "q"])

    sync_session.expire_all()
    drift = sync_session.execute(select(VariableValueDrift)).scalar_one()
    assert drift.scan_config_id == other_config.id
