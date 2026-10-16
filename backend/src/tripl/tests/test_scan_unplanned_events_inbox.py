"""Unplanned events: a scan adds them to the catalog, or leaves them to the inbox.

``ScanConfig.auto_create_events`` decides what a run does with an identity no
catalog event answers to. On (the default, and the behaviour before the setting
existed) the identity becomes an ``in_review`` event. Off, the catalog is left
alone and the metrics collection files the identity in the reconciliation
inbox as a shadow candidate. These tests pin both answers and every place the
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
from sqlalchemy import create_engine, select
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
from tripl.models.shadow_event_candidate import SHADOW_STATUS_NEW, ShadowEventCandidate
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
