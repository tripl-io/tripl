"""Unit tests for the event generator module."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from itertools import product

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers._event_generator_variables import (
    VARIABLE_VALUE_SAMPLE_LIMIT,
    preserve_existing_variable_context_values,
)
from tripl.core.analyzers._event_identity import (
    insert_event_claiming_identity as _insert_event_claiming_identity,
)
from tripl.core.analyzers.cardinality import BreakdownAnalysis, CardinalityResult
from tripl.core.analyzers.event_generator import (
    _ensure_variable,
    _resolve_main_branch_id,
    generate_events,
    merge_existing_events_for_group_rules,
)
from tripl.models import Base
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_filter import AlertRuleFilter
from tripl.models.anomaly_scope_override import AnomalyScopeOverride
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.domain_enums import MetricKind
from tripl.models.event import Event, EventStatus
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_metric import EventMetric
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.implementation_ticket import ImplementationTicket
from tripl.models.metric_definition import MetricDefinition
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.models.project import Project
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.models.variable_value import VariableValue
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.tests._sqlite import enable_sqlite_foreign_keys


def _make_analysis(
    cardinality: dict[str, CardinalityResult],
) -> BreakdownAnalysis:
    """Build a BreakdownAnalysis with all row combinations from sample_values."""
    reg_names = [name for name, cr in cardinality.items() if cr.json_path_combos is None]
    json_names = [name for name, cr in cardinality.items() if cr.json_path_combos is not None]

    # Build rows as cartesian product of sample values (regular) / path combos (json)
    value_lists: list[list] = []
    for name in reg_names:
        value_lists.append(cardinality[name].sample_values)
    for name in json_names:
        combos = cardinality[name].json_path_combos or [()]
        value_lists.append(combos)

    rows = [tuple(combo) for combo in product(*value_lists)] if value_lists else []

    return BreakdownAnalysis(
        results=cardinality,
        rows=rows,
        reg_names=reg_names,
        json_names=json_names,
    )


@pytest.fixture
def sync_session():
    engine = create_engine("sqlite:///:memory:")
    # Before create_all: the pooled connection is opened by the first statement,
    # and a listener registered after that never fires. Without the pragma the
    # merge tests below cannot fail — see ``_sqlite``.
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
def project_and_type(sync_session: Session):
    project = Project(
        id=uuid.uuid4(),
        name="Test Project",
        slug="test-eg",
        description="",
    )
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
    fd_action = FieldDefinition(
        id=uuid.uuid4(),
        event_type_id=et.id,
        name="action",
        display_name="Action",
        field_type="string",
        order=1,
    )
    fd_payload = FieldDefinition(
        id=uuid.uuid4(),
        event_type_id=et.id,
        name="payload",
        display_name="Payload",
        field_type="json",
        order=2,
    )
    sync_session.add_all([fd_screen, fd_action, fd_payload])
    sync_session.flush()
    sync_session.commit()

    return project, et, {"screen": fd_screen, "action": fd_action, "payload": fd_payload}


def _seed_scan_config(sync_session: Session, project: Project) -> uuid.UUID:
    """Persist the minimum data source + scan config an EventMetric can point at."""
    from tripl.models.data_source import DataSource
    from tripl.models.scan_config import ScanConfig

    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"wh-{uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=9000,
        database_name="db",
        username="u",
    )
    sync_session.add(data_source)
    sync_session.flush()
    scan_config = ScanConfig(
        id=uuid.uuid4(),
        project_id=project.id,
        data_source_id=data_source.id,
        name="main scan",
        base_query="SELECT 1",
    )
    sync_session.add(scan_config)
    sync_session.flush()
    return scan_config.id


class TestEventGeneration:
    def test_low_cardinality_generates_concrete_events(
        self, sync_session: Session, project_and_type
    ):
        project, et, fds = project_and_type
        cardinality = {
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=3,
                is_low=True,
                sample_values=["/home", "/about", "/contact"],
            ),
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=2,
                is_low=True,
                sample_values=["click", "view"],
            ),
        }
        analysis = _make_analysis(cardinality)
        result = generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
        )
        sync_session.commit()

        assert result.events_created == 6  # 3 screens × 2 actions
        assert result.events_skipped == 0

        events = (
            sync_session.execute(select(Event).where(Event.project_id == project.id))
            .scalars()
            .all()
        )
        assert len(events) == 6

    def test_two_nameless_events_sharing_one_name_adopt_it_for_the_live_one(
        self, sync_session: Session, project_and_type
    ):
        """Which row adopts a contested NAME is decided by the scan's winner rule.

        ``uq_event_scan_identity`` forbids the shape this test used to seed —
        two rows holding one ``source_name`` — so the ORDER BY no longer picks
        between twins. What it decides now is adoption: several rows with NO
        identity can share one name (authored through the API under a type with
        no name format), and exactly one of them may take that name as its
        identity, or the adoption UPDATE itself is the collision the constraint
        refuses (tripl-8tdl).

        Preference: the row traffic most recently landed on. The observable is
        which row gets the identity and the field values; the other stays NULL
        and is not touched at all. The stale row is inserted FIRST on purpose:
        without an ORDER BY SQLite hands rows back in insertion order, the stale
        row adopts first and shuts the live one out — so the test fails with the
        order removed, which is the point.
        """
        project, et, fds = project_and_type
        identity = "screen=/home | action=click"
        stale = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=et.id,
            name=identity,
            source_name=None,
            order=0,
            status="in_review",
            last_seen_at=None,
        )
        live = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=et.id,
            name=identity,
            source_name=None,
            order=1,
            status="in_review",
            last_seen_at=datetime.now(UTC),
        )
        sync_session.add(stale)
        sync_session.flush()
        sync_session.add(live)
        sync_session.flush()

        cardinality = {
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=1,
                is_low=True,
                sample_values=["/home"],
            ),
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=1,
                is_low=True,
                sample_values=["click"],
            ),
        }
        result = generate_events(sync_session, project.id, et.id, _make_analysis(cardinality), fds)
        sync_session.commit()

        # Neither row is recreated — the identity is known through the adopter.
        assert result.events_created == 0
        assert result.events_skipped == 1
        identity_by_event = dict(
            sync_session.execute(
                select(Event.id, Event.source_name).where(Event.id.in_([stale.id, live.id]))
            ).all()
        )
        assert identity_by_event == {live.id: identity, stale.id: None}
        values_by_event = dict(
            sync_session.execute(
                select(EventFieldValue.event_id, func.count())
                .where(EventFieldValue.event_id.in_([stale.id, live.id]))
                .group_by(EventFieldValue.event_id)
            ).all()
        )
        assert values_by_event.get(live.id, 0) > 0
        assert values_by_event.get(stale.id, 0) == 0

    def test_nameless_event_named_like_an_established_identity_is_left_alone(
        self, sync_session: Session, project_and_type
    ):
        """An established identity beats a lazy adoption.

        The row already holding ``source_name`` is the one every previous scan
        routed to. A NULL row that merely happens to be NAMED like it — authored
        by hand after the fact — must not adopt that name: the adoption is an
        UPDATE ``uq_event_scan_identity`` refuses, and before the constraint it
        could move the identity's history onto a row no scan ever wrote. The
        NULL row is the fresher one (``last_seen_at`` set) so a single ordered
        pass that adopts as it files would pick it; only filing every held
        identity BEFORE any adoption keeps it out (tripl-8tdl).
        """
        project, et, fds = project_and_type
        identity = "screen=/home | action=click"
        established = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=et.id,
            name="Home click (renamed)",
            source_name=identity,
            order=0,
            status="live",
            last_seen_at=None,
        )
        orphan = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=et.id,
            name=identity,
            source_name=None,
            order=1,
            status="draft",
            last_seen_at=datetime.now(UTC),
        )
        sync_session.add_all([established, orphan])
        sync_session.commit()

        cardinality = {
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=1,
                is_low=True,
                sample_values=["/home"],
            ),
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=1,
                is_low=True,
                sample_values=["click"],
            ),
        }
        result = generate_events(sync_session, project.id, et.id, _make_analysis(cardinality), fds)
        sync_session.commit()

        assert result.events_created == 0
        assert result.events_skipped == 1
        identity_by_event = dict(
            sync_session.execute(
                select(Event.id, Event.source_name).where(Event.id.in_([established.id, orphan.id]))
            ).all()
        )
        assert identity_by_event == {established.id: identity, orphan.id: None}
        values_by_event = dict(
            sync_session.execute(
                select(EventFieldValue.event_id, func.count())
                .where(EventFieldValue.event_id.in_([established.id, orphan.id]))
                .group_by(EventFieldValue.event_id)
            ).all()
        )
        assert values_by_event.get(established.id, 0) > 0
        assert values_by_event.get(orphan.id, 0) == 0

    def test_losing_the_identity_insert_race_routes_to_the_holder(
        self,
        sync_session: Session,
        project_and_type,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A scan that loses ``uq_event_scan_identity`` to another writer does not die.

        Two scan configs of one project scanning one type, or a scan racing an
        API create, can both miss the identity on load and both INSERT it. The
        loser used to take the whole job down with an IntegrityError; now the
        INSERT runs under a savepoint and the loser adopts the holder as the
        existing event — field values and contexts land on it, it is counted as
        skipped, nothing is created (tripl-8tdl).

        The race is real, not a raised exception. When the scan opens the
        savepoint that guards its INSERT, a competitor row for the identity is
        first written through the SAME connection — before the SAVEPOINT, which
        is exactly where a concurrent commit sits — so the flush inside the
        savepoint hits the actual unique violation and the recovery runs end to
        end against SQLite's enforcement of the constraint. It cannot be
        written inside the savepoint: ROLLBACK TO SAVEPOINT would undo the
        competitor with the scan's own INSERT and the re-select would find no
        holder. Injected once, so the re-select meets one holder.
        """
        project, et, fds = project_and_type
        identity = "screen=/home | action=click"
        main_branch_id = _resolve_main_branch_id(sync_session, project.id)
        assert main_branch_id is not None
        competitor_id = uuid.uuid4()
        original_begin_nested = sync_session.begin_nested
        state = {"raced": 0}

        def _begin_nested_after_a_competitor_lands():
            if not state["raced"]:
                state["raced"] += 1
                sync_session.connection().execute(
                    insert(Event.__table__).values(
                        id=competitor_id,
                        project_id=project.id,
                        branch_id=main_branch_id,
                        event_type_id=et.id,
                        name=identity,
                        source_name=identity,
                        description="",
                        order=7,
                        status="in_review",
                    )
                )
            return original_begin_nested()

        monkeypatch.setattr(sync_session, "begin_nested", _begin_nested_after_a_competitor_lands)

        cardinality = {
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=1,
                is_low=True,
                sample_values=["/home"],
            ),
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=1,
                is_low=True,
                sample_values=["click"],
            ),
        }
        result = generate_events(sync_session, project.id, et.id, _make_analysis(cardinality), fds)
        sync_session.commit()

        assert state["raced"] == 1
        assert result.events_created == 0
        assert result.events_skipped == 1
        holders = (
            sync_session.execute(
                select(Event.id).where(Event.event_type_id == et.id, Event.source_name == identity)
            )
            .scalars()
            .all()
        )
        assert holders == [competitor_id]
        assert result.events_by_name[identity].id == competitor_id
        values_on_holder = sync_session.execute(
            select(func.count()).where(EventFieldValue.event_id == competitor_id)
        ).scalar_one()
        assert values_on_holder == 2  # screen and action, upserted onto the holder

    def test_a_pending_row_of_the_callers_own_keeps_its_own_error(
        self,
        sync_session: Session,
        project_and_type,
    ):
        """The savepoint answers for its INSERT and for nothing else.

        Opening a nested transaction FLUSHES whatever the caller already had
        pending, at ROOT level, before the savepoint exists
        (``SessionTransaction._take_snapshot``). While that call stood inside
        ``insert_event_claiming_identity``'s ``try``, a caller's own violation —
        an adoption UPDATE that lost its race, say — was caught as if it were
        this INSERT's, and the recovery then queried a transaction the failed
        flush had already deactivated: ``PendingRollbackError`` naming nothing,
        for a row this function never touched.

        Here the pending row is a second event claiming an identity a committed
        row already holds, and the event being inserted claims a free one. The
        error the caller sees must be the ``IntegrityError`` its own row earned.
        """
        project, et, _fds = project_and_type
        main_branch_id = _resolve_main_branch_id(sync_session, project.id)
        assert main_branch_id is not None
        taken = "screen=/home | action=click"
        sync_session.add(
            Event(
                id=uuid.uuid4(),
                project_id=project.id,
                branch_id=main_branch_id,
                event_type_id=et.id,
                name=taken,
                source_name=taken,
                order=0,
                status="in_review",
            )
        )
        sync_session.commit()
        # The caller's own doomed work, queued but not flushed.
        sync_session.add(
            Event(
                id=uuid.uuid4(),
                project_id=project.id,
                branch_id=main_branch_id,
                event_type_id=et.id,
                name=taken,
                source_name=taken,
                order=1,
                status="in_review",
            )
        )

        with pytest.raises(IntegrityError):
            _insert_event_claiming_identity(
                sync_session,
                Event(
                    id=uuid.uuid4(),
                    project_id=project.id,
                    branch_id=main_branch_id,
                    event_type_id=et.id,
                    name="screen=/about | action=view",
                    source_name="screen=/about | action=view",
                    order=2,
                    status="in_review",
                ),
            )

    def test_losing_the_group_event_insert_race_merges_into_the_holder(
        self,
        sync_session: Session,
        project_and_type,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The group-rule pass survives losing the group identity the same way.

        ``_create_group_event_from_source`` mints the group event a matched
        source is folded into; when another writer claims the group name first,
        the holder comes back and the source is merged into it like any existing
        target — its field values are NOT copied onto the holder (those belong
        to whoever created the group), everything else the merge carries is
        carried, and the source row is gone. Same injection as the scan test,
        for the same reason (tripl-8tdl).
        """
        project, et, fds = project_and_type
        main_branch_id = _resolve_main_branch_id(sync_session, project.id)
        assert main_branch_id is not None
        source = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=et.id,
            name="button:primary",
            source_name="button:primary",
            order=0,
            status="implemented",
        )
        sync_session.add(source)
        sync_session.flush()
        sync_session.add(
            EventFieldValue(
                id=uuid.uuid4(),
                event_id=source.id,
                field_definition_id=fds["action"].id,
                value="button:primary",
            )
        )
        sync_session.commit()

        competitor_id = uuid.uuid4()
        original_begin_nested = sync_session.begin_nested
        state = {"raced": 0}

        def _begin_nested_after_a_competitor_lands():
            if not state["raced"]:
                state["raced"] += 1
                sync_session.connection().execute(
                    insert(Event.__table__).values(
                        id=competitor_id,
                        project_id=project.id,
                        branch_id=main_branch_id,
                        event_type_id=et.id,
                        name="button events",
                        source_name="button events",
                        description="",
                        order=7,
                        status="draft",
                    )
                )
            return original_begin_nested()

        monkeypatch.setattr(sync_session, "begin_nested", _begin_nested_after_a_competitor_lands)

        merged = merge_existing_events_for_group_rules(
            sync_session,
            project_id=project.id,
            event_type_ids=[et.id],
            event_group_rules=[
                {
                    "name": "button events",
                    "condition_logic": "all",
                    "conditions": [{"field": "action", "pattern": "^button:"}],
                }
            ],
        )
        sync_session.commit()

        assert state["raced"] == 1
        assert merged == 1
        assert sync_session.get(Event, source.id) is None
        events = (
            sync_session.execute(select(Event).where(Event.event_type_id == et.id)).scalars().all()
        )
        assert [event.id for event in events] == [competitor_id]
        holder = events[0]
        assert holder.source_name == "button events"
        # Merged, not copied: the merge lifts the status but leaves the holder's
        # field values to whoever created the group.
        assert holder.status == "implemented"
        values_on_holder = sync_session.execute(
            select(func.count()).where(EventFieldValue.event_id == competitor_id)
        ).scalar_one()
        assert values_on_holder == 0

    def test_group_rules_pass_adopts_a_shared_name_once(
        self, sync_session: Session, project_and_type
    ):
        """Two nameless rows sharing a name must not both adopt it in the group pass.

        ``merge_existing_events_for_group_rules`` used to adopt EVERY NULL row's
        name as its identity, last-wins. Under ``uq_event_scan_identity`` the
        second adoption is an UPDATE the flush refuses, and the apply-groups job
        died on it. Now the pass files by the scan's winner rule: the row traffic
        most recently landed on adopts the name and is grouped; its twin stays
        NULL, stays out of the map, and is not touched (tripl-8tdl).
        """
        project, et, fds = project_and_type
        # The twin that must NOT adopt is inserted first, so a pass that adopts
        # in insertion order rather than by the winner rule picks the wrong one.
        twin = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=et.id,
            name="button:primary",
            source_name=None,
            order=0,
            status="implemented",
            last_seen_at=None,
        )
        sync_session.add(twin)
        sync_session.flush()
        live = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=et.id,
            name="button:primary",
            source_name=None,
            order=1,
            status="implemented",
            last_seen_at=datetime.now(UTC),
        )
        sync_session.add(live)
        sync_session.flush()
        for event in (twin, live):
            sync_session.add(
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    field_definition_id=fds["action"].id,
                    value="button:primary",
                )
            )
        sync_session.commit()

        merged = merge_existing_events_for_group_rules(
            sync_session,
            project_id=project.id,
            event_type_ids=[et.id],
            event_group_rules=[
                {
                    "name": "button events",
                    "condition_logic": "all",
                    "conditions": [{"field": "action", "pattern": "^button:"}],
                }
            ],
        )
        sync_session.commit()

        assert merged == 1
        # The adopter was grouped away; the twin is exactly as it was seeded.
        assert sync_session.get(Event, live.id) is None
        identities = dict(
            sync_session.execute(
                select(Event.id, Event.source_name).where(Event.event_type_id == et.id)
            ).all()
        )
        assert identities.pop(twin.id) is None
        assert list(identities.values()) == ["button events"]

    def test_high_cardinality_generates_templated_events(
        self, sync_session: Session, project_and_type
    ):
        project, et, fds = project_and_type
        cardinality = {
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=5000,
                is_low=False,
                sample_values=[f"/users/{i}/profile" for i in range(200)],
            ),
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=2,
                is_low=True,
                sample_values=["click", "view"],
            ),
        }
        analysis = _make_analysis(cardinality)
        result = generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
        )
        sync_session.commit()

        # screen → 1 template, action → 2 values = 2 events
        assert result.events_created == 2
        assert result.variables_created >= 1

        # Check variable was created
        variables = (
            sync_session.execute(select(Variable).where(Variable.project_id == project.id))
            .scalars()
            .all()
        )
        assert len(variables) >= 1

        contexts = (
            sync_session.execute(
                select(VariableValue).where(VariableValue.project_id == project.id)
            )
            .scalars()
            .all()
        )
        assert len(contexts) == 2
        assert {context.value_kind for context in contexts} == {"high"}
        assert {context.observed_count for context in contexts} == {200}
        assert all(len(context.values) == 20 for context in contexts)

    def test_variable_context_keeps_all_low_cardinality_variable_values(
        self, sync_session: Session, project_and_type
    ):
        project, et, fds = project_and_type
        values = [f"group_cat{i % 12}_{i}" for i in range(120)]
        cardinality = {
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=120,
                is_low=False,
                sample_values=values,
            ),
        }
        analysis = _make_analysis(cardinality)
        generate_events(sync_session, project.id, et.id, analysis, fds, cardinality_threshold=100)
        sync_session.commit()

        contexts = (
            sync_session.execute(
                select(VariableValue).where(
                    VariableValue.project_id == project.id,
                    VariableValue.value_kind == "low",
                )
            )
            .scalars()
            .all()
        )
        assert len(contexts) == 1
        assert contexts[0].observed_count == 12
        assert contexts[0].values == sorted(f"cat{i}" for i in range(12))

    def test_variable_contexts_survive_a_disjoint_sample_window_on_rescan(
        self, sync_session: Session, project_and_type
    ):
        """A rescan whose sample shares nothing with the stored list evicts nothing.

        This test used to pin the opposite — a rescan REPLACED the stored list —
        which is the semantics that cost production one distinct value per
        context per scheduled cycle (2026-08-31). Existing values lead the
        union and the sample cap applies after, so the stored sample keeps the
        earliest values while ``observed_count`` keeps counting.
        """
        project, et, fds = project_and_type

        first = {
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=150,
                is_low=False,
                sample_values=[f"firstvalue{i:03d}" for i in range(150)],
            ),
        }
        generate_events(sync_session, project.id, et.id, _make_analysis(first), fds)
        sync_session.commit()

        second = {
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=150,
                is_low=False,
                sample_values=[f"secondvalue{i:03d}" for i in range(150)],
            ),
        }
        generate_events(sync_session, project.id, et.id, _make_analysis(second), fds)
        sync_session.commit()

        context = sync_session.execute(select(VariableValue)).scalar_one()
        assert len(context.values) == VARIABLE_VALUE_SAMPLE_LIMIT
        assert all(value.startswith("firstvalue") for value in context.values)
        assert context.observed_count == 150

    def test_rescan_preserves_replay_enriched_empty_variable_context_values(
        self, sync_session: Session, project_and_type
    ):
        project, et, fds = project_and_type
        cardinality = {
            "payload": CardinalityResult(
                column=ColumnInfo("payload", "JSON"),
                count=1,
                is_low=False,
                json_path_combos=[("user.id",)],
            ),
        }
        analysis = _make_analysis(cardinality)
        generate_events(sync_session, project.id, et.id, analysis, fds)
        sync_session.commit()

        context = sync_session.execute(select(VariableValue)).scalar_one()
        assert context.source_column == "payload.user.id"
        assert context.observed_count == 0
        assert context.values == []

        context.observed_count = 2
        context.values = ["u1", "u2"]
        sync_session.commit()

        generate_events(sync_session, project.id, et.id, analysis, fds)
        sync_session.commit()

        context = sync_session.execute(select(VariableValue)).scalar_one()
        assert context.value_kind == "high"
        assert context.observed_count == 2
        assert context.values == ["u1", "u2"]

    @staticmethod
    def _json_path_analysis(path: str) -> BreakdownAnalysis:
        """One JSON column carrying one path — the shape that mints a variable."""
        return _make_analysis(
            {
                "payload": CardinalityResult(
                    column=ColumnInfo("payload", "JSON"),
                    count=1,
                    is_low=False,
                    json_path_combos=[(path,)],
                ),
            }
        )

    def test_scan_fills_a_json_variable_context_from_supplied_samples(
        self, sync_session: Session, project_and_type
    ):
        """Observed values reach a JSON-path variable from the sampler, not the rows.

        Every other value-sampling test in this file forces ``is_replay=True``.
        This is the branch the scheduled collector runs, and until
        ``json_path_samples`` existed it wrote ``high``/0/``[]`` for every
        JSON-path variable in every project, unconditionally.
        """
        project, et, fds = project_and_type

        generate_events(
            sync_session,
            project.id,
            et.id,
            self._json_path_analysis("user.plan"),
            fds,
            json_path_samples={"payload": {"user.plan": ["free", "pro"]}},
        )
        sync_session.commit()

        context = sync_session.execute(select(VariableValue)).scalar_one()
        assert context.source_column == "payload.user.plan"
        assert context.observed_count == 2
        assert context.values == ["free", "pro"]
        # High even though two values is far under the cardinality threshold: a
        # sample cannot establish that it saw everything, and ``low`` is read by
        # the UI as "All values". Two back means "at least two".
        assert context.value_kind == "high"

    def test_scan_without_samples_keeps_the_zero_observation(
        self, sync_session: Session, project_and_type
    ):
        """No sampler supplied -> exactly what this wrote before there was one.

        The behaviour-preserving default is the reason ``json_path_samples`` is
        optional: the dry-run, the scan task and every caller that cannot afford a
        warehouse round-trip still plan the same contexts they always did.

        A control, not coverage: this asserts the hardcoded ``high``/0/``[]`` that
        the sampler exists to replace, so it holds whether or not any of the
        sampling machinery is present. Only the tests that supply samples pin it.
        """
        project, et, fds = project_and_type

        generate_events(
            sync_session,
            project.id,
            et.id,
            self._json_path_analysis("user.plan"),
            fds,
        )
        sync_session.commit()

        context = sync_session.execute(select(VariableValue)).scalar_one()
        assert context.value_kind == "high"
        assert context.observed_count == 0
        assert context.values == []

    def test_resampling_a_json_path_merges_into_stored_values(
        self, sync_session: Session, project_and_type
    ):
        """A fresh non-empty sample UNIONS into the stored list, never replaces it.

        The rotating sampler reads a 1-3h window, so a fresh payload carries
        only the values that happened to occur in that window — on production
        (2026-08-31) one scheduled cycle made ten contexts each lose exactly
        one historical value this way. With the pre-fix replace semantics this
        test fails with values == ["b", "e"].
        """
        project, et, fds = project_and_type
        analysis = self._json_path_analysis("user.plan")

        generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
            json_path_samples={"payload": {"user.plan": ["a", "b", "c", "d"]}},
        )
        sync_session.commit()

        generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
            json_path_samples={"payload": {"user.plan": ["b", "e"]}},
        )
        sync_session.commit()

        context = sync_session.execute(select(VariableValue)).scalar_one()
        # Existing values lead, incoming append unique — the same order the
        # metrics sink writes, so the rendered chip order stays stable.
        assert context.values == ["a", "b", "c", "d", "e"]
        assert context.observed_count == 5
        assert context.value_kind == "high"

    def test_merge_past_the_sample_cap_keeps_a_low_enumeration_intact(
        self, sync_session: Session, project_and_type
    ):
        """A low row is an exact enumeration: the sample cap must not trim it.

        Low legitimately holds up to the cardinality threshold's worth of
        values untrimmed — the popover renders it as "All values", and the
        replay sink documents why trimming it to the sample cap makes that
        badge lie. So a merge that outgrows the CAP but not the THRESHOLD
        keeps every value and stays low; demoting at the cap here rewrote
        every 21..100-value enumeration as a 20-value sample on its first
        rescan.
        """
        project, et, fds = project_and_type
        variable = Variable(
            id=uuid.uuid4(),
            project_id=project.id,
            name="plan_group",
            source_name="plan_group",
            variable_type="string",
        )
        event = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=et.id,
            name="Merged",
            source_name="Merged",
            order=0,
        )
        sync_session.add_all([variable, event])
        sync_session.flush()
        existing_values = [f"v{i:02d}" for i in range(VARIABLE_VALUE_SAMPLE_LIMIT - 2)]
        sync_session.add(
            VariableValue(
                id=uuid.uuid4(),
                project_id=project.id,
                variable_id=variable.id,
                event_id=event.id,
                field_definition_id=fds["screen"].id,
                source_column="screen",
                value_kind="low",
                observed_count=len(existing_values),
                values=existing_values,
            )
        )
        sync_session.commit()

        key = (variable.id, event.id, fds["screen"].id)
        # Two values re-observed, three new: the union holds cap + 1 distinct.
        # The incoming kind is HIGH on purpose — a sampled JSON-path
        # observation always arrives high — and must not decide the merged
        # row's kind: the union below the threshold is still an enumeration.
        incoming = [*existing_values[-2:], "w0", "w1", "w2"]
        contexts = {
            key: {
                "variable_id": variable.id,
                "event_id": event.id,
                "field_definition_id": fds["screen"].id,
                "source_column": "screen",
                "value_kind": "high",
                "observed_count": len(incoming),
                "values": incoming,
            }
        }

        prior = preserve_existing_variable_context_values(
            sync_session,
            project_id=project.id,
            branch_id=None,
            contexts=contexts,
        )

        context = contexts[key]
        assert context["values"] == [*existing_values, "w0", "w1", "w2"]
        assert len(context["values"]) == VARIABLE_VALUE_SAMPLE_LIMIT + 1
        assert context["observed_count"] == VARIABLE_VALUE_SAMPLE_LIMIT + 1
        assert context["value_kind"] == "low"
        # The snapshot is the PRE-merge stored list — what the write counter
        # compares the re-inserted row against.
        assert prior == {key: existing_values}

        # Crossing the cardinality THRESHOLD is what demotes: rerun the same
        # merge with the threshold forced down to the cap — now the union's
        # 21 distinct values outgrow an exact enumeration, the row leaves
        # ``low``, the list trims to the sample cap, and the count keeps the
        # uncapped truth. Same fixtures, so the two boundaries are compared
        # on identical data.
        contexts[key] = {
            "variable_id": variable.id,
            "event_id": event.id,
            "field_definition_id": fds["screen"].id,
            "source_column": "screen",
            "value_kind": "low",
            "observed_count": len(incoming),
            "values": list(incoming),
        }
        preserve_existing_variable_context_values(
            sync_session,
            project_id=project.id,
            branch_id=None,
            contexts=contexts,
            cardinality_threshold=VARIABLE_VALUE_SAMPLE_LIMIT,
        )
        demoted = contexts[key]
        assert demoted["values"] == [*existing_values, "w0", "w1"]
        assert len(demoted["values"]) == VARIABLE_VALUE_SAMPLE_LIMIT
        assert demoted["observed_count"] == VARIABLE_VALUE_SAMPLE_LIMIT + 1
        assert demoted["value_kind"] == "high"

        # And the RESTORE arm restores the kind with the values: an empty
        # planned-high observation over the stored low row must hand back the
        # untrimmed enumeration as ``low``, or the next merge trims it.
        contexts[key] = {
            "variable_id": variable.id,
            "event_id": event.id,
            "field_definition_id": fds["screen"].id,
            "source_column": "screen",
            "value_kind": "high",
            "observed_count": 0,
            "values": [],
        }
        preserve_existing_variable_context_values(
            sync_session,
            project_id=project.id,
            branch_id=None,
            contexts=contexts,
        )
        restored = contexts[key]
        assert restored["values"] == existing_values
        assert restored["value_kind"] == "low"
        assert restored["observed_count"] == len(existing_values)

    def test_variable_values_written_counts_only_new_or_changed_rows(
        self, sync_session: Session, project_and_type
    ):
        """Only a row left holding a NEW or CHANGED non-empty list counts.

        The scan task publishes this number in its ``result_summary``, so a
        steady-state cycle that merely restores every row must report zero —
        otherwise the count reads as churn that never happened.
        """
        project, et, fds = project_and_type
        analysis = self._json_path_analysis("user.plan")

        def _generate(samples: list[str] | None):
            return generate_events(
                sync_session,
                project.id,
                et.id,
                analysis,
                fds,
                json_path_samples=(
                    None if samples is None else {"payload": {"user.plan": samples}}
                ),
            )

        minted = _generate(None)
        sync_session.commit()
        assert minted.variable_values_written == 0, "created empty: nothing to read back"

        filled = _generate(["free", "pro"])
        sync_session.commit()
        assert filled.variable_values_written == 1, "a newly filled row counts"

        resampled = _generate(["free", "pro"])
        sync_session.commit()
        assert resampled.variable_values_written == 0, "a byte-identical rewrite does not"

        restored = _generate(None)
        sync_session.commit()
        assert restored.variable_values_written == 0, "an untouched restore does not"

        changed = _generate(["enterprise"])
        sync_session.commit()
        assert changed.variable_values_written == 1, "a changed row counts"

    def _seed_curated_event_with_context(
        self,
        sync_session: Session,
        project,
        et,
        fds,
        *,
        screen_is_authored: bool,
    ) -> VariableValue:
        """A hand-curated event whose ``screen`` reads ``${myvar}``, plus its context.

        The generated identity for the analysis below is ``screen=/home |
        action=click``, so the scan matches this event instead of creating one.
        """
        variable = Variable(
            id=uuid.uuid4(),
            project_id=project.id,
            name="myvar",
            source_name="myvar",
            variable_type="string",
        )
        event = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=et.id,
            name="Curated",
            source_name="screen=/home | action=click",
            order=0,
            status="live",
        )
        sync_session.add_all([variable, event])
        sync_session.flush()
        sync_session.add_all(
            [
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    field_definition_id=fds["screen"].id,
                    value="${myvar}",
                    is_authored=screen_is_authored,
                ),
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    field_definition_id=fds["action"].id,
                    value="click",
                    is_authored=True,
                ),
            ]
        )
        context = VariableValue(
            id=uuid.uuid4(),
            project_id=project.id,
            variable_id=variable.id,
            event_id=event.id,
            field_definition_id=fds["screen"].id,
            source_column="screen",
            value_kind="low",
            observed_count=42,
            values=["curated_a", "curated_b"],
        )
        sync_session.add(context)
        sync_session.flush()
        sync_session.commit()
        return context

    @staticmethod
    def _low_card_analysis() -> BreakdownAnalysis:
        """Both columns low-cardinality, so the run detects NO variables at all."""
        return _make_analysis(
            {
                "screen": CardinalityResult(
                    column=ColumnInfo("screen", "String"),
                    count=1,
                    is_low=True,
                    sample_values=["/home"],
                ),
                "action": CardinalityResult(
                    column=ColumnInfo("action", "String"),
                    count=1,
                    is_low=True,
                    sample_values=["click"],
                ),
            }
        )

    def test_rescan_keeps_variable_contexts_behind_authored_field_values(
        self, sync_session: Session, project_and_type
    ):
        """A scan must not wipe contexts hanging off values it is not allowed to write.

        The scan used to delete EVERY variable context for the event type before
        re-recording, so a curated ``${myvar}`` template it enumerates literally
        (low cardinality → no variable observations) lost its recorded values on
        the first scan — which is how a fresh demo emptied its own "Variables &
        value drift" chapter (bd tripl-jfm3.56).
        """
        project, et, fds = project_and_type
        context = self._seed_curated_event_with_context(
            sync_session, project, et, fds, screen_is_authored=True
        )

        result = generate_events(sync_session, project.id, et.id, self._low_card_analysis(), fds)
        sync_session.commit()
        assert result.events_created == 0
        assert result.events_skipped == 1

        survived = sync_session.get(VariableValue, context.id)
        assert survived is not None
        assert survived.observed_count == 42
        assert survived.values == ["curated_a", "curated_b"]
        # The authored template is still what the events table shows.
        field_value = sync_session.execute(
            select(EventFieldValue).where(
                EventFieldValue.event_id == survived.event_id,
                EventFieldValue.field_definition_id == fds["screen"].id,
            )
        ).scalar_one()
        assert field_value.value == "${myvar}"

    def test_rescan_drops_variable_contexts_whose_field_value_it_rewrote(
        self, sync_session: Session, project_and_type
    ):
        """The same context IS stale once the scan overwrites the value it describes."""
        project, et, fds = project_and_type
        context = self._seed_curated_event_with_context(
            sync_session, project, et, fds, screen_is_authored=False
        )

        generate_events(sync_session, project.id, et.id, self._low_card_analysis(), fds)
        sync_session.commit()

        assert sync_session.get(VariableValue, context.id) is None
        field_value = sync_session.execute(
            select(EventFieldValue).where(
                EventFieldValue.field_definition_id == fds["screen"].id,
            )
        ).scalar_one()
        assert field_value.value == "/home"

    def test_rescan_spares_an_excluded_variables_context_on_the_same_rewritten_field(
        self, sync_session: Session, project_and_type
    ):
        """A rewrite invalidates per VARIABLE, not per field.

        The two rows here sit on one ``(event, field)`` and one rewrite, and only
        the live one may go. ``record_variable_contexts`` skips an excluded
        variable, so nothing in this run — or any later one — re-inserts its row;
        invalidating it is a permanent delete, performed silently inside a scan,
        which is exactly what excluding a variable stopped meaning (bd
        tripl-95pu). The live row going in the same breath is what keeps the
        exemption from being read as "a rewrite invalidates nothing".
        """
        project, et, fds = project_and_type
        live = self._seed_curated_event_with_context(
            sync_session, project, et, fds, screen_is_authored=False
        )
        excluded = Variable(
            id=uuid.uuid4(),
            project_id=project.id,
            name="retired",
            source_name="retired",
            variable_type="string",
            excluded_from_scans=True,
        )
        sync_session.add(excluded)
        sync_session.flush()
        tombstoned = VariableValue(
            id=uuid.uuid4(),
            project_id=project.id,
            variable_id=excluded.id,
            event_id=live.event_id,
            field_definition_id=live.field_definition_id,
            source_column="screen",
            value_kind="low",
            observed_count=7,
            values=["seen_before_exclusion"],
        )
        sync_session.add(tombstoned)
        sync_session.commit()
        live_id, tombstoned_id = live.id, tombstoned.id

        generate_events(sync_session, project.id, et.id, self._low_card_analysis(), fds)
        sync_session.commit()

        assert sync_session.get(VariableValue, live_id) is None
        # Columns, not the entity: a row read back through the identity map would
        # look intact after the delete this test exists to catch.
        survived = sync_session.execute(
            select(VariableValue.observed_count, VariableValue.values).where(
                VariableValue.id == tombstoned_id
            )
        ).one_or_none()
        assert survived is not None, "a scan must not delete what no scan can re-record"
        assert survived == (7, ["seen_before_exclusion"])

    def test_rescan_spares_an_excluded_variable_whose_only_token_a_sibling_claims(
        self, sync_session: Session, project_and_type
    ):
        """Losing a token race does not make an excluded variable's rows collectable.

        Same exemption as the test above, but the excluded variable names exactly
        one token and an earlier-sorted sibling already claims it — ``myvar``
        bound to ``retired``, a binding the API accepts because
        ``_check_binding_conflicts`` compares against other variables'
        ``bindings`` and ``source_name`` and never their ``name``. The excluded
        variable is then absent from the index's token map, and reading the
        exemption set off that map left it out, so the rewrite below took its
        rows (bd tripl-cef2). Nothing restates them: ``resolve`` answers that
        token with the sibling, so ``record_variable_contexts`` never reaches the
        excluded one at all — the delete is permanent, and silent.

        The assertion is on the stored row, not on ``excluded_ids()``: a set the
        deleter had stopped reading would still satisfy the second.
        """
        project, et, fds = project_and_type
        live = self._seed_curated_event_with_context(
            sync_session, project, et, fds, screen_is_authored=False
        )
        shadowing = sync_session.get(Variable, live.variable_id)
        assert shadowing is not None
        shadowing.bindings = ["retired"]
        excluded = Variable(
            id=uuid.uuid4(),
            project_id=project.id,
            name="retired",
            # NULL on purpose: the replay path writes ``VariableValue`` rows
            # without backfilling ``source_name``, so a hand-made variable really
            # does reach a scan owning rows and nothing but its display name.
            source_name=None,
            variable_type="string",
            excluded_from_scans=True,
        )
        sync_session.add(excluded)
        sync_session.flush()
        tombstoned = VariableValue(
            id=uuid.uuid4(),
            project_id=project.id,
            variable_id=excluded.id,
            event_id=live.event_id,
            field_definition_id=live.field_definition_id,
            source_column="screen",
            value_kind="low",
            observed_count=7,
            values=["seen_before_exclusion"],
        )
        sync_session.add(tombstoned)
        sync_session.commit()
        live_id, tombstoned_id = live.id, tombstoned.id

        generate_events(sync_session, project.id, et.id, self._low_card_analysis(), fds)
        sync_session.commit()

        assert sync_session.get(VariableValue, live_id) is None
        # Columns, not the entity: a row read back through the identity map would
        # look intact after the delete this test exists to catch.
        survived = sync_session.execute(
            select(VariableValue.observed_count, VariableValue.values).where(
                VariableValue.id == tombstoned_id
            )
        ).one_or_none()
        assert survived is not None, "a shadowed exclusion is still an exclusion"
        assert survived == (7, ["seen_before_exclusion"])

    def test_event_name_column_enumerated_despite_high_cardinality(
        self, sync_session: Session, project_and_type
    ):
        """A high-cardinality column used as the event name must enumerate one event per
        distinct value instead of collapsing into a single ${col} template."""
        project, et, fds = project_and_type
        actions = [f"evt_{i}" for i in range(150)]
        cardinality = {
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=150,
                is_low=False,
                sample_values=actions,
            ),
        }
        analysis = _make_analysis(cardinality)
        result = generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
            event_name_format="{action}",
        )
        sync_session.commit()

        assert result.events_created == 150
        events = (
            sync_session.execute(select(Event).where(Event.project_id == project.id))
            .scalars()
            .all()
        )
        names = {e.name for e in events}
        assert "${action}" not in names
        assert "evt_0" in names
        assert "evt_149" in names

    def test_generate_events_writes_no_row_for_an_empty_derived_name(
        self, sync_session: Session, project_and_type
    ):
        """The persist half needs no guard of its own (tripl-wkwv.5).

        A NULL naming column derives ``""`` and the run used to write
        ``Event(name="", source_name="", status="in_review")`` — a row the metric
        collector's ``if event_name:`` gate can never measure, and a zero-width
        unlabelled link on every surface. The planner is the single place the run
        and the dry-run both read, so the guard lives there; re-declaring a naming
        rule in a second module is the drift this repo has already paid for twice.

        ``events_skipped`` stays 0 on purpose: that counter means "this identity
        was already in the plan", and these rows are not an identity at all.
        """
        project, et, fds = project_and_type
        cardinality = {
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=1,
                is_low=True,
                sample_values=[None],
            ),
        }

        result = generate_events(
            sync_session,
            project.id,
            et.id,
            _make_analysis(cardinality),
            fds,
            event_name_format="{action}",
        )
        sync_session.commit()

        assert result.events_created == 0
        assert result.events_skipped == 0
        written = (
            sync_session.execute(select(Event).where(Event.project_id == project.id))
            .scalars()
            .all()
        )
        assert written == []
        # The plan's disclosure reaches the run report through `details.extend`.
        assert "Skipped 1 row whose derived event name was empty" in result.details

    def test_group_rule_collapses_matching_generated_events(
        self, sync_session: Session, project_and_type
    ):
        project, et, fds = project_and_type
        cardinality = {
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=3,
                is_low=True,
                sample_values=["button:primary", "button:secondary", "page:view"],
            ),
        }
        analysis = _make_analysis(cardinality)
        result = generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
            event_name_format="{action}",
            event_group_rules=[
                {
                    "name": "button events",
                    "condition_logic": "all",
                    "conditions": [{"field": "action", "pattern": "^button:"}],
                }
            ],
        )
        sync_session.commit()

        assert result.events_created == 2
        assert result.events_skipped == 1
        assert result.events_grouped == 2

        events = (
            sync_session.execute(select(Event).where(Event.project_id == project.id))
            .scalars()
            .all()
        )
        assert {event.source_name for event in events} == {"button events", "page:view"}
        grouped_event = next(event for event in events if event.source_name == "button events")
        action_value = sync_session.execute(
            select(EventFieldValue.value).where(
                EventFieldValue.event_id == grouped_event.id,
                EventFieldValue.field_definition_id == fds["action"].id,
            )
        ).scalar_one()
        assert action_value == "/^button:/"

    def test_collapsing_rows_keep_the_value_of_the_busiest_row(
        self, sync_session: Session, project_and_type
    ):
        """Regression: an event keeps one value per field, so breakdown rows that
        collapse onto the same identity overwrite each other and the LAST row
        processed used to win. Every adapter returns rows ``ORDER BY _cnt DESC``,
        which made "last" the *rarest* combination the event ever fired on — a
        windbar tap advertising ``purchase/main`` because three sessions out of
        twelve thousand happened to reach it from there.
        """
        project, et, fds = project_and_type
        analysis = BreakdownAnalysis(
            results={
                "action": CardinalityResult(
                    column=ColumnInfo("action", "String"),
                    count=1,
                    is_low=True,
                    sample_values=["windbar_tap"],
                ),
                "screen": CardinalityResult(
                    column=ColumnInfo("screen", "String"),
                    count=3,
                    is_low=True,
                    sample_values=["spot", "profile", "purchase/main"],
                ),
            },
            # Shaped the way an adapter hands them over: one row per combination,
            # the ``_cnt`` appended last, ordered by it descending.
            rows=[
                ("windbar_tap", "spot", 12000),
                ("windbar_tap", "profile", 900),
                ("windbar_tap", "purchase/main", 3),
            ],
            reg_names=["action", "screen"],
            json_names=[],
        )

        result = generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
            event_name_format="{action}",
        )
        sync_session.commit()

        assert result.events_created == 1
        screen_value = sync_session.execute(
            select(EventFieldValue.value).where(
                EventFieldValue.field_definition_id == fds["screen"].id
            )
        ).scalar_one()
        assert screen_value == "spot"
        # And it says so, rather than discarding the other two in silence.
        assert any("windbar_tap.screen (3 values)" in detail for detail in result.details)

    def test_busiest_row_first_does_not_disturb_event_creation_order(
        self, sync_session: Session, project_and_type
    ):
        """Re-ordering happens WITHIN an identity, never across identities.

        ``order`` is handed out as identities are first created, so sorting the
        whole plan by row count would renumber events that had nothing to do with
        a collapse — here it would put ``debug_ping``, the rarest row in the scan,
        at the top of the catalog. Identities keep their first-appearance order;
        only the rows inside one of them move.
        """
        project, et, fds = project_and_type
        analysis = BreakdownAnalysis(
            results={
                "action": CardinalityResult(
                    column=ColumnInfo("action", "String"),
                    count=2,
                    is_low=True,
                    sample_values=["session_start", "debug_ping"],
                ),
                "screen": CardinalityResult(
                    column=ColumnInfo("screen", "String"),
                    count=3,
                    is_low=True,
                    sample_values=["map", "settings", "devtools"],
                ),
            },
            # Every row of ``session_start`` outruns the single ``debug_ping``
            # row, so a plan-wide sort by count would create them the other way
            # round. Within ``session_start`` the busiest row is NOT last.
            rows=[
                ("session_start", "map", 900),
                ("session_start", "settings", 800),
                ("debug_ping", "devtools", 5),
            ],
            reg_names=["action", "screen"],
            json_names=[],
        )

        result = generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
            event_name_format="{action}",
        )
        sync_session.commit()

        assert result.events_created == 2
        events = (
            sync_session.execute(
                select(Event).where(Event.project_id == project.id).order_by(Event.order)
            )
            .scalars()
            .all()
        )
        assert [event.name for event in events] == ["session_start", "debug_ping"]
        busiest_screen = sync_session.execute(
            select(EventFieldValue.value).where(
                EventFieldValue.event_id == events[0].id,
                EventFieldValue.field_definition_id == fds["screen"].id,
            )
        ).scalar_one()
        assert busiest_screen == "map"

    def test_group_collapse_into_existing_event_does_not_duplicate_field_values(
        self, sync_session: Session, project_and_type
    ):
        """Regression: several scan rows collapsing (via a group rule) into one
        pre-existing event must not re-insert the same
        ``(event_id, field_definition_id)`` pair and violate
        ``uq_event_field_value_event_field``.

        The existing event is loaded at scan start with its ``field_values``
        eagerly populated and *without* the action field. Each collapsing row
        used to queue a fresh ``EventFieldValue`` because the queued value never
        showed up in the already-loaded relationship — producing two inserts
        for the same pair in a single flush.
        """
        project, et, fds = project_and_type
        existing = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=et.id,
            name="button events",
            source_name="button events",
            order=0,
            status="live",
        )
        sync_session.add(existing)
        sync_session.commit()

        cardinality = {
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=2,
                is_low=True,
                sample_values=["button:primary", "button:secondary"],
            ),
        }
        analysis = _make_analysis(cardinality)
        result = generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
            event_name_format="{action}",
            event_group_rules=[
                {
                    "name": "button events",
                    "condition_logic": "all",
                    "conditions": [{"field": "action", "pattern": "^button:"}],
                }
            ],
        )
        sync_session.commit()

        assert result.events_created == 0
        events = (
            sync_session.execute(select(Event).where(Event.project_id == project.id))
            .scalars()
            .all()
        )
        assert {event.source_name for event in events} == {"button events"}

        field_values = (
            sync_session.execute(
                select(EventFieldValue).where(EventFieldValue.event_id == existing.id)
            )
            .scalars()
            .all()
        )
        # Exactly one row for (event_id, action) — not two.
        action_values = [fv for fv in field_values if fv.field_definition_id == fds["action"].id]
        assert len(action_values) == 1

    def test_group_rule_merges_existing_matching_events_and_metrics(
        self, sync_session: Session, project_and_type
    ):
        project, et, fds = project_and_type
        # A real ScanConfig row, not a fabricated uuid: EventMetric.scan_config_id
        # is a NOT NULL foreign key, and the engine now enforces it.
        scan_config_id = _seed_scan_config(sync_session, project)
        bucket = datetime(2026, 4, 12, 10, 0)
        old_events: list[Event] = []
        for index, action in enumerate(["button:primary", "button:secondary"]):
            event = Event(
                id=uuid.uuid4(),
                project_id=project.id,
                event_type_id=et.id,
                name=action,
                source_name=action,
                order=index,
                status="implemented",
            )
            sync_session.add(event)
            sync_session.flush()
            sync_session.add(
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    field_definition_id=fds["action"].id,
                    value=action,
                )
            )
            sync_session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config_id,
                    event_id=event.id,
                    event_type_id=None,
                    bucket=bucket,
                    count=5 + index,
                )
            )
            old_events.append(event)
        sync_session.commit()

        cardinality = {
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=2,
                is_low=True,
                sample_values=["button:primary", "button:secondary"],
            ),
        }
        analysis = _make_analysis(cardinality)
        result = generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
            event_name_format="{action}",
            event_group_rules=[
                {
                    "name": "button events",
                    "condition_logic": "all",
                    "conditions": [{"field": "action", "pattern": "^button:"}],
                }
            ],
        )
        sync_session.commit()

        assert result.events_merged == 2
        events = (
            sync_session.execute(select(Event).where(Event.project_id == project.id))
            .scalars()
            .all()
        )
        assert {event.source_name for event in events} == {"button events"}
        grouped_event = events[0]
        metric = sync_session.execute(
            select(EventMetric).where(EventMetric.event_id == grouped_event.id)
        ).scalar_one()
        assert metric.count == 11
        assert all(sync_session.get(Event, event.id) is None for event in old_events)

    def test_post_factum_group_rules_merge_existing_events(
        self, sync_session: Session, project_and_type
    ):
        project, et, fds = project_and_type
        for index, action in enumerate(["button:primary", "button:secondary"]):
            event = Event(
                id=uuid.uuid4(),
                project_id=project.id,
                event_type_id=et.id,
                name=action,
                source_name=action,
                order=index,
                status="implemented",
            )
            sync_session.add(event)
            sync_session.flush()
            sync_session.add(
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    field_definition_id=fds["action"].id,
                    value=action,
                )
            )
        sync_session.commit()

        merged = merge_existing_events_for_group_rules(
            sync_session,
            project_id=project.id,
            event_type_ids=[et.id],
            event_group_rules=[
                {
                    "name": "button events",
                    "condition_logic": "all",
                    "conditions": [{"field": "action", "pattern": "^button:"}],
                }
            ],
        )
        sync_session.commit()

        assert merged == 2
        events = (
            sync_session.execute(select(Event).where(Event.project_id == project.id))
            .scalars()
            .all()
        )
        assert {event.source_name for event in events} == {"button events"}
        grouped_event = events[0]
        action_value = sync_session.execute(
            select(EventFieldValue.value).where(
                EventFieldValue.event_id == grouped_event.id,
                EventFieldValue.field_definition_id == fds["action"].id,
            )
        ).scalar_one()
        assert action_value == "/^button:/"

    def test_post_factum_group_rules_merge_multiline_values(
        self, sync_session: Session, project_and_type
    ):
        # Free-text event values can be multi-line (e.g. a pasted notification body
        # captured as the activity name). An anchored ``^...$`` rule must still match
        # them against the whole value, so ``.`` has to span newlines.
        project, et, fds = project_and_type
        single_line = "page_select_sport_other_activity_running_chosen"
        multi_line = (
            "page_select_sport_other_activity_Gewitter (stark)\n"
            "Kreis Emsland\n\n> 50% - Wahrscheinlich\n_chosen"
        )
        for index, action in enumerate([single_line, multi_line]):
            event = Event(
                id=uuid.uuid4(),
                project_id=project.id,
                event_type_id=et.id,
                name=action,
                source_name=action,
                order=index,
                status="implemented",
            )
            sync_session.add(event)
            sync_session.flush()
            sync_session.add(
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=event.id,
                    field_definition_id=fds["action"].id,
                    value=action,
                )
            )
        sync_session.commit()

        merged = merge_existing_events_for_group_rules(
            sync_session,
            project_id=project.id,
            event_type_ids=[et.id],
            event_group_rules=[
                {
                    "name": "page_select_sport_other_activity_*_chosen",
                    "condition_logic": "all",
                    "conditions": [
                        {
                            "field": "action",
                            "pattern": "^page_select_sport_other_activity_.*_chosen$",
                        }
                    ],
                }
            ],
        )
        sync_session.commit()

        # Both the single-line and the multi-line value collapse into one group.
        assert merged == 2
        events = (
            sync_session.execute(select(Event).where(Event.project_id == project.id))
            .scalars()
            .all()
        )
        assert {event.source_name for event in events} == {
            "page_select_sport_other_activity_*_chosen"
        }

    def test_ensure_variable_tolerates_same_name_on_other_branch(
        self, sync_session: Session, project_and_type
    ):
        # Variable uniqueness is per (project_id, branch_id, source_name), so a working
        # plan branch can hold a same-named variable as main. An unscoped lookup spans
        # branches and used to raise "Multiple rows were found"; scoping to the scan's
        # main branch must find exactly the main-branch row (regression test).
        project, _et, _fds = project_and_type
        # The fixture's ORM inserts already auto-created the project's main branch.
        main_branch_id = _resolve_main_branch_id(sync_session, project.id)
        assert main_branch_id is not None
        working_branch = PlanBranch(
            id=uuid.uuid4(),
            project_id=project.id,
            name="feature",
            kind=BranchKind.working.value,
            status=BranchStatus.draft.value,
            description="",
        )
        sync_session.add(working_branch)
        sync_session.flush()
        for branch_id in (main_branch_id, working_branch.id):
            sync_session.add(
                Variable(
                    id=uuid.uuid4(),
                    project_id=project.id,
                    branch_id=branch_id,
                    name="property.spot_id",
                    source_name="property.spot_id",
                    variable_type="string",
                )
            )
        sync_session.commit()

        resolved = _resolve_main_branch_id(sync_session, project.id)
        assert resolved == main_branch_id

        # Scoped lookup finds the existing main-branch variable without raising.
        created = _ensure_variable(
            sync_session, project.id, "property.spot_id", "string", branch_id=resolved
        )
        assert created == 0

        variables = (
            sync_session.execute(select(Variable).where(Variable.source_name == "property.spot_id"))
            .scalars()
            .all()
        )
        assert len(variables) == 2  # nothing new created

    def test_ensure_variable_handles_integrity_race(
        self,
        sync_session: Session,
        project_and_type,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Concurrent inserts should be treated as 'already exists' (no exception)."""
        project, _et, _fds = project_and_type
        main_branch_id = _resolve_main_branch_id(sync_session, project.id)
        assert main_branch_id is not None

        original_flush = sync_session.flush
        state = {"raised": False}

        def _flush_once_fails(*args, **kwargs):
            has_target_pending = any(
                isinstance(obj, Variable) and obj.source_name == "property.concurrent"
                for obj in sync_session.new
            )
            if has_target_pending and not state["raised"]:
                state["raised"] = True
                raise IntegrityError("insert into variables", {}, Exception("duplicate key"))
            return original_flush(*args, **kwargs)

        monkeypatch.setattr(sync_session, "flush", _flush_once_fails)

        created = _ensure_variable(
            sync_session,
            project.id,
            "property.concurrent",
            "string",
            branch_id=main_branch_id,
        )
        assert created == 0

    def test_dedup_skips_existing(self, sync_session: Session, project_and_type):
        project, et, fds = project_and_type
        cardinality = {
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=2,
                is_low=True,
                sample_values=["/home", "/about"],
            ),
        }
        analysis = _make_analysis(cardinality)
        # First run
        result1 = generate_events(sync_session, project.id, et.id, analysis, fds)
        sync_session.commit()
        assert result1.events_created == 2

        # Second run — same data — should skip
        result2 = generate_events(sync_session, project.id, et.id, analysis, fds)
        sync_session.commit()
        assert result2.events_created == 0
        assert result2.events_skipped == 2

    def test_scan_preserves_authored_field_values_and_creates_missing_values(
        self, sync_session: Session, project_and_type
    ):
        project, et, fds = project_and_type
        existing = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=et.id,
            name="/home",
            source_name="/home",
            order=0,
            status="live",
        )
        sync_session.add(existing)
        sync_session.flush()
        sync_session.add(
            EventFieldValue(
                id=uuid.uuid4(),
                event_id=existing.id,
                field_definition_id=fds["screen"].id,
                value="${screen}",
                is_authored=True,
            )
        )
        sync_session.commit()

        cardinality = {
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=1,
                is_low=True,
                sample_values=["/home"],
            ),
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=1,
                is_low=True,
                sample_values=["view"],
            ),
        }
        analysis = _make_analysis(cardinality)

        first_result = generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
            event_name_format="{screen}",
        )
        sync_session.commit()

        assert first_result.events_created == 0
        assert first_result.events_skipped == 1
        first_values = {
            field_value.field_definition_id: field_value
            for field_value in sync_session.execute(
                select(EventFieldValue).where(EventFieldValue.event_id == existing.id)
            ).scalars()
        }
        assert first_values[fds["screen"].id].value == "${screen}"
        assert first_values[fds["screen"].id].is_authored is True
        assert first_values[fds["action"].id].value == "view"
        assert first_values[fds["action"].id].is_authored is False

        second_result = generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
            event_name_format="{screen}",
        )
        sync_session.commit()

        assert second_result.events_created == 0
        assert second_result.events_skipped == 1
        second_values = (
            sync_session.execute(
                select(EventFieldValue).where(EventFieldValue.event_id == existing.id)
            )
            .scalars()
            .all()
        )
        assert len(second_values) == 2
        second_values_by_field = {
            field_value.field_definition_id: field_value for field_value in second_values
        }
        assert second_values_by_field[fds["screen"].id].value == "${screen}"
        assert second_values_by_field[fds["screen"].id].is_authored is True
        assert second_values_by_field[fds["action"].id].value == "view"
        assert second_values_by_field[fds["action"].id].is_authored is False

    def test_rename_does_not_recreate_event(self, sync_session: Session, project_and_type):
        """Renaming an event's display ``name`` must not make the next scan duplicate it:
        dedup keys on the stable ``source_name`` identity, not ``name``."""
        project, et, fds = project_and_type
        cardinality = {
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=2,
                is_low=True,
                sample_values=["/home", "/about"],
            ),
        }
        analysis = _make_analysis(cardinality)
        r1 = generate_events(
            sync_session, project.id, et.id, analysis, fds, event_name_format="{screen}"
        )
        sync_session.commit()
        assert r1.events_created == 2

        # source_name is set to the scan identity on creation
        home = sync_session.execute(select(Event).where(Event.source_name == "/home")).scalar_one()
        assert home.name == "/home"
        # User renames the display name
        home.name = "Home Page (renamed)"
        sync_session.commit()

        # Re-scan same data: must match by source_name, not recreate
        r2 = generate_events(
            sync_session, project.id, et.id, analysis, fds, event_name_format="{screen}"
        )
        sync_session.commit()
        assert r2.events_created == 0
        assert r2.events_skipped == 2

        events = (
            sync_session.execute(select(Event).where(Event.project_id == project.id))
            .scalars()
            .all()
        )
        assert len(events) == 2  # no duplicate
        renamed = sync_session.execute(
            select(Event).where(Event.source_name == "/home")
        ).scalar_one()
        assert renamed.name == "Home Page (renamed)"  # rename preserved across scan

    def test_legacy_event_without_source_name_is_backfilled_not_duplicated(
        self, sync_session: Session, project_and_type
    ):
        """An event predating source_name (NULL) is adopted on the next scan instead of
        being recreated."""
        project, et, fds = project_and_type
        legacy = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=et.id,
            name="/home",
            source_name=None,
            description="manually created",
            order=0,
            status="live",
        )
        sync_session.add(legacy)
        sync_session.add(
            EventFieldValue(
                id=uuid.uuid4(),
                event_id=legacy.id,
                field_definition_id=fds["screen"].id,
                value="/home",
            )
        )
        sync_session.commit()

        cardinality = {
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=1,
                is_low=True,
                sample_values=["/home"],
            ),
        }
        analysis = _make_analysis(cardinality)
        result = generate_events(
            sync_session, project.id, et.id, analysis, fds, event_name_format="{screen}"
        )
        sync_session.commit()

        assert result.events_created == 0
        assert result.events_skipped == 1
        refreshed = sync_session.execute(select(Event).where(Event.id == legacy.id)).scalar_one()
        assert refreshed.source_name == "/home"  # backfilled

    def test_max_events_limit(self, sync_session: Session, project_and_type):
        project, et, fds = project_and_type
        cardinality = {
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=50,
                is_low=True,
                sample_values=[f"/page/{i}" for i in range(50)],
            ),
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=50,
                is_low=True,
                sample_values=[f"act_{i}" for i in range(50)],
            ),
        }
        analysis = _make_analysis(cardinality)
        result = generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
            max_events=10,
        )
        sync_session.commit()
        assert result.events_created == 10

    def test_skips_unmatched_columns(self, sync_session: Session, project_and_type):
        project, et, fds = project_and_type
        cardinality = {
            "unknown_col": CardinalityResult(
                column=ColumnInfo("unknown_col", "String"),
                count=5,
                is_low=True,
                sample_values=["a", "b"],
            ),
        }
        analysis = _make_analysis(cardinality)
        result = generate_events(sync_session, project.id, et.id, analysis, fds)
        assert result.events_created == 0
        assert "no matching field definition" in result.details[0].lower()

    def test_does_not_warn_about_a_column_this_event_type_never_fills(
        self, sync_session: Session, project_and_type
    ):
        """A grouped scan sees the whole table's columns on every event type.

        Warning about one that held nothing for these rows is noise, not a plan
        gap — the demo logged ~21 of them per scan (tripl-jfm3.57). ``count``
        excludes NULLs, so 0 means no value in any row of this group.
        """
        project, et, fds = project_and_type
        cardinality = {
            "belongs_to_another_event": CardinalityResult(
                column=ColumnInfo("belongs_to_another_event", "Float64"),
                count=0,
                is_low=True,
                sample_values=[],
            ),
        }
        analysis = _make_analysis(cardinality)
        result = generate_events(sync_session, project.id, et.id, analysis, fds)

        assert not any("no matching field definition" in d.lower() for d in result.details)
        # The separate "nothing matched at all" notice is a different, accurate
        # message and must survive — this test suppresses per-column noise, not
        # the summary that tells you the scan produced nothing.
        assert result.details == ["No columns matched field definitions"]

    def test_does_not_warn_about_a_reserved_column_that_carries_data(
        self, sync_session: Session, project_and_type
    ):
        """A reserved column has no FieldDefinition BY DESIGN, so saying so is wrong.

        app_version / platform / event-group-rule columns are metric dimensions
        or identity inputs, and ``reserved_catalog_columns`` is exactly what keeps
        them out of the catalog. Reporting their absence as a plan gap left a
        fresh demo's first scan claiming six missing fields when one was missing
        (tripl-jfm3.90). Unlike the count==0 case above, these columns DO carry
        data — the emptiness rule cannot cover them.
        """
        project, et, fds = project_and_type
        cardinality = {
            "app_version": CardinalityResult(
                column=ColumnInfo("app_version", "String"),
                count=12,
                is_low=True,
                sample_values=["7.1.0", "7.2.0"],
            ),
            "event_name": CardinalityResult(
                column=ColumnInfo("event_name", "String"),
                count=9,
                is_low=True,
                sample_values=["Home Screen View"],
            ),
            "screen_name": CardinalityResult(
                column=ColumnInfo("screen_name", "String"),
                count=7,
                is_low=True,
                sample_values=["home"],
            ),
        }
        analysis = _make_analysis(cardinality)
        result = generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
            reserved_columns={"app_version", "event_name"},
        )

        # 'screen_name' is genuinely undeclared and must still be reported — the
        # message stays useful precisely because the reserved ones stopped firing.
        assert [d for d in result.details if "no matching field definition" in d.lower()] == [
            "Skipped column 'screen_name': no matching field definition"
        ]

    def test_an_undeclared_column_still_reports_without_a_reserved_set(
        self, sync_session: Session, project_and_type
    ):
        """The default (no reserved_columns) must not silence anything."""
        project, et, fds = project_and_type
        cardinality = {
            "app_version": CardinalityResult(
                column=ColumnInfo("app_version", "String"),
                count=12,
                is_low=True,
                sample_values=["7.1.0"],
            ),
        }
        analysis = _make_analysis(cardinality)
        result = generate_events(sync_session, project.id, et.id, analysis, fds)

        assert any("no matching field definition" in d.lower() for d in result.details)

    def test_event_type_column_excluded(self, sync_session: Session, project_and_type):
        project, et, fds = project_and_type
        cardinality = {
            "event_type": CardinalityResult(
                column=ColumnInfo("event_type", "String"),
                count=3,
                is_low=True,
                sample_values=["pv", "se", "pp"],
            ),
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=2,
                is_low=True,
                sample_values=["/home", "/about"],
            ),
        }
        analysis = _make_analysis(cardinality)
        gen_result = generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
            event_type_column="event_type",
        )
        sync_session.commit()
        # event_type column excluded → only screen → 2 events
        assert gen_result.events_created == 2

    def test_field_values_stored_correctly(self, sync_session: Session, project_and_type):
        project, et, fds = project_and_type
        cardinality = {
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=1,
                is_low=True,
                sample_values=["/home"],
            ),
        }
        analysis = _make_analysis(cardinality)
        generate_events(sync_session, project.id, et.id, analysis, fds)
        sync_session.commit()

        events = (
            sync_session.execute(select(Event).where(Event.project_id == project.id))
            .scalars()
            .all()
        )
        assert len(events) == 1

        fvs = (
            sync_session.execute(
                select(EventFieldValue).where(EventFieldValue.event_id == events[0].id)
            )
            .scalars()
            .all()
        )
        assert len(fvs) == 1
        assert fvs[0].value == "/home"
        assert fvs[0].field_definition_id == fds["screen"].id

    def test_json_paths_can_keep_selected_values_as_is(
        self,
        sync_session: Session,
        project_and_type,
    ):
        project, et, fds = project_and_type
        analysis = BreakdownAnalysis(
            results={
                "payload": CardinalityResult(
                    column=ColumnInfo("payload", "JSON"),
                    count=1,
                    is_low=True,
                    json_path_combos=[("extra.key", "locale")],
                ),
            },
            rows=[(("extra.key", "locale"), '"TASK-123"')],
            reg_names=[],
            json_names=["payload"],
            json_value_names=["payload.extra.key"],
        )

        result = generate_events(sync_session, project.id, et.id, analysis, fds)
        sync_session.commit()

        assert result.events_created == 1

        payload_value = sync_session.execute(
            select(EventFieldValue.value).where(
                EventFieldValue.field_definition_id == fds["payload"].id
            )
        ).scalar_one()
        assert payload_value == '{"extra": {"key": "TASK-123"}, "locale": "${locale}"}'

        variable_names = {
            variable.name
            for variable in sync_session.execute(
                select(Variable).where(Variable.project_id == project.id)
            ).scalars()
        }
        # Scan-created path variables get short display names (identity stays
        # on source_name/bindings).
        assert "locale" in variable_names
        assert "payload.locale" not in variable_names
        assert "extra.key" not in variable_names

    def test_longest_event_name_truncated(
        self,
        sync_session: Session,
        project_and_type,
    ):
        project, et, fds = project_and_type
        longest_name = "a" * 600
        analysis = BreakdownAnalysis(
            results={
                "screen": CardinalityResult(
                    column=ColumnInfo("screen", "String"),
                    count=1,
                    is_low=True,
                    sample_values=[longest_name],
                ),
            },
            rows=[(longest_name,)],
            reg_names=["screen"],
            json_names=[],
            json_value_names=[],
        )

        result = generate_events(
            sync_session,
            project.id,
            et.id,
            analysis,
            fds,
            event_name_format="{screen}",
        )
        sync_session.commit()

        assert result.events_created == 1
        events = (
            sync_session.execute(select(Event).where(Event.project_id == project.id))
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert len(events[0].name) == 500
        assert events[0].name.endswith("...")

    def test_archived_event_not_in_events_by_name(
        self,
        sync_session: Session,
        project_and_type,
    ):
        project, et, fds = project_and_type
        analysis = BreakdownAnalysis(
            results={
                "screen": CardinalityResult(
                    column=ColumnInfo("screen", "String"),
                    count=1,
                    is_low=True,
                    sample_values=["/dashboard"],
                ),
            },
            rows=[("/dashboard",)],
            reg_names=["screen"],
            json_names=[],
            json_value_names=[],
        )

        # Create event and mark archived = True
        event = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=et.id,
            name="screen=/dashboard",
            source_name="screen=/dashboard",
            status="archived",
            order=0,
        )
        sync_session.add(event)
        sync_session.commit()

        # Run generate_events
        result = generate_events(sync_session, project.id, et.id, analysis, fds)
        sync_session.commit()

        assert result.events_created == 0
        assert result.events_skipped == 1
        assert "screen=/dashboard" not in result.events_by_name


