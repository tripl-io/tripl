"""Regression tests for batch 13: event generator, variables and name templates."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers._event_generator_merge import _merge_event_into_group
from tripl.core.analyzers._event_generator_variables import (
    VariableIndex,
    build_variable_index,
    ensure_variable,
)
from tripl.core.analyzers.cardinality import BreakdownAnalysis, CardinalityResult
from tripl.core.analyzers.event_generator import lock_project_catalog
from tripl.core.analyzers.event_plan import plan_column_meta
from tripl.core.analyzers.variable_detector import detect_variables
from tripl.core.name_template import resolve_dotted_keys
from tripl.models import Base
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.event import Event
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.variable import VARIABLE_NAME_MAX_LENGTH, Variable
from tripl.models.variable_value import VariableValue
from tripl.schemas.variable import VariableUpdate
from tripl.services.scan_config_lookup import configs_naming_column
from tripl.worker.utils.reserved_columns import reserved_catalog_columns


@pytest.fixture
def sync_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _project(session: Session) -> Project:
    project = Project(id=uuid.uuid4(), name="Batch 13", slug=f"b13-{uuid.uuid4().hex[:8]}")
    session.add(project)
    session.flush()
    return project


def test_adoption_does_not_backfill_a_source_name_another_variable_holds(
    sync_session: Session,
) -> None:
    project = _project(sync_session)
    branch_id = uuid.uuid4()
    renamed = Variable(
        id=uuid.uuid4(),
        project_id=project.id,
        branch_id=branch_id,
        name="screen_name",
        source_name="screen",
        variable_type="string",
        bindings=[],
    )
    hand_made = Variable(
        id=uuid.uuid4(),
        project_id=project.id,
        branch_id=branch_id,
        name="screen",
        variable_type="string",
        bindings=[],
    )
    sync_session.add_all([renamed, hand_made])
    sync_session.commit()
    index = VariableIndex([renamed, hand_made])
    assert index.resolve("screen") is hand_made

    created = ensure_variable(
        sync_session, project.id, "screen", "string", branch_id=branch_id, index=index
    )
    sync_session.commit()

    assert created == 0
    assert hand_made.source_name is None
    assert renamed.source_name == "screen"


def test_json_path_longer_than_the_variable_column_is_skipped_and_reported() -> None:
    long_key = "k" * VARIABLE_NAME_MAX_LENGTH
    analysis = BreakdownAnalysis(
        results={
            "props": CardinalityResult(
                column=ColumnInfo("props", "JSON"),
                count=1,
                is_low=False,
                json_path_combos=[("short", long_key)],
            )
        },
        reg_names=[],
        json_names=["props"],
        json_value_names=[],
        rows=[],
    )

    col_meta, needed, details, _ = plan_column_meta(analysis, {"props": uuid.uuid4()})

    assert [need.name for need in needed] == ["props.short"]
    assert [obs.name for obs in col_meta["props"]["variable_observations"]] == ["props.short"]
    assert any("longer than" in detail for detail in details)


def test_catalog_lock_is_taken_on_postgresql_only() -> None:
    statements: list[str] = []

    class _Dialect:
        def __init__(self, name: str) -> None:
            self.name = name

    class _Bind:
        def __init__(self, name: str) -> None:
            self.dialect = _Dialect(name)

    class _Session:
        def __init__(self, name: str) -> None:
            self._bind = _Bind(name)

        def get_bind(self) -> _Bind:
            return self._bind

        def execute(self, statement: Any, params: Any = None) -> None:
            statements.append(str(statement))

    project_id = uuid.uuid4()
    lock_project_catalog(_Session("sqlite"), project_id)  # type: ignore[arg-type]
    assert statements == []
    lock_project_catalog(_Session("postgresql"), project_id)  # type: ignore[arg-type]
    assert statements == ["SELECT pg_advisory_xact_lock(:namespace, :key)"]


def test_group_merge_keeps_a_value_drift_items_own_reference(sync_session: Session) -> None:
    project = _project(sync_session)
    event_type_id = uuid.uuid4()
    source = Event(id=uuid.uuid4(), project_id=project.id, event_type_id=event_type_id, name="Home")
    target = Event(
        id=uuid.uuid4(), project_id=project.id, event_type_id=event_type_id, name="Group"
    )
    sync_session.add_all([source, target])
    sync_session.flush()
    drift_id = str(uuid.uuid4())

    def _item(scope_type: str, scope_ref: str, scope_name: str) -> AlertDeliveryItem:
        return AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=uuid.uuid4(),
            event_id=source.id,
            scope_type=scope_type,
            scope_ref=scope_ref,
            scope_name=scope_name,
            bucket=datetime(2026, 9, 1, tzinfo=UTC),
            direction="drop",
            actual_count=0.0,
            expected_count=1.0,
            absolute_delta=1.0,
            percent_delta=100.0,
        )

    drift_item = _item("variable_value_drift", drift_id, "Home.locale")
    event_item = _item("event", str(source.id), "Home")
    sync_session.add_all([drift_item, event_item])
    sync_session.flush()

    _merge_event_into_group(sync_session, source=source, target=target, cardinality_threshold=100)
    sync_session.expire_all()

    drift_row = sync_session.get(AlertDeliveryItem, drift_item.id)
    event_row = sync_session.get(AlertDeliveryItem, event_item.id)
    assert drift_row is not None and event_row is not None
    assert (drift_row.event_id, drift_row.scope_ref, drift_row.scope_name) == (
        target.id,
        drift_id,
        "Home.locale",
    )
    assert (event_row.event_id, event_row.scope_ref, event_row.scope_name) == (
        target.id,
        str(target.id),
        "Group",
    )


def test_variable_index_does_not_load_value_contexts(sync_session: Session) -> None:
    project = _project(sync_session)
    variable = Variable(
        id=uuid.uuid4(), project_id=project.id, name="screen", variable_type="string"
    )
    sync_session.add(variable)
    sync_session.add(
        VariableValue(
            id=uuid.uuid4(),
            project_id=project.id,
            variable_id=variable.id,
            event_id=uuid.uuid4(),
            field_definition_id=uuid.uuid4(),
            source_column="screen",
            values=["home"],
        )
    )
    sync_session.commit()
    sync_session.expunge_all()

    index = build_variable_index(sync_session, project_id=project.id, branch_id=None)

    loaded = index.resolve("screen")
    assert loaded is not None
    assert "value_contexts" in inspect(loaded).unloaded


def test_a_real_dotted_column_in_the_name_format_is_not_reserved() -> None:
    config = ScanConfig(
        time_column="ts",
        event_name_format="{params.screen}",
        event_group_rules=[{"conditions": [{"field": "params.screen", "pattern": "^x"}]}],
    )
    assert reserved_catalog_columns(config) == {"ts"}
    assert configs_naming_column([config], "params.screen") == [config]
    assert configs_naming_column([config], "params") == [config]


def test_two_numeric_path_positions_get_distinct_variables() -> None:
    values = [f"/users/{i}/posts/{i * 7 + 1000}" for i in range(200)]

    pattern = detect_variables("url", values, cardinality_threshold=100)

    assert pattern is not None
    names = [variable.name for variable in pattern.variables]
    assert len(names) == 2
    assert len(set(names)) == 2
    assert pattern.template == f"/users/${{{names[0]}}}/posts/${{{names[1]}}}"


def test_dotted_keys_render_json_values_as_the_scan_does() -> None:
    values = {"action": "tap", "payload": '{"is_premium": true, "plan": null, "n": 2.0}'}

    resolved = resolve_dotted_keys(
        "{action}_{payload.is_premium}_{payload.plan}_{payload.n}", values
    )

    assert resolved["payload.is_premium"] == "true"
    assert resolved["payload.plan"] == "null"
    assert resolved["payload.n"] == "2"


def test_update_schema_accepts_names_and_bindings_the_scan_writes() -> None:
    update = VariableUpdate(name="userId", bindings=["props.$os", "props.utm source"])
    assert update.name == "userId"
    with pytest.raises(ValueError):
        VariableUpdate(bindings=["bad}binding"])


async def _project_via_api(client: AsyncClient, slug: str) -> None:
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_saving_a_scan_created_variable_resends_its_bindings(client: AsyncClient) -> None:
    from tripl.tests.conftest import TestSessionLocal

    slug = "b13-resave"
    await _project_via_api(client, slug)
    created = await client.post(
        f"/api/v1/projects/{slug}/variables", json={"name": "os", "variable_type": "string"}
    )
    assert created.status_code == 201
    variable_id = created.json()["id"]
    async with TestSessionLocal() as session:
        variable = await session.get(Variable, uuid.UUID(variable_id))
        assert variable is not None
        variable.bindings = ["props.$os"]
        variable.source_name = "props.$os"
        await session.commit()

    unchanged = await client.patch(
        f"/api/v1/projects/{slug}/variables/{variable_id}",
        json={"name": "os", "description": "documented", "bindings": ["props.$os"]},
    )
    assert unchanged.status_code == 200, unchanged.text

    added = await client.patch(
        f"/api/v1/projects/{slug}/variables/{variable_id}",
        json={"bindings": ["props.$os", "props.$browser"]},
    )
    assert added.status_code == 422


# --- API side ------------------------------------------


@pytest.mark.asyncio
async def test_tokens_cannot_be_claimed_by_two_variables(client: AsyncClient) -> None:
    slug = "b13-tokens"
    await _project_via_api(client, slug)
    base = f"/api/v1/projects/{slug}/variables"
    screen = await client.post(base, json={"name": "screen", "variable_type": "string"})
    assert screen.status_code == 201

    binding_on_a_name = await client.post(
        base, json={"name": "page", "variable_type": "string", "bindings": ["screen"]}
    )
    assert binding_on_a_name.status_code == 409

    bound = await client.post(
        base, json={"name": "view", "variable_type": "string", "bindings": ["page_view"]}
    )
    assert bound.status_code == 201
    name_on_a_binding = await client.post(
        base, json={"name": "page_view", "variable_type": "string"}
    )
    assert name_on_a_binding.status_code == 409

    rename_onto_binding = await client.patch(
        f"{base}/{screen.json()['id']}", json={"name": "page_view"}
    )
    assert rename_onto_binding.status_code == 409
