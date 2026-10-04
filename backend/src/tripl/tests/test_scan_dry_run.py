"""The scan dry-run: "what would this scan create?", answered honestly.

quick-start.md promises the preview "shows exactly which events, fields, and
values tripl would create from the real data". These tests pin the two halves of
keeping that promise: the answer comes out of the real planner, and it never
claims to be more complete than the sample it looked at.
"""

import ast
import pathlib
import uuid
from datetime import datetime

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.models import Base, DataSource, Project, ScanConfig
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.scan_dry_run_job import ScanDryRunJob
from tripl.schemas.scan_config import ScanDryRunResponse
from tripl.worker import celery_app as celery_app_module
from tripl.worker import db as worker_db
from tripl.worker.tasks import scan_dry_run as dry_run_tasks

DRY_RUN_TASK_NAME = "tripl.worker.tasks.scan.dry_run_scan_config_async"


def user_visible_422(resp: Response) -> str:
    """The part of a 422 body a person is actually shown.

    ``formatValidationDetail`` in ``frontend/src/api/client.ts`` builds the
    message from each error's ``loc`` (minus the ``body``/``query`` prefix) and
    its ``msg``; nothing else in the pydantic error dict reaches a screen. In
    particular FastAPI's default handler echoes the whole submitted body back in
    each error's ``input``, so asserting a name is absent from ``resp.text``
    proves only that the *test's own request* omitted that key — not that the
    error stopped naming it. Assert against this string instead.
    """
    parts = []
    for item in resp.json()["detail"]:
        path = ".".join(str(seg) for seg in item["loc"] if seg not in ("body", "query"))
        parts.append(f"{path}: {item['msg']}" if path else item["msg"])
    return "; ".join(parts)


@pytest.fixture
async def project(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "Dry Run", "slug": "dry-run", "description": ""},
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture
async def data_source(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Dry CH",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "test_db",
        },
    )
    assert resp.status_code == 201
    return resp.json()


class _FakeAdapter:
    """Two string columns and whatever breakdown rows the test hands it.

    Row layout is ``BaseAdapter.get_full_breakdown``'s: regular values, then the
    JSON path arrays, then the kept JSON values, then ``_cnt`` last.
    """

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return [
            ColumnInfo(name="screen", type_name="String"),
            ColumnInfo(name="action", type_name="String"),
        ]

    def get_full_breakdown(
        self,
        base_query: str,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None = None,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        limit: int = 50000,
    ) -> tuple[list[str], list[str], list[str], list[tuple[object, ...]]]:
        return (["screen", "action"], [], [], self._rows[:limit])

    def close(self) -> None:
        return None


