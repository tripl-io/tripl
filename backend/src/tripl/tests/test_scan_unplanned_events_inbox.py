"""Unplanned events: a scan adds them to the catalog, or leaves them to the inbox.

``ScanConfig.auto_create_events`` decides what a run does with an identity no
catalog event answers to. On (the default, and the behaviour before the setting
existed) the identity becomes an ``in_review`` event. Off, no event is added
and the metrics collection files the identity in the reconciliation inbox as a
shadow candidate. These tests pin both answers and every place the
setting has to reach: the API, the generator, the metrics collection and the
dry run, which must not promise events the run would not create.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.cardinality import BreakdownAnalysis, CardinalityResult
from tripl.core.analyzers.event_generator import GenerationResult, generate_events
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_dry_run_job import ScanDryRunJob
from tripl.models.shadow_event_candidate import (
    SHADOW_STATUS_ACCEPTED,
    SHADOW_STATUS_DISMISSED,
    SHADOW_STATUS_NEW,
    ShadowEventCandidate,
)
from tripl.models.variable import Variable
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_metrics_tasks import _create_scan_config
from tripl.tests.test_scan_dry_run import _FakeAdapter, _run, _seed
from tripl.worker.tasks import scan_dry_run as dry_run_tasks

INBOX_WARNING = "This scan sends unplanned events to the reconciliation inbox"


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'unplanned.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _catalog(session: Session) -> tuple[Project, EventType, dict[str, FieldDefinition]]:
    project = Project(id=uuid.uuid4(), name="Inbox", slug="inbox", description="")
    session.add(project)
    session.flush()
    event_type = EventType(
        id=uuid.uuid4(), project_id=project.id, name="pv", display_name="Page View", description=""
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
        for order, name in enumerate(("screen", "action"))
    }
    session.add_all(fields.values())
    session.commit()
    return project, event_type, fields


def _analysis(rows: list[tuple[str, str]]) -> BreakdownAnalysis:
    screens = sorted({screen for screen, _ in rows})
    actions = sorted({action for _, action in rows})
    return BreakdownAnalysis(
        results={
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=len(screens),
                is_low=True,
                sample_values=screens,
            ),
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=len(actions),
                is_low=True,
                sample_values=actions,
            ),
        },
        rows=list(rows),
        reg_names=["screen", "action"],
        json_names=[],
    )


def _events(session: Session, project_id: uuid.UUID) -> list[Event]:
    return list(session.scalars(select(Event).where(Event.project_id == project_id)))


# --------------------------------------------------------------------------- #
# the generator
# --------------------------------------------------------------------------- #


def test_a_scan_adds_unplanned_events_in_review_by_default(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        project, event_type, fields = _catalog(session)
        result = generate_events(
            session,
            project.id,
            event_type.id,
            _analysis([("/home", "click"), ("/about", "click")]),
            fields,
        )
        session.commit()

        assert result.events_created == 2
        assert result.events_left_unplanned == 0
        assert {event.status for event in _events(session, project.id)} == {"in_review"}


def test_with_creation_off_the_catalog_is_left_alone_and_known_events_still_match(
    session_factory: sessionmaker[Session],
) -> None:
    """The plan is the authority: only events it already has are touched.

    ``/about`` arrives on two rows and is still one unplanned identity, so the
    run's count is the number of events the inbox will be asked about, not a
    row count.
    """
    with session_factory() as session:
        project, event_type, fields = _catalog(session)
        generate_events(session, project.id, event_type.id, _analysis([("/home", "click")]), fields)
        session.commit()
        [planned] = _events(session, project.id)

        result = generate_events(
            session,
            project.id,
            event_type.id,
            _analysis([("/home", "click"), ("/about", "click"), ("/about", "click")]),
            fields,
            create_events=False,
        )
        session.commit()

        assert result.events_created == 0
        assert result.events_left_unplanned == 1
        assert [event.id for event in _events(session, project.id)] == [planned.id]
        assert planned.id in {event.id for event in result.events_by_name.values()}


def _with_path(rows: list[tuple[str, str]]) -> BreakdownAnalysis:
    """``_analysis`` plus ``path``, a column the planner turns into ``${path_id}``."""
    analysis = _analysis(rows)
    analysis.results["path"] = CardinalityResult(
        column=ColumnInfo("path", "String"),
        count=79,
        is_low=False,
        sample_values=[f"/item/{index}" for index in range(1, 80)],
    )
    analysis.reg_names.append("path")
    analysis.rows = [(*row, "/item/1") for row in rows]
    return analysis


def _variable_names(session: Session, project_id: uuid.UUID) -> list[str]:
    return list(session.scalars(select(Variable.name).where(Variable.project_id == project_id)))


def test_with_creation_off_no_property_is_minted_for_traffic_the_run_leaves_out(
    session_factory: sessionmaker[Session],
) -> None:
    """A property only unplanned rows carry would be referenced by nothing.

    The end-of-run sweep would retire it and the next run mint it again, a
    created/retired pair per tick. Once the plan has an event whose row carries
    the token, the property is the plan's and is created as before.
    """
    with session_factory() as session:
        project, event_type, fields = _catalog(session)
        fields["path"] = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=event_type.id,
            name="path",
            display_name="path",
            field_type="string",
            order=2,
        )
        session.add(fields["path"])
        session.commit()

        left_out = generate_events(
            session,
            project.id,
            event_type.id,
            _with_path([("/home", "click")]),
            fields,
            event_name_format="{screen}",
            create_events=False,
        )
        session.commit()
        assert left_out.events_left_unplanned == 1
        assert left_out.variables_created == 0
        assert _variable_names(session, project.id) == []

        # The plan gets ``/home`` (from a run that did not read ``path``); now
        # the row that carries the token reaches an event.
        generate_events(
            session,
            project.id,
            event_type.id,
            _analysis([("/home", "click")]),
            fields,
            event_name_format="{screen}",
        )
        session.commit()
        planned = generate_events(
            session,
            project.id,
            event_type.id,
            _with_path([("/home", "click")]),
            fields,
            event_name_format="{screen}",
            create_events=False,
        )
        session.commit()
        assert planned.events_left_unplanned == 0
        assert _variable_names(session, project.id) == ["path_id"]


def test_with_creation_off_a_group_rule_mints_no_group_event(
    session_factory: sessionmaker[Session],
) -> None:
    """The merge pass is a second way to create an event, and obeys the same setting."""
    rules = [{"name": "Pages", "conditions": [{"field": "screen", "pattern": "^/"}]}]
    with session_factory() as session:
        project, event_type, fields = _catalog(session)
        rows = [("/home", "click"), ("/about", "click")]
        generate_events(session, project.id, event_type.id, _analysis(rows), fields)
        session.commit()
        before = sorted(event.id for event in _events(session, project.id))

        result = generate_events(
            session,
            project.id,
            event_type.id,
            _analysis(rows),
            fields,
            event_group_rules=rules,
            create_events=False,
        )
        session.commit()

        assert result.events_created == 0
        assert sorted(event.id for event in _events(session, project.id)) == before
        assert "Pages" not in {event.name for event in _events(session, project.id)}


# --------------------------------------------------------------------------- #
# the API
# --------------------------------------------------------------------------- #


async def test_the_api_stores_the_setting_and_an_edit_without_it_keeps_it(
    client: AsyncClient,
) -> None:
    await client.post("/api/v1/projects", json={"name": "Inbox", "slug": "inbox-api"})
    source = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Inbox warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert source.status_code == 201, source.text
    body = {"data_source_id": source.json()["id"], "base_query": "SELECT 1"}
    scans = "/api/v1/projects/inbox-api/scans"

    default = await client.post(scans, json={**body, "name": "Default"})
    assert default.status_code == 201, default.text
    assert default.json()["auto_create_events"] is True

    inbox = await client.post(scans, json={**body, "name": "Inbox", "auto_create_events": False})
    assert inbox.status_code == 201, inbox.text
    assert inbox.json()["auto_create_events"] is False
    url = f"{scans}/{inbox.json()['id']}"

    # Neither an edit that leaves it out nor an explicit null turns creation back on.
    for patch in ({"name": "Renamed"}, {"auto_create_events": None}):
        resp = await client.patch(url, json=patch)
        assert resp.status_code == 200, resp.text
        assert resp.json()["auto_create_events"] is False

    resp = await client.patch(url, json={"auto_create_events": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["auto_create_events"] is True


# --------------------------------------------------------------------------- #
# the metrics collection
# --------------------------------------------------------------------------- #


def test_the_metrics_collection_files_an_unplanned_event_in_the_inbox(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The setting reaches the collection's catalog step, and the inbox gets the event.

    The generator is a spy here: what it would do with ``create_events=False``
    is pinned above. This test pins that the collection passes the scan's
    setting on, and that what the generator left out is filed as a shadow
    candidate rather than lost.
    """
    from tripl.worker.tasks.metrics import tasks as metrics

    with session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        config.auto_create_events = False
        login = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="live",
        )
        session.add(login)
        session.commit()
        config_id = config.id
        login_id = login.id

    class Adapter:
        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
            ]

        def get_time_bucketed_counts(self, *args: object, **kwargs: object) -> object:
            return (
                ["event_name"],
                [],
                [
                    (datetime(2026, 1, 1, 10), "Login", 12),
                    (datetime(2026, 1, 1, 10), "Mystery", 30),
                ],
            )

        def close(self) -> None:
            return None

    create_events_seen: list[object] = []

    def spy_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        create_events_seen.append(kwargs.get("create_events"))
        with session_factory() as session:
            event = session.get(Event, login_id)
            assert event is not None
            return GenerationResult(
                columns_analyzed=1,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": event},
                events_left_unplanned=1,
            )

    monkeypatch.setattr(metrics, "_get_sync_session", session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: Adapter())
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11), False),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())
    monkeypatch.setattr(metrics, "generate_events", spy_generate_events)

    metrics.collect_metrics.run(str(config_id))

    assert create_events_seen == [False]
    with session_factory() as session:
        candidate = session.scalars(
            select(ShadowEventCandidate).where(ShadowEventCandidate.scan_config_id == config_id)
        ).one()
        assert candidate.event_name == "event_name=Mystery"
        assert candidate.observed_count == 30
        assert candidate.status == SHADOW_STATUS_NEW
        names = session.scalars(select(Event.name).where(Event.project_id == config.project_id))
        assert list(names) == ["event_name=Login"]


