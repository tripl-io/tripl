"""Why a Variable can end up with no Event linked to it.

The only thing that links a ``Variable`` to an ``Event`` is a ``VariableValue``
row keyed by ``(variable_id, event_id, field_definition_id)``. Scans are the only
writer on the happy path, and they rebuild those rows from the CURRENT scan
window while ``Variable`` rows live forever. These tests pin the two halves of
that asymmetry that a production audit of tripl.example.com turned up:

* a rescan that rewrites a field value but KEEPS the ``${token}`` must re-record
  the context — dropping it there is what leaves a variable showing "0 events"
  while an event visibly still references it;
* a JSON column whose KEYS are free text mints one ``Variable`` per distinct
  key, and those variables outlive their contexts — generation never retires
  one, and the end-of-run sweep is what does.
"""

import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.cardinality import BreakdownAnalysis, CardinalityResult
from tripl.core.analyzers.event_generator import generate_events
from tripl.models import Base
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue
from tripl.worker.variable_sweep import retire_unused_variables


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


@pytest.fixture
def project_and_type(sync_session: Session):
    project = Project(id=uuid.uuid4(), name="Orphans", slug="orphans", description="")
    sync_session.add(project)
    sync_session.flush()

    et = EventType(
        id=uuid.uuid4(),
        project_id=project.id,
        name="pv",
        display_name="Page View",
        description="",
    )
    sync_session.add(et)
    sync_session.flush()

    fd_screen = FieldDefinition(
        id=uuid.uuid4(),
        event_type_id=et.id,
        name="screen",
        display_name="Screen",
        field_type="string",
        order=0,
    )
    fd_payload = FieldDefinition(
        id=uuid.uuid4(),
        event_type_id=et.id,
        name="payload",
        display_name="Payload",
        field_type="json",
        order=1,
    )
    sync_session.add_all([fd_screen, fd_payload])
    sync_session.flush()
    sync_session.commit()

    return project, et, {"screen": fd_screen, "payload": fd_payload}


def _analysis(payload_paths: tuple[str, ...]) -> BreakdownAnalysis:
    """One event on a stable name column, carrying ``payload_paths`` as JSON keys.

    ``screen`` is the event-name column, so the event identity stays put across
    scans and the payload value is the only thing that moves — which is exactly
    the shape that makes a rescan rewrite a value in place.
    """
    return BreakdownAnalysis(
        results={
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=1,
                is_low=True,
                sample_values=["/home"],
            ),
            "payload": CardinalityResult(
                column=ColumnInfo("payload", "JSON"),
                count=1,
                is_low=True,
                json_path_combos=[payload_paths],
            ),
        },
        rows=[("/home", payload_paths)],
        reg_names=["screen"],
        json_names=["payload"],
        json_value_names=[],
    )


def _contexts_by_variable(session: Session, project_id: uuid.UUID) -> dict[str, int]:
    rows = session.execute(
        select(Variable.name, VariableValue.id)
        .join(VariableValue, VariableValue.variable_id == Variable.id, isouter=True)
        .where(Variable.project_id == project_id)
    ).all()
    counts: dict[str, int] = {}
    for name, context_id in rows:
        counts[name] = counts.get(name, 0) + (1 if context_id is not None else 0)
    return counts


def _scan(session: Session, project, et, fds, paths: tuple[str, ...]):
    result = generate_events(
        session,
        project.id,
        et.id,
        _analysis(paths),
        fds,
        event_name_format="{screen}",
    )
    session.commit()
    return result


def test_rescan_that_keeps_the_token_rekeeps_the_variable_link(
    sync_session: Session, project_and_type
):
    """A rewrite that still references ``${locale}`` must not orphan ``locale``.

    ``delete_variable_contexts_for_event_type`` drops every context whose
    ``(event, field)`` this run rewrote, on the theory that the run is about to
    re-record whatever is still true. That theory only holds while the delete and
    the re-record see the same token, so this pins the pairing: adding a second
    JSON key rewrites the payload value, which invalidates the ``locale`` context,
    and the same run has to put it back because the value still says
    ``${locale}``. Failing this is the shape behind the production rows where a
    variable reads "0 events" next to an event that plainly references it.
    """
    project, et, fds = project_and_type

    _scan(sync_session, project, et, fds, ("locale",))
    assert _contexts_by_variable(sync_session, project.id) == {"locale": 1}

    _scan(sync_session, project, et, fds, ("locale", "theme"))

    payload_value = sync_session.execute(
        select(EventFieldValue.value).where(
            EventFieldValue.field_definition_id == fds["payload"].id
        )
    ).scalar_one()
    # The rewrite happened (theme is new) and locale survived it.
    assert "${theme}" in payload_value
    assert "${locale}" in payload_value

    # Both tokens in the stored value must have a link behind them.
    assert _contexts_by_variable(sync_session, project.id) == {"locale": 1, "theme": 1}