# --- binding-based adoption + token normalization (tripl-j94c.2) -------------


def _payload_locale_analysis() -> BreakdownAnalysis:
    return BreakdownAnalysis(
        results={
            "payload": CardinalityResult(
                column=ColumnInfo("payload", "JSON"),
                count=1,
                is_low=True,
                json_path_combos=[("locale",)],
            ),
        },
        rows=[(("locale",),)],
        reg_names=[],
        json_names=["payload"],
        json_value_names=[],
    )


def test_manual_variable_with_binding_is_adopted_not_duplicated(
    sync_session: Session, project_and_type
):
    project, et, fds = project_and_type
    manual = Variable(
        id=uuid.uuid4(),
        project_id=project.id,
        name="variant",
        variable_type="string",
        description="documented before implementation",
        bindings=["payload.locale"],
    )
    sync_session.add(manual)
    sync_session.commit()

    result = generate_events(sync_session, project.id, et.id, _payload_locale_analysis(), fds)
    sync_session.commit()

    assert result.events_created == 1
    assert result.variables_created == 0

    variables = (
        sync_session.execute(select(Variable).where(Variable.project_id == project.id))
        .scalars()
        .all()
    )
    assert [v.name for v in variables] == ["variant"]
    # Adoption backfills the scan identity for future runs.
    assert variables[0].source_name == "payload.locale"

    # The stored template is normalized to the display name...
    payload_value = sync_session.execute(
        select(EventFieldValue.value).where(
            EventFieldValue.field_definition_id == fds["payload"].id
        )
    ).scalar_one()
    assert payload_value == '{"locale": "${variant}"}'

    # ...and the observation attributes to the manual variable.
    contexts = (
        sync_session.execute(select(VariableValue).where(VariableValue.project_id == project.id))
        .scalars()
        .all()
    )
    assert len(contexts) == 1
    assert contexts[0].variable_id == manual.id
    assert contexts[0].source_column == "payload.locale"


