"""A group merge moves the survivor along the progression axis only.

``event_status_rank`` deliberately orders ``deprecated`` (5) and ``archived``
(6) above ``live`` (4) so a closing implementation ticket cannot drag a retired
event back to ``implemented``. The group merge used to take a plain max over
that same table, which made "retire one member of a family" mean "retire the
whole group": the survivor came out ``deprecated``, with no sunset date and
nothing that ever lowers a status again.

These tests pin the rule the merge applies instead, in three parts: a retired
member does not retire the group, a live member does not revive a group the
user retired, and the surviving status is the SAME whichever member the walk
reaches first. That last one is not free. The pass walks its sources in
``scan_identity_winner_order()`` — most recently seen first — and the member it
reaches first is the one that mints the group row, so any status the mint copies
off that member is a status decided by warehouse traffic timing. The mint takes
a constant instead (``in_review``, where ``generate_events`` starts every
scan-minted event) and the fold is a max over the progression band, which is
commutative: retired members contribute nothing from any position, and every
other member contributes the same maximum in any order.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.analyzers._event_generator_merge import (
    _PROGRESSION_STATUSES,
    _RETIRED_STATUSES,
)
from tripl.core.analyzers.event_generator import merge_existing_events_for_group_rules
from tripl.models import Base
from tripl.models.event import Event, EventStatus, event_status_rank
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.tests._sqlite import enable_sqlite_foreign_keys

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

# The group the rule names is ``profile_click``; ``^profile_click_`` claims the
# per-surface members without claiming the group event itself.
_PROFILE_CLICK_RULE = [
    {
        "name": "profile_click",
        "condition_logic": "all",
        "conditions": [{"field": "__event_name", "pattern": "^profile_click_"}],
    }
]

# The progression axis, lowest first — what "furthest along" has to mean for the
# fold to be a maximum over a total order. Spelled out here rather than derived
# from the model so that this file DISAGREES with the model the day a status is
# added or the band is reordered.
_PROGRESSION_AXIS = (
    EventStatus.draft,
    EventStatus.in_review,
    EventStatus.ready_for_dev,
    EventStatus.implemented,
    EventStatus.live,
)

# Where a minted group row starts, and therefore the floor the fold cannot sink
# below: a family with no member further along than this still lands here.
_MINT_FLOOR = EventStatus.in_review


_ProjectAndType = tuple[Project, EventType, dict[str, FieldDefinition]]


@pytest.fixture
def sync_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    # Before create_all: in-memory SQLite pools one connection, and a listener
    # registered after the first checkout never fires — see ``_sqlite``. The
    # merge ends in ``session.delete(source)``, so these tests need a database
    # that actually deletes.
    enable_sqlite_foreign_keys(engine)
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
def project_and_type(sync_session: Session) -> _ProjectAndType:
    project = Project(
        id=uuid.uuid4(), name="Lifecycle", slug="group-merge-lifecycle", description=""
    )
    sync_session.add(project)
    sync_session.flush()

    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project.id,
        name="pv",
        display_name="Page View",
        description="",
    )
    sync_session.add(event_type)
    sync_session.flush()

    field_definitions = {
        "action": FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=event_type.id,
            name="action",
            display_name="Action",
            field_type="string",
            order=0,
        )
    }
    sync_session.add_all(field_definitions.values())
    sync_session.commit()
    return project, event_type, field_definitions


def _seed_event(
    session: Session,
    *,
    project: Project,
    event_type: EventType,
    field_definitions: dict[str, FieldDefinition],
    name: str,
    status: EventStatus,
    order: int,
    last_seen_at: datetime,
    sunset_at: datetime | None = None,
) -> Event:
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=event_type.id,
        name=name,
        source_name=name,
        status=status,
        order=order,
        last_seen_at=last_seen_at,
        sunset_at=sunset_at,
    )
    session.add(event)
    session.flush()
    session.add(
        EventFieldValue(
            id=uuid.uuid4(),
            event_id=event.id,
            field_definition_id=field_definitions["action"].id,
            value=name,
        )
    )
    return event


def _apply_groups(session: Session, *, project: Project, event_type: EventType) -> int:
    merged = merge_existing_events_for_group_rules(
        session,
        project_id=project.id,
        event_type_ids=[event_type.id],
        event_group_rules=_PROFILE_CLICK_RULE,
    )
    session.commit()
    return merged


def _clear_events(session: Session) -> None:
    """Empty the event table so one engine can host several walk orders.

    Creating the schema is nearly all of what a case costs, and the project, the
    type, the field definition and the rule are identical for every order — only
    the family changes.
    """
    session.execute(delete(EventFieldValue))
    session.execute(delete(Event))
    session.commit()
    assert session.execute(select(func.count()).select_from(Event)).scalar_one() == 0


def _walk_orders(statuses: tuple[EventStatus, ...]) -> list[tuple[EventStatus, ...]]:
    """The walk orders worth spending a database on for one family.

    Permuting a six-member family is 720 merges. What an order can change is
    which member the loop reaches FIRST, because that is the member that mints
    the group row; rotating the family, and rotating it reversed, puts every
    member in that position twice with a different sequence behind it. For two
    and three members that is already every permutation.
    """
    orders: list[tuple[EventStatus, ...]] = []
    for base in (statuses, tuple(reversed(statuses))):
        for offset in range(len(base)):
            rotated = base[offset:] + base[:offset]
            if rotated not in orders:
                orders.append(rotated)
    return orders


def _merge_one_family(
    session: Session,
    project_and_type: _ProjectAndType,
    walk_order: tuple[EventStatus, ...],
) -> tuple[Event, list[Event], int]:
    """Seed one family in ``walk_order``, apply the rule, return what survived.

    Position 0 gets the newest ``last_seen_at``, so the tuple IS the order the
    pass reaches its sources in (``scan_identity_winner_order``). Returns the
    group row, the rows that were not merged away, and the merged count.
    """
    project, event_type, field_definitions = project_and_type
    _clear_events(session)
    seeded = [
        _seed_event(
            session,
            project=project,
            event_type=event_type,
            field_definitions=field_definitions,
            name=f"profile_click_{position}",
            status=status,
            order=position,
            last_seen_at=NOW - timedelta(days=position),
            sunset_at=NOW + timedelta(days=30) if status is EventStatus.deprecated else None,
        )
        for position, status in enumerate(walk_order)
    ]
    session.commit()

    merged = _apply_groups(session, project=project, event_type=event_type)

    rows = session.execute(select(Event).where(Event.project_id == project.id)).scalars().all()
    group = [row for row in rows if row.source_name == "profile_click"]
    assert len(group) == 1, f"expected one group row, got {[row.source_name for row in rows]}"
    # ``seeded`` is deliberately still referenced here, across the merge above:
    # SQLite hands a RE-LOADED row a naive ``last_seen_at`` while a row still in
    # the session's (weak) identity map keeps the aware one it was seeded with,
    # and ``_merge_event_into_group`` compares the two.
    assert len(seeded) == len(walk_order)
    return group[0], [row for row in rows if row.source_name != "profile_click"], merged


def test_the_merge_classifies_every_status_the_model_defines() -> None:
    """The two bands partition ``EventStatus``, and the rank table orders one.

    ``_PROGRESSION_STATUSES`` is derived from the enum, so this is not a
    tautology about the module — it pins the MODEL's set of statuses against the
    classification the merge applies to them. Add an ``EventStatus.rejected``
    and this goes red, which is exactly the question that then needs answering:
    does a rejected member fold in like a draft, or contribute nothing like a
    retirement?
    """
    assert {EventStatus.deprecated.value, EventStatus.archived.value} == _RETIRED_STATUSES
    assert {status.value for status in _PROGRESSION_AXIS} == _PROGRESSION_STATUSES
    assert _RETIRED_STATUSES.isdisjoint(_PROGRESSION_STATUSES)
    assert {status.value for status in EventStatus} == _RETIRED_STATUSES | _PROGRESSION_STATUSES

    # ...and "furthest along" is the shared rank table's order over that band,
    # strictly increasing, so the fold's max is a max over a total order.
    ranks = [event_status_rank(status) for status in _PROGRESSION_AXIS]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


def test_a_deprecated_member_does_not_retire_the_live_group(
    sync_session: Session,
    project_and_type: _ProjectAndType,
) -> None:
    """Folding one retired member in is not a statement about the group.

    ``deprecated`` outranks ``live`` in the shared rank table, so the old max
    handed the surviving group event a retirement nobody asked for — and one
    with no sunset date, since the row carrying the real one is deleted.
    """
    project, event_type, field_definitions = project_and_type
    group = _seed_event(
        sync_session,
        project=project,
        event_type=event_type,
        field_definitions=field_definitions,
        name="profile_click",
        status=EventStatus.live,
        order=0,
        last_seen_at=NOW,
    )
    member = _seed_event(
        sync_session,
        project=project,
        event_type=event_type,
        field_definitions=field_definitions,
        name="profile_click_kite",
        status=EventStatus.deprecated,
        order=1,
        last_seen_at=NOW - timedelta(days=1),
        sunset_at=NOW + timedelta(days=30),
    )
    sync_session.commit()
    group_id, member_id = group.id, member.id

    assert _apply_groups(sync_session, project=project, event_type=event_type) == 1

    assert sync_session.get(Event, member_id) is None
    survivor = sync_session.get(Event, group_id)
    assert survivor is not None
    assert survivor.status == EventStatus.live
    # ...and nothing smuggled a sunset date onto it either: the group is not in
    # the retired band at all.
    assert survivor.sunset_at is None


def test_a_group_minted_from_a_deprecated_source_is_not_born_retired(
    sync_session: Session,
    project_and_type: _ProjectAndType,
) -> None:
    """The auto-generated group row starts where every scan-minted event starts.

    The user retired ``profile_click_kite``, not ``profile_click`` — a name that
    did not exist until this pass invented it. Minting it ``deprecated`` also
    minted it with a NULL ``sunset_at``, which is precisely the shape the sunset
    alert cannot fire on.
    """
    project, event_type, field_definitions = project_and_type
    member = _seed_event(
        sync_session,
        project=project,
        event_type=event_type,
        field_definitions=field_definitions,
        name="profile_click_kite",
        status=EventStatus.deprecated,
        order=0,
        last_seen_at=NOW,
        sunset_at=NOW + timedelta(days=30),
    )
    sync_session.commit()
    member_id = member.id

    assert _apply_groups(sync_session, project=project, event_type=event_type) == 1

    assert sync_session.get(Event, member_id) is None
    minted = sync_session.execute(
        select(Event).where(Event.source_name == "profile_click")
    ).scalar_one()
    assert minted.status == _MINT_FLOOR
    assert minted.sunset_at is None


@pytest.mark.parametrize(
    ("family", "expected_group_status", "expected_left_alone"),
    [
        pytest.param(
            (EventStatus.draft, EventStatus.deprecated),
            _MINT_FLOOR,
            (),
            id="a-draft-member-below-the-floor-plus-a-retired-one",
        ),
        pytest.param(
            (EventStatus.live, EventStatus.deprecated),
            EventStatus.live,
            (),
            id="a-live-member-plus-a-retired-one",
        ),
        pytest.param(
            (*_PROGRESSION_AXIS, EventStatus.deprecated),
            EventStatus.live,
            (),
            id="the-whole-progression-axis-plus-a-retired-member",
        ),
        pytest.param(
            (EventStatus.draft, EventStatus.draft),
            _MINT_FLOOR,
            (),
            id="nothing-but-drafts",
        ),
        pytest.param(
            (EventStatus.archived, EventStatus.deprecated, EventStatus.draft),
            _MINT_FLOOR,
            (EventStatus.archived,),
            id="an-archived-member-is-not-part-of-the-family",
        ),
    ],
)
def test_group_status_does_not_depend_on_member_order(
    sync_session: Session,
    project_and_type: _ProjectAndType,
    family: tuple[EventStatus, ...],
    expected_group_status: EventStatus,
    expected_left_alone: tuple[EventStatus, ...],
) -> None:
    """One family, every walk order, one answer — across the whole status band.

    The interesting shape is the first one. ``draft`` is the only progression
    status that ranks BELOW the floor a minted group starts at, so a family
    whose non-retired members are all drafts is the one a mint that copied its
    source's status could still push around: reached retired-member-first the
    group was born at the floor, reached draft-first it was born ``draft``, and
    a group born ``draft`` never enters the review queue the scan's own rows
    start in. The remaining cases pin that fixing that did not cost the rest of
    the band its answer — ``live`` still wins over anything below it, an
    ``archived`` member is still not part of the family at all, and no order of
    any of them retires the group or gives it a sunset date.
    """
    for walk_order in _walk_orders(family):
        group, left_alone, merged = _merge_one_family(sync_session, project_and_type, walk_order)
        order_note = f"walk order {[status.value for status in walk_order]}"

        assert group.status == expected_group_status, order_note
        assert group.sunset_at is None, order_note
        assert merged == len(walk_order) - len(expected_left_alone), order_note
        assert sorted(str(row.status) for row in left_alone) == sorted(
            status.value for status in expected_left_alone
        ), order_note


def test_a_live_member_does_not_revive_a_deprecated_group(
    sync_session: Session,
    project_and_type: _ProjectAndType,
) -> None:
    """The other half of the rule, which must not be over-fixed away.

    Excluding the retired band is symmetric on purpose: "a merge never moves a
    target into retirement" and "a merge never takes one out of it" are the same
    sentence. A group the user deliberately deprecated keeps its sunset date
    when a still-live member is folded into it.
    """
    project, event_type, field_definitions = project_and_type
    sunset_at = NOW + timedelta(days=30)
    group = _seed_event(
        sync_session,
        project=project,
        event_type=event_type,
        field_definitions=field_definitions,
        name="profile_click",
        status=EventStatus.deprecated,
        order=0,
        last_seen_at=NOW,
        sunset_at=sunset_at,
    )
    member = _seed_event(
        sync_session,
        project=project,
        event_type=event_type,
        field_definitions=field_definitions,
        name="profile_click_kite",
        status=EventStatus.live,
        order=1,
        last_seen_at=NOW - timedelta(days=1),
    )
    sync_session.commit()
    group_id, member_id = group.id, member.id

    assert _apply_groups(sync_session, project=project, event_type=event_type) == 1

    assert sync_session.get(Event, member_id) is None
    survivor = sync_session.get(Event, group_id)
    assert survivor is not None
    assert survivor.status == EventStatus.deprecated
    assert survivor.sunset_at == sunset_at
