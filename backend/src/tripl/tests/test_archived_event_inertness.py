"""Archived events are inert.

Archiving means "put it away". These tests pin the three ways that promise was
being broken, and the one thing it deliberately does NOT mean:

* an archived identity is never filed as a shadow event candidate;
* archived volume leaves BOTH sides of coverage, so archiving a busy event
  cannot move the project's coverage percentage;
* a scan neither rewrites an archived row's field values nor lets a group rule
  merge or delete it;
* "archived but still arriving" is still reported — on the scan run, which is
  off the coverage percentage rather than folded into it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.cardinality import BreakdownAnalysis, CardinalityResult
from tripl.core.analyzers.event_generator import (
    GenerationResult,
    generate_events,
    merge_existing_events_for_group_rules,
)
from tripl.models import Base
from tripl.models.coverage_metric import CoverageMetric
from tripl.models.event import Event, EventStatus
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.models.shadow_event_candidate import SHADOW_STATUS_NEW, ShadowEventCandidate
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.conftest import TestSessionLocal

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def sync_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    # Before create_all: the pooled connection is opened by the first statement,
    # and a listener registered after that never fires. Archival inertness is a
    # claim about what a merge does NOT delete, which needs a database that
    # actually deletes — see ``_sqlite``.
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


_ProjectAndType = tuple[Project, EventType, dict[str, FieldDefinition]]


@pytest.fixture
def project_and_type(sync_session: Session) -> _ProjectAndType:
    project = Project(id=uuid.uuid4(), name="Archival", slug="archival-inert", description="")
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
        name: FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=event_type.id,
            name=name,
            display_name=name.title(),
            field_type="string",
            order=order,
        )
        for order, name in enumerate(["screen", "action"])
    }
    sync_session.add_all(field_definitions.values())
    sync_session.commit()
    return project, event_type, field_definitions


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'archived_inertness.db'}")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _screen_analysis(*values: str) -> BreakdownAnalysis:
    return BreakdownAnalysis(
        results={
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=len(values),
                is_low=True,
                sample_values=list(values),
            ),
        },
        rows=[(value,) for value in values],
        reg_names=["screen"],
        json_names=[],
        json_value_names=[],
    )


# --------------------------------------------------------------------------
# generate_events: archived rows are frozen and reported separately
# --------------------------------------------------------------------------


class TestGenerateEventsLeavesArchivedRowsAlone:
    def test_archived_identity_is_partitioned_out_of_events_by_name(
        self,
        sync_session: Session,
        project_and_type: _ProjectAndType,
    ) -> None:
        """The identity leaves ``events_by_name`` but stays reachable by name.

        This is the contract the metrics collector relies on to tell "put away"
        apart from "never planned"; the end-to-end tests below mirror it.
        """
        project, event_type, field_definitions = project_and_type
        sync_session.add(
            Event(
                id=uuid.uuid4(),
                project_id=project.id,
                event_type_id=event_type.id,
                name="Dashboard",
                source_name="screen=/dashboard",
                status=EventStatus.archived,
                order=0,
            )
        )
        sync_session.commit()

        result = generate_events(
            sync_session,
            project.id,
            event_type.id,
            _screen_analysis("/dashboard"),
            field_definitions,
        )
        sync_session.commit()

        assert result.events_created == 0
        assert "screen=/dashboard" not in result.events_by_name
        assert result.archived_identities == {"screen=/dashboard"}

    def test_scan_does_not_rewrite_field_values_on_an_archived_row(
        self,
        sync_session: Session,
        project_and_type: _ProjectAndType,
    ) -> None:
        project, event_type, field_definitions = project_and_type
        archived = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=event_type.id,
            name="Dashboard",
            source_name="screen=/dashboard",
            status=EventStatus.archived,
            order=0,
        )
        sync_session.add(archived)
        sync_session.flush()
        stale = EventFieldValue(
            id=uuid.uuid4(),
            event_id=archived.id,
            field_definition_id=field_definitions["screen"].id,
            value="/dashboard-as-it-was-when-archived",
            is_authored=False,
        )
        sync_session.add(stale)
        sync_session.commit()

        generate_events(
            sync_session,
            project.id,
            event_type.id,
            _screen_analysis("/dashboard"),
            field_definitions,
        )
        sync_session.commit()

        value = sync_session.execute(
            select(EventFieldValue.value).where(EventFieldValue.event_id == archived.id)
        ).scalar_one()
        assert value == "/dashboard-as-it-was-when-archived"

    def test_scan_does_not_add_field_values_to_an_archived_row(
        self,
        sync_session: Session,
        project_and_type: _ProjectAndType,
    ) -> None:
        """Frozen means frozen: not even a first observation gets written."""
        project, event_type, field_definitions = project_and_type
        archived = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=event_type.id,
            name="Dashboard",
            source_name="screen=/dashboard",
            status=EventStatus.archived,
            order=0,
        )
        sync_session.add(archived)
        sync_session.commit()
        archived_id = archived.id

        result = generate_events(
            sync_session,
            project.id,
            event_type.id,
            _screen_analysis("/dashboard"),
            field_definitions,
        )
        sync_session.commit()

        # Still counted against the plan — the identity is known, just retired.
        assert result.events_skipped == 1
        values = (
            sync_session.execute(
                select(EventFieldValue).where(EventFieldValue.event_id == archived_id)
            )
            .scalars()
            .all()
        )
        assert values == []

    def test_a_live_sibling_identity_is_still_refreshed(
        self,
        sync_session: Session,
        project_and_type: _ProjectAndType,
    ) -> None:
        """Freezing archived rows must not freeze the rest of the catalog."""
        project, event_type, field_definitions = project_and_type
        live = Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=event_type.id,
            name="Settings",
            source_name="screen=/settings",
            status=EventStatus.live,
            order=0,
        )
        sync_session.add(live)
        sync_session.flush()
        sync_session.add(
            EventFieldValue(
                id=uuid.uuid4(),
                event_id=live.id,
                field_definition_id=field_definitions["screen"].id,
                value="stale",
                is_authored=False,
            )
        )
        sync_session.commit()

        generate_events(
            sync_session,
            project.id,
            event_type.id,
            _screen_analysis("/settings"),
            field_definitions,
        )
        sync_session.commit()

        value = sync_session.execute(
            select(EventFieldValue.value).where(EventFieldValue.event_id == live.id)
        ).scalar_one()
        assert value == "/settings"


# --------------------------------------------------------------------------
# group rules never rewrite or delete an archived row
# --------------------------------------------------------------------------


def _seed_event_with_action(
    session: Session,
    *,
    project: Project,
    event_type: EventType,
    field_definitions: dict[str, FieldDefinition],
    action: str,
    status: EventStatus,
    order: int,
) -> Event:
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=event_type.id,
        name=action,
        source_name=action,
        status=status,
        order=order,
    )
    session.add(event)
    session.flush()
    session.add(
        EventFieldValue(
            id=uuid.uuid4(),
            event_id=event.id,
            field_definition_id=field_definitions["action"].id,
            value=action,
        )
    )
    return event


_BUTTON_RULE = [
    {
        "name": "button events",
        "condition_logic": "all",
        "conditions": [{"field": "action", "pattern": "^button:"}],
    }
]


class TestGroupRulesCannotTouchArchivedRows:
    def test_merge_cannot_delete_an_archived_row(
        self,
        sync_session: Session,
        project_and_type: _ProjectAndType,
    ) -> None:
        project, event_type, field_definitions = project_and_type
        archived = _seed_event_with_action(
            sync_session,
            project=project,
            event_type=event_type,
            field_definitions=field_definitions,
            action="button:primary",
            status=EventStatus.archived,
            order=0,
        )
        live = _seed_event_with_action(
            sync_session,
            project=project,
            event_type=event_type,
            field_definitions=field_definitions,
            action="button:secondary",
            status=EventStatus.live,
            order=1,
        )
        sync_session.commit()
        archived_id, live_id = archived.id, live.id

        merged = merge_existing_events_for_group_rules(
            sync_session,
            project_id=project.id,
            event_type_ids=[event_type.id],
            event_group_rules=_BUTTON_RULE,
        )
        sync_session.commit()

        # The archived row survives, keeps its identity and its value.
        survivor = sync_session.get(Event, archived_id)
        assert survivor is not None
        assert survivor.source_name == "button:primary"
        assert survivor.status == EventStatus.archived
        action_value = sync_session.execute(
            select(EventFieldValue.value).where(EventFieldValue.event_id == archived_id)
        ).scalar_one()
        assert action_value == "button:primary"

        # ...while the live sibling is still grouped as before.
        assert merged == 1
        assert sync_session.get(Event, live_id) is None
        source_names = set(
            sync_session.execute(
                select(Event.source_name).where(Event.project_id == project.id)
            ).scalars()
        )
        assert source_names == {"button:primary", "button events"}

    def test_live_volume_is_not_merged_into_an_archived_group_event(
        self,
        sync_session: Session,
        project_and_type: _ProjectAndType,
    ) -> None:
        """A merge whose TARGET is archived is refused, so neither row is rewritten.

        This pins the CATALOG outcome, not a volume: merging would have deleted
        the live source (``_merge_event_into_group`` ends in ``session.delete``)
        to hand its identity to a row the user retired. Collection-time grouping
        still routes incoming warehouse rows to the group name either way — that
        half is what the coverage tests above cover.
        """
        project, event_type, field_definitions = project_and_type
        archived_target = _seed_event_with_action(
            sync_session,
            project=project,
            event_type=event_type,
            field_definitions=field_definitions,
            action="button events",
            status=EventStatus.archived,
            order=0,
        )
        live = _seed_event_with_action(
            sync_session,
            project=project,
            event_type=event_type,
            field_definitions=field_definitions,
            action="button:primary",
            status=EventStatus.live,
            order=1,
        )
        sync_session.commit()
        live_id = live.id
        archived_id = archived_target.id

        merged = merge_existing_events_for_group_rules(
            sync_session,
            project_id=project.id,
            event_type_ids=[event_type.id],
            event_group_rules=_BUTTON_RULE,
        )
        sync_session.commit()

        assert merged == 0
        # The live source is the row the merge would have deleted.
        survivor = sync_session.get(Event, live_id)
        assert survivor is not None
        assert survivor.status == EventStatus.live
        # ...and the retired row is left exactly as retired: a refused merge must
        # not quietly revive the target it declined to write into.
        target = sync_session.get(Event, archived_id)
        assert target is not None
        assert target.status == EventStatus.archived

    def test_scan_time_grouping_also_spares_archived_rows(
        self,
        sync_session: Session,
        project_and_type: _ProjectAndType,
    ) -> None:
        """``generate_events`` shares the merge helper — pin it on that path too."""
        project, event_type, field_definitions = project_and_type
        archived = _seed_event_with_action(
            sync_session,
            project=project,
            event_type=event_type,
            field_definitions=field_definitions,
            action="button:primary",
            status=EventStatus.archived,
            order=0,
        )
        sync_session.commit()
        archived_id = archived.id

        generate_events(
            sync_session,
            project.id,
            event_type.id,
            BreakdownAnalysis(
                results={
                    "action": CardinalityResult(
                        column=ColumnInfo("action", "String"),
                        count=1,
                        is_low=True,
                        sample_values=["button:primary"],
                    ),
                },
                rows=[("button:primary",)],
                reg_names=["action"],
                json_names=[],
                json_value_names=[],
            ),
            field_definitions,
            event_name_format="{action}",
            event_group_rules=_BUTTON_RULE,
        )
        sync_session.commit()

        assert sync_session.get(Event, archived_id) is not None


# --------------------------------------------------------------------------
# collect_metrics: coverage and the shadow inbox
# --------------------------------------------------------------------------


def _fake_adapter(rows: Sequence[tuple[object, ...]]) -> type:
    class FakeAdapter:
        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
            ]

        def get_time_bucketed_counts(
            self,
            base_query: str,
            time_column: str,
            interval: str,
            regular_columns: list[str],
            json_columns: list[str],
            json_value_paths: dict[str, list[str]] | None,
            time_from: datetime,
            time_to: datetime,
            limit: int = 100000,
        ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
            return (["event_name"], [], list(rows))

        def close(self) -> None:
            return None

    return FakeAdapter


def _generation_result_for(events: list[Event]) -> GenerationResult:
    """Split events the way ``generate_events`` hands them to the collector.

    Mirrors the tail of ``generate_events`` (pinned directly by
    ``test_archived_identity_is_partitioned_out_of_events_by_name``) so these
    end-to-end tests exercise the real collector against a faithful input.
    """
    by_identity = {str(event.source_name or event.name): event for event in events}
    result = GenerationResult(
        columns_analyzed=1,
        col_meta={"event_name": {"is_json": False, "is_low": True}},
    )
    result.events_by_name = {
        identity: event
        for identity, event in by_identity.items()
        if event.status != EventStatus.archived
    }
    result.archived_identities = {
        identity for identity, event in by_identity.items() if event.status == EventStatus.archived
    }
    return result


def _install_collector_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_factory: sessionmaker[Session],
    rows: Sequence[tuple[object, ...]],
    event_ids: list[uuid.UUID],
) -> None:
    from tripl.worker.tasks.metrics import tasks as metrics

    monkeypatch.setattr(metrics, "_get_sync_session", session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: _fake_adapter(rows)())
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11), False),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with session_factory() as session:
            events = [session.get(Event, event_id) for event_id in event_ids]
            return _generation_result_for([event for event in events if event is not None])

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)


def _coverage_pct(session: Session, scan_config_id: uuid.UUID) -> float:
    """The same arithmetic ``reconciliation_service.get_coverage`` reports."""
    rows = (
        session.execute(
            select(CoverageMetric.total_count, CoverageMetric.matched_count).where(
                CoverageMetric.scan_config_id == scan_config_id
            )
        )
    ).all()
    total = sum(int(row[0]) for row in rows)
    matched = sum(int(row[1]) for row in rows)
    return round(matched / total * 100, 2) if total else 0.0


def _seed_collector_project(
    session: Session, *, identities: list[str]
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    from tripl.tests.test_metrics_tasks import _create_scan_config

    config = _create_scan_config(session, with_event_type=True)
    assert config.event_type_id is not None
    event_ids: list[uuid.UUID] = []
    for order, identity in enumerate(identities):
        event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name=identity,
            source_name=identity,
            description="",
            status=EventStatus.live,
            order=order,
        )
        session.add(event)
        event_ids.append(event.id)
    session.commit()
    return config.id, config.project_id, event_ids


def _archive(session_factory: sessionmaker[Session], event_id: uuid.UUID) -> None:
    with session_factory() as session:
        event = session.get(Event, event_id)
        assert event is not None
        event.status = EventStatus.archived
        session.commit()


def test_archiving_a_busy_event_does_not_move_the_coverage_percentage(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline promise of archiving.

    A fully instrumented project sits at 100% coverage. Archiving its busiest
    event — nine tenths of all traffic — must leave that number exactly where it
    was: archiving withdraws the event from the plan-vs-reality question on BOTH
    sides. Before the fix the archived volume stayed in the denominator only and
    dropped coverage to 10%.
    """
    from tripl.worker.tasks.metrics import tasks as metrics

    warehouse_rows = [
        (datetime(2026, 1, 1, 10), "Login", 900),
        (datetime(2026, 1, 1, 10), "Checkout", 100),
    ]
    with sync_session_factory() as session:
        config_id, _project_id, event_ids = _seed_collector_project(
            session, identities=["event_name=Login", "event_name=Checkout"]
        )
    login_id = event_ids[0]

    _install_collector_fakes(
        monkeypatch,
        session_factory=sync_session_factory,
        rows=warehouse_rows,
        event_ids=event_ids,
    )

    metrics.collect_metrics.run(str(config_id))
    with sync_session_factory() as session:
        pct_before = _coverage_pct(session, config_id)
    assert pct_before == 100.0

    _archive(sync_session_factory, login_id)
    metrics.collect_metrics.run(str(config_id))

    with sync_session_factory() as session:
        assert _coverage_pct(session, config_id) == pct_before
        coverage = session.execute(
            select(CoverageMetric).where(CoverageMetric.scan_config_id == config_id)
        ).scalar_one()
        # The archived event's 900 rows left the denominator with it.
        assert coverage.total_count == 100
        assert coverage.matched_count == 100