def test_scan_twice_with_adopted_binding_is_idempotent(sync_session: Session, project_and_type):
    project, et, fds = project_and_type
    sync_session.add(
        Variable(
            id=uuid.uuid4(),
            project_id=project.id,
            name="variant",
            variable_type="string",
            description="",
            bindings=["payload.locale"],
        )
    )
    sync_session.commit()

    first = generate_events(sync_session, project.id, et.id, _payload_locale_analysis(), fds)
    sync_session.commit()
    second = generate_events(sync_session, project.id, et.id, _payload_locale_analysis(), fds)
    sync_session.commit()

    assert first.events_created == 1
    assert second.events_created == 0
    assert second.events_skipped == 1
    assert second.variables_created == 0

    values = (
        sync_session.execute(
            select(EventFieldValue.value).where(
                EventFieldValue.field_definition_id == fds["payload"].id
            )
        )
        .scalars()
        .all()
    )
    assert values == ['{"locale": "${variant}"}']
    contexts = (
        sync_session.execute(select(VariableValue).where(VariableValue.project_id == project.id))
        .scalars()
        .all()
    )
    assert len(contexts) == 1


def test_renamed_scan_variable_keeps_matching_and_normalizes_on_rescan(
    sync_session: Session, project_and_type
):
    project, et, fds = project_and_type

    first = generate_events(sync_session, project.id, et.id, _payload_locale_analysis(), fds)
    sync_session.commit()
    assert first.variables_created == 1

    scanned = sync_session.execute(
        select(Variable).where(Variable.project_id == project.id)
    ).scalar_one()
    assert scanned.name == "locale"
    assert scanned.source_name == "payload.locale"
    assert scanned.bindings == ["payload.locale"]

    # User renames the scan-created variable; identity lives in
    # source_name/bindings so the next scan must adopt, not duplicate.
    scanned.name = "locale_var"
    sync_session.commit()

    second = generate_events(sync_session, project.id, et.id, _payload_locale_analysis(), fds)
    sync_session.commit()

    assert second.variables_created == 0
    variables = (
        sync_session.execute(select(Variable).where(Variable.project_id == project.id))
        .scalars()
        .all()
    )
    assert [v.name for v in variables] == ["locale_var"]

    # The rescan rewrote the stored template to the new display name.
    payload_value = sync_session.execute(
        select(EventFieldValue.value).where(
            EventFieldValue.field_definition_id == fds["payload"].id
        )
    ).scalar_one()
    assert payload_value == '{"locale": "${locale_var}"}'


