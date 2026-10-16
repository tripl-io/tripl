"""Value drift for fields an author wrote as exactly one ``${variable}`` token.

The planner only produces variable observations for a high-cardinality column or
a JSON path, so a variable documented on a low-cardinality column (three store
products, four platforms) was never checked against its documented list. The
author's token is what says the field carries the variable; these tests pin that
the scan now reads the raw values behind it and leaves everything else alone.

Above the threshold the plan carries the planner's template instead of the raw
values, so this path has nothing to read there and the planner's own
observations remain the only evidence.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from itertools import product

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.cardinality import BreakdownAnalysis, CardinalityResult
from tripl.core.analyzers.event_generator import GenerationResult, generate_events
from tripl.models import Base
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.tests._sqlite import enable_sqlite_foreign_keys

IDENTITY = "Trial Started"


@pytest.fixture
def session() -> Iterator[Session]:
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


def _analysis(products: list[str], *, threshold: int) -> BreakdownAnalysis:
    """One identity (``event_name``) observed with each of ``products``."""
    is_low = len(products) <= threshold
    results = {
        "event_name": CardinalityResult(
            column=ColumnInfo("event_name", "String"),
            count=1,
            is_low=True,
            sample_values=[IDENTITY],
        ),
        "product_id": CardinalityResult(
            column=ColumnInfo("product_id", "String"),
            count=len(products),
            is_low=is_low,
            sample_values=list(products),
        ),
    }
    rows: list[tuple[object, ...]] = [tuple(combo) for combo in product([IDENTITY], products)]
    return BreakdownAnalysis(
        results=results,
        rows=rows,
        reg_names=["event_name", "product_id"],
        json_names=[],
    )


def _seed(
    session: Session, *, authored_value: str, allowed: list[str]
) -> tuple[Project, EventType, dict[str, FieldDefinition], Event, Variable]:
    project = Project(id=uuid.uuid4(), name="Shop", slug="shop", description="")
    session.add(project)
    session.flush()
    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project.id,
        name="purchase",
        display_name="Purchase",
        description="",
    )
    session.add(event_type)
    session.flush()
    fields = {
        name: FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=event_type.id,
            name=name,
            display_name=name,
            field_type="string",
            order=order,
        )
        for order, name in enumerate(("event_name", "product_id"))
    }
    session.add_all(fields.values())
    variable = Variable(
        id=uuid.uuid4(),
        project_id=project.id,
        name="product_id",
        variable_type="string",
        description="",
        allowed_values=list(allowed),
    )
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=event_type.id,
        name=IDENTITY,
        source_name=IDENTITY,
        description="",
        order=0,
        status="live",
    )
    session.add_all([variable, event])
    session.flush()
    session.add_all(
        [
            EventFieldValue(
                id=uuid.uuid4(),
                event_id=event.id,
                field_definition_id=fields["event_name"].id,
                value=IDENTITY,
                is_authored=True,
            ),
            EventFieldValue(
                id=uuid.uuid4(),
                event_id=event.id,
                field_definition_id=fields["product_id"].id,
                value=authored_value,
                is_authored=True,
            ),
        ]
    )
    session.commit()
    return project, event_type, fields, event, variable


def _scan(
    session: Session,
    project: Project,
    event_type: EventType,
    fields: dict[str, FieldDefinition],
    products: list[str],
    *,
    threshold: int = 100,
) -> GenerationResult:
    result = generate_events(
        session,
        project.id,
        event_type.id,
        _analysis(products, threshold=threshold),
        fields,
        cardinality_threshold=threshold,
        event_name_format="{event_name}",
    )
    session.commit()
    return result


def _drifts(session: Session, project: Project) -> list[VariableValueDrift]:
    return list(
        session.execute(
            select(VariableValueDrift).where(VariableValueDrift.project_id == project.id)
        ).scalars()
    )


def test_a_low_cardinality_column_reports_the_undocumented_value(session: Session) -> None:
    project, event_type, fields, event, variable = _seed(
        session, authored_value="${product_id}", allowed=["prod_monthly", "prod_annual"]
    )

    result = _scan(
        session, project, event_type, fields, ["prod_monthly", "prod_annual", "prod_weekly"]
    )

    assert result.events_created == 0
    assert result.value_drifts_detected == 1
    (drift,) = _drifts(session, project)
    assert (drift.variable_id, drift.event_id) == (variable.id, event.id)
    assert drift.observed_values == ["prod_weekly"]
    assert drift.status == "open"


def test_documented_values_raise_nothing(session: Session) -> None:
    project, event_type, fields, _event, _variable = _seed(
        session, authored_value="${product_id}", allowed=["prod_monthly", "prod_annual"]
    )

    result = _scan(session, project, event_type, fields, ["prod_monthly", "prod_annual"])

    assert result.value_drifts_detected == 0
    assert _drifts(session, project) == []


def test_a_token_inside_other_text_is_not_read_as_the_variable(session: Session) -> None:
    project, event_type, fields, _event, _variable = _seed(
        session, authored_value="promo_${product_id}", allowed=["prod_monthly"]
    )

    result = _scan(session, project, event_type, fields, ["prod_monthly", "prod_weekly"])

    assert result.value_drifts_detected == 0
    assert _drifts(session, project) == []


def test_no_documented_list_means_no_contract(session: Session) -> None:
    project, event_type, fields, _event, _variable = _seed(
        session, authored_value="${product_id}", allowed=[]
    )

    result = _scan(session, project, event_type, fields, ["prod_monthly", "prod_weekly"])

    assert result.value_drifts_detected == 0


def test_the_check_writes_no_variable_context(session: Session) -> None:
    """Drift only: the authored field's contexts stay the variable detector's record."""
    project, event_type, fields, _event, _variable = _seed(
        session, authored_value="${product_id}", allowed=["prod_monthly"]
    )

    _scan(session, project, event_type, fields, ["prod_monthly", "prod_weekly"])

    assert session.execute(select(VariableValue)).scalars().all() == []


def test_a_rescan_refreshes_the_same_row(session: Session) -> None:
    project, event_type, fields, _event, _variable = _seed(
        session, authored_value="${product_id}", allowed=["prod_monthly"]
    )

    _scan(session, project, event_type, fields, ["prod_monthly", "prod_weekly"])
    _scan(session, project, event_type, fields, ["prod_monthly", "prod_weekly", "prod_daily"])

    (drift,) = _drifts(session, project)
    assert sorted(drift.observed_values) == ["prod_daily", "prod_weekly"]


def test_an_excluded_variable_is_not_judged(session: Session) -> None:
    project, event_type, fields, _event, variable = _seed(
        session, authored_value="${product_id}", allowed=["prod_monthly"]
    )
    variable.excluded_from_scans = True
    session.commit()

    result = _scan(session, project, event_type, fields, ["prod_monthly", "prod_weekly"])

    assert result.value_drifts_detected == 0


@pytest.mark.parametrize(
    ("field_type", "variable_type", "allowed", "observed", "novel"),
    [
        # A ``numeric(10,2)`` arrives as ``10.00``; the author wrote ``10``.
        ("number", "string", ["10", "25"], ["10.00", "25", "30.50"], ["30.5"]),
        # A PostgreSQL boolean arrives as ``True``; the author wrote ``true``.
        ("boolean", "string", ["true", "false"], ["True", "False"], []),
        # A text field carrying a number property compares as the property says.
        ("string", "number", ["1"], ["1.0", "2"], ["2"]),
    ],
)
def test_a_typed_field_compares_values_not_spellings(
    session: Session,
    field_type: str,
    variable_type: str,
    allowed: list[str],
    observed: list[str],
    novel: list[str],
) -> None:
    project, event_type, fields, _event, variable = _seed(
        session, authored_value="${product_id}", allowed=allowed
    )
    fields["product_id"].field_type = field_type
    variable.variable_type = variable_type
    session.commit()

    result = _scan(session, project, event_type, fields, observed)

    assert result.value_drifts_detected == (1 if novel else 0)
    assert [drift.observed_values for drift in _drifts(session, project)] == (
        [novel] if novel else []
    )


def test_a_sensitive_field_is_never_copied_into_a_drift(session: Session) -> None:
    """A drift stores and alerts the novel values, which here are the PII itself."""
    project, event_type, fields, _event, _variable = _seed(
        session, authored_value="${product_id}", allowed=["prod_monthly"]
    )
    fields["product_id"].sensitivity = "pii"
    session.commit()

    result = _scan(session, project, event_type, fields, ["prod_monthly", "jane@example.com"])

    assert result.value_drifts_detected == 0
    assert _drifts(session, project) == []
