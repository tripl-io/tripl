"""One "active drift" rule and one retention, read by every drift surface.

``core.drift_activity`` replaced about a dozen hand copies: the schema and value
drift services each defined the constants and the predicates, and the property
drift service, the alert replay, the live dispatch, the digest and the readiness
probe restated the clause or the 30 days inline. These pin what the shared rule
answers and that no copy of it comes back.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import tripl
from tripl.core.drift_activity import (
    ACTIVE_DRIFT_STATUSES,
    DRIFT_RETENTION_DAYS,
    active_drift_clauses,
    retention_cutoff,
)
from tripl.models.base import Base
from tripl.models.property_drift import PropertyDrift
from tripl.models.schema_drift import SchemaDrift
from tripl.models.variable_value_drift import VariableValueDrift

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


@pytest.fixture
def drift_session(tmp_path: Path) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'drift_activity.db'}")
    Base.metadata.create_all(engine, tables=[SchemaDrift.__table__])
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory() as session:
            yield session
    finally:
        engine.dispose()


def _drift(
    name: str,
    *,
    status: str = "open",
    snoozed_until: datetime | None = None,
    detected_at: datetime = NOW - timedelta(days=1),
) -> SchemaDrift:
    return SchemaDrift(
        id=uuid.uuid4(),
        event_type_id=uuid.uuid4(),
        field_name=name,
        drift_type="new_field",
        status=status,
        snoozed_until=snoozed_until,
        detected_at=detected_at,
    )


def test_retention_is_thirty_days_back_from_the_given_instant() -> None:
    assert DRIFT_RETENTION_DAYS == 30
    assert retention_cutoff(NOW) == NOW - timedelta(days=30)
    before = datetime.now(UTC)
    cutoff = retention_cutoff()
    assert before - timedelta(days=30) <= cutoff <= datetime.now(UTC) - timedelta(days=30)


def test_active_means_open_or_a_snooze_that_is_over(drift_session: Session) -> None:
    drift_session.add_all(
        [
            _drift("open"),
            _drift("snoozed_over", status="snoozed", snoozed_until=NOW - timedelta(hours=1)),
            _drift("snoozed_open_ended", status="snoozed", snoozed_until=None),
            _drift("snoozed_ahead", status="snoozed", snoozed_until=NOW + timedelta(days=2)),
            _drift("accepted", status="accepted"),
            _drift("false_positive", status="false_positive"),
            # Active says nothing about age: retention is the caller's second clause.
            _drift("old_open", detected_at=NOW - timedelta(days=DRIFT_RETENTION_DAYS + 5)),
        ]
    )
    drift_session.commit()

    active = set(
        drift_session.execute(
            select(SchemaDrift.field_name).where(*active_drift_clauses(SchemaDrift, NOW))
        ).scalars()
    )
    assert active == {"open", "snoozed_over", "snoozed_open_ended", "old_open"}

    in_view = set(
        drift_session.execute(
            select(SchemaDrift.field_name).where(
                SchemaDrift.detected_at >= retention_cutoff(NOW),
                *active_drift_clauses(SchemaDrift, NOW),
            )
        ).scalars()
    )
    assert in_view == {"open", "snoozed_over", "snoozed_open_ended"}


@pytest.mark.parametrize("model", [SchemaDrift, VariableValueDrift, PropertyDrift])
def test_the_clauses_read_the_model_they_are_given(
    model: type[SchemaDrift] | type[VariableValueDrift] | type[PropertyDrift],
) -> None:
    rendered = [str(clause) for clause in active_drift_clauses(model, NOW)]
    table = model.__tablename__
    assert f"{table}.status" in rendered[0]
    assert f"{table}.snoozed_until" in rendered[1]


def test_accepted_and_false_positive_are_never_active() -> None:
    assert set(ACTIVE_DRIFT_STATUSES) == {"open", "snoozed"}


# Shapes the retired copies were written in. A new reader of drift takes
# ``core.drift_activity`` instead of spelling the rule again.
_RESTATED_RULE = (
    re.compile(r'in_\(\("open", "snoozed"\)\)'),
    re.compile(r'in_\(\{"open", "snoozed"\}\)'),
    re.compile(r'status != "snoozed"'),
    re.compile(r"\b_?DRIFT_RETENTION_DAYS\s*="),
)


def test_no_module_restates_the_rule() -> None:
    root = Path(tripl.__file__).resolve().parent
    home = root / "core" / "drift_activity.py"
    offenders = [
        f"{path.relative_to(root)}: {shape.pattern}"
        for path in sorted(root.rglob("*.py"))
        if "tests" not in path.parts and path != home
        for shape in _RESTATED_RULE
        if shape.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