# --- variable value drift detection (tripl-j94c.5) ---------------------------


def _drift_rows(session: Session, project_id):
    from tripl.models.variable_value_drift import VariableValueDrift

    return (
        session.execute(
            select(VariableValueDrift).where(VariableValueDrift.project_id == project_id)
        )
        .scalars()
        .all()
    )


def _make_context_entry(variable, event, fd, values):
    return {
        (variable.id, event.id, fd.id): {
            "variable_id": variable.id,
            "event_id": event.id,
            "field_definition_id": fd.id,
            "source_column": "screen",
            "value_kind": "low",
            "observed_count": len(values),
            "values": list(values),
        }
    }


def _seed_variable_event(sync_session: Session, project, et, fds, allowed_values):
    variable = Variable(
        id=uuid.uuid4(),
        project_id=project.id,
        name="variant",
        variable_type="string",
        description="",
        allowed_values=list(allowed_values),
        bindings=["screen"],
    )
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=et.id,
        name="Onboarding",
        description="",
        order=0,
    )
    sync_session.add_all([variable, event])
    sync_session.commit()
    return variable, event


def test_value_drift_detected_for_undocumented_values(sync_session: Session, project_and_type):
    from tripl.core.analyzers._variable_value_drift import detect_variable_value_drifts

    project, et, fds = project_and_type
    variable, event = _seed_variable_event(sync_session, project, et, fds, ["a", "b"])

    detected = detect_variable_value_drifts(
        sync_session,
        project_id=project.id,
        branch_id=variable.branch_id,
        scan_config_id=None,
        contexts=_make_context_entry(variable, event, fds["screen"], ["a", "x", "y"]),
    )
    sync_session.commit()

    assert detected == 1
    rows = _drift_rows(sync_session, project.id)
    assert len(rows) == 1
    assert rows[0].observed_values == ["x", "y"]
    assert rows[0].status == "open"


def test_value_drift_respects_event_override(sync_session: Session, project_and_type):
    from tripl.core.analyzers._variable_value_drift import detect_variable_value_drifts
    from tripl.models.variable_event_value_override import VariableEventValueOverride

    project, et, fds = project_and_type
    variable, event = _seed_variable_event(sync_session, project, et, fds, ["a", "b"])
    sync_session.add(
        VariableEventValueOverride(
            id=uuid.uuid4(),
            project_id=project.id,
            branch_id=variable.branch_id,
            variable_id=variable.id,
            event_id=event.id,
            values=["x"],
        )
    )
    sync_session.commit()

    detected = detect_variable_value_drifts(
        sync_session,
        project_id=project.id,
        branch_id=variable.branch_id,
        scan_config_id=None,
        contexts=_make_context_entry(variable, event, fds["screen"], ["a", "x", "y"]),
    )
    sync_session.commit()

    # Override replaces the global list: "a" is novel now, "x" documented.
    assert detected == 1
    rows = _drift_rows(sync_session, project.id)
    assert rows[0].observed_values == ["a", "y"]


def test_value_drift_skipped_without_documented_contract(sync_session: Session, project_and_type):
    from tripl.core.analyzers._variable_value_drift import detect_variable_value_drifts

    project, et, fds = project_and_type
    variable, event = _seed_variable_event(sync_session, project, et, fds, [])

    detected = detect_variable_value_drifts(
        sync_session,
        project_id=project.id,
        branch_id=variable.branch_id,
        scan_config_id=None,
        contexts=_make_context_entry(variable, event, fds["screen"], ["whatever"]),
    )
    sync_session.commit()

    assert detected == 0
    assert _drift_rows(sync_session, project.id) == []


def test_value_drift_upsert_refreshes_without_reopening(sync_session: Session, project_and_type):
    from tripl.core.analyzers._variable_value_drift import detect_variable_value_drifts

    project, et, fds = project_and_type
    variable, event = _seed_variable_event(sync_session, project, et, fds, ["a"])

    detect_variable_value_drifts(
        sync_session,
        project_id=project.id,
        branch_id=variable.branch_id,
        scan_config_id=None,
        contexts=_make_context_entry(variable, event, fds["screen"], ["x"]),
    )
    sync_session.commit()

    row = _drift_rows(sync_session, project.id)[0]
    row.status = "false_positive"
    sync_session.commit()

    detect_variable_value_drifts(
        sync_session,
        project_id=project.id,
        branch_id=variable.branch_id,
        scan_config_id=None,
        contexts=_make_context_entry(variable, event, fds["screen"], ["x", "z"]),
    )
    sync_session.commit()

    # The upsert bypasses the ORM identity map — expire before re-reading.
    sync_session.expire_all()
    rows = _drift_rows(sync_session, project.id)
    assert len(rows) == 1
    # Refresh updated the sample but did NOT reopen the resolution.
    assert rows[0].observed_values == ["x", "z"]
    assert rows[0].status == "false_positive"


def test_accepted_value_drift_reopens_on_value_outside_resolved_set(
    sync_session: Session, project_and_type
):
    """An accepted row must not silently absorb a value nobody accepted."""
    from tripl.core.analyzers._variable_value_drift import detect_variable_value_drifts

    project, et, fds = project_and_type
    variable, event = _seed_variable_event(sync_session, project, et, fds, ["a"])

    detect_variable_value_drifts(
        sync_session,
        project_id=project.id,
        branch_id=variable.branch_id,
        scan_config_id=None,
        contexts=_make_context_entry(variable, event, fds["screen"], ["a", "x"]),
    )
    sync_session.commit()

    # Accept exactly what the service does: the novel values join the
    # documented list and the row is resolved.
    row = _drift_rows(sync_session, project.id)[0]
    assert row.observed_values == ["x"]
    row.status = "accepted"
    row.resolved_at = datetime.now(UTC)
    row.resolution_note = "expected experiment arm"
    variable.allowed_values = ["a", "x"]
    sync_session.commit()

    # A later scan sees a value that was never part of the accepted set.
    detect_variable_value_drifts(
        sync_session,
        project_id=project.id,
        branch_id=variable.branch_id,
        scan_config_id=None,
        contexts=_make_context_entry(variable, event, fds["screen"], ["a", "x", "z"]),
    )
    sync_session.commit()

    sync_session.expire_all()
    rows = _drift_rows(sync_session, project.id)
    assert len(rows) == 1
    assert rows[0].observed_values == ["z"]
    assert rows[0].status == "open"
    assert rows[0].resolved_at is None
    assert rows[0].resolution_note is None


def test_accepted_value_drift_stays_resolved_for_values_inside_resolved_set(
    sync_session: Session, project_and_type
):
    """Re-observing an already-accepted value must not re-nag.

    The pair carries an OVERRIDE, which is what decides novelty, while the
    acceptance was global and so landed in ``allowed_values``. That is the one
    shape where an accepted value legitimately keeps arriving as novel, and the
    row must stay resolved through it.
    """
    from tripl.core.analyzers._variable_value_drift import detect_variable_value_drifts
    from tripl.models.variable_event_value_override import VariableEventValueOverride
    from tripl.models.variable_value_drift import VariableValueDrift

    project, et, fds = project_and_type
    variable, event = _seed_variable_event(sync_session, project, et, fds, ["a", "x"])
    sync_session.add(
        VariableEventValueOverride(
            id=uuid.uuid4(),
            project_id=project.id,
            branch_id=variable.branch_id,
            variable_id=variable.id,
            event_id=event.id,
            values=["a"],
        )
    )
    resolved_at = datetime(2026, 1, 1, tzinfo=UTC)
    accepted = VariableValueDrift(
        id=uuid.uuid4(),
        project_id=project.id,
        variable_id=variable.id,
        event_id=event.id,
        observed_values=["x"],
        status="accepted",
        detected_at=resolved_at,
    )
    sync_session.add(accepted)
    sync_session.commit()

    detected = detect_variable_value_drifts(
        sync_session,
        project_id=project.id,
        branch_id=variable.branch_id,
        scan_config_id=None,
        contexts=_make_context_entry(variable, event, fds["screen"], ["a", "x"]),
    )
    sync_session.commit()

    sync_session.expire_all()
    rows = _drift_rows(sync_session, project.id)
    assert detected == 0
    assert len(rows) == 1
    assert rows[0].status == "accepted"
    # The evidence is the resolved set and stays frozen, retention clock included.
    assert rows[0].observed_values == ["x"]
    assert rows[0].detected_at.replace(tzinfo=UTC) == resolved_at