def _seed(
    session_factory,
    *,
    event_name_format: str | None = None,
    sample_row_limit: int = 5000,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A project with one event type, two fields, and a saved scan config."""
    project_id = uuid.uuid4()
    data_source_id = uuid.uuid4()
    event_type_id = uuid.uuid4()
    scan_config_id = uuid.uuid4()
    job_id = uuid.uuid4()

    with session_factory() as session:
        session.add_all(
            [
                Project(id=project_id, name="P", slug="p", description=""),
                DataSource(
                    id=data_source_id,
                    name="DS",
                    db_type="clickhouse",
                    host="localhost",
                    port=8123,
                    database_name="default",
                    username="default",
                    password_encrypted="",
                ),
                EventType(
                    id=event_type_id,
                    project_id=project_id,
                    name="pv",
                    display_name="Page View",
                    description="",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                FieldDefinition(
                    id=uuid.uuid4(),
                    event_type_id=event_type_id,
                    name=name,
                    display_name=name,
                    field_type="string",
                    order=order,
                )
                for order, name in enumerate(("screen", "action"))
            ]
        )
        session.add(
            ScanConfig(
                id=scan_config_id,
                project_id=project_id,
                data_source_id=data_source_id,
                event_type_id=event_type_id,
                name="dry",
                base_query="SELECT * FROM events",
                event_name_format=event_name_format,
                cardinality_threshold=100,
            )
        )
        session.add(
            ScanDryRunJob(
                id=job_id,
                project_id=project_id,
                scan_config_id=scan_config_id,
                sample_row_limit=sample_row_limit,
                status="pending",
            )
        )
        session.commit()
    return job_id, project_id


def _seed_grouped(session_factory) -> tuple[uuid.UUID, uuid.UUID]:
    """A grouped (``event_type_column``) scan whose two groups collide on one name.

    ``event_name_format='{screen}'`` names every row after its screen, and both
    ``click`` and ``view`` see ``screen='home'`` — so a real run writes TWO
    Events, one under each event type. ``home`` already exists under ``click``
    only, which is what makes the per-event-type status labelling observable.
    """
    project_id = uuid.uuid4()
    data_source_id = uuid.uuid4()
    click_id = uuid.uuid4()
    view_id = uuid.uuid4()
    scan_config_id = uuid.uuid4()
    job_id = uuid.uuid4()

    with session_factory() as session:
        session.add_all(
            [
                Project(id=project_id, name="G", slug="g", description=""),
                DataSource(
                    id=data_source_id,
                    name="DS",
                    db_type="clickhouse",
                    host="localhost",
                    port=8123,
                    database_name="default",
                    username="default",
                    password_encrypted="",
                ),
                EventType(
                    id=click_id,
                    project_id=project_id,
                    name="click",
                    display_name="Click",
                    description="",
                ),
                EventType(
                    id=view_id,
                    project_id=project_id,
                    name="view",
                    display_name="View",
                    description="",
                ),
            ]
        )
        session.flush()
        session.add(
            Event(
                id=uuid.uuid4(),
                project_id=project_id,
                event_type_id=click_id,
                name="home",
                source_name="home",
                description="",
                order=0,
                status="in_review",
            )
        )
        session.add(
            ScanConfig(
                id=scan_config_id,
                project_id=project_id,
                data_source_id=data_source_id,
                name="grouped",
                base_query="SELECT * FROM events",
                event_type_column="action",
                event_name_format="{screen}",
                cardinality_threshold=100,
            )
        )
        session.add(
            ScanDryRunJob(
                id=job_id,
                project_id=project_id,
                scan_config_id=scan_config_id,
                sample_row_limit=5000,
                status="pending",
            )
        )
        session.commit()
    return job_id, project_id


def _run(monkeypatch, session_factory, adapter: _FakeAdapter, job_id: uuid.UUID) -> dict:
    monkeypatch.setitem(
        dry_run_tasks.dry_run_scan_config_async.run.__globals__,
        "_get_sync_session",
        session_factory,
    )
    monkeypatch.setitem(
        dry_run_tasks.dry_run_scan_config_async.run.__globals__,
        "_build_adapter",
        lambda ds: adapter,
    )
    return dry_run_tasks.dry_run_scan_config_async.run(str(job_id))


class TestDryRunWorker:
    def test_names_the_events_in_cnt_descending_order_and_writes_nothing(
        self, tmp_path, monkeypatch
    ) -> None:
        """The whole feature in one test.

        The names are the ones ``generate_events`` would produce — they come out
        of the same planner — and the order is the adapter's own ``_cnt``
        ordering, which is what makes "the most common combinations" a true
        description. Nothing is written: routing the dry-run through
        ``generate_events`` instead of ``plan_events`` makes Event rows appear
        and the count assertion names them.
        """
        engine = create_engine(f"sqlite:///{tmp_path / 'dry.db'}")
        try:
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine, expire_on_commit=False)
            job_id, project_id = _seed(factory)

            adapter = _FakeAdapter(
                [
                    ("/home", "click", 50),
                    ("/about", "view", 30),
                    ("/contact", "click", 5),
                ]
            )
            result = _run(monkeypatch, factory, adapter, job_id)

            assert [event["name"] for event in result["events"]] == [
                "screen=/home | action=click",
                "screen=/about | action=view",
                "screen=/contact | action=click",
            ]
            assert [event["approx_row_count"] for event in result["events"]] == [50, 30, 5]
            assert all(event["status"] == "new" for event in result["events"])
            assert result["sampled_rows"] == 85
            assert result["breakdown_combinations"] == 3
            assert result["sample_is_complete"] is True
            assert result["events_truncated"] is False
            assert result["errors"] == []
            # An exact count needs a complete sample AND no lookback window.
            assert all(event["count_confidence"] == "exact" for event in result["events"])

            with factory() as session:
                written = (
                    session.execute(select(Event).where(Event.project_id == project_id))
                    .scalars()
                    .all()
                )
                assert written == [], f"dry run persisted {len(written)} events"
                job = session.get(ScanDryRunJob, job_id)
                assert job.status == "completed"
                assert job.error_message is None
                assert job.result_summary["events"][0]["name"] == "screen=/home | action=click"

            # The stored payload must satisfy the response model the poll route
            # serializes through, or GET /dry-run-jobs/{id} answers 500 for a job
            # that completed fine. Nothing else exercises that pairing: the API
            # tests can only reach a pending job.
            validated = ScanDryRunResponse.model_validate(result)
            assert validated.events[0].name == "screen=/home | action=click"
            assert validated.events[0].status == "new"
            assert validated.sample_is_complete is True
        finally:
            engine.dispose()

    def test_an_unknown_name_format_key_completes_the_job_and_names_the_key(
        self, tmp_path, monkeypatch
    ) -> None:
        """Caught before the scan exists rather than after 200 runs.

        A format referencing a column the rows cannot supply kills every
        production run of that config. The dry-run must REPORT it, not raise:
        failing the job would hide the one string that says which key is wrong.
        """
        engine = create_engine(f"sqlite:///{tmp_path / 'dry_fmt.db'}")
        try:
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine, expire_on_commit=False)
            job_id, _project_id = _seed(factory, event_name_format="{checkout_step}")

            adapter = _FakeAdapter([("/home", "click", 7)])
            result = _run(monkeypatch, factory, adapter, job_id)

            assert result["errors"], "an unknown name-format key must be reported"
            assert "checkout_step" in result["errors"][0]
            assert result["events"] == []

            with factory() as session:
                job = session.get(ScanDryRunJob, job_id)
                assert job.status == "completed"
                assert "checkout_step" in job.result_summary["errors"][0]
        finally:
            engine.dispose()

    def test_dry_run_does_not_promise_an_unnamed_event(self, tmp_path, monkeypatch) -> None:
        """Preview/run parity for the rows the run now refuses to name.

        Both halves read the same planner, which is the whole
        reason ``event_plan`` exists — a preview that still listed a nameless
        event would be promising a row the run no longer writes. The skip is a
        warning rather than a silence because the operator's next question is
        which rows, and the answer is the name format or the base query.
        """
        engine = create_engine(f"sqlite:///{tmp_path / 'dry_unnamed.db'}")
        try:
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine, expire_on_commit=False)
            job_id, _project_id = _seed(factory, event_name_format="{action}")

            # The production shape: one row whose naming column is NULL, beside a
            # row that names an event perfectly well.
            adapter = _FakeAdapter([("/home", None, 9), ("/about", "click", 5)])
            result = _run(monkeypatch, factory, adapter, job_id)

            assert [event["name"] for event in result["events"]] == ["click"]
            assert "Skipped 1 row whose derived event name was empty" in result["warnings"]
            assert result["errors"] == []
        finally:
            engine.dispose()

    def test_a_grouped_dry_run_reports_one_total_for_the_rows_it_skipped(
        self, tmp_path, monkeypatch
    ) -> None:
        """One count, not one line per event type.

        A grouped scan plans once per group value, each plan counts its own
        unnamed rows, and every plan appended its own sentence — so a config
        dropping one row inside each of two groups told the operator "Skipped 1
        row" twice. Two identical sentences do not read as two rows; they read
        as one problem printed twice, or as two separate one-row problems.
        Neither is what happened.

        Uses the GROUPED seed deliberately: ``_seed`` names an explicit event
        type, which is the single-target path where per-plan and total are the
        same number and this assertion would hold either way.
        """
        engine = create_engine(f"sqlite:///{tmp_path / 'dry_unnamed_grouped.db'}")
        try:
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine, expire_on_commit=False)
            job_id, _project_id = _seed_grouped(factory)

            # ``event_name_format='{screen}'``: the NULL-screen row inside each of
            # the two groups is a row a run would refuse to name.
            adapter = _FakeAdapter(
                [
                    ("home", "click", 50),
                    (None, "click", 3),
                    ("home", "view", 30),
                    (None, "view", 2),
                ]
            )
            result = _run(monkeypatch, factory, adapter, job_id)

            unnamed = [w for w in result["warnings"] if "derived event name was empty" in w]
            assert unnamed == ["Skipped 2 rows whose derived event name was empty"], (
                "the dry run reports the total, once, not one line per event type"
            )
            # The two named rows still become events, one per event type.
            assert [(e["event_type"], e["source_name"]) for e in result["events"]] == [
                ("click", "home"),
                ("view", "home"),
            ]
            assert result["errors"] == []
        finally:
            engine.dispose()

    def test_an_incomplete_sample_is_reported_as_incomplete(self, tmp_path, monkeypatch) -> None:
        """ "At least N" or nothing.

        When the breakdown hit its row cap, more distinct events exist than this
        pass looked at. Reporting ``sample_is_complete: true`` here would let the
        panel print a flat "Would create N events" that is simply false.
        """
        engine = create_engine(f"sqlite:///{tmp_path / 'dry_trunc.db'}")
        try:
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine, expire_on_commit=False)
            job_id, _project_id = _seed(factory, sample_row_limit=100)

            adapter = _FakeAdapter([(f"/p/{i}", "view", 200 - i) for i in range(101)])
            result = _run(monkeypatch, factory, adapter, job_id)

            assert result["sample_is_complete"] is False
            assert result["events_truncated"] is True
            assert result["events"], "a truncated sample still names what it saw"
            assert all(event["count_confidence"] == "sampled" for event in result["events"])
        finally:
            engine.dispose()

    def test_an_empty_window_claims_no_column_is_unmapped(self, tmp_path, monkeypatch) -> None:
        """Knowing nothing is not the same as knowing every column is unmapped.

        With no rows the grouped analysis returns no group values, so there are
        no targets, the per-target loop never runs and ``known_field_names``
        stays empty. Deriving ``unmapped_columns`` from that emptiness reported
        EVERY column as one a run would skip — the opposite of the truth, since
        a run over a non-empty window creates fields for exactly those columns.

        It also mis-routed the panel: ``fields == [] and unmapped_columns != []``
        is how it detects the explicit-event-type path, so a zero-row auto-detect
        answer rendered a sentence about "the fields this event type already
        declares" on a path that has no event type.

        Uses the GROUPED seed deliberately. ``_seed`` names an explicit event
        type whose two declared fields are the same two columns the fake adapter
        returns, so ``known_field_names`` covers everything and this assertion
        would hold with or without the fix — a test that proves nothing. The
        grouped config derives its targets from the rows, so zero rows is the
        only way to reach the empty-targets branch at all.
        """
        engine = create_engine(f"sqlite:///{tmp_path / 'dry_empty.db'}")
        try:
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine, expire_on_commit=False)
            job_id, _project_id = _seed_grouped(factory)

            result = _run(monkeypatch, factory, _FakeAdapter([]), job_id)

            assert result["sampled_rows"] == 0
            assert result["events"] == []
            assert result["fields"] == []
            assert result["unmapped_columns"] == [], (
                "a dry run that read no rows analysed no columns, so it cannot "
                "report any column as unmapped"
            )
        finally:
            engine.dispose()

    def test_one_name_under_two_event_types_is_two_events_not_one(
        self, tmp_path, monkeypatch
    ) -> None:
        """An event is ``(event type, name)`` — the same key a real run writes on.

        ``generate_events`` runs once per event type and dedups inside a set
        scoped to that one ``event_type_id``, so ``home`` under ``click`` and
        ``home`` under ``view`` are two Events. Accumulating the dry run on the
        bare name merged them: the panel promised one event for work that
        creates two, and unioning the two event types' existing identities
        labelled the genuinely new one "already in your plan".
        """
        engine = create_engine(f"sqlite:///{tmp_path / 'dry_grouped.db'}")
        try:
            Base.metadata.create_all(engine)
            factory = sessionmaker(engine, expire_on_commit=False)
            job_id, project_id = _seed_grouped(factory)

            adapter = _FakeAdapter([("home", "click", 50), ("home", "view", 30)])
            result = _run(monkeypatch, factory, adapter, job_id)

            assert [(e["event_type"], e["source_name"]) for e in result["events"]] == [
                ("click", "home"),
                ("view", "home"),
            ], "one name under two event types must stay two events"
            assert [e["approx_row_count"] for e in result["events"]] == [50, 30]
            # ``home`` exists under ``click`` only. Unioning the two event types'
            # identities is what used to hide the new one.
            assert [e["status"] for e in result["events"]] == ["existing", "new"]

            with factory() as session:
                written = (
                    session.execute(
                        select(Event).where(Event.project_id == project_id, Event.description != "")
                    )
                    .scalars()
                    .all()
                )
                assert written == [], f"dry run persisted {len(written)} events"

            validated = ScanDryRunResponse.model_validate(result)
            assert [event.event_type for event in validated.events] == ["click", "view"]
        finally:
            engine.dispose()


class TestDryRunAPI:
    async def test_dispatches_a_worker_job_and_polls(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        dispatched: list[str] = []
        monkeypatch.setattr(
            dry_run_tasks.dry_run_scan_config_async,
            "delay",
            lambda job_id: dispatched.append(job_id),
        )

        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/dry-run",
            json={
                "data_source_id": data_source["id"],
                "base_query": "SELECT * FROM events",
                "event_type_column": "event_name",
            },
        )

        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "pending"
        assert body["result_summary"] is None
        assert dispatched == [body["id"]]

        poll = await client.get(
            f"/api/v1/projects/{project['slug']}/scans/dry-run-jobs/{body['id']}"
        )
        assert poll.status_code == 200
        assert poll.json()["status"] == "pending"

    async def test_requires_a_config_or_a_draft(self, client: AsyncClient, project: dict) -> None:
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/dry-run",
            json={"base_query": "SELECT * FROM events"},
        )
        assert resp.status_code == 422

    async def test_a_draft_must_say_how_its_events_are_named(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The defect this guard closes: the scan form fired a dry-run on every
        new scan's first "Load preview" with neither field set, the job was
        dispatched, and ``_dry_run_targets`` aborted on its own precondition —
        which reached the user as ``Scan failed: Either event_type_id or
        event_type_column must be specified``. Unanswerable is a 422 before any
        warehouse query, not a failed job."""
        monkeypatch.setattr(dry_run_tasks.dry_run_scan_config_async, "delay", lambda job_id: None)

        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/dry-run",
            json={
                "data_source_id": data_source["id"],
                "base_query": "SELECT * FROM events",
            },
        )
        assert resp.status_code == 422, resp.text
        # The 422 is read by a person, so the message names the CONTROLS, never
        # the columns behind them. Naming `event_type_id` here would reintroduce,
        # one layer down, exactly the string this guard was added to stop
        # reaching a user.
        shown = user_visible_422(resp)
        assert "Event type column" in shown, shown
        assert "event_type_id" not in shown, shown

        # The body shape the product actually posts. `toDryRunRequest`
        # (frontend/src/pages/settings/scans/useScanForm.ts) emits every key
        # unconditionally, so an unanswered form sends `event_type_id: null`
        # rather than omitting it — and any client generated from the OpenAPI
        # schema does the same. The request above omits the key, which is why the
        # earlier `"event_type_id" not in resp.text` form of this assertion held
        # for reasons that had nothing to do with the message.
        as_the_form_sends_it = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/dry-run",
            json={
                "data_source_id": data_source["id"],
                "base_query": "SELECT * FROM events",
                "event_type_id": None,
                "event_type_column": None,
            },
        )
        assert as_the_form_sends_it.status_code == 422, as_the_form_sends_it.text
        shown = user_visible_422(as_the_form_sends_it)
        assert "Event type column" in shown, shown
        assert "event_type_id" not in shown, shown

        with_column = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/dry-run",
            json={
                "data_source_id": data_source["id"],
                "base_query": "SELECT * FROM events",
                "event_type_column": "event_name",
            },
        )
        assert with_column.status_code == 202, with_column.text

    async def test_base_query_must_be_a_read_only_select(
        self, client: AsyncClient, project: dict, data_source: dict
    ) -> None:
        """The dry-run executes free-text SQL, so it carries the same gate as create."""
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/scans/dry-run",
            json={
                "data_source_id": data_source["id"],
                "base_query": "SELECT 1; DROP TABLE events",
            },
        )
        assert resp.status_code == 422

    async def test_job_not_found(self, client: AsyncClient, project: dict) -> None:
        resp = await client.get(
            f"/api/v1/projects/{project['slug']}/scans/dry-run-jobs/"
            "00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 404


class TestDryRunTaskWiring:
    """What the module split could have broken silently.

    None of it shows up as a failing assertion elsewhere: a renamed task still
    runs when you call ``.run()`` directly, an unregistered task still imports
    fine, and a function-local ``_build_adapter`` still returns a real adapter.
    Every one of them surfaces first in production, so each gets an explicit pin.
    """

    def test_the_task_name_still_says_tasks_scan_after_the_module_moved(self) -> None:
        """The broker routes on this string, so it is a wire identifier, not a
        module path. Renaming it to match ``tasks.scan_dry_run`` would leave every
        dry-run job already queued by the running deployment unroutable."""
        assert dry_run_tasks.dry_run_scan_config_async.name == DRY_RUN_TASK_NAME

    def test_the_celery_app_imports_this_module_so_the_worker_registers_the_task(self) -> None:
        """Read at the SOURCE level on purpose.

        ``celery_app.tasks`` cannot answer this question, and a test that asks it
        passes whether or not the registration line exists — importing this test
        module already imports ``scan_dry_run``, and the decorator registers the
        task as a side effect. The worker process imports only
        ``tripl.worker.celery_app``, so that file's import list IS the mechanism,
        and it is invisible to every runtime assertion the suite can make. Drop
        the line and the API dispatches dry-run messages a worker answers
        ``NotRegistered`` to.
        """
        module_source = pathlib.Path(celery_app_module.__file__ or "").read_text()
        imported = {
            alias.name
            for node in ast.parse(module_source).body
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "tripl.worker.tasks.scan_dry_run" in imported, sorted(
            name for name in imported if name.startswith("tripl.worker.tasks")
        )

    def test_the_db_helpers_are_module_level_in_the_module_that_owns_the_task(self) -> None:
        """``_get_sync_session``/``_build_adapter`` are patched through
        ``run.__globals__`` (see ``_run`` above). Deferring either to a
        function-local import would leave the patch pointing at nothing, and the
        whole worker half of this file would quietly open real connections
        instead of the sqlite factory and the fake adapter it was handed."""
        task_globals = dry_run_tasks.dry_run_scan_config_async.run.__globals__
        absent = [
            name for name in ("_get_sync_session", "_build_adapter") if name not in task_globals
        ]
        assert not absent, (
            f"{absent} left the module namespace the task closes over — "
            "monkeypatching run.__globals__ now silently no-ops and the worker "
            "half of this file opens real connections"
        )
        assert task_globals["_get_sync_session"] is worker_db._get_sync_session
        assert task_globals["_build_adapter"] is worker_db._build_adapter
