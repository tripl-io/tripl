"""A nested object is one property with a sub-schema (F23.4e, owner decision 5).

Covers the fold of leaf paths into object properties, the templates and event
identities a scan writes with it, the migration of the dotted properties older
scans minted, sub-schema inference, key drift against the sub-schema, and the
object samples each adapter returns.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.core.adapters.clickhouse import ClickHouseAdapter
from tripl.core.adapters.postgres import PostgresAdapter
from tripl.core.adapters.synthetic import SyntheticAdapter
from tripl.core.analyzers._event_generator_variables import SCAN_PROVENANCE_DESCRIPTION
from tripl.core.analyzers.cardinality import BreakdownAnalysis, CardinalityResult
from tripl.core.analyzers.event_generator import generate_events
from tripl.core.analyzers.event_plan import plan_events, render_default_event_name
from tripl.core.event_properties import event_properties, object_schema
from tripl.core.property_drift import type_findings
from tripl.core.property_schema import (
    check_schema_matches_type,
    infer_property_type,
    is_scan_inferred_schema,
    object_schema_changes,
    validate_property_schema,
)
from tripl.json_paths import build_json_value, flatten_json_paths, object_property_paths
from tripl.models import Base
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.models.property_drift import PropertyDriftKind
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.models.variable_value import VariableValue
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.worker.variable_sweep import retire_unused_variables

# --- The fold ------------------------------------------------------------------


def test_a_nested_object_folds_into_one_property_at_its_shallowest_key() -> None:
    paths = ["screen", "user.id", "user.plan", "user.address.city", "ctx.app.version"]
    assert object_property_paths(paths) == {
        "user.id": "user",
        "user.plan": "user",
        "user.address.city": "user",
        "ctx.app.version": "ctx",
    }


def test_a_key_that_is_a_scalar_on_some_rows_keeps_its_dotted_leaves() -> None:
    # One type per property: ``user`` is a string here and an object there.
    assert object_property_paths(["user", "user.id", "ctx.os"]) == {"ctx.os": "ctx"}


def test_a_pinned_leaf_stays_dotted_and_its_siblings_fold_one_level_down() -> None:
    paths = ["user.type", "user.id", "user.address.city", "user.address.zip"]
    assert object_property_paths(paths, pinned=["user.type"]) == {
        "user.address.city": "user.address",
        "user.address.zip": "user.address",
    }


def test_a_documented_object_is_where_its_subtree_folds() -> None:
    paths = ["user.id", "user.address.city", "user.address.zip"]
    # Without the documentation ``user`` would swallow ``user.address``.
    assert object_property_paths(paths, documented=["user.address"]) == {
        "user.address.city": "user.address",
        "user.address.zip": "user.address",
    }
    # A documented leaf is a pin.
    assert object_property_paths(paths, documented=["user.id"]) == {
        "user.address.city": "user.address",
        "user.address.zip": "user.address",
    }


def test_a_flat_column_folds_nothing() -> None:
    assert object_property_paths(["Adana", "Albany, OR", "session_time"]) == {}


def test_the_template_names_the_object_whole() -> None:
    paths = ["screen", "user.id", "user.plan"]
    folded = build_json_value("props", paths, property_paths=object_property_paths(paths))
    assert json.loads(folded) == {"screen": "${props.screen}", "user": "${props.user}"}
    # Unfolded is what the event name keeps reading.
    assert json.loads(build_json_value("props", paths)) == {
        "screen": "${props.screen}",
        "user": {"id": "${props.user.id}", "plan": "${props.user.plan}"},
    }


def test_a_kept_value_is_never_folded() -> None:
    paths = ["user.id", "user.type"]
    folded = build_json_value(
        "props",
        paths,
        preserved_values={"props.user.type": "admin"},
        property_paths=object_property_paths(paths, pinned=["user.type"]),
    )
    assert json.loads(folded) == {"user": {"id": "${props.user.id}", "type": "admin"}}


def test_flatten_reports_non_empty_objects_only_when_asked() -> None:
    doc = {"a": 1, "user": {"id": "u", "geo": {"lat": 1}}, "empty": {}}
    assert [path for path, _ in flatten_json_paths(doc)] == ["a", "user.id", "user.geo.lat"]
    assert [path for path, _ in flatten_json_paths(doc, include_objects=True)] == [
        "a",
        "user",
        "user.id",
        "user.geo",
        "user.geo.lat",
    ]


# --- Sub-schema inference ------------------------------------------------------


def test_an_object_sub_schema_is_inferred_from_sampled_objects() -> None:
    inferred = infer_property_type(
        [
            {"id": "u1", "age": 30, "signup": "2026-01-02", "tags": ["a"], "geo": {"lat": 1.5}},
            {"id": "u2", "age": 31, "signup": "2026-01-03", "flag": True},
            {"id": "u3", "age": None, "signup": "2026-02-01", "mixed": 1},
            {"id": "u4", "age": 2, "signup": "2026-02-02", "mixed": "x"},
        ]
    )
    assert inferred == (
        "json",
        {
            "type": "object",
            "properties": {
                "age": {"type": "number"},
                "flag": {"type": "boolean"},
                "geo": {
                    "type": "object",
                    "properties": {"lat": {"type": "number"}},
                    "required": ["lat"],
                },
                "id": {"type": "string"},
                # ``mixed`` disagrees about its kind: left out, never guessed.
                "signup": {"type": "string", "format": "date"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            # ``age`` was null once: present on every sample is not enough.
            "required": ["id", "signup"],
        },
    )
    schema = inferred[1]
    assert schema is not None
    validate_property_schema(schema)
    check_schema_matches_type("json", schema)
    assert is_scan_inferred_schema(schema)


def test_a_person_annotated_sub_schema_is_not_the_scans() -> None:
    schema = {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "The account id"}},
    }
    assert not is_scan_inferred_schema(schema)
    assert not is_scan_inferred_schema({"type": "object", "additionalProperties": False})
    # A scalar variable's schema never reads as inferred beyond the fixed set.
    assert not is_scan_inferred_schema({"type": "string", "format": "date"})


def test_a_deep_object_stops_at_the_schema_depth_limit() -> None:
    value: dict[str, Any] = {"leaf": 1}
    for _ in range(12):
        value = {"k": value}
    inferred = infer_property_type([value])
    assert inferred is not None and inferred[1] is not None
    validate_property_schema(inferred[1])


# --- Key drift against the sub-schema --------------------------------------------

_USER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Who did it",
    "properties": {
        "id": {"type": "string"},
        "plan": {"type": "string", "enum": ["free", "pro"]},
        "geo": {"type": "object", "properties": {"lat": {"type": "number"}}},
    },
    "required": ["id", "plan"],
}


def test_sampled_objects_are_checked_against_the_sub_schema() -> None:
    changes, merged = object_schema_changes(
        _USER_SCHEMA,
        [
            {"id": "u1", "plan": "pro", "geo": {"lat": "north"}},
            {"id": 7, "seats": 3},
        ],
    )
    assert changes == [
        {
            "path": "geo.lat",
            "change": "type_change",
            "expected_type": "number",
            "observed_type": "string",
        },
        {
            "path": "id",
            "change": "type_change",
            "expected_type": "string",
            "observed_type": "number",
        },
        {"path": "plan", "change": "missing_required"},
        {"path": "seats", "change": "new_key"},
    ]
    # Accepting writes the schema with the changes applied and keeps the rest.
    assert merged["description"] == "Who did it"
    assert merged["properties"]["plan"] == {"type": "string", "enum": ["free", "pro"]}
    assert merged["properties"]["seats"] == {"type": "number"}
    assert merged["properties"]["id"] == {"type": "number"}
    assert merged["properties"]["geo"]["properties"]["lat"] == {"type": "string"}
    assert merged["required"] == ["id"]
    validate_property_schema(merged)


def test_agreeing_samples_and_a_bare_object_report_nothing() -> None:
    assert object_schema_changes(_USER_SCHEMA, [{"id": "u", "plan": "free"}])[0] == []
    assert object_schema_changes({"type": "object"}, [{"anything": [1]}])[0] == []


def test_a_key_inference_left_out_is_not_reported_as_new() -> None:
    samples = [{"id": "u1", "mixed": 1}, {"id": "u2", "mixed": "x"}]
    inferred = infer_property_type(samples)
    assert inferred is not None and inferred[1] is not None
    assert "mixed" not in inferred[1]["properties"]
    assert object_schema_changes(inferred[1], samples)[0] == []


def _variable(schema: dict[str, Any] | None, *, variable_type: str = "json") -> Variable:
    return Variable(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="user",
        source_name="props.user",
        bindings=["props.user"],
        variable_type=variable_type,
        json_schema=schema,
        description="Who did it",
        excluded_from_scans=False,
    )


def test_key_drift_is_a_type_change_that_says_where() -> None:
    variable = _variable(_USER_SCHEMA)
    samples = [{"id": "u1", "plan": "pro", "seats": 3}]
    findings = type_findings(
        [variable],
        {"props.user": infer_property_type(samples)},  # type: ignore[dict-item]
        object_samples={"props.user": samples},
    )
    assert [(f.variable_id, f.event_id, f.kind) for f in findings] == [
        (variable.id, None, PropertyDriftKind.type_change)
    ]
    detail = findings[0].detail
    assert detail["nested_changes"] == [{"path": "seats", "change": "new_key"}]
    assert detail["observed_schema"]["properties"]["seats"] == {"type": "number"}
    assert (detail["expected_type"], detail["observed_type"]) == ("json", "json")


def test_an_object_turning_into_an_array_is_a_type_change() -> None:
    findings = type_findings(
        [_variable(_USER_SCHEMA)],
        {"props.user": ("json", {"type": "array"})},
    )
    assert findings[0].detail["observed_schema"] == {"type": "array"}


def test_objects_that_agree_report_nothing() -> None:
    samples = [{"id": "u1", "plan": "pro"}]
    assert (
        type_findings(
            [_variable(_USER_SCHEMA)],
            {"props.user": infer_property_type(samples)},  # type: ignore[dict-item]
            object_samples={"props.user": samples},
        )
        == []
    )


# --- Export reads the object property whole ----------------------------------------


def test_the_export_reads_an_object_property_whole() -> None:
    schema = infer_property_type([{"id": "u1"}])
    assert schema is not None
    props = event_properties(
        {"props": json.dumps({"user": "${user}", "v": 2})},
        ["props"],
        token_types={"user": schema},
        required_tokens={"user"},
        allowed_for=lambda _token: (),
    )
    assert [(p.path, p.token) for p in props] == [("user", "user"), ("v", None)]
    assert object_schema(props) == {
        "type": "object",
        "properties": {
            "user": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            "v": {"const": 2},
        },
        "required": ["user"],
    }


# --- Adapters: whole objects on request ------------------------------------------------


def test_the_fallback_sampler_reports_objects_only_when_asked() -> None:
    adapter = object.__new__(SyntheticAdapter)
    rows = [
        (json.dumps({"user": {"id": "u1", "plan": "pro"}, "screen": "home"}),),
        (json.dumps({"user": {"id": "u2"}}),),
    ]
    adapter.get_preview_rows = lambda *_a, **_k: (["props"], rows)  # type: ignore[method-assign]

    leaves = adapter.get_json_path_samples("SELECT 1", ["props"])
    assert set(leaves["props"]) == {"user.id", "user.plan", "screen"}

    with_objects = adapter.get_json_path_samples("SELECT 1", ["props"], include_objects=True)
    assert with_objects["props"]["user"] == [{"id": "u1", "plan": "pro"}, {"id": "u2"}]
    assert with_objects["props"]["user.id"] == ["u1", "u2"]


class _CHResult:
    def __init__(self, column_names: list[str], rows: list[tuple[object, ...]]) -> None:
        self.column_names = column_names
        self.result_rows = rows


class _CHClient:
    """Answers the discovery query with paths, the sample query with rows."""

    def __init__(self, paths: list[str], rows: list[tuple[object, ...]]) -> None:
        self.sql: list[str] = []
        self._paths = paths
        self._rows = rows

    def query(self, sql: str) -> _CHResult:
        self.sql.append(sql)
        if "arrayJoin" in sql:
            return _CHResult(["_path"], [(path,) for path in self._paths])
        return _CHResult([f"__json_path_{i}" for i in range(len(self._paths))], self._rows)


def _clickhouse(
    paths: list[str], rows: list[tuple[object, ...]]
) -> tuple[ClickHouseAdapter, _CHClient]:
    client = _CHClient(paths, rows)
    adapter = object.__new__(ClickHouseAdapter)
    adapter._client = client
    adapter._allowed_columns = {"props"}
    adapter._json_path_discovery = "all"
    return adapter, client


def test_clickhouse_rebuilds_each_rows_object_from_its_leaves() -> None:
    paths = ["screen", "user.geo.lat", "user.id"]
    rows: list[tuple[object, ...]] = [
        ('"home"', "1.5", '"u1"'),
        ('"cart"', "null", '"u2"'),
        ('"home"', "null", "null"),  # carries no ``user`` at all: no sample
    ]
    adapter, client = _clickhouse(paths, rows)

    samples = adapter.get_json_path_samples("SELECT props FROM t", ["props"], include_objects=True)

    # No extra query: the object is read off the row the leaves came in.
    assert len(client.sql) == 2
    assert samples["props"]["user"] == ['{"geo":{"lat":1.5},"id":"u1"}', '{"id":"u2"}']
    assert samples["props"]["user.geo"] == ['{"lat":1.5}']
    assert samples["props"]["user.id"] == ['"u1"', '"u2"']
    assert infer_property_type(
        [json.loads(text) for text in samples["props"]["user"]]  # type: ignore[arg-type]
    ) == (
        "json",
        {
            "type": "object",
            "properties": {
                "geo": {
                    "type": "object",
                    "properties": {"lat": {"type": "number"}},
                    "required": ["lat"],
                },
                "id": {"type": "string"},
            },
            "required": ["id"],
        },
    )


def test_clickhouse_reports_leaves_only_by_default() -> None:
    adapter, _client = _clickhouse(["user.id"], [('"u1"',)])
    samples = adapter.get_json_path_samples("SELECT props FROM t", ["props"])
    assert samples["props"] == {"user.id": ['"u1"']}


class _PGCursor:
    def __init__(self, conn: _PGConn) -> None:
        self._conn = conn

    def __enter__(self) -> _PGCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self._conn.sql.append(sql)

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._conn.rows


class _PGConn:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.sql: list[str] = []
        self.rows = rows

    def cursor(self) -> _PGCursor:
        return _PGCursor(self)


def test_postgres_keeps_non_empty_object_nodes_when_asked() -> None:
    conn = _PGConn([("user", '{"id": "u1"}'), ("user.id", '"u1"')])
    adapter = object.__new__(PostgresAdapter)
    adapter._conn = conn
    adapter._allowed_columns = {"props"}

    samples = adapter.get_json_path_samples("SELECT props FROM t", ["props"], include_objects=True)
    assert "_value <> '{}'::jsonb" in conn.sql[0]
    assert "jsonb_typeof(_value) <> 'object'" not in conn.sql[0]
    assert samples["props"]["user"] == ['{"id": "u1"}']

    adapter.get_json_path_samples("SELECT props FROM t", ["props"])
    assert "jsonb_typeof(_value) <> 'object'" in conn.sql[1]


# --- The scan ------------------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def catalog(session: Session):
    project = Project(id=uuid.uuid4(), name="Objects", slug="objects", description="")
    session.add(project)
    session.flush()
    event_type = EventType(
        id=uuid.uuid4(), project_id=project.id, name="pv", display_name="PV", description=""
    )
    session.add(event_type)
    session.flush()
    fields = {
        "screen": FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=event_type.id,
            name="screen",
            display_name="Screen",
            field_type="string",
            order=0,
        ),
        "payload": FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=event_type.id,
            name="payload",
            display_name="Payload",
            field_type="json",
            order=1,
        ),
    }
    session.add_all(fields.values())
    session.commit()
    return project, event_type, fields


def _analysis(
    rows: list[tuple[object, ...]],
    *,
    with_screen: bool = False,
    json_value_names: list[str] | None = None,
) -> BreakdownAnalysis:
    """Rows of ``([screen,] paths, *kept values, count)``."""
    offset = 1 if with_screen else 0
    combos = sorted({tuple(row[offset]) for row in rows})  # type: ignore[arg-type]
    results: dict[str, CardinalityResult] = {
        "payload": CardinalityResult(
            column=ColumnInfo("payload", "JSON"),
            count=len(combos),
            is_low=False,
            json_path_combos=[tuple(combo) for combo in combos],
        )
    }
    reg_names: list[str] = []
    if with_screen:
        screens = sorted({str(row[0]) for row in rows})
        results = {
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=len(screens),
                is_low=True,
                sample_values=screens,
            ),
            **results,
        }
        reg_names = ["screen"]
    return BreakdownAnalysis(
        results=results,
        rows=rows,
        reg_names=reg_names,
        json_names=["payload"],
        json_value_names=json_value_names or [],
    )


def _variables(session: Session) -> dict[str, Variable]:
    return {v.source_name or v.name: v for v in session.execute(select(Variable)).scalars()}


def _payload(session: Session, event: Event, fields: dict[str, FieldDefinition]) -> Any:
    value = session.execute(
        select(EventFieldValue.value).where(
            EventFieldValue.event_id == event.id,
            EventFieldValue.field_definition_id == fields["payload"].id,
        )
    ).scalar_one()
    return json.loads(value)


def test_a_scan_mints_one_object_property_and_references_it_whole(
    session: Session, catalog
) -> None:
    project, event_type, fields = catalog
    paths = ["screen", "user.address.city", "user.id"]
    analysis = _analysis([(paths, 10)])

    result = generate_events(session, project.id, event_type.id, analysis, fields)
    session.commit()

    variables = _variables(session)
    assert set(variables) == {"payload.screen", "payload.user"}
    user = variables["payload.user"]
    assert (user.variable_type, user.json_schema) == ("json", {"type": "object"})
    assert user.description == SCAN_PROVENANCE_DESCRIPTION
    assert result.variables_created == 2

    event = session.execute(select(Event)).scalar_one()
    assert _payload(session, event, fields) == {
        "screen": f"${{{variables['payload.screen'].name}}}",
        "user": f"${{{user.name}}}",
    }
    # The identity is the unfolded template's, exactly what earlier scans and the
    # metric collector derive: no event is re-minted by the fold.
    assert event.source_name == render_default_event_name(
        [("payload", build_json_value("payload", paths))]
    )
    context = session.execute(
        select(VariableValue).where(VariableValue.variable_id == user.id)
    ).scalar_one()
    assert context.source_column == "payload.user"
    assert context.presence_rate == 1.0


def test_an_object_property_gets_the_presence_of_rows_carrying_it(
    session: Session, catalog
) -> None:
    project, event_type, fields = catalog
    analysis = _analysis(
        [
            ("home", ["user.id", "user.plan"], 3),
            ("home", ["user.id"], 1),
            ("home", ["x"], 4),
        ],
        with_screen=True,
    )

    generate_events(
        session, project.id, event_type.id, analysis, fields, event_name_format="{screen}"
    )
    session.commit()

    variables = _variables(session)
    assert set(variables) == {"payload.user", "payload.x"}
    event = session.execute(select(Event)).scalar_one()
    assert _payload(session, event, fields) == {
        "user": f"${{{variables['payload.user'].name}}}",
        "x": f"${{{variables['payload.x'].name}}}",
    }
    rates = {
        row.variable_id: row.presence_rate
        for row in session.execute(select(VariableValue)).scalars()
    }
    assert rates == {variables["payload.user"].id: 0.5, variables["payload.x"].id: 0.5}


def test_kept_values_and_name_placeholders_stay_dotted(session: Session, catalog) -> None:
    project, event_type, fields = catalog
    paths = ["ctx.os.name", "ctx.os.version", "user.id", "user.type"]
    analysis = _analysis(
        [(paths, '"admin"', 5)],
        json_value_names=["payload.user.type"],
    )

    plan = plan_events(
        analysis,
        {name: fd.id for name, fd in fields.items()},
        event_name_format="promo_{payload.ctx.os.name}_shown",
    )

    # The name uses the placeholder's value and so pins it: ``ctx.os.version``
    # is its sibling, and the kept ``user.type`` pins ``user`` the same way.
    assert plan.events[0].name == "promo_${payload.ctx.os.name}_shown"
    template = json.loads(plan.events[0].field_values[0][2])
    assert template == {
        "ctx": {"os": {"name": "${payload.ctx.os.name}", "version": "${payload.ctx.os.version}"}},
        "user": {"id": "${payload.user.id}", "type": "admin"},
    }
    assert [need.name for need in plan.variables_needed] == [
        "payload.ctx.os.name",
        "payload.ctx.os.version",
        "payload.user.id",
    ]


def _legacy_catalog(
    session: Session,
    catalog,
    paths: list[str],
    *,
    edited: str | None = None,
    listed: str | None = None,
) -> tuple[Event, dict[str, Variable]]:
    """What a scan before F23.4e left: dotted leaf properties and their template."""
    project, event_type, fields = catalog
    name = render_default_event_name([("payload", build_json_value("payload", paths))])
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=event_type.id,
        name=name,
        source_name=name,
        order=0,
        status="in_review",
    )
    session.add(event)
    session.flush()
    template: dict[str, Any] = {}
    variables: dict[str, Variable] = {}
    for path in paths:
        token = f"payload.{path}"
        variable = Variable(
            id=uuid.uuid4(),
            project_id=project.id,
            name=token.replace(".", "_"),
            source_name=token,
            bindings=[token],
            variable_type="string",
            description=(
                "Signed-up user's plan" if token == edited else SCAN_PROVENANCE_DESCRIPTION
            ),
        )
        variables[token] = variable
        session.add(variable)
        session.flush()
        cursor = template
        parts = path.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = f"${{{variable.name}}}"
        session.add(
            VariableValue(
                id=uuid.uuid4(),
                project_id=project.id,
                variable_id=variable.id,
                event_id=event.id,
                field_definition_id=fields["payload"].id,
                source_column=token,
                value_kind="high",
                observed_count=1,
                values=["v"],
            )
        )
        if token == listed:
            session.add(
                VariableEventValueOverride(
                    project_id=project.id,
                    branch_id=variable.branch_id,
                    variable_id=variable.id,
                    event_id=event.id,
                    values=None,
                    required=True,
                )
            )
    session.add(
        EventFieldValue(
            id=uuid.uuid4(),
            event_id=event.id,
            field_definition_id=fields["payload"].id,
            value=json.dumps(template, sort_keys=True),
            is_authored=False,
        )
    )
    session.commit()
    return event, variables


def test_a_rescan_folds_the_dotted_properties_an_older_scan_minted(
    session: Session, catalog
) -> None:
    project, event_type, fields = catalog
    paths = ["screen", "user.id", "user.plan"]
    event, old = _legacy_catalog(session, catalog, paths)

    result = generate_events(session, project.id, event_type.id, _analysis([(paths, 3)]), fields)
    session.commit()

    # Same event, re-planned in place.
    assert result.events_created == 0
    assert session.execute(select(Event)).scalars().all() == [event]
    user = _variables(session)["payload.user"]
    assert _payload(session, event, fields) == {
        "screen": f"${{{old['payload.screen'].name}}}",
        "user": f"${{{user.name}}}",
    }
    # The dotted leaves lost their token and their contexts ...
    orphaned = {old["payload.user.id"].id, old["payload.user.plan"].id}
    assert not session.execute(
        select(VariableValue).where(VariableValue.variable_id.in_(orphaned))
    ).all()

    # ... and the sweep that retires every orphaned scan-owned property takes them.
    retired = retire_unused_variables(session, project_id=project.id, branch_id=None)
    session.commit()
    assert retired == 2
    assert set(_variables(session)) == {"payload.screen", "payload.user"}


@pytest.mark.parametrize("claim", ["edited", "listed"])
def test_a_rescan_never_folds_away_a_property_a_person_made_theirs(
    session: Session, catalog, claim: str
) -> None:
    project, event_type, fields = catalog
    paths = ["user.id", "user.plan", "user.geo.lat", "user.geo.lon"]
    kwargs = {claim: "payload.user.plan"}
    event, old = _legacy_catalog(session, catalog, paths, **kwargs)  # type: ignore[arg-type]

    generate_events(session, project.id, event_type.id, _analysis([(paths, 3)]), fields)
    session.commit()

    geo = _variables(session)["payload.user.geo"]
    # ``user.plan`` keeps naming its property, so ``user`` cannot fold; its
    # sibling leaf stays a leaf and ``user.geo`` folds on its own.
    assert _payload(session, event, fields) == {
        "user": {
            "geo": f"${{{geo.name}}}",
            "id": f"${{{old['payload.user.id'].name}}}",
            "plan": f"${{{old['payload.user.plan'].name}}}",
        }
    }
    retire_unused_variables(session, project_id=project.id, branch_id=None)
    session.commit()
    assert set(_variables(session)) == {"payload.user.id", "payload.user.plan", "payload.user.geo"}


def test_an_authored_template_is_never_rewritten(session: Session, catalog) -> None:
    project, event_type, fields = catalog
    paths = ["user.id", "user.plan"]
    event, _old = _legacy_catalog(session, catalog, paths)
    stored = session.execute(select(EventFieldValue)).scalar_one()
    stored.is_authored = True
    before = stored.value
    session.commit()

    generate_events(session, project.id, event_type.id, _analysis([(paths, 3)]), fields)
    session.commit()

    assert session.execute(select(EventFieldValue.value)).scalar_one() == before


# --- Catalog sync: sampling objects, refining the schema -------------------------------


def _scan_config(session: Session, project: Project):
    from tripl.models.data_source import DataSource
    from tripl.models.scan_config import ScanConfig

    source = DataSource(
        id=uuid.uuid4(),
        name=f"wh-{uuid.uuid4().hex[:8]}",
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
        name="main scan",
        base_query="SELECT 1",
    )
    session.add(config)
    session.commit()
    return config


def test_the_sampler_asks_for_objects_only_for_an_object_property(
    session: Session, catalog
) -> None:
    from tripl.worker.tasks.metrics.catalog_sync import (
        _apply_inferred_types,
        _collect_json_path_samples,
        _detect_type_drifts,
    )

    project, event_type, fields = catalog
    config = _scan_config(session, project)
    generate_events(session, project.id, event_type.id, _analysis([(["user.id"], 2)]), fields)
    session.commit()

    calls: list[dict[str, object]] = []

    class _Adapter:
        json_path_samples_are_text = True

        def get_json_path_samples(self, *args: object, **kwargs: object):
            calls.append(kwargs)
            return {"payload": {"user": ['{"id":"u1","plan":"pro"}', '{"id":"u2"}']}}

    def sample():
        return _collect_json_path_samples(
            session,
            adapter=_Adapter(),
            config=config,
            columns=[ColumnInfo("payload", "JSON")],
            catalog_scan_window=None,
            time_from_dt=datetime(2026, 9, 1, tzinfo=UTC),
            time_to_dt=datetime(2026, 9, 1, 1, tzinfo=UTC),
        )

    sampling = sample()
    assert calls[0]["include_objects"] is True
    assert sampling.samples == {
        "payload": {"user": ['{"id": "u1", "plan": "pro"}', '{"id": "u2"}']}
    }
    assert sampling.objects == {"payload": {"user": [{"id": "u1", "plan": "pro"}, {"id": "u2"}]}}
    expected_schema = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "plan": {"type": "string"}},
        "required": ["id"],
    }
    assert sampling.types == {"payload": {"user": ("json", expected_schema)}}

    typed = _apply_inferred_types(
        session, project_id=project.id, branch_id=None, types=sampling.types
    )
    session.commit()
    user = _variables(session)["payload.user"]
    assert typed == 1
    assert user.json_schema == expected_schema
    # Still the scan's: the sweep and diff housekeeping read it as untouched.
    assert is_scan_inferred_schema(user.json_schema)

    # Refined once. A later sample with another key is drift, not a rewrite.
    later = {"payload": {"user": ("json", {"type": "object"})}}
    assert _apply_inferred_types(session, project_id=project.id, branch_id=None, types=later) == 0
    detected = _detect_type_drifts(
        session,
        project_id=project.id,
        branch_id=None,
        scan_config_id=config.id,
        types={"payload": {"user": infer_property_type([{"id": "u3", "seats": 2}])}},  # type: ignore[dict-item]
        objects={"payload": {"user": [{"id": "u3", "seats": 2}]}},
    )
    session.commit()
    assert detected == 1
    assert user.json_schema == expected_schema


def test_a_person_typed_object_property_is_never_refined(session: Session, catalog) -> None:
    from tripl.worker.tasks.metrics.catalog_sync import _apply_inferred_types

    project, event_type, fields = catalog
    generate_events(session, project.id, event_type.id, _analysis([(["user.id"], 2)]), fields)
    user = _variables(session)["payload.user"]
    user.description = "The signed-in user"
    session.commit()

    types = {"payload": {"user": infer_property_type([{"id": "u1"}])}}
    assert (
        _apply_inferred_types(session, project_id=project.id, branch_id=None, types=types)  # type: ignore[arg-type]
        == 0
    )
    assert user.json_schema == {"type": "object"}


def test_replay_never_extracts_an_object_property_as_a_grouping_value() -> None:
    from tripl.core.analyzers._event_generator_variables import VariableIndex
    from tripl.worker.tasks.metrics.generation import (
        _augment_json_value_paths_for_replay_tokens,
    )

    user = _variable({"type": "object"})
    plan = _variable(None, variable_type="string")
    plan.name, plan.source_name, plan.bindings = "plan", "props.plan", ["props.plan"]
    event = Event(id=uuid.uuid4(), name="e", source_name="e")
    event.field_values = [
        EventFieldValue(
            id=uuid.uuid4(),
            field_definition_id=uuid.uuid4(),
            value=json.dumps({"user": "${user}", "plan": "${plan}"}),
        )
    ]

    paths = _augment_json_value_paths_for_replay_tokens(
        json_value_path_map={},
        json_columns=["props"],
        replay_events=[event],
        variable_index=VariableIndex([user, plan]),
    )
    assert paths == {"props": ["plan"]}


def test_a_leaf_sampler_is_called_as_it_always_was(session: Session, catalog) -> None:
    from tripl.worker.tasks.metrics.catalog_sync import _collect_json_path_samples

    project, event_type, fields = catalog
    config = _scan_config(session, project)
    generate_events(session, project.id, event_type.id, _analysis([(["plan"], 2)]), fields)
    session.commit()

    calls: list[dict[str, object]] = []

    class _Adapter:
        def get_json_path_samples(self, *args: object, **kwargs: object):
            calls.append(kwargs)
            return {"payload": {"plan": ["pro"]}}

    _collect_json_path_samples(
        session,
        adapter=_Adapter(),
        config=config,
        columns=[ColumnInfo("payload", "JSON")],
        catalog_scan_window=None,
        time_from_dt=datetime(2026, 9, 1, tzinfo=UTC),
        time_to_dt=datetime(2026, 9, 1, 1, tzinfo=UTC),
    )
    assert "include_objects" not in calls[0]