def test_accepted_value_drift_reopens_on_a_value_it_only_absorbed(
    sync_session: Session, project_and_type
):
    """A value the row swallowed but nobody accepted must not stay suppressed.

    This is the population the bug created. Builds before the freeze refreshed
    ``observed_values`` on accepted rows, so a live row's stored evidence can
    hold values that were never accepted and therefore never documented. Reading
    that column as "the set the user accepted" would let the fix skip exactly the
    rows it exists for. Acceptance documents and absorption does not, so the
    documented lists are what tell the two apart — no migration can, after the
    fact.
    """
    from tripl.core.analyzers._variable_value_drift import detect_variable_value_drifts
    from tripl.models.variable_value_drift import VariableValueDrift

    project, et, fds = project_and_type
    # "a" is documented; "cart" was accepted (and documented with it). "promo_v2"
    # arrived a week later and was silently absorbed into the evidence.
    variable, event = _seed_variable_event(sync_session, project, et, fds, ["a", "cart"])
    accepted = VariableValueDrift(
        id=uuid.uuid4(),
        project_id=project.id,
        variable_id=variable.id,
        event_id=event.id,
        observed_values=["cart", "promo_v2"],
        status="accepted",
        resolution_note='accepted "cart"',
        detected_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    sync_session.add(accepted)
    sync_session.commit()

    detected = detect_variable_value_drifts(
        sync_session,
        project_id=project.id,
        branch_id=variable.branch_id,
        scan_config_id=None,
        contexts=_make_context_entry(variable, event, fds["screen"], ["a", "cart", "promo_v2"]),
    )
    sync_session.commit()

    sync_session.expire_all()
    rows = _drift_rows(sync_session, project.id)
    assert detected == 1
    assert len(rows) == 1
    assert rows[0].status == "open", "an undocumented value must not read as resolved"
    assert rows[0].observed_values == ["promo_v2"]


# --- scheduled observed-value sampling (tripl-xv77.2) ------------------------


def _seed_context_row(sync_session: Session, variable, event, fd, *, observed_count, values):
    """A stored context whose observation count decides what the sampler asks for."""
    row = VariableValue(
        id=uuid.uuid4(),
        project_id=variable.project_id,
        branch_id=variable.branch_id,
        variable_id=variable.id,
        event_id=event.id,
        field_definition_id=fd.id,
        source_column="screen",
        value_kind="high",
        observed_count=observed_count,
        values=list(values),
    )
    sync_session.add(row)
    sync_session.commit()
    return row


def _sampling_fixtures(
    sync_session: Session, project, *, source_name: str, time_column: str | None = None
):
    """A scan config plus one JSON-path variable that has observed nothing yet."""
    from tripl.models.scan_config import ScanConfig

    config = sync_session.get(ScanConfig, _seed_scan_config(sync_session, project))
    config.time_column = time_column
    sync_session.add(
        Variable(
            id=uuid.uuid4(),
            project_id=project.id,
            name="plan",
            source_name=source_name,
            variable_type="string",
            bindings=[source_name],
        )
    )
    sync_session.commit()
    return config


def test_json_path_sampling_collects_only_the_paths_that_need_values(
    sync_session: Session, project_and_type
):
    """Candidates come from the variables, and the adapter's extras are dropped.

    The adapter discovers every path in the column; only the ones a variable
    actually needs may become an observation, or a project would grow contexts
    for paths nothing in the plan references.
    """
    from tripl.worker.tasks.metrics.catalog_sync import _collect_json_path_samples

    project, et, fds = project_and_type
    config = _sampling_fixtures(sync_session, project, source_name="payload.user.plan")
    variable = sync_session.execute(select(Variable)).scalar_one()
    event = _seed_event(sync_session, project, et, "Signup")
    _seed_context_row(sync_session, variable, event, fds["payload"], observed_count=0, values=[])

    class _Adapter:
        def get_json_path_samples(self, *args: object, **kwargs: object):
            # Values arrive as the JSON text every adapter emits, including a
            # repeat, so the formatting and dedup on the way in are exercised.
            return {
                "payload": {
                    "user.plan": ['"pro"', '"free"', '"pro"'],
                    "user.unwatched": ['"noise"'],
                }
            }

    samples = _collect_json_path_samples(
        sync_session,
        adapter=_Adapter(),
        config=config,
        columns=[ColumnInfo("payload", "JSON")],
        catalog_scan_window=None,
        time_from_dt=datetime(2026, 8, 30, tzinfo=UTC),
        time_to_dt=datetime(2026, 8, 30, 1, tzinfo=UTC),
    )

    assert samples.samples == {"payload": {"user.plan": ["pro", "free"]}}
    assert samples.ring_size == 1
    assert samples.paths_sampled == 1
    assert samples.paths_with_samples == 1


def _seed_event(sync_session: Session, project, et, name: str, *, order: int = 0) -> Event:
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=et.id,
        name=name,
        source_name=name,
        order=order,
    )
    sync_session.add(event)
    sync_session.commit()
    return event


def _add_path_variable(sync_session: Session, project, source_name: str) -> Variable:
    """A second JSON-path variable, so a run has more candidates than it may sample."""
    variable = Variable(
        id=uuid.uuid4(),
        project_id=project.id,
        name=source_name.rpartition(".")[2],
        source_name=source_name,
        variable_type="string",
        bindings=[source_name],
    )
    sync_session.add(variable)
    sync_session.commit()
    return variable


def test_json_path_sampling_skips_a_variable_whose_every_context_is_observed(
    sync_session: Session, project_and_type
):
    """The cost is self-extinguishing: a fully observed path leaves the candidate set.

    With nothing left to fill there is no warehouse call at all, which is what
    keeps this affordable on a project that has already converged.
    """
    from tripl.worker.tasks.metrics.catalog_sync import _collect_json_path_samples

    project, et, fds = project_and_type
    config = _sampling_fixtures(sync_session, project, source_name="payload.user.plan")
    variable = sync_session.execute(select(Variable)).scalar_one()
    event = _seed_event(sync_session, project, et, "Signup")
    _seed_context_row(
        sync_session, variable, event, fds["payload"], observed_count=3, values=["pro"]
    )

    calls: list[object] = []

    class _Adapter:
        def get_json_path_samples(self, *args: object, **kwargs: object):
            calls.append(args)
            return {}

    samples = _collect_json_path_samples(
        sync_session,
        adapter=_Adapter(),
        config=config,
        columns=[ColumnInfo("payload", "JSON")],
        catalog_scan_window=None,
        time_from_dt=datetime(2026, 8, 30, tzinfo=UTC),
        time_to_dt=datetime(2026, 8, 30, 1, tzinfo=UTC),
    )

    assert samples.samples == {}
    assert samples.ring_size == 0
    # Recorded rather than raised: the collector swallows adapter exceptions on
    # purpose, so an assert inside the double would be caught and pass silently.
    assert calls == [], "a filled path must not cost a warehouse query"


def test_json_path_sampling_asks_again_while_one_context_is_still_empty(
    sync_session: Session, project_and_type
):
    """A filled context retires that CONTEXT, never the whole variable.

    ``variable_values`` is keyed on (variable, event, field) and one variable
    covers every event on the path — every scan config's events too, since the row
    is unique on (project, branch, source_name). So an empty context turns up long
    after the first one is filled: a new event, an event that gained the field, a
    context a re-scan dropped and re-created, or a sibling config whose events the
    first config's fill retired the path for. Testing "observed anything anywhere"
    left all of them empty for good — the production symptom this sampler exists
    to fix, back for every event but the first.
    """
    from tripl.worker.tasks.metrics.catalog_sync import _collect_json_path_samples

    project, et, fds = project_and_type
    config = _sampling_fixtures(sync_session, project, source_name="payload.user.plan")
    variable = sync_session.execute(select(Variable)).scalar_one()
    filled = _seed_event(sync_session, project, et, "Signup")
    later = _seed_event(sync_session, project, et, "Checkout", order=1)
    _seed_context_row(
        sync_session, variable, filled, fds["payload"], observed_count=3, values=["pro"]
    )
    _seed_context_row(sync_session, variable, later, fds["payload"], observed_count=0, values=[])

    calls: list[object] = []

    class _Adapter:
        def get_json_path_samples(self, *args: object, **kwargs: object):
            calls.append(args)
            return {"payload": {"user.plan": ['"pro"', '"free"']}}

    samples = _collect_json_path_samples(
        sync_session,
        adapter=_Adapter(),
        config=config,
        columns=[ColumnInfo("payload", "JSON")],
        catalog_scan_window=None,
        time_from_dt=datetime(2026, 8, 30, tzinfo=UTC),
        time_to_dt=datetime(2026, 8, 30, 1, tzinfo=UTC),
    )

    assert samples.samples == {"payload": {"user.plan": ["pro", "free"]}}
    # Recorded out here for the same reason as above: the collector swallows
    # adapter exceptions, so an assert inside the double would pass silently.
    assert len(calls) == 1, "an unobserved context must still be worth one query"


def test_json_path_sampling_degrades_to_empty_when_the_adapter_raises(
    sync_session: Session, project_and_type
):
    """An adapter failure costs the samples, never the collection job.

    This runs inside ``collect_metrics``' single try, so an unguarded raise here
    would fail the whole ScanJob and stop metric collection for the config — far
    worse than going without the enrichment it was fetching.
    """
    from tripl.worker.tasks.metrics.catalog_sync import _collect_json_path_samples

    project, et, fds = project_and_type
    config = _sampling_fixtures(sync_session, project, source_name="payload.user.plan")
    variable = sync_session.execute(select(Variable)).scalar_one()
    event = _seed_event(sync_session, project, et, "Signup")
    _seed_context_row(sync_session, variable, event, fds["payload"], observed_count=0, values=[])

    class _RaisingAdapter:
        def get_json_path_samples(self, *args: object, **kwargs: object):
            raise TimeoutError("warehouse timed out")

    samples = _collect_json_path_samples(
        sync_session,
        adapter=_RaisingAdapter(),
        config=config,
        columns=[ColumnInfo("payload", "JSON")],
        catalog_scan_window=None,
        time_from_dt=datetime(2026, 8, 30, tzinfo=UTC),
        time_to_dt=datetime(2026, 8, 30, 1, tzinfo=UTC),
    )

    assert samples.samples == {}
    # The counters still say what was attempted: sampled-but-nothing-back is
    # the signature of a failing adapter, and all-zeros would hide it behind
    # "nothing to do".
    assert samples.ring_size == 1
    assert samples.paths_sampled == 1
    assert samples.paths_with_samples == 0


def test_json_path_sampling_bounds_the_adapter_query_by_the_collection_window(
    sync_session: Session, project_and_type
):
    """The windowed call is the only one a scheduled run makes.

    ``collect_metrics`` builds a ``catalog_scan_window`` whenever the config names
    a time column, so the unwindowed branch the sampling tests above take is not
    one production reaches. What the adapter is handed here is the whole cost
    control: unbounded, this is a full pass over a warehouse table rather than
    the rounding error it is budgeted as.
    """
    from tripl.core.analyzers._event_generator_variables import VARIABLE_VALUE_SAMPLE_LIMIT
    from tripl.worker.tasks.metrics.catalog_sync import (
        _PATH_DISCOVERY_LIMIT,
        _SAMPLE_ROW_LIMIT,
        _collect_json_path_samples,
    )

    project, et, fds = project_and_type
    config = _sampling_fixtures(
        sync_session, project, source_name="payload.user.plan", time_column="ts"
    )
    variable = sync_session.execute(select(Variable)).scalar_one()
    event = _seed_event(sync_session, project, et, "Signup")
    _seed_context_row(sync_session, variable, event, fds["payload"], observed_count=0, values=[])
    window_from = datetime(2026, 8, 30, tzinfo=UTC)
    window_to = datetime(2026, 8, 30, 1, tzinfo=UTC)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class _Adapter:
        def get_json_path_samples(self, *args: object, **kwargs: object):
            calls.append((args, kwargs))
            return {"payload": {"user.plan": ['"pro"']}}

    samples = _collect_json_path_samples(
        sync_session,
        adapter=_Adapter(),
        config=config,
        columns=[ColumnInfo("payload", "JSON"), ColumnInfo("ts", "DateTime")],
        catalog_scan_window=(window_from, window_to),
        time_from_dt=window_from,
        time_to_dt=window_to,
    )

    assert samples.samples == {"payload": {"user.plan": ["pro"]}}
    # Recorded and asserted out here: the collector swallows adapter exceptions,
    # so an assert raised inside the double would be logged and the test pass.
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (config.base_query, ["payload"])
    assert kwargs == {
        "time_column": "ts",
        "time_from": window_from,
        "time_to": window_to,
        "path_limit": _PATH_DISCOVERY_LIMIT,
        # The stored sample is truncated to this, so asking for more buys rows
        # that are discarded on arrival.
        "sample_limit": VARIABLE_VALUE_SAMPLE_LIMIT,
        "sample_row_limit": _SAMPLE_ROW_LIMIT,
    }


_ROTATION_TICK = timedelta(hours=1)
_ROTATION_END = datetime(2026, 8, 30, 12, tzinfo=UTC)


def _path_candidates(count: int) -> list[tuple[str, str]]:
    return [("payload", f"user.p{index}") for index in range(count)]


def test_rotating_window_returns_every_candidate_that_fits():
    from tripl.worker.tasks.metrics.catalog_sync import _rotating_window

    candidates = _path_candidates(3)

    window = _rotating_window(candidates, size=5, window_end=_ROTATION_END, tick=_ROTATION_TICK)

    assert window == candidates


def test_rotating_window_hands_back_one_windows_worth_of_distinct_candidates():
    from tripl.worker.tasks.metrics.catalog_sync import _rotating_window

    candidates = _path_candidates(50)

    window = _rotating_window(candidates, size=7, window_end=_ROTATION_END, tick=_ROTATION_TICK)

    assert len(window) == 7
    assert len(set(window)) == 7, "a run must not spend its budget sampling one path twice"
    assert set(window) <= set(candidates)


def test_rotating_window_wraps_past_the_end_of_the_candidate_list():
    """A start near the end continues from the front instead of coming up short.

    Size 3 and ring 4 are coprime, so four consecutive ticks reach every start
    regardless of where the epoch arithmetic happens to begin.
    """
    from tripl.worker.tasks.metrics.catalog_sync import _rotating_window

    candidates = _path_candidates(4)

    windows = [
        _rotating_window(
            candidates,
            size=3,
            window_end=_ROTATION_END + _ROTATION_TICK * tick,
            tick=_ROTATION_TICK,
        )
        for tick in range(len(candidates))
    ]

    assert all(len(set(window)) == 3 for window in windows)
    assert any(window[0] != candidates[0] and candidates[0] in window for window in windows), (
        "a window that runs off the end must pick up again at the first candidate"
    )


def test_rotating_window_strides_a_whole_slice_per_scheduled_tick():
    """The starvation guard: consecutive runs must sample DIFFERENT paths.

    Advancing one candidate per tick looked like rotation and was not:
    consecutive slices shared all but one element, so the ring's tail waited
    months for its first attempt (the 2026-08-31 production stall, where whole
    cycles resampled the same already-filled prefix). Striding by the slice's
    own size makes consecutive windows disjoint whenever the ring holds at
    least two slices' worth — under the pre-fix +1 stride the two windows
    below overlap on three of four paths and this test fails. One tick here is
    one SCHEDULED interval, which is what the collector passes; see the
    collector-level test below for why that is not the collection window.
    """
    from tripl.worker.tasks.metrics.catalog_sync import _rotating_window

    candidates = _path_candidates(9)

    first = _rotating_window(candidates, size=4, window_end=_ROTATION_END, tick=_ROTATION_TICK)
    second = _rotating_window(
        candidates,
        size=4,
        window_end=_ROTATION_END + _ROTATION_TICK,
        tick=_ROTATION_TICK,
    )

    assert set(second).isdisjoint(first)
    # The stride is exactly one slice: the second window begins on the ring
    # right where the first one ended.
    assert second[0] == candidates[(candidates.index(first[-1]) + 1) % len(candidates)]


def test_rotating_window_repeats_itself_for_a_retried_run():
    """A retry re-samples what the failed attempt was doing rather than skipping it.

    The rotation is keyed on the window end precisely so that a second attempt at
    the same job asks the same questions.
    """
    from tripl.worker.tasks.metrics.catalog_sync import _rotating_window

    candidates = _path_candidates(9)

    attempt = _rotating_window(candidates, size=4, window_end=_ROTATION_END, tick=_ROTATION_TICK)
    retry = _rotating_window(candidates, size=4, window_end=_ROTATION_END, tick=_ROTATION_TICK)

    assert retry == attempt


def test_json_path_sampling_rotates_on_the_scheduled_interval_not_the_window(
    sync_session: Session, project_and_type, monkeypatch
):
    """Two runs one tick apart must not spend that tick on the same path.

    The collection window is no run counter: ``_resolve_collection_window`` starts
    it at the last stored bucket, so a caught-up hourly config hands the sampler
    a window several hours wide and a project with no metrics yet one thirty times
    its interval. Divided by that, consecutive runs land in the same slot and the
    starvation guard only fires when the backlog changes length — which is a
    property of how far behind the config is, not of how many times it has run.
    """
    from tripl.worker.tasks.metrics import catalog_sync
    from tripl.worker.tasks.metrics.catalog_sync import _collect_json_path_samples

    project, et, fds = project_and_type
    config = _sampling_fixtures(
        sync_session, project, source_name="payload.user.plan", time_column="ts"
    )
    config.interval = "1h"
    plan_variable = sync_session.execute(select(Variable)).scalar_one()
    tier_variable = _add_path_variable(sync_session, project, "payload.user.tier")
    event = _seed_event(sync_session, project, et, "Signup")
    _seed_context_row(
        sync_session, plan_variable, event, fds["payload"], observed_count=0, values=[]
    )
    _seed_context_row(
        sync_session, tier_variable, event, fds["payload"], observed_count=0, values=[]
    )
    sync_session.commit()
    # One path per run, so which one this run picked is visible in what it returns.
    monkeypatch.setattr(catalog_sync, "_SAMPLED_PATHS_PER_RUN", 1)

    class _Adapter:
        def get_json_path_samples(self, *args: object, **kwargs: object):
            return {"payload": {"user.plan": ['"pro"'], "user.tier": ['"gold"']}}

    def _sample(window_end: datetime):
        window_from = window_end - timedelta(hours=6)
        return _collect_json_path_samples(
            sync_session,
            adapter=_Adapter(),
            config=config,
            columns=[ColumnInfo("payload", "JSON"), ColumnInfo("ts", "DateTime")],
            catalog_scan_window=(window_from, window_end),
            time_from_dt=window_from,
            time_to_dt=window_end,
        )

    # 12:00 UTC opens a six-hour bucket, so both runs sit inside one collection
    # window's worth of epoch arithmetic: keyed on the window, they are the same
    # run as far as the rotation can tell.
    first = _sample(datetime(2026, 8, 30, 12, tzinfo=UTC))
    second = _sample(datetime(2026, 8, 30, 13, tzinfo=UTC))

    assert len(first.samples["payload"]) == 1
    assert len(second.samples["payload"]) == 1
    assert first.samples != second.samples, (
        "one scheduled tick must move the rotation on to the other path"
    )


# --- the collector's own wiring, end to end (tripl-xv77.2) -------------------

_JSON_SCAN_COLUMNS = [
    ColumnInfo("screen", "String"),
    ColumnInfo("payload", "JSON"),
    ColumnInfo("ts", "DateTime"),
]