def test_the_collection_itself_resolves_a_candidate_the_plan_now_holds(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driven through ``collect_metrics``, not the resolver: the wiring is the claim.

    ``Login`` was filed while the plan lacked it and has an event now; the run
    links it, with nobody recorded as resolving it. ``Mystery`` stays new, and
    a candidate someone dismissed stays dismissed even though its name is held.
    """
    from tripl.worker.tasks.metrics import tasks as metrics

    seen = datetime(2026, 1, 1, 9)
    with session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        config.auto_create_events = False
        login = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="live",
        )
        signup = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Signup",
            description="",
            status="live",
        )
        session.add_all([login, signup])
        session.flush()
        for name, status in (
            ("event_name=Login", SHADOW_STATUS_NEW),
            ("event_name=Signup", SHADOW_STATUS_DISMISSED),
        ):
            session.add(
                ShadowEventCandidate(
                    id=uuid.uuid4(),
                    project_id=config.project_id,
                    scan_config_id=config.id,
                    event_type_id=config.event_type_id,
                    event_name=name,
                    observed_count=5,
                    first_seen_at=seen,
                    last_seen_at=seen,
                    status=status,
                )
            )
        session.commit()
        config_id = config.id
        events_by_name = {"event_name=Login": login.id, "event_name=Signup": signup.id}

    class Adapter:
        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
            ]

        def get_time_bucketed_counts(self, *args: object, **kwargs: object) -> object:
            return (
                ["event_name"],
                [],
                [
                    (datetime(2026, 1, 1, 10), "Login", 12),
                    (datetime(2026, 1, 1, 10), "Mystery", 30),
                ],
            )

        def close(self) -> None:
            return None

    def generate(*args: object, **kwargs: object) -> GenerationResult:
        with session_factory() as session:
            return GenerationResult(
                columns_analyzed=1,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={
                    name: session.get_one(Event, event_id)
                    for name, event_id in events_by_name.items()
                },
                events_left_unplanned=1,
            )

    monkeypatch.setattr(metrics, "_get_sync_session", session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: Adapter())
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11), False),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())
    monkeypatch.setattr(metrics, "generate_events", generate)

    metrics.collect_metrics.run(str(config_id))

    with session_factory() as session:
        rows = {
            row.event_name: row
            for row in session.scalars(
                select(ShadowEventCandidate).where(ShadowEventCandidate.scan_config_id == config_id)
            )
        }
    assert rows["event_name=Login"].status == SHADOW_STATUS_ACCEPTED
    assert rows["event_name=Login"].accepted_event_id == events_by_name["event_name=Login"]
    assert rows["event_name=Login"].resolved_by is None
    assert rows["event_name=Signup"].status == SHADOW_STATUS_DISMISSED
    assert rows["event_name=Signup"].accepted_event_id is None
    assert rows["event_name=Mystery"].status == SHADOW_STATUS_NEW


def _upsert(session: Session, rows: list[dict[str, object]], **kwargs: object) -> None:
    from tripl.worker.tasks.metrics.metric_rows import _upsert_shadow_event_candidates

    _upsert_shadow_event_candidates(session, rows=rows, **kwargs)  # type: ignore[arg-type]
    session.commit()


def test_a_chunked_run_adds_its_chunks_and_a_resolved_type_stays_put(
    session_factory: sessionmaker[Session],
) -> None:
    """``observed_count`` is one run's window, however many chunks collect it.

    A name an earlier chunk of the run already counted is added to; any other
    name is replaced, so the previous run's count is never carried over. Once a
    person has resolved a candidate, its event type is the one they resolved it
    under, whatever type later windows find busiest.
    """
    with session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        other_type = EventType(
            id=uuid.uuid4(),
            project_id=config.project_id,
            name="other",
            display_name="Other",
            description="",
        )
        session.add(other_type)
        session.commit()
        at = datetime(2026, 1, 1, 10)

        def row(name: str, count: int, type_id: uuid.UUID | None) -> dict[str, object]:
            return {
                "id": uuid.uuid4(),
                "project_id": config.project_id,
                "scan_config_id": config.id,
                "event_type_id": type_id,
                "event_name": name,
                "observed_count": count,
                "first_seen_at": at,
                "last_seen_at": at,
                "status": SHADOW_STATUS_NEW,
                "sample_properties": [],
            }

        # The previous run.
        _upsert(session, [row("a", 100, config.event_type_id), row("b", 100, config.event_type_id)])
        # This run: chunk 1 sees both, chunk 2 sees ``a`` again.
        _upsert(session, [row("a", 7, config.event_type_id), row("b", 3, config.event_type_id)])
        _upsert(session, [row("a", 5, config.event_type_id)], accumulate_names={"a", "b"})

        counts = {
            c.event_name: c.observed_count for c in session.scalars(select(ShadowEventCandidate))
        }
        assert counts == {"a": 12, "b": 3}

        session.execute(
            update(ShadowEventCandidate)
            .where(ShadowEventCandidate.event_name == "a")
            .values(status=SHADOW_STATUS_DISMISSED)
        )
        session.commit()
        _upsert(session, [row("a", 50, other_type.id), row("b", 50, other_type.id)])
        types = {
            c.event_name: c.event_type_id
            for c in session.scalars(
                select(ShadowEventCandidate).execution_options(populate_existing=True)
            )
        }
        assert types == {"a": config.event_type_id, "b": other_type.id}


# --------------------------------------------------------------------------- #
# the dry run
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("auto_create_events", [True, False])
def test_the_dry_run_warns_when_its_new_events_would_go_to_the_inbox(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    auto_create_events: bool,
) -> None:
    """Still listed, so the reader sees what the inbox will get, but not promised."""
    job_id, project_id = _seed(session_factory)
    with session_factory() as session:
        config = session.scalars(
            select(ScanConfig).where(ScanConfig.project_id == project_id)
        ).one()
        config.auto_create_events = auto_create_events
        session.commit()

    result = _run(monkeypatch, session_factory, _FakeAdapter([("/home", "click", 9)]), job_id)

    assert len(result["events"]) == 1
    warned = [warning for warning in result["warnings"] if warning.startswith(INBOX_WARNING)]
    if auto_create_events:
        assert warned == []
    else:
        assert warned == [
            f"{INBOX_WARNING}: the 1 new event(s) below would not be added to the catalog. "
            "They appear under Reconciliation once metrics are collected."
        ]


def test_the_dry_run_warns_when_a_new_name_is_shared_by_several_event_types() -> None:
    """The inbox is unique on the name, so the dry run says two types become one row."""
    from tripl.worker.tasks.scan_dry_run import _shared_new_name_warning

    assert _shared_new_name_warning(
        {("click", "home"): 4, ("view", "home"): 9, ("view", "about"): 1, ("click", "known"): 2},
        existing_identities={("click", "known")},
    ) == (
        "1 new event name(s) appear under more than one event type ('home'). The "
        "reconciliation inbox lists each name once, under the type with the most "
        "volume, and Accept adds it to that type only."
    )
    assert _shared_new_name_warning({("view", "home"): 9}, existing_identities=set()) is None


def test_a_draft_dry_run_reads_the_setting_the_form_holds(
    session_factory: sessionmaker[Session],
) -> None:
    """A draft has no saved config to read it from, so its job carries the answer."""
    with session_factory() as session:
        project = Project(id=uuid.uuid4(), name="Draft", slug="draft", description="")
        source = DataSource(
            id=uuid.uuid4(),
            name="DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        session.add_all([project, source])
        session.flush()
        jobs = {
            setting: ScanDryRunJob(
                id=uuid.uuid4(),
                project_id=project.id,
                data_source_id=source.id,
                base_query="SELECT * FROM events",
                status="pending",
                **({} if setting is None else {"auto_create_events": setting}),
            )
            for setting in (None, False)
        }
        session.add_all(jobs.values())
        session.commit()

        assert dry_run_tasks._dry_run_config(session, jobs[None]).auto_create_events is True
        assert dry_run_tasks._dry_run_config(session, jobs[False]).auto_create_events is False


async def test_the_dry_run_api_stores_the_drafts_setting(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dry_run_tasks.dry_run_scan_config_async, "delay", lambda job_id: None)
    await client.post("/api/v1/projects", json={"name": "Draft", "slug": "draft-api"})
    source = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Draft warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
        },
    )
    assert source.status_code == 201, source.text

    resp = await client.post(
        "/api/v1/projects/draft-api/scans/dry-run",
        json={
            "data_source_id": source.json()["id"],
            "base_query": "SELECT * FROM events",
            "event_type_column": "event_name",
            "auto_create_events": False,
        },
    )

    assert resp.status_code == 202, resp.text
    async with TestSessionLocal() as session:
        job = await session.get(ScanDryRunJob, uuid.UUID(resp.json()["id"]))
        assert job is not None
        assert job.auto_create_events is False
