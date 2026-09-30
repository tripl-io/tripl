"""Regressions for the variables leftovers batch (2026-09-25).

tripl-p5ac, tripl-ifuv, tripl-nluj and tripl-0zpq.370.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import create_engine, false, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.cardinality import BreakdownAnalysis, CardinalityResult
from tripl.core.analyzers.event_generator import merge_existing_events_for_group_rules
from tripl.core.analyzers.event_plan import plan_events
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue
from tripl.schemas.data_source import DataSourceUpdate
from tripl.schemas.metric_definition import SqlConfig
from tripl.services import datasource_service
from tripl.services._plan_branch_renames import pair_renames
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_plan_branches import (
    _approve_and_merge,
    _attach_variable_values,
    _create_branch,
    _main_branch_id,
    _main_variables_by_source,
    _seed_main_variables,
    _seed_plan,
)


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


# --- tripl-p5ac: a rule condition on a JSON column --------------------------

_JSON_COLUMN_RULE = [{"name": "Grouped", "conditions": [{"field": "payload", "pattern": "screen"}]}]


def test_a_json_column_condition_groups_but_keeps_the_planned_template() -> None:
    analysis = BreakdownAnalysis(
        results={
            "payload": CardinalityResult(
                column=ColumnInfo("payload", "JSON"),
                count=1,
                is_low=False,
                json_path_combos=[("action", "screen")],
            )
        },
        rows=[(["action", "screen"], "checkout", 7)],
        reg_names=[],
        json_names=["payload"],
        json_value_names=["payload.action"],
    )

    plan = plan_events(analysis, {"payload": uuid.uuid4()}, event_group_rules=_JSON_COLUMN_RULE)

    assert [event.name for event in plan.events] == ["Grouped"]
    ((_fd_id, col_name, value),) = plan.events[0].field_values
    assert col_name == "payload"
    assert value == '{"action": "checkout", "screen": "${payload.screen}"}'


def test_grouping_existing_events_on_a_json_column_keeps_their_contexts(
    sync_session: Session,
) -> None:
    project = Project(id=uuid.uuid4(), name="P5ac", slug=f"p5ac-{uuid.uuid4().hex[:8]}")
    sync_session.add(project)
    sync_session.flush()
    event_type = EventType(
        id=uuid.uuid4(), project_id=project.id, name="Page", display_name="Page", description=""
    )
    sync_session.add(event_type)
    sync_session.flush()
    payload = FieldDefinition(
        id=uuid.uuid4(),
        event_type_id=event_type.id,
        name="payload",
        display_name="payload",
        field_type="json",
        order=0,
    )
    variable = Variable(
        id=uuid.uuid4(),
        project_id=project.id,
        name="payload.screen",
        source_name="payload.screen",
        variable_type="string",
    )
    source = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=event_type.id,
        name="checkout",
        source_name="checkout",
        order=0,
    )
    sync_session.add_all([payload, variable, source])
    sync_session.flush()
    template = '{"action": "checkout", "screen": "${payload.screen}"}'
    sync_session.add(
        EventFieldValue(
            id=uuid.uuid4(),
            event_id=source.id,
            field_definition_id=payload.id,
            value=template,
        )
    )
    context = VariableValue(
        id=uuid.uuid4(),
        project_id=project.id,
        variable_id=variable.id,
        event_id=source.id,
        field_definition_id=payload.id,
        source_column="payload.screen",
        value_kind="high",
        observed_count=2,
        values=["home", "cart"],
    )
    sync_session.add(context)
    sync_session.commit()

    merged = merge_existing_events_for_group_rules(
        sync_session,
        project_id=project.id,
        event_type_ids=[event_type.id],
        event_group_rules=_JSON_COLUMN_RULE,
    )
    sync_session.commit()
    sync_session.expire_all()

    assert merged == 1
    group = sync_session.execute(select(Event).where(Event.name == "Grouped")).scalar_one()
    group_value = sync_session.execute(
        select(EventFieldValue.value).where(EventFieldValue.event_id == group.id)
    ).scalar_one()
    assert group_value == template
    surviving = sync_session.execute(select(VariableValue)).scalars().all()
    assert [(row.variable_id, row.event_id) for row in surviving] == [(variable.id, group.id)]
    assert surviving[0].values == ["home", "cart"]


# --- tripl-ifuv: delete b, rename a onto b ----------------------------------


def test_pair_renames_vacates_only_a_destination_the_branch_deleted() -> None:
    base = {("a",): "s1", ("b",): "s2"}
    branch = {("b",): "s1"}

    assert pair_renames(base, base, branch, vacate_removed=True) == {("a",): ("b",)}
    # The default keeps the old answer for events.
    assert pair_renames(base, base, branch) == {}
    # No identity on the occupant: the branch's ``b`` may be the occupant edited.
    anonymous = {("a",): "s1", ("b",): None}
    assert pair_renames(anonymous, anonymous, branch, vacate_removed=True) == {}
    # Main re-identified the occupant after the cut.
    assert pair_renames(base, {("a",): "s1", ("b",): "s9"}, branch, vacate_removed=True) == {}
    # The branch still carries the occupant's identity: not a deletion.
    kept = {("b",): "s1", ("z",): "s2"}
    assert pair_renames(base, {**base, ("z",): "s3"}, kept, vacate_removed=True) == {}


@pytest.mark.asyncio
async def test_merge_of_a_delete_and_a_rename_onto_its_name_keeps_the_survivors_identity(
    client: AsyncClient,
) -> None:
    slug = "leftovers-delete-then-rename"
    await _seed_plan(client, slug)
    main_ids = await _seed_main_variables(
        slug, {"cart_total": "cart_total_raw", "cart_count": "cart_count_raw"}
    )
    await _attach_variable_values(slug, main_ids)
    main_branch_id = await _main_branch_id()

    branch_id = await _create_branch(client, slug)
    async with TestSessionLocal() as session:
        branch_rows = {
            v.source_name: v
            for v in (
                await session.execute(
                    select(Variable).where(Variable.branch_id == uuid.UUID(branch_id))
                )
            )
            .scalars()
            .all()
        }
        await session.delete(branch_rows["cart_count_raw"])
        await session.flush()
        branch_rows["cart_total_raw"].name = "cart_count"
        await session.commit()

    resp = await _approve_and_merge(client, slug, branch_id)
    assert resp.status_code == 200, resp.text

    merged = await _main_variables_by_source(main_branch_id)
    # One row survives: the kept one, under the freed name, with ITS OWN id and
    # ITS OWN source_name.
    assert {source: v.name for source, v in merged.items()} == {"cart_total_raw": "cart_count"}
    assert merged["cart_total_raw"].id == main_ids["cart_total_raw"]

    async with TestSessionLocal() as session:
        values = (
            (
                await session.execute(
                    select(VariableValue).where(VariableValue.branch_id == main_branch_id)
                )
            )
            .scalars()
            .all()
        )
    # ITS OWN value history, and only that: the deleted row's went with it.
    assert [v.variable_id for v in values] == [main_ids["cart_total_raw"]]
    assert values[0].source_column == "properties.cart_total_raw"


# --- tripl-nluj: an unnamed value column means ``value`` --------------------


def test_sql_config_without_value_column_must_project_value() -> None:
    with pytest.raises(ValidationError, match="value column 'value'"):
        SqlConfig(metric_sql="SELECT 1 AS v, now() AS t", time_column="t")
    SqlConfig(metric_sql="SELECT 1 AS value, now() AS t", time_column="t")
    # A SELECT missing both is still reported under the explicit time column.
    with pytest.raises(ValidationError, match="time column 't'"):
        SqlConfig(metric_sql="SELECT 1 AS v, now() AS x", time_column="t")


# --- tripl-0zpq.370: a rename race on uq_data_sources_organization_name ------


async def _create_source(client: AsyncClient, name: str) -> str:
    response = await client.post(
        "/api/v1/data-sources",
        json={
            "name": name,
            "db_type": "clickhouse",
            "host": "warehouse.example",
            "database_name": "analytics",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.mark.asyncio
async def test_a_rename_that_loses_the_race_for_a_name_is_a_409(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _create_source(client, "Race winner")
    loser_id = uuid.UUID(await _create_source(client, "Race loser"))

    async with TestSessionLocal() as session:
        real_execute = session.execute
        blinded: list[bool] = []

        async def execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            # The pre-check is the first query filtering on the name. Blind
            # it, as if the rival rename had not committed yet when it ran.
            if not blinded and "data_sources.name =" in str(statement):
                blinded.append(True)
                return await real_execute(select(DataSource.id).where(false()))
            return await real_execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", execute)
        with pytest.raises(HTTPException) as caught:
            await datasource_service.update_data_source(
                session, loser_id, DataSourceUpdate(name="Race winner")
            )
    assert blinded == [True]
    assert caught.value.status_code == 409

    unchanged = await client.get(f"/api/v1/data-sources/{loser_id}")
    assert unchanged.json()["name"] == "Race loser"