def _json_scan_analysis(*, screens: tuple[str, ...] = ("home",)) -> BreakdownAnalysis:
    """A JSON path that no ``json_value_paths`` entry keeps — that is, a variable.

    The exact shape the production defect lived in: a path becomes a variable
    only when the config does NOT list it as a kept value, so the breakdown rows
    carry a value for every path except this one and nothing but the sampler can
    fill it.

    ``screens`` is the enumerated column, so a second value is a second row and
    therefore a second event on the same path.
    """
    return _make_analysis(
        {
            "screen": CardinalityResult(
                column=_JSON_SCAN_COLUMNS[0],
                count=len(screens),
                is_low=True,
                sample_values=list(screens),
            ),
            "payload": CardinalityResult(
                column=_JSON_SCAN_COLUMNS[1],
                count=1,
                is_low=False,
                json_path_combos=[("user.plan",)],
            ),
        }
    )


class _SamplingAdapter:
    """The warehouse as this path uses it: sampling only.

    ``sync_catalog`` also asks an adapter to validate field contracts, but the
    fields these scans declare carry none, so it returns before reaching for it.
    """

    def __init__(self) -> None:
        self.sample_calls = 0

    def get_json_path_samples(self, *args: object, **kwargs: object):
        self.sample_calls += 1
        return {"payload": {"user.plan": ['"pro"', '"free"']}}


def _seed_json_scan_config(sync_session: Session, project, *, grouped: bool):
    """A non-replay scan config over a JSON column, in one of sync_catalog's shapes.

    The grouped shape auto-creates its event types from the scanned columns; the
    single shape needs one to point at, carrying the fields the scan may fill.
    """
    from tripl.models.scan_config import ScanConfig

    config = sync_session.get(ScanConfig, _seed_scan_config(sync_session, project))
    config.time_column = "ts"
    if grouped:
        config.event_type_column = "screen"
    else:
        event_type = EventType(
            id=uuid.uuid4(),
            project_id=project.id,
            name="signup",
            display_name="Signup",
            description="",
        )
        sync_session.add(event_type)
        sync_session.flush()
        sync_session.add_all(
            [
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
                    name="payload",
                    display_name="Payload",
                    field_type="json",
                    order=1,
                ),
            ]
        )
        config.event_type_id = event_type.id
    sync_session.commit()
    return config


def _run_scheduled_tick(
    sync_session: Session,
    config,
    adapter,
    *,
    screens: tuple[str, ...] = ("home",),
    groups: tuple[str, ...] = ("home",),
) -> None:
    """One collection tick through the real ``sync_catalog``, replay off.

    Only the two cardinality queries are stubbed — the sampler, the generator and
    everything wiring them together is the production code. ``screens`` are the
    values the scan finds in the enumerated column, so one more of them is one
    more event; ``groups`` are the event types the grouped branch iterates, each
    handed the same analysis the way one flat table gives every group the same
    columns.
    """
    from tripl.worker.tasks.metrics.catalog_sync import sync_catalog
    from tripl.worker.utils.reserved_columns import reserved_catalog_columns

    analysis = _json_scan_analysis(screens=screens)
    window_to = datetime(2026, 8, 30, 1, tzinfo=UTC)
    window_from = window_to - timedelta(hours=1)
    sync_catalog(
        sync_session,
        adapter=adapter,
        config=config,
        columns=_JSON_SCAN_COLUMNS,
        skip_cols=reserved_catalog_columns(config),
        json_value_path_map={},
        scan_row_limit=50000,
        metrics_row_limit=50000,
        time_from_dt=window_from,
        time_to_dt=window_to,
        catalog_scan_window=(window_from, window_to),
        is_replay=False,
        analyze_cardinality_fn=lambda *args, **kwargs: analysis,
        analyze_cardinality_grouped_fn=lambda *args, **kwargs: (
            list(groups),
            dict.fromkeys(groups, analysis),
        ),
        generate_events_fn=generate_events,
    )
    sync_session.commit()


def _only_context(sync_session: Session) -> VariableValue:
    return sync_session.execute(select(VariableValue)).scalar_one()


def test_scheduled_single_scan_fills_a_json_variable_from_the_warehouse(
    sync_session: Session, project_and_type
):
    """The wiring the fix is: ``sync_catalog`` samples, and forwards what it sampled.

    Every other test of this feature calls ``generate_events`` with a sample dict
    it built itself, so the collector's own two lines — the sampler call and the
    ``json_path_samples`` argument — could both be deleted with the suite green
    and the production defect back verbatim: active events showing no values.

    Two ticks because that is the production sequence. A path has no variable
    until a scan mints one, and the sampler asks only about variables that exist
    and have observed nothing.
    """
    project, _, _ = project_and_type
    config = _seed_json_scan_config(sync_session, project, grouped=False)
    adapter = _SamplingAdapter()

    _run_scheduled_tick(sync_session, config, adapter)

    minted = _only_context(sync_session)
    assert minted.source_column == "payload.user.plan"
    assert minted.observed_count == 0, "the tick that mints the variable has nothing to sample"

    _run_scheduled_tick(sync_session, config, adapter)

    filled = _only_context(sync_session)
    assert filled.observed_count == 2
    assert filled.values == ["pro", "free"]
    assert adapter.sample_calls == 1, "only the tick with an unfilled variable queries"


def test_scheduled_grouped_scan_fills_a_json_variable_from_the_warehouse(
    sync_session: Session, project_and_type
):
    """The same wiring through ``sync_catalog``'s other shape.

    The grouped branch has its own ``generate_events`` call and can therefore
    lose the samples on its own, which is the whole reason this exists twice.
    """
    project, _, _ = project_and_type
    config = _seed_json_scan_config(sync_session, project, grouped=True)
    adapter = _SamplingAdapter()

    _run_scheduled_tick(sync_session, config, adapter)
    assert _only_context(sync_session).observed_count == 0

    _run_scheduled_tick(sync_session, config, adapter)

    filled = _only_context(sync_session)
    assert filled.source_column == "payload.user.plan"
    assert filled.observed_count == 2
    assert filled.values == ["pro", "free"]
    assert adapter.sample_calls == 1


def test_an_event_that_appears_after_the_fill_gets_the_values_too(
    sync_session: Session, project_and_type
):
    """The regression the per-variable candidate test caused, end to end.

    Tick 1 mints the variable, tick 2 fills the first event's context. A second
    event then appears on the same path — the everyday case, since a project adds
    events for years after its first scan — and needs one more sampled tick.
    Retiring the variable on its first observation stranded it at zero for good,
    which is the production symptom ("active events show no observed values")
    reintroduced for every event but the first.
    """
    project, _, _ = project_and_type
    config = _seed_json_scan_config(sync_session, project, grouped=False)
    adapter = _SamplingAdapter()

    _run_scheduled_tick(sync_session, config, adapter)
    _run_scheduled_tick(sync_session, config, adapter)
    assert _only_context(sync_session).observed_count == 2

    _run_scheduled_tick(sync_session, config, adapter, screens=("home", "cart"))
    _run_scheduled_tick(sync_session, config, adapter, screens=("home", "cart"))

    contexts = list(sync_session.execute(select(VariableValue)).scalars())
    assert len(contexts) == 2, "the new event must carry a context of its own"
    assert [context.values for context in contexts] == [["pro", "free"], ["pro", "free"]]
    assert adapter.sample_calls == 2, (
        "the new event costs exactly one more query: none while every context was filled, "
        "one on the tick that found the empty one"
    )


def test_a_grouped_scan_gives_every_group_the_same_config_wide_sample(
    sync_session: Session, project_and_type
):
    """One sample per config, not one per group — pinned, because it is a choice.

    The regular-column half of a grouped scan is group-scoped and this half is
    not: ``get_json_path_samples`` takes no group predicate on any adapter, and a
    per-group query would repeat every tick forever on a group that never emits
    the path, since that group's context never leaves zero. So a group's context
    lists values drawn from the whole config's rows. ``sync_catalog`` argues that
    out where the sample is fetched; this is what keeps the two honest.
    """
    project, _, _ = project_and_type
    config = _seed_json_scan_config(sync_session, project, grouped=True)
    adapter = _SamplingAdapter()

    _run_scheduled_tick(sync_session, config, adapter, groups=("home", "cart"))
    _run_scheduled_tick(sync_session, config, adapter, groups=("home", "cart"))

    contexts = list(sync_session.execute(select(VariableValue)).scalars())
    assert len(contexts) == 2, "one context per group"
    assert [context.values for context in contexts] == [["pro", "free"], ["pro", "free"]]
    assert adapter.sample_calls == 1, "one warehouse query for the config, not one per group"


# --- authored provenance across grouping copies (tripl-j94c.9) ---------------


def test_group_merge_preserves_authorship_unless_rule_overrides(
    sync_session: Session, project_and_type
):
    project, et, fds = project_and_type
    for index, action in enumerate(["click:one", "click:two"]):
        event = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=et.id,
            name=action,
            source_name=action,
            order=index,
            status="implemented",
        )
        sync_session.add(event)
        sync_session.flush()
        # 'screen' is hand-authored and untouched by the rule; 'action' is the
        # matched field whose value the rule replaces with /pattern/.
        sync_session.add(
            EventFieldValue(
                id=uuid.uuid4(),
                event_id=event.id,
                field_definition_id=fds["screen"].id,
                value="${variant}",
                is_authored=True,
            )
        )
        sync_session.add(
            EventFieldValue(
                id=uuid.uuid4(),
                event_id=event.id,
                field_definition_id=fds["action"].id,
                value=action,
                is_authored=True,
            )
        )
    sync_session.commit()

    merged = merge_existing_events_for_group_rules(
        sync_session,
        project_id=project.id,
        event_type_ids=[et.id],
        event_group_rules=[
            {
                "name": "click events",
                "condition_logic": "all",
                "conditions": [{"field": "action", "pattern": "^click:"}],
            }
        ],
    )
    sync_session.commit()
    assert merged == 2

    grouped_event = sync_session.execute(
        select(Event).where(Event.project_id == project.id)
    ).scalar_one()
    values = {
        fv.field_definition_id: fv
        for fv in sync_session.execute(
            select(EventFieldValue).where(EventFieldValue.event_id == grouped_event.id)
        ).scalars()
    }
    # Untouched value keeps its authored provenance...
    assert values[fds["screen"].id].value == "${variant}"
    assert values[fds["screen"].id].is_authored is True
    # ...the rule-overridden value does not (it is no longer the user's text).
    assert values[fds["action"].id].value == "/^click:/"
    assert values[fds["action"].id].is_authored is False


# --- what a group merge carries off the event it deletes (tripl-xfxa) --------
#
# ``_merge_event_into_group`` ends in ``session.delete(source)``. Every one of
# these rows FKs to ``events.id`` with ``ondelete="CASCADE"`` and none of them
# is rebuilt by a later scan, so anything the merge does not explicitly move is
# gone for good. The engine has ``PRAGMA foreign_keys=ON`` (see ``_sqlite``);
# without it the cascade never fires and every assertion below passes vacuously.


_CLICK_GROUP_RULE = [
    {
        "name": "click events",
        "condition_logic": "all",
        "conditions": [{"field": "action", "pattern": "^click:"}],
    }
]


def _add_event(sync_session: Session, project, et, fds, *, name, screen, action, order):
    """Persist one catalog event with its two field values."""
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=et.id,
        name=name,
        source_name=name,
        order=order,
        status="implemented",
    )
    sync_session.add(event)
    sync_session.flush()
    sync_session.add_all(
        [
            EventFieldValue(
                id=uuid.uuid4(),
                event_id=event.id,
                field_definition_id=fds["screen"].id,
                value=screen,
            ),
            EventFieldValue(
                id=uuid.uuid4(),
                event_id=event.id,
                field_definition_id=fds["action"].id,
                value=action,
            ),
        ]
    )
    sync_session.flush()
    return event


def _add_variable(sync_session: Session, project, *, name, binding):
    variable = Variable(
        id=uuid.uuid4(),
        project_id=project.id,
        name=name,
        source_name=binding,
        variable_type="string",
        description="",
        bindings=[binding],
    )
    sync_session.add(variable)
    sync_session.flush()
    return variable


def _add_context(sync_session: Session, project, variable, event, fd, *, values, count, kind):
    context = VariableValue(
        id=uuid.uuid4(),
        project_id=project.id,
        variable_id=variable.id,
        event_id=event.id,
        field_definition_id=fd.id,
        source_column=fd.name,
        value_kind=kind,
        observed_count=count,
        values=list(values),
    )
    sync_session.add(context)
    sync_session.flush()
    return context


def _contexts(sync_session: Session, project_id) -> list[VariableValue]:
    return list(
        sync_session.execute(
            select(VariableValue).where(VariableValue.project_id == project_id)
        ).scalars()
    )


def test_group_merge_moves_variable_contexts_onto_the_surviving_event(
    sync_session: Session, project_and_type
):
    # A context is only rewritten when the CURRENT run observes that (event,
    # field) pair again, so one that dies with the merged-away event is never
    # rebuilt: the variable keeps a live reference in the group event's field
    # value and an empty /values list forever. Re-pointing is the whole fix.
    project, et, fds = project_and_type
    variable = _add_variable(sync_session, project, name="variant", binding="screen")
    source = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click:one",
        screen="${variant}",
        action="click:one",
        order=0,
    )
    _add_context(
        sync_session,
        project,
        variable,
        source,
        fds["screen"],
        values=["a", "b"],
        count=7,
        kind="low",
    )
    sync_session.commit()

    merged = merge_existing_events_for_group_rules(
        sync_session,
        project_id=project.id,
        event_type_ids=[et.id],
        event_group_rules=_CLICK_GROUP_RULE,
    )
    sync_session.commit()

    assert merged == 1
    grouped_event = sync_session.execute(
        select(Event).where(Event.project_id == project.id)
    ).scalar_one()
    assert grouped_event.source_name == "click events"
    contexts = _contexts(sync_session, project.id)
    assert len(contexts) == 1
    # The context followed the volume, with its observations intact.
    assert contexts[0].event_id == grouped_event.id
    assert contexts[0].field_definition_id == fds["screen"].id
    assert contexts[0].values == ["a", "b"]
    assert contexts[0].observed_count == 7


def test_group_merge_carries_the_event_discussion_onto_the_survivor(
    sync_session: Session, project_and_type
):
    # A comment is human input: no later scan step rebuilds it, and the FK
    # cascades, so anything the merge does not explicitly move dies with the
    # source event (tripl-h2sx.25 added this anchor; the FK ledger pins it).
    project, et, fds = project_and_type
    source = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click:one",
        screen="home",
        action="click:one",
        order=0,
    )
    parent = EventPhotoComment(id=uuid.uuid4(), event_id=source.id, body="Is this the old one?")
    sync_session.add(parent)
    sync_session.flush()
    sync_session.add(
        EventPhotoComment(
            id=uuid.uuid4(), event_id=source.id, parent_id=parent.id, body="Yes, since March."
        )
    )
    sync_session.commit()

    merged = merge_existing_events_for_group_rules(
        sync_session,
        project_id=project.id,
        event_type_ids=[et.id],
        event_group_rules=_CLICK_GROUP_RULE,
    )
    sync_session.commit()

    assert merged == 1
    grouped_event = sync_session.execute(
        select(Event).where(Event.project_id == project.id)
    ).scalar_one()
    comments = (
        sync_session.execute(select(EventPhotoComment).order_by(EventPhotoComment.body))
        .scalars()
        .all()
    )
    # The whole thread, reply included — a reply carries the same anchor as its
    # parent, so one UPDATE moves both.
    assert [comment.body for comment in comments] == ["Is this the old one?", "Yes, since March."]
    assert {comment.event_id for comment in comments} == {grouped_event.id}


def test_group_merge_folds_colliding_contexts_instead_of_violating_the_unique_constraint(
    sync_session: Session, project_and_type
):
    # uq_variable_value_context is the bare (variable_id, event_id,
    # field_definition_id), so a blanket ``UPDATE ... SET event_id`` would raise
    # on the second row. This pins the easy half of ``_fold_context_facts``:
    # ``high`` on either side wins the kind and neither side's samples are
    # dropped. The columns it decides from BOTH sides — threshold demotion and
    # the union's size as the count — are pinned by the two tests at the end of
    # this module, where the lists are long enough for either to bite.
    project, et, fds = project_and_type
    variable = _add_variable(sync_session, project, name="variant", binding="screen")
    # The group event already exists, so it — not a freshly created row — is the
    # merge target, and the two sources collide against it in a fixed order.
    target = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click events",
        screen="${variant}",
        action="/^click:/",
        order=0,
    )
    _add_context(
        sync_session, project, variable, target, fds["screen"], values=["a"], count=3, kind="low"
    )
    source = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click:one",
        screen="${variant}",
        action="click:one",
        order=1,
    )
    _add_context(
        sync_session,
        project,
        variable,
        source,
        fds["screen"],
        values=["b", "c"],
        count=9,
        kind="high",
    )
    sync_session.commit()

    merged = merge_existing_events_for_group_rules(
        sync_session,
        project_id=project.id,
        event_type_ids=[et.id],
        event_group_rules=_CLICK_GROUP_RULE,
    )
    sync_session.commit()

    assert merged == 1
    assert sync_session.get(Event, source.id) is None
    contexts = _contexts(sync_session, project.id)
    assert len(contexts) == 1
    folded = contexts[0]
    assert folded.event_id == target.id
    assert folded.observed_count == 9
    # ``high`` is the wider claim about the value space, so it wins...
    assert folded.value_kind == "high"
    # ...and neither side's samples are dropped.
    assert set(folded.values) == {"a", "b", "c"}


def test_group_merge_drops_a_context_whose_token_is_gone_from_the_target_value(
    sync_session: Session, project_and_type
):
    # A rule replaces the matched field's value wholesale with /pattern/. A
    # context migrated onto that literal would assert a ${var} reference the
    # group event does not make. Dropping is per-field, not a blanket wipe:
    # the untouched field's context still moves.
    project, et, fds = project_and_type
    variable = _add_variable(sync_session, project, name="variant", binding="screen")
    source = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click:${variant}",
        screen="${variant}",
        action="click:${variant}",
        order=0,
    )
    _add_context(
        sync_session,
        project,
        variable,
        source,
        fds["action"],
        values=["x"],
        count=4,
        kind="low",
    )
    _add_context(
        sync_session,
        project,
        variable,
        source,
        fds["screen"],
        values=["y"],
        count=5,
        kind="low",
    )
    sync_session.commit()

    merged = merge_existing_events_for_group_rules(
        sync_session,
        project_id=project.id,
        event_type_ids=[et.id],
        event_group_rules=_CLICK_GROUP_RULE,
    )
    sync_session.commit()

    assert merged == 1
    grouped_event = sync_session.execute(
        select(Event).where(Event.project_id == project.id)
    ).scalar_one()
    values = {
        fv.field_definition_id: fv.value
        for fv in sync_session.execute(
            select(EventFieldValue).where(EventFieldValue.event_id == grouped_event.id)
        ).scalars()
    }
    # The rule rewrote 'action' and left 'screen' alone...
    assert values[fds["action"].id] == "/^click:/"
    assert values[fds["screen"].id] == "${variant}"
    # ...so only the 'screen' context survives, on the group event.
    contexts = _contexts(sync_session, project.id)
    assert len(contexts) == 1
    assert contexts[0].field_definition_id == fds["screen"].id
    assert contexts[0].event_id == grouped_event.id


def test_group_merge_moves_variable_event_overrides_and_lets_the_target_win(
    sync_session: Session, project_and_type
):
    # VariableEventValueOverride is written only through the API — the scan
    # pipeline never touches one — so letting it cascade away meant a scan
    # silently deleting a list a human typed. Nothing is folded: two authored
    # lists are two opinions, and merging them would invent a third nobody wrote.
    project, et, fds = project_and_type
    contested = _add_variable(sync_session, project, name="variant", binding="screen")
    uncontested = _add_variable(sync_session, project, name="locale", binding="payload.locale")
    target = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click events",
        screen="${variant}",
        action="/^click:/",
        order=0,
    )
    sync_session.add(
        VariableEventValueOverride(
            id=uuid.uuid4(),
            project_id=project.id,
            variable_id=contested.id,
            event_id=target.id,
            values=["kept-by-target"],
        )
    )
    source = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click:one",
        screen="${variant}",
        action="click:one",
        order=1,
    )
    sync_session.add_all(
        [
            VariableEventValueOverride(
                id=uuid.uuid4(),
                project_id=project.id,
                variable_id=contested.id,
                event_id=source.id,
                values=["dropped-on-collision"],
            ),
            VariableEventValueOverride(
                id=uuid.uuid4(),
                project_id=project.id,
                variable_id=uncontested.id,
                event_id=source.id,
                values=["carried-over"],
            ),
        ]
    )
    sync_session.commit()

    merged = merge_existing_events_for_group_rules(
        sync_session,
        project_id=project.id,
        event_type_ids=[et.id],
        event_group_rules=_CLICK_GROUP_RULE,
    )
    sync_session.commit()

    assert merged == 1
    overrides = {
        row.variable_id: row
        for row in sync_session.execute(select(VariableEventValueOverride)).scalars()
    }
    assert set(overrides) == {contested.id, uncontested.id}
    assert all(row.event_id == target.id for row in overrides.values())
    # The surviving event's own list stands; the source's uncontested one moves.
    assert overrides[contested.id].values == ["kept-by-target"]
    assert overrides[uncontested.id].values == ["carried-over"]