def test_only_one_event_is_kept_so_links_are_not_double_counted(
    sync_session: Session, project_and_type
):
    """The rescan updates the existing event rather than minting a second one."""
    project, et, fds = project_and_type

    _scan(sync_session, project, et, fds, ("locale",))
    second = _scan(sync_session, project, et, fds, ("locale", "theme"))

    assert second.events_created == 0
    events = (
        sync_session.execute(select(Event).where(Event.project_id == project.id)).scalars().all()
    )
    assert [event.name for event in events] == ["/home"]


def test_generating_events_never_retires_the_variables_it_minted(
    sync_session: Session, project_and_type
):
    """A vanished JSON key leaves an orphan behind — and generation is right to.

    ``plan_column_meta`` needs a variable for EVERY distinct JSON path it sees,
    with no cardinality guard, so a column whose keys are user-typed text mints
    one ``Variable`` per distinct key. When a key stops appearing, the next scan
    rewrites the value without it and the context is correctly dropped; the
    ``Variable`` row survives.

    That used to be permanent, and on production it accounted for 1279 of
    acme-ios' 1518 variables. It is now swept up by
    ``worker.variable_sweep`` — but deliberately at the END of a scan run, once
    every event type has been generated, and NOT inside ``generate_events``. The
    reason is scope: contexts are deleted per event type, so a variable this
    event type no longer references may still be the live property of another
    one, and a sweep running inside generation would judge it on a fraction of
    the evidence.

    So this pins the boundary rather than the defect: generation leaves the row,
    and something later, holding the whole project, decides.
    ``test_the_sweep_retires_what_generation_left_behind`` is the other half.
    """
    project, et, fds = project_and_type

    _scan(sync_session, project, et, fds, ("Bodrum", "Alicante"))
    assert _contexts_by_variable(sync_session, project.id) == {"bodrum": 1, "alicante": 1}

    # Next window: the two typed-in places are gone, a third shows up.
    _scan(sync_session, project, et, fds, ("Caorle",))

    payload_value = sync_session.execute(
        select(EventFieldValue.value).where(
            EventFieldValue.field_definition_id == fds["payload"].id
        )
    ).scalar_one()
    assert "${bodrum}" not in payload_value
    assert "${caorle}" in payload_value

    # The variables survive the keys that created them, with nothing linked.
    assert _contexts_by_variable(sync_session, project.id) == {
        "bodrum": 0,
        "alicante": 0,
        "caorle": 1,
    }


def test_the_sweep_retires_what_generation_left_behind(sync_session: Session, project_and_type):
    """The other half: a scan run ends by clearing the orphans, keeping the live one.

    ``caorle`` is the key the second window actually carried, so its token is in
    the stored value and its context exists — the sweep must not touch it. The
    two place names nobody typed again have neither, and go.

    This is the end-to-end shape of the orphan cleanup: the catalog stops growing
    without anyone pressing anything.
    """
    project, et, fds = project_and_type

    _scan(sync_session, project, et, fds, ("Bodrum", "Alicante"))
    _scan(sync_session, project, et, fds, ("Caorle",))
    assert set(_contexts_by_variable(sync_session, project.id)) == {
        "bodrum",
        "alicante",
        "caorle",
    }

    retired = retire_unused_variables(sync_session, project_id=project.id, branch_id=None)

    assert retired == 2
    assert _contexts_by_variable(sync_session, project.id) == {"caorle": 1}
    # Idempotent: a second run over a swept project deletes nothing.
    assert retire_unused_variables(sync_session, project_id=project.id, branch_id=None) == 0