def test_archived_identity_is_not_filed_as_a_shadow_candidate(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tripl.worker.tasks.metrics import tasks as metrics

    warehouse_rows = [
        (datetime(2026, 1, 1, 10), "Login", 900),
        (datetime(2026, 1, 1, 10), "Mystery", 100),
    ]
    with sync_session_factory() as session:
        config_id, _project_id, event_ids = _seed_collector_project(
            session, identities=["event_name=Login"]
        )
    _archive(sync_session_factory, event_ids[0])

    _install_collector_fakes(
        monkeypatch,
        session_factory=sync_session_factory,
        rows=warehouse_rows,
        event_ids=event_ids,
    )
    metrics.collect_metrics.run(str(config_id))

    with sync_session_factory() as session:
        candidates = (
            session.execute(
                select(ShadowEventCandidate).where(ShadowEventCandidate.scan_config_id == config_id)
            )
            .scalars()
            .all()
        )
        # Only the genuinely unplanned identity shows up.
        assert [candidate.event_name for candidate in candidates] == ["event_name=Mystery"]

        # ...and only its volume is unmatched plan traffic.
        coverage = session.execute(
            select(CoverageMetric).where(CoverageMetric.scan_config_id == config_id)
        ).scalar_one()
        assert coverage.total_count == 100
        assert coverage.matched_count == 0


def test_still_arriving_archived_volume_is_reported_on_the_run(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off the coverage percentage, but not thrown away."""
    from tripl.worker.tasks.metrics import tasks as metrics

    warehouse_rows = [(datetime(2026, 1, 1, 10), "Login", 900)]
    with sync_session_factory() as session:
        config_id, _project_id, event_ids = _seed_collector_project(
            session, identities=["event_name=Login"]
        )
    _archive(sync_session_factory, event_ids[0])

    _install_collector_fakes(
        monkeypatch,
        session_factory=sync_session_factory,
        rows=warehouse_rows,
        event_ids=event_ids,
    )
    summary = metrics.collect_metrics.run(str(config_id))

    assert summary["archived_event_volume"] == 900
    assert summary["archived_events_still_arriving"] == 1
    assert any("Archived events still arriving" in detail for detail in summary["details"])


def test_a_quiet_archived_event_reports_nothing(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tripl.worker.tasks.metrics import tasks as metrics

    warehouse_rows = [(datetime(2026, 1, 1, 10), "Checkout", 100)]
    with sync_session_factory() as session:
        config_id, _project_id, event_ids = _seed_collector_project(
            session, identities=["event_name=Login", "event_name=Checkout"]
        )
    _archive(sync_session_factory, event_ids[0])

    _install_collector_fakes(
        monkeypatch,
        session_factory=sync_session_factory,
        rows=warehouse_rows,
        event_ids=event_ids,
    )
    summary = metrics.collect_metrics.run(str(config_id))

    assert summary["archived_event_volume"] == 0
    assert summary["archived_events_still_arriving"] == 0
    assert not any("Archived events still arriving" in detail for detail in summary["details"])


# --------------------------------------------------------------------------
# reconciliation inbox: candidates written before the fix stay hidden
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_inbox_hides_candidates_for_archived_identities(
    client: AsyncClient,
) -> None:
    """Rows written before archiving shipped must not sit in the inbox forever.

    Accepting one only 409s on the duplicate source identity, so without this
    filter the user has no way to clear it.
    """
    from tripl.models.data_source import DataSource
    from tripl.models.scan_config import ScanConfig

    await client.post("/api/v1/projects", json={"name": "Arch", "slug": "arch-inbox"})
    event_type = await client.post(
        "/api/v1/projects/arch-inbox/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    event = await client.post(
        "/api/v1/projects/arch-inbox/events",
        json={"event_type_id": event_type.json()["id"], "name": "Retired"},
    )
    event_id = uuid.UUID(event.json()["id"])

    async with TestSessionLocal() as session:
        retired = await session.get(Event, event_id)
        assert retired is not None
        retired.source_name = "event_name=Retired"
        retired.status = EventStatus.archived
        project_id = retired.project_id

        data_source = DataSource(
            name=f"wh-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=9000,
            database_name="db",
            username="u",
        )
        session.add(data_source)
        await session.flush()
        config = ScanConfig(
            project_id=project_id,
            data_source_id=data_source.id,
            name="main scan",
            base_query="SELECT 1",
        )
        session.add(config)
        await session.flush()
        session.add_all(
            [
                ShadowEventCandidate(
                    project_id=project_id,
                    scan_config_id=config.id,
                    event_name=name,
                    observed_count=count,
                    first_seen_at=NOW - timedelta(days=2),
                    last_seen_at=NOW,
                    status=SHADOW_STATUS_NEW,
                )
                for name, count in [("event_name=Retired", 900), ("event_name=Genuine", 5)]
            ]
        )
        await session.commit()

    resp = await client.get("/api/v1/projects/arch-inbox/reconciliation/shadow-events")
    assert resp.status_code == 200
    body = resp.json()
    assert [item["event_name"] for item in body["items"]] == ["event_name=Genuine"]
    assert body["total"] == 1
    assert body["new_count"] == 1