def test_group_merge_moves_variable_value_drifts_and_lets_the_target_win(
    sync_session: Session, project_and_type
):
    # A drift row carries accepted/snoozed/false_positive — a decision a person
    # made, and an ``accepted`` row is deliberately frozen against rescan.
    # Dropping it re-opens a question that was already answered.
    project, et, fds = project_and_type
    contested = _add_variable(sync_session, project, name="variant", binding="screen")
    uncontested = _add_variable(sync_session, project, name="locale", binding="payload.locale")
    target = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click events",
        screen="${variant}",
        action="/^click:/",
        order=0,
    )
    sync_session.add(
        VariableValueDrift(
            id=uuid.uuid4(),
            project_id=project.id,
            variable_id=contested.id,
            event_id=target.id,
            observed_values=["kept-by-target"],
            status="accepted",
        )
    )
    source = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click:one",
        screen="${variant}",
        action="click:one",
        order=1,
    )
    sync_session.add_all(
        [
            VariableValueDrift(
                id=uuid.uuid4(),
                project_id=project.id,
                variable_id=contested.id,
                event_id=source.id,
                observed_values=["dropped-on-collision"],
                status="open",
            ),
            VariableValueDrift(
                id=uuid.uuid4(),
                project_id=project.id,
                variable_id=uncontested.id,
                event_id=source.id,
                observed_values=["carried-over"],
                status="false_positive",
            ),
        ]
    )
    sync_session.commit()

    merged = merge_existing_events_for_group_rules(
        sync_session,
        project_id=project.id,
        event_type_ids=[et.id],
        event_group_rules=_CLICK_GROUP_RULE,
    )
    sync_session.commit()

    assert merged == 1
    drifts = {
        row.variable_id: row for row in sync_session.execute(select(VariableValueDrift)).scalars()
    }
    assert set(drifts) == {contested.id, uncontested.id}
    assert all(row.event_id == target.id for row in drifts.values())
    # The surviving event's triage is the more recent judgement...
    assert drifts[contested.id].observed_values == ["kept-by-target"]
    assert drifts[contested.id].status == "accepted"
    # ...and the uncontested decision moves across with its resolution intact.
    assert drifts[uncontested.id].observed_values == ["carried-over"]
    assert drifts[uncontested.id].status == "false_positive"


def test_scan_short_names_extend_on_collision(sync_session: Session, project_and_type):
    from tripl.core.analyzers._event_generator_variables import (
        VariableIndex,
        derive_display_name,
    )

    project, et, fds = project_and_type
    taken = Variable(
        id=uuid.uuid4(),
        project_id=project.id,
        name="locale",
        variable_type="string",
        description="",
        bindings=["settings.locale"],
    )
    sync_session.add(taken)
    sync_session.commit()

    index = VariableIndex([taken])
    # Last segment is claimed -> extend with the parent segment; raw path is
    # the final fallback when everything is taken.
    assert derive_display_name("payload.locale", index) == "payload_locale"
    assert derive_display_name("user.Name-Full.v2", index) == "v2"
    assert derive_display_name("plain_column", index) == "plain_column"


def test_excluded_variable_is_not_recreated_or_attributed(sync_session: Session, project_and_type):
    project, et, fds = project_and_type
    tombstone = Variable(
        id=uuid.uuid4(),
        project_id=project.id,
        name="locale",
        source_name="payload.locale",
        variable_type="string",
        description="",
        bindings=["payload.locale"],
        excluded_from_scans=True,
    )
    sync_session.add(tombstone)
    sync_session.commit()

    result = generate_events(sync_session, project.id, et.id, _payload_locale_analysis(), fds)
    sync_session.commit()

    # The tombstone prevents re-creation...
    assert result.variables_created == 0
    variables = (
        sync_session.execute(select(Variable).where(Variable.project_id == project.id))
        .scalars()
        .all()
    )
    assert [v.name for v in variables] == ["locale"]
    # ...no observed contexts accumulate...
    contexts = (
        sync_session.execute(select(VariableValue).where(VariableValue.project_id == project.id))
        .scalars()
        .all()
    )
    assert contexts == []
    # ...and the scan-written value keeps the raw token (no normalization to
    # an excluded variable's display name).
    payload_value = sync_session.execute(
        select(EventFieldValue.value).where(
            EventFieldValue.field_definition_id == fds["payload"].id
        )
    ).scalar_one()
    assert payload_value == '{"locale": "${payload.locale}"}'


def test_excluding_a_variable_keeps_the_values_it_already_observed(
    sync_session: Session, project_and_type
):
    """One Exclude click, one scan, and the observations have to still be there.

    The whole path, unmocked, because the defect only exists end to end: moving
    the purge out of the endpoint left it in the SCAN. Excluding is itself what
    makes the value change — ``normalize_variable_tokens`` stops resolving the
    token, so the stored ``${locale}`` reverts to the raw ``${payload.locale}``
    the planner emits — and a changed value is what
    ``delete_variable_contexts_for_event_type`` used to read as "this context is
    no longer true" (bd tripl-95pu).

    The trigger is the display name differing from the emitted token, so this is
    NOT confined to dotted paths: a manually-created variable adopted through a
    binding reverts the same way.
    """
    project, et, fds = project_and_type
    analysis = _payload_locale_analysis()

    generate_events(sync_session, project.id, et.id, analysis, fds)
    sync_session.commit()

    variable = sync_session.execute(
        select(Variable).where(Variable.project_id == project.id)
    ).scalar_one()
    assert (variable.name, variable.source_name) == ("locale", "payload.locale")

    # A replay filled the context in; the scan path never had these values, and
    # nothing but this row remembers them.
    context = sync_session.execute(select(VariableValue)).scalar_one()
    context.observed_count = 2
    context.values = ["en", "fr"]
    variable.excluded_from_scans = True
    sync_session.commit()
    context_id = context.id

    generate_events(sync_session, project.id, et.id, analysis, fds)
    sync_session.commit()

    # Columns, not the entity: a row read back through the identity map would
    # look intact after the delete this test exists to catch.
    survived = sync_session.execute(
        select(VariableValue.observed_count, VariableValue.values).where(
            VariableValue.id == context_id
        )
    ).one_or_none()
    assert survived is not None, "excluding a variable must not destroy its observations"
    assert survived == (2, ["en", "fr"])
    # The rewrite that used to take the row still happens — the row is spared
    # because of WHOSE it is, not because the scan left the value alone.
    payload_value = sync_session.execute(
        select(EventFieldValue.value).where(
            EventFieldValue.field_definition_id == fds["payload"].id
        )
    ).scalar_one()
    assert payload_value == '{"locale": "${payload.locale}"}'


# ---------------------------------------------------------------------------
# References a group merge must carry that no foreign key can find
#
# ``_event_generator_merge_refs`` handles event ids stored as strings or inside
# JSON lists, plus the one real FK whose ``SET NULL`` was silent. Reflection
# cannot see any of them, so these tests are the only thing standing between a
# merge and a reference that quietly stops meaning anything (tripl-avf4,
# tripl-jtnv).
# ---------------------------------------------------------------------------


def _add_metric(
    sync_session: Session, project, *, name, numerator, denominator=None, composition="single"
):
    definition = MetricDefinition(
        id=uuid.uuid4(),
        project_id=project.id,
        name=name,
        display_name=name,
        kind=MetricKind.event_composition.value,
        composition=composition,
        numerator_event_id=numerator,
        denominator_event_id=denominator,
    )
    sync_session.add(definition)
    sync_session.flush()
    return definition


def _click_source_and_other(sync_session: Session, project, et, fds):
    """One event a group rule matches, plus one it does not."""
    source = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click:one",
        screen="s",
        action="click:one",
        order=0,
    )
    keeper = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="scroll:one",
        screen="s",
        action="scroll:one",
        order=1,
    )
    return source, keeper


def _merge_clicks(sync_session: Session, project, et, *, expected: int = 1) -> Event:
    merged = merge_existing_events_for_group_rules(
        sync_session,
        project_id=project.id,
        event_type_ids=[et.id],
        event_group_rules=_CLICK_GROUP_RULE,
    )
    sync_session.commit()
    assert merged == expected
    # Re-read, never assert off the in-memory instance: these columns are plain
    # JSON with no MutableList mapped anywhere, so an in-place edit would leave
    # a stale object asserting happily while nothing was ever written.
    sync_session.expire_all()
    return sync_session.execute(
        select(Event).where(Event.source_name == "click events")
    ).scalar_one()


def test_group_merge_repoints_a_metric_composition_operand_instead_of_nulling_it(
    sync_session: Session, project_and_type
):
    """The FK is ON DELETE SET NULL and the resulting failure says nothing.

    With the operand NULL, the collector reads no series, reports success with
    zero values so the status never goes red, and the scheduler then never
    marks the metric due again. The metric flatlines forever while the series
    it wants sits under the target, already summed there by the merge.
    """
    project, et, fds = project_and_type
    source, _ = _click_source_and_other(sync_session, project, et, fds)
    definition = _add_metric(sync_session, project, name="clicks", numerator=source.id)
    sync_session.commit()

    grouped = _merge_clicks(sync_session, project, et)

    refreshed = sync_session.get(MetricDefinition, definition.id)
    assert refreshed is not None
    assert refreshed.numerator_event_id == grouped.id
    assert refreshed.last_collection_status is None, "a plain re-point is not a failure"


def test_group_merge_marks_a_ratio_whose_operands_collapse_onto_one_event_as_failed(
    sync_session: Session, project_and_type
):
    """A self-ratio computes a constant 1.0 — healthy-looking arithmetic that means nothing.

    Re-pointing is still right (a dangling NULL is worse), so the metric is
    re-pointed AND driven red, naming both originals so an operator can see
    what happened instead of trusting a flat line at 100%.
    """
    project, et, fds = project_and_type
    first = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click:one",
        screen="s",
        action="click:one",
        order=0,
    )
    second = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click:two",
        screen="s",
        action="click:two",
        order=1,
    )
    definition = _add_metric(
        sync_session,
        project,
        name="ratio",
        numerator=first.id,
        denominator=second.id,
        composition="ratio",
    )
    sync_session.commit()

    # Two matching events collapse into one group, so two merges happen.
    grouped = _merge_clicks(sync_session, project, et, expected=2)

    refreshed = sync_session.get(MetricDefinition, definition.id)
    assert refreshed is not None
    assert refreshed.numerator_event_id == grouped.id
    assert refreshed.denominator_event_id == grouped.id
    assert refreshed.last_collection_status == "error"
    assert refreshed.last_collection_failed_at is not None, "the cooldown must be stamped too"
    message = refreshed.last_collection_error or ""
    assert str(grouped.id) in message, "an operator needs to know which event survived"
    assert str(first.id) in message or str(second.id) in message, "and which one was merged into it"


def test_group_merge_leaves_a_metric_in_another_project_untouched(
    sync_session: Session, project_and_type
):
    project, et, fds = project_and_type
    source, _ = _click_source_and_other(sync_session, project, et, fds)
    other = Project(id=uuid.uuid4(), name="Other", slug="other-eg", description="")
    sync_session.add(other)
    sync_session.flush()
    # Deliberately impossible data — a metric in another project naming this
    # event. The scoping predicate is what must skip it, not the data.
    foreign = _add_metric(sync_session, other, name="foreign", numerator=source.id)
    sync_session.commit()

    _merge_clicks(sync_session, project, et)

    refreshed = sync_session.get(MetricDefinition, foreign.id)
    assert refreshed is not None
    assert refreshed.numerator_event_id is None, "the FK cascade, not the mover, cleared it"


def _add_override(
    sync_session,
    project,
    *,
    scope_ref,
    scan_config_id,
    sigma,
    min_count,
    clicks,
    scope_type="event",
):
    row = AnomalyScopeOverride(
        id=uuid.uuid4(),
        project_id=project.id,
        scan_config_id=scan_config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        scope_name="whatever",
        sigma_threshold=sigma,
        min_expected_count=min_count,
        false_positive_count=clicks,
    )
    sync_session.add(row)
    sync_session.flush()
    return row


def test_group_merge_folds_an_anomaly_override_keeping_the_stricter_setting(
    sync_session: Session, project_and_type
):
    """Both events were tuned; the survivor keeps the tighter of each knob.

    ``max`` is not a coin toss — the ratchet only ever tightens and is capped,
    so taking the maximum is literally "never undo a click". The click counts
    add up because that is the number Detection settings shows.
    """
    project, et, fds = project_and_type
    source, _ = _click_source_and_other(sync_session, project, et, fds)
    scan_config_id = _seed_scan_config(sync_session, project)
    target_seed = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click events",
        screen="s",
        action="click:zero",
        order=5,
    )
    _add_override(
        sync_session,
        project,
        scope_ref=str(source.id),
        scan_config_id=scan_config_id,
        sigma=5.5,
        min_count=30,
        clicks=3,
    )
    _add_override(
        sync_session,
        project,
        scope_ref=str(target_seed.id),
        scan_config_id=scan_config_id,
        sigma=4.0,
        min_count=45,
        clicks=2,
    )
    sync_session.commit()

    merge_existing_events_for_group_rules(
        sync_session,
        project_id=project.id,
        event_type_ids=[et.id],
        event_group_rules=_CLICK_GROUP_RULE,
    )
    sync_session.commit()
    sync_session.expire_all()

    grouped = sync_session.execute(
        select(Event).where(Event.source_name == "click events")
    ).scalar_one()
    rows = list(
        sync_session.execute(
            select(AnomalyScopeOverride).where(AnomalyScopeOverride.project_id == project.id)
        ).scalars()
    )
    assert len(rows) == 1, "the fold must not leave a duplicate to violate the unique key"
    assert rows[0].scope_ref == str(grouped.id)
    assert rows[0].sigma_threshold == 5.5
    assert rows[0].min_expected_count == 45
    assert rows[0].false_positive_count == 5


def test_group_merge_leaves_a_non_event_scoped_anomaly_override_alone(
    sync_session: Session, project_and_type
):
    """``scope_ref`` is polymorphic — a metric-scope ref that happens to collide must not move."""
    project, et, fds = project_and_type
    source, _ = _click_source_and_other(sync_session, project, et, fds)
    row = _add_override(
        sync_session,
        project,
        scope_ref=str(source.id),
        scan_config_id=None,
        sigma=6.0,
        min_count=10,
        clicks=1,
        scope_type="metric",
    )
    sync_session.commit()

    _merge_clicks(sync_session, project, et)

    refreshed = sync_session.get(AnomalyScopeOverride, row.id)
    assert refreshed is not None
    assert refreshed.scope_ref == str(source.id)


def test_group_merge_rewrites_event_ids_in_alert_rule_filter_values_and_dedupes(
    sync_session: Session, project_and_type
):
    """A dead id in an ``in`` rule under-alerts; in a ``not_in`` rule it over-alerts."""
    project, et, fds = project_and_type
    source, keeper = _click_source_and_other(sync_session, project, et, fds)
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type="webhook",
        name="hook",
    )
    sync_session.add(destination)
    sync_session.flush()
    rule = AlertRule(id=uuid.uuid4(), destination_id=destination.id, name="rule")
    sync_session.add(rule)
    sync_session.flush()
    target_seed = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click events",
        screen="s",
        action="click:zero",
        order=5,
    )
    event_filter = AlertRuleFilter(
        id=uuid.uuid4(),
        rule_id=rule.id,
        field="event",
        operator="in",
        values=[str(source.id), str(target_seed.id), str(keeper.id)],
    )
    direction_filter = AlertRuleFilter(
        id=uuid.uuid4(),
        rule_id=rule.id,
        field="direction",
        operator="eq",
        values=[str(source.id)],
    )
    sync_session.add_all([event_filter, direction_filter])
    sync_session.commit()

    merge_existing_events_for_group_rules(
        sync_session,
        project_id=project.id,
        event_type_ids=[et.id],
        event_group_rules=_CLICK_GROUP_RULE,
    )
    sync_session.commit()
    sync_session.expire_all()

    grouped = sync_session.execute(
        select(Event).where(Event.source_name == "click events")
    ).scalar_one()
    refreshed = sync_session.get(AlertRuleFilter, event_filter.id)
    assert refreshed is not None
    assert refreshed.values == [str(grouped.id), str(keeper.id)], (
        "the source collapses onto the target already in the list, exactly once"
    )
    untouched = sync_session.get(AlertRuleFilter, direction_filter.id)
    assert untouched is not None
    assert untouched.values == [str(source.id)], "a non-event field is not an event reference"


def test_group_merge_repoints_only_event_scoped_chart_annotations(
    sync_session: Session, project_and_type
):
    """The marker's text is history; its scope_ref is only where it gets drawn."""
    project, et, fds = project_and_type
    source, _ = _click_source_and_other(sync_session, project, et, fds)
    scoped = ChartAnnotation(
        id=uuid.uuid4(),
        project_id=project.id,
        scope_type="event",
        scope_ref=str(source.id),
        bucket=datetime(2026, 1, 1, tzinfo=UTC),
        label="shipped 4.2",
    )
    project_wide = ChartAnnotation(
        id=uuid.uuid4(),
        project_id=project.id,
        scope_type=None,
        scope_ref=None,
        bucket=datetime(2026, 1, 1, tzinfo=UTC),
        label="outage",
    )
    sync_session.add_all([scoped, project_wide])
    sync_session.commit()

    grouped = _merge_clicks(sync_session, project, et)

    refreshed = sync_session.get(ChartAnnotation, scoped.id)
    assert refreshed is not None
    assert refreshed.scope_ref == str(grouped.id)
    assert refreshed.label == "shipped 4.2", "the text is history and stays as written"
    wide = sync_session.get(ChartAnnotation, project_wide.id)
    assert wide is not None
    assert wide.scope_ref is None


def test_group_merge_repoints_an_open_ticket_and_leaves_a_closed_one(
    sync_session: Session, project_and_type
):
    """An open ticket says "not built yet"; a closed one records what shipped."""
    project, et, fds = project_and_type
    source, _ = _click_source_and_other(sync_session, project, et, fds)
    # The project fixture already owns a main branch; creating a second one
    # trips uq_plan_branch_project_name.
    branch = (
        sync_session.execute(select(PlanBranch).where(PlanBranch.project_id == project.id))
        .scalars()
        .first()
    )
    if branch is None:
        branch = PlanBranch(
            id=uuid.uuid4(),
            project_id=project.id,
            name="main",
            kind=BranchKind.main.value,
            status=BranchStatus.draft.value,
        )
        sync_session.add(branch)
        sync_session.flush()
    # One ticket per branch: uq_implementation_ticket_branch (tripl-l33u.11).
    shipped_branch = PlanBranch(
        id=uuid.uuid4(),
        project_id=project.id,
        name="shipped",
        kind=BranchKind.working.value,
        status=BranchStatus.draft.value,
    )
    sync_session.add(shipped_branch)
    sync_session.flush()
    open_ticket = ImplementationTicket(
        id=uuid.uuid4(),
        project_id=project.id,
        branch_id=branch.id,
        status="open",
        event_ids=[str(source.id)],
    )
    closed_ticket = ImplementationTicket(
        id=uuid.uuid4(),
        project_id=project.id,
        branch_id=shipped_branch.id,
        status="closed",
        event_ids=[str(source.id)],
    )
    sync_session.add_all([open_ticket, closed_ticket])
    sync_session.commit()

    grouped = _merge_clicks(sync_session, project, et)

    reopened = sync_session.get(ImplementationTicket, open_ticket.id)
    assert reopened is not None
    assert reopened.event_ids == [str(grouped.id)]
    closed = sync_session.get(ImplementationTicket, closed_ticket.id)
    assert closed is not None
    assert closed.event_ids == [str(source.id)], "history is not rewritten"


# --- contexts recorded before the merge that deletes their event (tripl-gsum) ---


_CATCH_ALL_AFTER_SPECIFIC_RULES = [
    {
        "name": "Clicks",
        "condition_logic": "all",
        "conditions": [{"field": "action", "pattern": "^click$"}],
    },
    {
        # The trailing catch-all every grouped config grows: specific rules
        # first, then one broad rule for the rest. It matches NO raw warehouse
        # value here — neither "click" nor "view" starts with "/" — so the
        # planner never reaches it. What it does match is the "/^click$/"
        # override literal the first rule wrote, which is what the merge pass
        # re-reads, and that is the whole non-idempotence.
        "name": "Everything",
        "condition_logic": "all",
        "conditions": [{"field": "action", "pattern": "^/"}],
    },
]


def test_a_rule_matching_an_earlier_rules_output_keeps_this_runs_observations(
    sync_session: Session, project_and_type
):
    """A merge that deletes an event this run just recorded contexts for.

    ``generate_events`` records contexts into an in-memory map keyed on
    ``event.id`` and inserts them AFTER the merge pass, which can
    ``session.delete`` one of those events. Unfixed, the insert writes the dead
    id and the flush raises ``IntegrityError`` on
    ``variable_values_event_id_fkey`` — the whole scan job, not one event.

    Dropping the entry instead would be silent loss, which is why the assertion
    below is about the group event's OBSERVATIONS and not about the absence of an
    exception: "Everything" is minted inside the merge pass itself, appears in no
    plan, and is therefore an event nothing else will ever record a context for.
    """
    project, et, fds = project_and_type
    cardinality = {
        "screen": CardinalityResult(
            column=ColumnInfo("screen", "String"),
            count=5000,
            is_low=False,
            sample_values=[f"/users/{i}/profile" for i in range(200)],
        ),
        "action": CardinalityResult(
            column=ColumnInfo("action", "String"),
            count=2,
            is_low=True,
            sample_values=["click", "view"],
        ),
    }
    result = generate_events(
        sync_session,
        project.id,
        et.id,
        _make_analysis(cardinality),
        fds,
        event_group_rules=_CATCH_ALL_AFTER_SPECIFIC_RULES,
    )
    sync_session.commit()

    # Pinned so the test cannot pass by the second rule quietly ceasing to match:
    # two events planned, one of them merged away by the catch-all.
    assert result.events_created == 2
    assert result.events_merged == 1

    events = list(
        sync_session.execute(select(Event).where(Event.project_id == project.id)).scalars()
    )
    identities = {event.source_name for event in events}
    assert "Everything" in identities
    assert "Clicks" not in identities, "the catch-all swallowed the specific group"

    contexts = _contexts(sync_session, project.id)
    live_event_ids = {event.id for event in events}
    # THE disable-the-fix assertion. Unfixed this never runs — the flush above
    # raises first — but it is what stays true if the engine ever stops enforcing
    # the foreign key: a context naming the deleted "Clicks" row.
    assert {context.event_id for context in contexts} <= live_event_ids

    group = next(event for event in events if event.source_name == "Everything")
    group_contexts = [context for context in contexts if context.event_id == group.id]
    assert len(group_contexts) == 1, "the merged-away event's observation followed its volume"
    assert group_contexts[0].field_definition_id == fds["screen"].id
    assert group_contexts[0].value_kind == "high"
    assert group_contexts[0].observed_count == 200
    assert len(group_contexts[0].values) == VARIABLE_VALUE_SAMPLE_LIMIT
    # Both surviving events report a written row; neither is the empty list a
    # dropped entry would have left behind.
    assert result.variable_values_written == 2


# --- folding two stored contexts decides every column from both sides (tripl-3rex) ---


def test_group_merge_demotes_a_fold_that_crosses_the_cardinality_threshold(
    sync_session: Session, project_and_type
):
    """Two ``low`` rows whose union is no longer an enumeration.

    ``low`` means "fewer distinct values than the threshold" and renders as an
    "All values" badge, so a fold that carries the row past the threshold has to
    demote it — and only ``high`` rows are trimmed, so leaving it ``low`` parked
    an uncapped over-threshold list behind that badge.
    """
    project, et, fds = project_and_type
    variable = _add_variable(sync_session, project, name="variant", binding="screen")
    target = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click events",
        screen="${variant}",
        action="/^click:/",
        order=0,
    )
    _add_context(
        sync_session,
        project,
        variable,
        target,
        fds["screen"],
        values=[f"t{i}" for i in range(30)],
        count=30,
        kind="low",
    )
    source = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click:one",
        screen="${variant}",
        action="click:one",
        order=1,
    )
    _add_context(
        sync_session,
        project,
        variable,
        source,
        fds["screen"],
        values=[f"s{i}" for i in range(30)],
        count=30,
        kind="low",
    )
    sync_session.commit()

    merged = merge_existing_events_for_group_rules(
        sync_session,
        project_id=project.id,
        event_type_ids=[et.id],
        event_group_rules=_CLICK_GROUP_RULE,
        # Passed rather than defaulted, so the threading from the caller is under
        # test too: a project that lowered the bound gets the demotion it asked
        # for, not the module's fallback of 100.
        cardinality_threshold=50,
    )
    sync_session.commit()

    assert merged == 1
    (folded,) = _contexts(sync_session, project.id)
    assert folded.event_id == target.id
    assert folded.value_kind == "high", "60 distinct values is not an enumeration under 50"
    assert folded.observed_count == 60, "counted before the trim, not after"
    assert len(folded.values) == VARIABLE_VALUE_SAMPLE_LIMIT


def test_group_merge_counts_the_union_even_when_the_fold_stays_an_enumeration(
    sync_session: Session, project_and_type
):
    """The count is the union's size, not the larger of the two stored counts.

    Same two rows under the default threshold: the fold is still an exact
    enumeration and keeps every value, so ``observed_count`` is the only column
    that moves — and a row whose stored count sits below the values it holds is
    exactly what the replay sink then has to correct on some later tick.
    """
    project, et, fds = project_and_type
    variable = _add_variable(sync_session, project, name="variant", binding="screen")
    target = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click events",
        screen="${variant}",
        action="/^click:/",
        order=0,
    )
    _add_context(
        sync_session,
        project,
        variable,
        target,
        fds["screen"],
        values=[f"t{i}" for i in range(30)],
        count=30,
        kind="low",
    )
    source = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click:one",
        screen="${variant}",
        action="click:one",
        order=1,
    )
    _add_context(
        sync_session,
        project,
        variable,
        source,
        fds["screen"],
        values=[f"s{i}" for i in range(30)],
        count=30,
        kind="low",
    )
    sync_session.commit()

    merged = merge_existing_events_for_group_rules(
        sync_session,
        project_id=project.id,
        event_type_ids=[et.id],
        event_group_rules=_CLICK_GROUP_RULE,
        # Left at DEFAULT_CARDINALITY_THRESHOLD on purpose: 60 distinct values
        # sit under it, so nothing demotes and the count is the sole change.
    )
    sync_session.commit()

    assert merged == 1
    (folded,) = _contexts(sync_session, project.id)
    assert folded.value_kind == "low"
    assert len(folded.values) == 60, "a low row is an exact enumeration and is never trimmed"
    assert folded.observed_count == 60


def test_group_merge_never_leaves_the_target_superseded_by_itself(
    sync_session: Session, project_and_type
):
    """A target that named the merged-away source comes out pointing at nothing.

    The trigger is ordinary: "click events" was deprecated in favour of
    "click:one", and a later scan's group rule folds "click:one" back into it.
    ``_move_superseded_pointers`` re-points every event whose successor was the
    source onto the target — and the target is the ONE row that must not be
    re-pointed, because "send this instead: itself" is not an instruction
    (tripl-0zpq.86). It outlives the merge: the event form re-sends
    ``superseded_by_event_id`` on every save while an event is deprecated, and
    ``event_service._resolve_successor`` answers 400 "An event cannot replace
    itself" on each one, so the row cannot be edited again until somebody
    un-deprecates it.

    RED on a revert: delete ``Event.id != target.id`` from the SELECT in
    ``_event_generator_merge_refs._move_superseded_pointers`` and the
    ``if target.superseded_by_event_id == source.id`` tail below it, and the
    blanket re-point writes ``target.id`` into the target's own pointer — the
    first assertion below reads ``target.id`` where it wants ``None``, in
    memory and again in the database.

    The in-memory assertion is not redundant with the stored one, and it is the
    half that pins the tail on its own: ``events.superseded_by_event_id`` is
    ``ON DELETE SET NULL`` with deliberately no ``relationship()``
    (``models/event.py``), so deleting the source clears the column in the
    DATABASE whatever the merge did or did not do to the loaded object. Only
    the object the rest of the transaction goes on reading shows the
    difference.
    """
    project, et, fds = project_and_type
    target = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click events",
        screen="/home",
        # Not a ``^click:`` match, so the group event is not itself a merge
        # candidate — the same spelling the fold tests above use.
        action="/^click:/",
        order=0,
    )
    # Retired, which is both the realistic state for an event with a successor
    # and the state that keeps the status fold out of this test: with either
    # side retired the merge leaves the target's status exactly as it is.
    target.status = "deprecated"
    source = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="click:one",
        screen="/home",
        action="click:one",
        order=1,
    )
    bystander = _add_event(
        sync_session,
        project,
        et,
        fds,
        name="view:one",
        screen="/home",
        action="view:one",
        order=2,
    )
    # Both retired events point at the row the group rule is about to swallow.
    target.superseded_by_event_id = source.id
    bystander.superseded_by_event_id = source.id
    bystander.status = "deprecated"
    sync_session.commit()

    merged = merge_existing_events_for_group_rules(
        sync_session,
        project_id=project.id,
        event_type_ids=[et.id],
        event_group_rules=_CLICK_GROUP_RULE,
    )

    assert merged == 1
    assert target.superseded_by_event_id is None, "an event may not supersede itself"
    assert bystander.superseded_by_event_id == target.id, "the target is what to send now"

    sync_session.commit()
    # ``expire_on_commit=False`` on this fixture, so the re-read has to be asked
    # for: without the expiry the loop below would hand back the same objects
    # just asserted on and prove nothing about what was written.
    sync_session.expire_all()
    stored = {
        row.name: row.superseded_by_event_id
        for row in sync_session.execute(
            select(Event).where(Event.project_id == project.id)
        ).scalars()
    }
    assert stored == {"click events": None, "view:one": target.id}


def test_json_path_sampling_reads_each_paths_type_before_the_values_turn_to_text(
    sync_session: Session, project_and_type
):
    """F23.4: the decoded sample still knows 42 from "42"; the stored context
    does not, so the type is read here or nowhere."""
    from tripl.worker.tasks.metrics.catalog_sync import _collect_json_path_samples

    project, et, fds = project_and_type
    config = _sampling_fixtures(sync_session, project, source_name="payload.user.plan")
    event = _seed_event(sync_session, project, et, "Signup")
    for source_name in ("payload.cart.qty", "payload.user.code"):
        extra = _add_path_variable(sync_session, project, source_name)
        _seed_context_row(sync_session, extra, event, fds["payload"], observed_count=0, values=[])
    plan = sync_session.execute(select(Variable).where(Variable.name == "plan")).scalar_one()
    _seed_context_row(sync_session, plan, event, fds["payload"], observed_count=0, values=[])

    class _Adapter:
        json_path_samples_are_text = True

        def get_json_path_samples(self, *args: object, **kwargs: object):
            return {
                "payload": {
                    "user.plan": ['"pro"', "null"],
                    "cart.qty": ["2", "10"],
                    "user.code": ['"42"', "42"],
                }
            }

    sampling = _collect_json_path_samples(
        sync_session,
        adapter=_Adapter(),
        config=config,
        columns=[ColumnInfo("payload", "JSON")],
        catalog_scan_window=None,
        time_from_dt=datetime(2026, 8, 30, tzinfo=UTC),
        time_to_dt=datetime(2026, 8, 30, 1, tzinfo=UTC),
    )

    # A string beside a number is a conflict, so user.code gets no type.
    assert sampling.types == {
        "payload": {"user.plan": ("string", None), "cart.qty": ("number", None)}
    }


def test_decoded_samples_are_not_decoded_twice(sync_session: Session, project_and_type):
    """The fallback sampler (BigQuery, Synthetic) returns decoded values: the
    string "123" is a string there, not the JSON text of a number."""
    from tripl.worker.tasks.metrics.catalog_sync import _collect_json_path_samples

    project, et, fds = project_and_type
    config = _sampling_fixtures(sync_session, project, source_name="payload.user.plan")
    plan = sync_session.execute(select(Variable)).scalar_one()
    event = _seed_event(sync_session, project, et, "Signup")
    _seed_context_row(sync_session, plan, event, fds["payload"], observed_count=0, values=[])

    class _Adapter:
        def get_json_path_samples(self, *args: object, **kwargs: object):
            return {"payload": {"user.plan": ["123", "true"]}}

    sampling = _collect_json_path_samples(
        sync_session,
        adapter=_Adapter(),
        config=config,
        columns=[ColumnInfo("payload", "JSON")],
        catalog_scan_window=None,
        time_from_dt=datetime(2026, 8, 30, tzinfo=UTC),
        time_to_dt=datetime(2026, 8, 30, 1, tzinfo=UTC),
    )
    assert sampling.types == {"payload": {"user.plan": ("string", None)}}


def test_inferred_types_land_only_on_untyped_scan_variables(
    sync_session: Session, project_and_type
):
    from tripl.core.analyzers._event_generator_variables import SCAN_PROVENANCE_DESCRIPTION
    from tripl.worker.tasks.metrics.catalog_sync import _apply_inferred_types

    project, _et, _fds = project_and_type

    def variable(source_name: str, **overrides: object) -> Variable:
        row = _add_path_variable(sync_session, project, source_name)
        row.description = SCAN_PROVENANCE_DESCRIPTION
        for key, value in overrides.items():
            setattr(row, key, value)
        sync_session.commit()
        return row

    fresh = variable("payload.cart.qty")
    # A JSON ``null`` rather than SQL NULL, as a row copied before the column
    # stored None as NULL holds it.
    json_null = variable("payload.cart.seen")
    sync_session.execute(
        Variable.__table__.update()
        .where(Variable.__table__.c.id == json_null.id)
        .values(json_schema=sa.text("'null'"))
    )
    sync_session.commit()
    described = variable("payload.cart.total", description="What the cart cost")
    retyped = variable("payload.cart.items", variable_type="number")
    excluded = variable("payload.cart.flag", excluded_from_scans=True)

    typed = _apply_inferred_types(
        sync_session,
        project_id=project.id,
        branch_id=None,
        types={
            "payload": {
                "cart.qty": ("json", {"type": "array"}),
                "cart.seen": ("boolean", None),
                "cart.total": ("number", None),
                "cart.items": ("json", {"type": "array"}),
                "cart.flag": ("boolean", None),
            }
        },
    )
    sync_session.commit()

    assert typed == 2
    assert (fresh.variable_type, fresh.json_schema) == ("json", {"type": "array"})
    sync_session.refresh(json_null)
    assert json_null.variable_type == "boolean"
    assert (described.variable_type, described.json_schema) == ("string", None)
    assert (retyped.variable_type, retyped.json_schema) == ("number", None)
    assert (excluded.variable_type, excluded.json_schema) == ("string", None)


def test_collapsing_rows_keep_every_json_key_with_its_presence(
    sync_session: Session, project_and_type
):
    """F23: an optional property used to vanish when the busiest row lacked it.
    The event now carries the union of the keys, and each path's context says
    how often the event carried it."""
    project, et, fds = project_and_type
    analysis = BreakdownAnalysis(
        results={
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=1,
                is_low=True,
                sample_values=["purchase"],
            ),
            "payload": CardinalityResult(
                column=ColumnInfo("payload", "JSON"),
                count=2,
                is_low=True,
                json_path_combos=[("amount", "currency"), ("amount", "coupon")],
            ),
        },
        rows=[
            ("purchase", ("amount", "currency"), 90),
            ("purchase", ("amount", "coupon"), 10),
        ],
        reg_names=["action"],
        json_names=["payload"],
    )

    result = generate_events(
        sync_session, project.id, et.id, analysis, fds, event_name_format="{action}"
    )
    sync_session.commit()

    assert result.events_created == 1
    payload_value = sync_session.execute(
        select(EventFieldValue.value).where(
            EventFieldValue.field_definition_id == fds["payload"].id
        )
    ).scalar_one()
    assert json.loads(payload_value) == {
        "amount": "${amount}",
        "coupon": "${coupon}",
        "currency": "${currency}",
    }
    presence = {
        variable.source_name: context.presence_rate
        for context, variable in sync_session.execute(
            select(VariableValue, Variable).join(Variable, Variable.id == VariableValue.variable_id)
        ).all()
    }
    assert presence == {
        "payload.amount": 1.0,
        "payload.currency": 0.9,
        "payload.coupon": 0.1,
    }
    # The JSON column no longer reports a collapse: every row carries the union.
    assert not any("payload" in detail and "values)" in detail for detail in result.details)


def test_property_drift_reports_new_and_missing_required_keys(
    sync_session: Session, project_and_type
):
    """F23.5: a second scan reports the key the list does not name and the
    required key the event carried too rarely, and writes no list entry."""
    from tripl.models.property_drift import PropertyDrift

    project, et, fds = project_and_type

    def analysis() -> BreakdownAnalysis:
        return BreakdownAnalysis(
            results={
                "action": CardinalityResult(
                    column=ColumnInfo("action", "String"),
                    count=1,
                    is_low=True,
                    sample_values=["purchase"],
                ),
                "payload": CardinalityResult(
                    column=ColumnInfo("payload", "JSON"),
                    count=2,
                    is_low=True,
                    json_path_combos=[("amount", "currency"), ("amount",)],
                ),
            },
            rows=[
                ("purchase", ("amount", "currency"), 90),
                ("purchase", ("amount",), 10),
            ],
            reg_names=["action"],
            json_names=["payload"],
        )

    generate_events(sync_session, project.id, et.id, analysis(), fds, event_name_format="{action}")
    sync_session.commit()
    event = sync_session.execute(select(Event)).scalar_one()
    by_source = {v.source_name: v for v in sync_session.execute(select(Variable)).scalars()}
    # The list names amount (required) and currency (required); coupon is not there.
    for source_name in ("payload.amount", "payload.currency"):
        sync_session.add(
            VariableEventValueOverride(
                project_id=project.id,
                variable_id=by_source[source_name].id,
                event_id=event.id,
                values=None,
                required=True,
            )
        )
    sync_session.commit()
    assert sync_session.execute(select(PropertyDrift)).scalars().all() == []

    second = analysis()
    second.results["payload"].json_path_combos = [("amount", "currency"), ("amount", "coupon")]
    second.rows = [("purchase", ("amount", "currency"), 90), ("purchase", ("amount", "coupon"), 10)]
    result = generate_events(
        sync_session, project.id, et.id, second, fds, event_name_format="{action}"
    )
    sync_session.commit()

    drifts = {
        (d.variable.source_name, d.kind): d.detail
        for d in sync_session.execute(select(PropertyDrift)).scalars()
    }
    assert drifts == {
        ("payload.coupon", "new_property"): {"presence_rate": 0.1},
        ("payload.currency", "missing_required"): {"presence_rate": 0.9, "threshold": 0.95},
    }
    assert result.property_drifts_detected == 2
    # A lower threshold on the event clears the missing-required finding's cause.
    event.required_presence_threshold = 0.8
    sync_session.commit()
    generate_events(sync_session, project.id, et.id, second, fds, event_name_format="{action}")
    sync_session.commit()
    kinds = {d.kind for d in sync_session.execute(select(PropertyDrift)).scalars()}
    # The untriaged missing-required row went with its cause.
    assert kinds == {"new_property"}
    # The scan wrote no list entry for coupon.
    listed = {
        o.variable_id for o in sync_session.execute(select(VariableEventValueOverride)).scalars()
    }
    assert by_source["payload.amount"].id in listed
    assert len(listed) == 2

    # An archived event is frozen: its required keys are not judged.
    event.required_presence_threshold = None
    event.status = EventStatus.archived
    for drift in sync_session.execute(select(PropertyDrift)).scalars():
        sync_session.delete(drift)
    sync_session.commit()
    generate_events(sync_session, project.id, et.id, second, fds, event_name_format="{action}")
    sync_session.commit()
    assert sync_session.execute(select(PropertyDrift)).scalars().all() == []
