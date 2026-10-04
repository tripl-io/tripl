"""Scan jobs: blast radius, Cancel, grouped manual runs, snapshots, anchoring.

Five defects on the scan-job seam — what a run is allowed to touch, and what
"now" means to the passes that run after it:

* ``apply_event_groups`` derived its event-type list from a
  branch-blind ``SELECT DISTINCT Event.event_type_id``, so on a project with
  open working branches it folded and DELETED every branch's deep-copied
  events, while only main was reindexed afterwards.
* neither ``run_scan`` nor ``apply_event_groups`` ever read
  ``ScanJob.status`` back, so a job the user stopped was flipped to ``running``,
  rewrote the catalog and was committed ``completed`` while still carrying
  "Cancelled by user"; and neither recorded ``celery_task_id``, which left
  ``cancel_scan_job``'s revoke branch unreachable for both.
* a manual grouped run only LOOKED UP its event types by name
  and skipped the group when one was missing, while the dry run promised the
  type "would be added" and the scheduled catalog sync actually created it. Its
  review follow-up is here too: once a raw warehouse value is CREATED from, it
  has to obey the catalog's own name rule (``event_types.name`` is
  ``String(100)``, and a NULL group cell arrives as ``""``), and the dry run
  needs the matching filter so the preview refuses what the run refuses.
* ``_load_latest_generation_snapshot`` took the newest completed
  ScanJob with any summary, but only ``run_scan`` writes ``generation_snapshot``,
  so from the first collection tick after a scan every replay silently fell
  through to the heuristic rebuild.
* ``_recalculate_release_regressions`` anchored on the caller's
  collection window, so a replay of a past window replaced the current release's
  verdict with the release that was newest back then.

Sync sqlite fixtures mirror ``test_metrics_tasks.py`` and the worker-task tests
in ``test_scans.py``: a file-backed engine plus ``monkeypatch.setitem`` on the
task's ``__globals__``, which is how a Celery task body is driven here.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.cardinality import BreakdownAnalysis, CardinalityResult
from tripl.core.analyzers.event_generator import GenerationResult
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.models.project import Project
from tripl.models.release_regression import ReleaseComparability, ReleaseRegression
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.models.variable import Variable
from tripl.services import scan_service
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.worker.tasks import scan as scan_tasks
from tripl.worker.tasks._errors import ScanError, user_facing_error
from tripl.worker.tasks.metrics.generation import (
    _load_latest_generation_snapshot,
    _merge_replay_variable_samples,
)
from tripl.worker.tasks.metrics.regression import _recalculate_release_regressions
from tripl.worker.tasks.scan_dry_run import _MAX_REFUSAL_ERRORS, build_dry_run_payload
from tripl.worker.utils.event_types import (
    EVENT_TYPE_NAME_MAX_LEN,
    ensure_event_type_with_fields,
    event_type_name_rejection,
)

CLICK_RULE = [
    {
        "name": "click events",
        "condition_logic": "all",
        "conditions": [{"field": "__event_name", "pattern": "^click:"}],
    }
]


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch3_a4.db'}")
    # Before ``create_all``, as the helper's docstring requires. This module
    # tests a replay path whose whole failure mode IS a foreign key — a snapshot
    # naming an event that no longer exists — so a fixture that leaves SQLite's
    # enforcement off (the default) is a fixture in which that class of bug
    # cannot be written down. It still cannot fully stand in for Postgres, but
    # it is the difference between an assertion that could fail and one that
    # could not.
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _seed_project(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    project_id = uuid.uuid4()
    data_source_id = uuid.uuid4()
    session.add_all(
        [
            Project(
                id=project_id,
                name="Batch3 A4",
                slug=f"batch3-a4-{uuid.uuid4().hex[:8]}",
                description="",
            ),
            DataSource(
                id=data_source_id,
                name=f"DS {uuid.uuid4().hex[:8]}",
                db_type="clickhouse",
                host="localhost",
                port=8123,
                database_name="default",
                username="default",
                password_encrypted="",
            ),
        ]
    )
    session.flush()
    return project_id, data_source_id


def _seed_branches(session: Session, project_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Main plus one open working branch, both with explicit ids.

    Seeding main explicitly matters: ``main_branch_id`` is the value the fixed
    query filters on, and letting ``default_branch_id`` mint it implicitly would
    leave the test unable to distinguish the two branches.
    """
    main_id = uuid.uuid4()
    working_id = uuid.uuid4()
    session.add_all(
        [
            PlanBranch(
                id=main_id,
                project_id=project_id,
                name="main",
                kind=BranchKind.main.value,
                status=BranchStatus.merged.value,
                description="",
            ),
            PlanBranch(
                id=working_id,
                project_id=project_id,
                name="acme",
                kind=BranchKind.working.value,
                status=BranchStatus.draft.value,
                description="",
            ),
        ]
    )
    session.flush()
    return main_id, working_id


def _seed_event_type_with_clicks(
    session: Session,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    description: str = "",
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """One event type carrying two ``click:*`` events, on the given branch.

    Field definitions are load-bearing rather than decoration: the merge skips an
    event type that declares none, so a branch copy without them could not leak
    even on the unfixed code.
    """
    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project_id,
        branch_id=branch_id,
        name="pv",
        display_name="Page View",
        description="",
    )
    session.add(event_type)
    session.flush()
    session.add(
        FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=event_type.id,
            name="action",
            display_name="Action",
            field_type="string",
            order=0,
        )
    )
    session.flush()
    event_ids: list[uuid.UUID] = []
    for index, source_name in enumerate(("click:one", "click:two")):
        event = Event(
            id=uuid.uuid4(),
            project_id=project_id,
            branch_id=branch_id,
            event_type_id=event_type.id,
            name=source_name,
            source_name=source_name,
            description=description,
            order=index,
            status="implemented",
        )
        session.add(event)
        event_ids.append(event.id)
    session.flush()
    return event_type.id, event_ids


# ── apply-groups stays on the main plan ───────────────────────


def test_apply_event_groups_leaves_working_branch_events_untouched(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """A mutating, DELETING pass must not reach into open working branches.

    A branch deep-copies every EventType, FieldDefinition and Event under fresh
    ids, so the old branch-blind ``SELECT DISTINCT Event.event_type_id`` returned
    the branch's private copies too. The merge then rewrote and DELETED the
    branch author's events — analyst edits included — and minted a group event
    with ``branch_id`` copied from its source, which only main's reindex would
    ever have indexed.

    DISABLE-THE-FIX: drop the two ``branch_id`` predicates from
    ``apply_event_groups`` and all three assertions below go red.
    """
    config_id = uuid.uuid4()
    job_id = uuid.uuid4()
    with sync_session_factory() as session:
        project_id, data_source_id = _seed_project(session)
        main_id, working_id = _seed_branches(session, project_id)
        session.add_all(
            [
                ScanConfig(
                    id=config_id,
                    project_id=project_id,
                    data_source_id=data_source_id,
                    name="Daily",
                    base_query="SELECT * FROM events",
                    event_group_rules=CLICK_RULE,
                ),
                ScanJob(id=job_id, scan_config_id=config_id, status="pending"),
            ]
        )
        session.flush()
        _seed_event_type_with_clicks(session, project_id=project_id, branch_id=main_id)
        _, branch_event_ids = _seed_event_type_with_clicks(
            session,
            project_id=project_id,
            branch_id=working_id,
            description="analyst note",
        )
        session.commit()

    monkeypatch.setitem(
        scan_tasks.apply_event_groups.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        scan_tasks.apply_event_groups.run.__globals__,
        "reindex_main_branch_from_worker",
        lambda session, project_id: None,
    )

    summary = scan_tasks.apply_event_groups.run(str(config_id), str(job_id))

    # The cheapest direct statement of the defect: two event types were folded.
    assert summary["event_types_processed"] == 1

    with sync_session_factory() as session:
        branch_events = (
            session.execute(select(Event).where(Event.branch_id == working_id)).scalars().all()
        )
        assert {event.id for event in branch_events} == set(branch_event_ids)
        assert {event.description for event in branch_events} == {"analyst note"}
        assert not [event for event in branch_events if event.source_name == "click events"]

        # Scoped, not disabled: main still got its fold.
        main_group = (
            session.execute(
                select(Event).where(
                    Event.branch_id == main_id,
                    Event.source_name == "click events",
                )
            )
            .scalars()
            .all()
        )
        assert len(main_group) == 1


# ── Stop run is honoured by every scan task ───────────────────

# Far enough from ``datetime.now`` that a close-out stamping its own
# ``completed_at`` over the closer's is unmistakable.
CANCELLED_AT = datetime(2026, 9, 12, tzinfo=UTC)


def _seed_single_type_scan(
    session: Session,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A config on the single-event-type path, so ``run_scan``'s flow is linear."""
    project_id, data_source_id = _seed_project(session)
    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project_id,
        name="pv",
        display_name="Page View",
        description="",
    )
    session.add(event_type)
    session.flush()
    config_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session.add_all(
        [
            ScanConfig(
                id=config_id,
                project_id=project_id,
                data_source_id=data_source_id,
                event_type_id=event_type.id,
                name="Daily",
                base_query="SELECT * FROM events",
            ),
            ScanJob(id=job_id, scan_config_id=config_id, status="pending"),
        ]
    )
    session.commit()
    return project_id, config_id, job_id


class _EmptyAnalysis:
    row_limit_reached = False
    rows: list[object] = []


class _CatalogAdapter:
    """The warehouse as ``run_scan`` uses it once cardinality is stubbed out."""

    def __init__(self, on_get_columns: Any = None) -> None:
        self._on_get_columns = on_get_columns

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        if self._on_get_columns is not None:
            self._on_get_columns()
        return [ColumnInfo(name="event_name", type_name="String")]

    def close(self) -> None:
        return None


def _stub_run_scan_pipeline(
    monkeypatch: MonkeyPatch,
    sync_session_factory: sessionmaker[Session],
    *,
    adapter: _CatalogAdapter,
    swept: list[object],
    reindexed: list[object],
    on_reindex: Any = None,
) -> None:
    def _sweep(session: Session, **kwargs: object) -> int:
        swept.append(kwargs)
        return 0

    def _reindex(session: Session, project_id: uuid.UUID) -> None:
        reindexed.append(project_id)
        if on_reindex is not None:
            on_reindex()

    for name, value in (
        ("_get_sync_session", sync_session_factory),
        ("_build_adapter", lambda ds: adapter),
        ("analyze_cardinality", lambda *a, **k: _EmptyAnalysis()),
        ("generate_events", lambda *a, **k: GenerationResult(columns_analyzed=1)),
        ("retire_unused_variables", _sweep),
        ("reindex_main_branch_from_worker", _reindex),
    ):
        monkeypatch.setitem(scan_tasks.run_scan.run.__globals__, name, value)


def test_run_scan_skips_a_job_cancelled_before_start(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """A stopped job must not be resurrected by an ``acks_late`` redelivery.

    ``task_acks_late`` plus ``task_reject_on_worker_lost`` make redelivery of a
    job somebody has already closed a routine event rather than a race, and
    ``_reject_if_already_running`` stops guarding the config the moment a job
    leaves ``pending``/``running`` — so a second Run is accepted while this
    message is still in flight.
    """
    with sync_session_factory() as session:
        _, config_id, job_id = _seed_single_type_scan(session)
        job = session.get(ScanJob, job_id)
        assert job is not None
        job.status = ScanJobStatus.cancelled.value
        job.error_message = "Cancelled by user"
        job.completed_at = datetime(2026, 9, 12, tzinfo=UTC)
        session.commit()

    def _explode(ds: object) -> None:
        raise AssertionError("adapter must not be built for a cancelled job")

    monkeypatch.setitem(
        scan_tasks.run_scan.run.__globals__, "_get_sync_session", sync_session_factory
    )
    monkeypatch.setitem(scan_tasks.run_scan.run.__globals__, "_build_adapter", _explode)

    result = scan_tasks.run_scan.run(str(config_id), str(job_id))

    assert result["cancelled"] is True
    assert result["job_status"] == ScanJobStatus.cancelled.value

    with sync_session_factory() as session:
        job = session.get(ScanJob, job_id)
        assert job is not None
        assert job.status == ScanJobStatus.cancelled.value
        assert job.started_at is None
        assert job.result_summary is None
        assert job.error_message == "Cancelled by user"
        assert session.execute(select(Event)).scalars().all() == []


def test_run_scan_cancelled_mid_run_writes_nothing_and_stays_cancelled(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """Stop pressed while the warehouse query runs: the generation is discarded.

    The guard has to sit BEFORE the generation commit. Checking "before the final
    commit" instead would leave the catalog rewritten, the variables swept and
    the branch reindexed, and would still flip the row from ``cancelled`` to
    ``completed`` while it carried "Cancelled by user".

    The cancel is issued from a SECOND connection while the task's session holds
    no write lock, which is both the realistic moment and the only one pysqlite
    will let a test reproduce.

    DISABLE-THE-FIX: remove the ``job_is_cancelled`` block from ``run_scan`` and
    the job comes back ``completed`` with both recorders fired.
    """
    with sync_session_factory() as session:
        _, config_id, job_id = _seed_single_type_scan(session)

    def _cancel_from_elsewhere() -> None:
        with sync_session_factory() as other:
            job = other.get(ScanJob, job_id)
            assert job is not None
            job.status = ScanJobStatus.cancelled.value
            job.completed_at = datetime(2026, 9, 12, tzinfo=UTC)
            job.error_message = "Cancelled by user"
            other.commit()

    swept: list[object] = []
    reindexed: list[object] = []
    _stub_run_scan_pipeline(
        monkeypatch,
        sync_session_factory,
        adapter=_CatalogAdapter(on_get_columns=_cancel_from_elsewhere),
        swept=swept,
        reindexed=reindexed,
    )

    result = scan_tasks.run_scan.run(str(config_id), str(job_id))

    assert result == {"cancelled": True, "scan_config_id": str(config_id)}
    assert swept == []
    assert reindexed == []

    with sync_session_factory() as session:
        job = session.get(ScanJob, job_id)
        assert job is not None
        assert job.status == ScanJobStatus.cancelled.value
        assert job.result_summary is None
        assert job.error_message == "Cancelled by user"


def test_run_scan_skips_a_job_whose_completed_ack_was_lost(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """``completed`` is terminal here too, exactly as it is for ``collect_metrics``.

    ``task_acks_late`` plus ``task_reject_on_worker_lost`` redeliver a message
    whose ack never landed, so a job that already finished can be handed back to
    the worker. Re-running it re-queries the warehouse and rewrites the plan, but
    the user-visible half is worse: the row goes back to ``running`` while still
    carrying the first run's ``completed_at``, which is precisely what
    ``_reject_if_already_running`` selects on, so every Run the user presses on
    this config 409s for as long as the duplicate lasts.

    DISABLE-THE-FIX: drop ``completed`` from ``TERMINAL_SCAN_JOB_STATUSES`` and
    the adapter is built, the row is re-opened and this goes red three times.
    """
    finished_at = datetime(2026, 9, 12, tzinfo=UTC)
    with sync_session_factory() as session:
        _, config_id, job_id = _seed_single_type_scan(session)
        job = session.get(ScanJob, job_id)
        assert job is not None
        job.status = ScanJobStatus.completed.value
        job.completed_at = finished_at
        job.result_summary = {"events_created": 3}
        session.commit()

    def _explode(ds: object) -> None:
        raise AssertionError("adapter must not be built for an already-completed job")

    monkeypatch.setitem(
        scan_tasks.run_scan.run.__globals__, "_get_sync_session", sync_session_factory
    )
    monkeypatch.setitem(scan_tasks.run_scan.run.__globals__, "_build_adapter", _explode)

    result = scan_tasks.run_scan.run(str(config_id), str(job_id))

    # ``skipped``, not ``cancelled``: nobody stopped this run, its ack was lost.
    assert result == {
        "skipped": True,
        "job_status": ScanJobStatus.completed.value,
        "scan_config_id": str(config_id),
    }

    with sync_session_factory() as session:
        job = session.get(ScanJob, job_id)
        assert job is not None
        assert job.status == ScanJobStatus.completed.value
        assert job.started_at is None
        assert job.result_summary == {"events_created": 3}


def _cancel_job_from_another_session(
    sync_session_factory: sessionmaker[Session], job_id: uuid.UUID
) -> None:
    """What ``cancel_scan_job`` writes, issued on a second connection.

    The request session is a different one from the worker's, which is the whole
    reason the worker has to re-read rather than trust its own instance.
    """
    with sync_session_factory() as other:
        job = other.get(ScanJob, job_id)
        assert job is not None
        job.status = ScanJobStatus.cancelled.value
        job.completed_at = CANCELLED_AT
        job.error_message = "Cancelled by user"
        other.commit()


def test_run_scan_cancelled_during_the_reindex_keeps_the_cancelled_verdict(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """Stop pressed AFTER the generation commit, while the reindex runs.

    The mid-run checkpoint cannot help here — the catalog rewrite is already
    durable and stays, which is what the docs promise. What must not happen is
    the close-out reopening the row: that left a run reading *Succeeded* while
    carrying "Cancelled by user", with the user's own ``completed_at`` replaced
    by this run's.

    On a project large enough for a full main-branch reindex this window is
    seconds wide, and ``ScanJob`` has no ``version_id_col``, so the close-out
    UPDATE is ``WHERE id`` only and wins by default.

    DISABLE-THE-FIX: make the ``job.status = completed`` write unconditional
    again and the status, the message and the timestamp all go red.
    """
    with sync_session_factory() as session:
        _, config_id, job_id = _seed_single_type_scan(session)

    swept: list[object] = []
    reindexed: list[object] = []
    _stub_run_scan_pipeline(
        monkeypatch,
        sync_session_factory,
        adapter=_CatalogAdapter(),
        swept=swept,
        reindexed=reindexed,
        on_reindex=lambda: _cancel_job_from_another_session(sync_session_factory, job_id),
    )

    summary = scan_tasks.run_scan.run(str(config_id), str(job_id))

    # The run really did finish: this is the post-commit window, not the
    # checkpoint one, so the sweep and the reindex both ran.
    assert swept and reindexed
    assert summary["columns_analyzed"] == 1

    with sync_session_factory() as session:
        job = session.get(ScanJob, job_id)
        assert job is not None
        assert job.status == ScanJobStatus.cancelled.value
        assert job.error_message == "Cancelled by user"
        assert job.completed_at is not None
        # The closer's timestamp, not one this run stamped on top of it.
        assert job.completed_at.replace(tzinfo=None) == CANCELLED_AT.replace(tzinfo=None)
        # The run report is still recorded — only the verdict belongs to the closer.
        assert job.result_summary is not None
        assert job.result_summary["columns_analyzed"] == 1


def test_run_scan_failing_after_a_cancel_keeps_the_cancellation(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """The failure path is a closing write too, and obeys the same rule.

    Stopping a run commonly makes it die — the revoke, a dropped warehouse
    connection, a driver error on the way down — and the sanitiser turns
    whatever that was into "Scan failed due to an internal error." Writing that
    over the user's own cancellation replaces an explanation they recognise with
    one that reads like a product fault.

    DISABLE-THE-FIX: restore the unconditional ``failed`` write in ``run_scan``'s
    ``except`` branch and both assertions below go red.
    """
    with sync_session_factory() as session:
        _, config_id, job_id = _seed_single_type_scan(session)

    def _cancel_then_die() -> None:
        _cancel_job_from_another_session(sync_session_factory, job_id)
        raise RuntimeError("warehouse connection reset")

    swept: list[object] = []
    reindexed: list[object] = []
    _stub_run_scan_pipeline(
        monkeypatch,
        sync_session_factory,
        adapter=_CatalogAdapter(on_get_columns=_cancel_then_die),
        swept=swept,
        reindexed=reindexed,
    )

    with pytest.raises(RuntimeError):
        scan_tasks.run_scan.run(str(config_id), str(job_id))

    with sync_session_factory() as session:
        job = session.get(ScanJob, job_id)
        assert job is not None
        assert job.status == ScanJobStatus.cancelled.value
        assert job.error_message == "Cancelled by user"


def test_apply_event_groups_cancelled_during_the_reindex_keeps_the_verdict(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """The event-group apply has the same tail window, and the same rule.

    Its post-commit tail is the main-branch reindex the fold needs so the
    surviving group event carries a search document. A Stop landing there cannot
    bring the deleted sources back, but it still owns the row.

    DISABLE-THE-FIX: make ``apply_event_groups``' ``completed`` write
    unconditional again and both assertions go red.
    """
    config_id = uuid.uuid4()
    job_id = uuid.uuid4()
    with sync_session_factory() as session:
        project_id, data_source_id = _seed_project(session)
        main_id, _working_id = _seed_branches(session, project_id)
        session.add_all(
            [
                ScanConfig(
                    id=config_id,
                    project_id=project_id,
                    data_source_id=data_source_id,
                    name="Daily",
                    base_query="SELECT * FROM events",
                    event_group_rules=CLICK_RULE,
                ),
                ScanJob(id=job_id, scan_config_id=config_id, status="pending"),
            ]
        )
        session.flush()
        _seed_event_type_with_clicks(session, project_id=project_id, branch_id=main_id)
        session.commit()

    monkeypatch.setitem(
        scan_tasks.apply_event_groups.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )

    def _reindex_then_cancel(session: Session, project_id: uuid.UUID) -> None:
        _cancel_job_from_another_session(sync_session_factory, job_id)

    monkeypatch.setitem(
        scan_tasks.apply_event_groups.run.__globals__,
        "reindex_main_branch_from_worker",
        _reindex_then_cancel,
    )

    summary = scan_tasks.apply_event_groups.run(str(config_id), str(job_id))

    assert summary["mode"] == "event_groups_apply"

    with sync_session_factory() as session:
        job = session.get(ScanJob, job_id)
        assert job is not None
        assert job.status == ScanJobStatus.cancelled.value
        assert job.error_message == "Cancelled by user"


def test_run_scan_records_its_celery_task_id_so_a_cancel_can_revoke(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """``ScanJob.celery_task_id``'s documented invariant, finally honoured.

    ``.run()`` leaves ``self.request.id`` as None, so this drives the task the
    eager way to get a real request id. Without the write, ``cancel_scan_job``'s
    ``if job.celery_task_id:`` branch is dead code for every catalog run.
    """
    with sync_session_factory() as session:
        _, config_id, job_id = _seed_single_type_scan(session)

    _stub_run_scan_pipeline(
        monkeypatch,
        sync_session_factory,
        adapter=_CatalogAdapter(),
        swept=[],
        reindexed=[],
    )

    async_result = scan_tasks.run_scan.apply(args=[str(config_id), str(job_id)], throw=True)

    with sync_session_factory() as session:
        job = session.get(ScanJob, job_id)
        assert job is not None
        assert job.status == ScanJobStatus.completed.value
        assert job.celery_task_id is not None
        assert job.celery_task_id == async_result.id


def test_apply_event_groups_skips_a_cancelled_job(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """The same guard on the task that DELETES the events it folds."""
    config_id = uuid.uuid4()
    job_id = uuid.uuid4()
    with sync_session_factory() as session:
        project_id, data_source_id = _seed_project(session)
        main_id, _ = _seed_branches(session, project_id)
        session.add_all(
            [
                ScanConfig(
                    id=config_id,
                    project_id=project_id,
                    data_source_id=data_source_id,
                    name="Daily",
                    base_query="SELECT * FROM events",
                    event_group_rules=CLICK_RULE,
                ),
                ScanJob(
                    id=job_id,
                    scan_config_id=config_id,
                    status=ScanJobStatus.cancelled.value,
                    error_message="Cancelled by user",
                ),
            ]
        )
        session.flush()
        _seed_event_type_with_clicks(session, project_id=project_id, branch_id=main_id)
        session.commit()

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("a cancelled job must not merge or reindex")

    monkeypatch.setitem(
        scan_tasks.apply_event_groups.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        scan_tasks.apply_event_groups.run.__globals__,
        "merge_existing_events_for_group_rules",
        _explode,
    )
    monkeypatch.setitem(
        scan_tasks.apply_event_groups.run.__globals__,
        "reindex_main_branch_from_worker",
        _explode,
    )

    result = scan_tasks.apply_event_groups.run(str(config_id), str(job_id))

    assert result["cancelled"] is True

    with sync_session_factory() as session:
        job = session.get(ScanJob, job_id)
        assert job is not None
        assert job.status == ScanJobStatus.cancelled.value
        assert job.started_at is None
        assert job.result_summary is None


class _FakeAsyncSession:
    """Just enough AsyncSession for ``cancel_scan_job``'s terminal write."""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, instance: object) -> None:
        return None


async def test_cancel_scan_job_revokes_off_the_request_event_loop(
    monkeypatch: MonkeyPatch,
) -> None:
    """A kombu broadcast must never run inline inside an ``async def``.

    Recording ``celery_task_id`` on the scan tasks activates this branch on every
    Stop run, and ``celery_app.control.revoke`` is synchronous socket I/O: inline
    against a hung broker it freezes the whole uvicorn worker, not just the
    caller. That is precisely what ``services._celery_dispatch`` exists to
    prevent, and the rest of this module already routes through it.

    DISABLE-THE-FIX: drop the ``await dispatch(...)`` wrapper and ``inline``
    below records the call.
    """
    from tripl.worker.celery_app import celery_app

    job = ScanJob(
        id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        status=ScanJobStatus.running.value,
        celery_task_id="task-abc",
    )

    async def _fake_get_scan_job(*args: object, **kwargs: object) -> ScanJob:
        return job

    inline: list[object] = []
    dispatched: list[tuple[object, tuple[object, ...]]] = []

    def _revoke(*args: object, **kwargs: object) -> None:
        inline.append(args)

    async def _fake_dispatch(send: object, /, *args: object, **kwargs: object) -> None:
        dispatched.append((send, args))

    monkeypatch.setattr(scan_service, "get_scan_job", _fake_get_scan_job)
    monkeypatch.setattr(scan_service, "dispatch", _fake_dispatch)
    monkeypatch.setattr(celery_app.control, "revoke", _revoke)

    session = _FakeAsyncSession()
    result = await scan_service.cancel_scan_job(
        session,  # type: ignore[arg-type]
        "slug",
        job.scan_config_id,
        job.id,
    )

    assert dispatched == [(celery_app.control.revoke, ("task-abc",))]
    assert inline == [], "revoke was called inline on the event loop"
    assert result.status == ScanJobStatus.cancelled.value
    assert result.error_message == "Cancelled by user"
    assert session.commits == 1


# ── a manual grouped run creates what the dry run promised ────

_GROUP_COLUMNS = [
    ColumnInfo("screen", "String"),
    ColumnInfo("locale", "String"),
]


def _grouped_analysis(*, columns: list[ColumnInfo]) -> BreakdownAnalysis:
    results = {
        column.name: CardinalityResult(
            column=column,
            count=1,
            is_low=True,
            sample_values=["home" if column.name == "screen" else "en"],
        )
        for column in columns
    }
    return BreakdownAnalysis(
        results=results,
        rows=[tuple("home" if column.name == "screen" else "en" for column in columns)],
        reg_names=[column.name for column in columns],
        json_names=[],
    )


def _grouped_config(session: Session) -> tuple[uuid.UUID, ScanConfig]:
    project_id, data_source_id = _seed_project(session)
    config = ScanConfig(
        id=uuid.uuid4(),
        project_id=project_id,
        data_source_id=data_source_id,
        name="Grouped",
        base_query="SELECT * FROM events",
        event_type_column="screen",
        time_column="ts",
        cardinality_threshold=100,
    )
    session.add(config)
    session.commit()
    return project_id, config


def test_manual_grouped_scan_creates_its_missing_event_types(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """ "Catalog only — adds events and fields when you run it" has to be true.

    A Catalog-only config never reaches the scheduler by construction (the
    dispatcher keys on ``interval``), so before this the mode created zero events
    forever: the manual grouped run looked the event type up by name, found
    nothing, appended "Skipped event type …: not found in project" and moved on —
    while the dry run for the very same config promised the type "would be
    added".

    DISABLE-THE-FIX: restore the lookup-or-skip block and all four assertions go
    red at once.
    """
    with sync_session_factory() as session:
        project_id, config = _grouped_config(session)
        analysis = _grouped_analysis(columns=_GROUP_COLUMNS)
        monkeypatch.setattr(
            scan_tasks,
            "analyze_cardinality_grouped",
            lambda *a, **k: (["home"], {"home": analysis}),
        )

        result, per_group, _rows, _warehouse_rows = scan_tasks._scan_with_grouping(
            session,
            project_id,
            config,
            adapter=object(),  # type: ignore[arg-type]
            columns=_GROUP_COLUMNS,
            scan_window=None,
            row_limit=50_000,
        )
        session.commit()

        assert not [line for line in result.details if "Skipped event type" in line]

        event_type = session.execute(
            select(EventType).where(
                EventType.project_id == project_id,
                EventType.name == "home",
            )
        ).scalar_one()
        # ``screen`` is the event_type_column and ``ts`` the time column, so both
        # are reserved — the same set the generator is told to stay quiet about.
        assert {fd.name for fd in event_type.field_definitions} == {"locale"}

        assert result.events_created >= 1
        assert "home" in per_group
        created = (
            session.execute(select(Event).where(Event.event_type_id == event_type.id))
            .scalars()
            .all()
        )
        assert created


def test_manual_grouped_scan_declares_a_new_warehouse_column(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """The second half: an undeclared column used to be dropped until the next tick.

    ``plan_events`` drops any column with no field definition from the event
    identity and from field values, so a scheduled config that grew a warehouse
    column lost it from every manual run in between.
    """
    with sync_session_factory() as session:
        project_id, config = _grouped_config(session)
        event_type = EventType(
            id=uuid.uuid4(),
            project_id=project_id,
            name="home",
            display_name="home",
            description="",
        )
        session.add(event_type)
        session.flush()
        session.add(
            FieldDefinition(
                id=uuid.uuid4(),
                event_type_id=event_type.id,
                name="payload",
                display_name="payload",
                field_type="string",
                order=0,
            )
        )
        session.commit()

        analysis = _grouped_analysis(columns=_GROUP_COLUMNS)
        monkeypatch.setattr(
            scan_tasks,
            "analyze_cardinality_grouped",
            lambda *a, **k: (["home"], {"home": analysis}),
        )

        result, _per_group, _rows, _warehouse_rows = scan_tasks._scan_with_grouping(
            session,
            project_id,
            config,
            adapter=object(),  # type: ignore[arg-type]
            columns=_GROUP_COLUMNS,
            scan_window=None,
            row_limit=50_000,
        )
        session.commit()

        session.refresh(event_type)
        assert {fd.name for fd in event_type.field_definitions} == {"payload", "locale"}
        assert not [line for line in result.details if "Skipped column 'locale'" in line]


# ── tripl-0zpq.45 (review): the catalog's name rule, enforced on both sides ──
#
# Making the manual run CREATE what the dry run promised put a warehouse value
# straight into ``EventType(name=...)`` with no emptiness and no length check,
# while ``models.event_type`` declares ``name`` as ``String(100)``. Both grouped
# runners reach that one resolver, so this was never a manual-run-only path: the
# SCHEDULED ``catalog_sync`` calls it too.
#
# Two reachable values broke it. ``analyze_cardinality_grouped`` maps a NULL
# group cell to ``""``, so a blank name needs no exotic data at all — it created
# one nameless event type per project, which no screen can render and no user
# could have created, quietly collecting every NULL row. And an over-long value
# raised mid-flush on PostgreSQL, killing the whole run under "Scan failed due to
# an internal error."
#
# The policy is REJECT — see ``event_type_name_rejection``'s docstring for why
# truncating is worse than refusing. These tests pin both halves of it: the
# resolver refuses, and the dry run refuses the SAME values so the preview never
# promises a type the run will not accept.

_A_VALID_NAME = "a" * EVENT_TYPE_NAME_MAX_LEN
_TOO_LONG = "b" * (EVENT_TYPE_NAME_MAX_LEN + 1)


class _DryRunAdapter:
    """``screen``/``locale``, and whatever breakdown rows the test hands it.

    Row layout is ``BaseAdapter.get_full_breakdown``'s: the regular values, then
    the JSON path arrays, then the kept JSON values, then ``_cnt`` last.
    """

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return list(_GROUP_COLUMNS)

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
        return ([column.name for column in _GROUP_COLUMNS], [], [], self._rows[:limit])

    def close(self) -> None:
        return None


def test_auto_created_event_type_refuses_a_blank_name(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A NULL in the grouping column is not an event type called "".

    ``analyze_cardinality_grouped`` turns a NULL group cell into ``""``, so this
    is the ordinary shape of a nullable column, not a corner case — and the
    resolver used to INSERT it, producing one unnamed row per project that
    absorbed every NULL group and could not be renamed from any screen.

    Refused before the lookup on purpose: checking afterwards would find the
    blank event type an earlier run created and keep feeding it.

    DISABLE-THE-FIX: drop the guard and the raise never happens — SQLite stores
    "" happily, which is exactly why this defect survived the suite.
    """
    with sync_session_factory() as session:
        project_id, _config = _grouped_config(session)

        for blank in ("", "   ", "\t\n"):
            with pytest.raises(ScanError) as excinfo:
                ensure_event_type_with_fields(
                    session, project_id, blank, _GROUP_COLUMNS, {"screen"}
                )
            assert "blank value" in str(excinfo.value)
            # A ScanError is surfaced verbatim; anything else would reach the
            # operator as the generic "Scan failed due to an internal error."
            # banner this message exists to replace.
            assert user_facing_error(excinfo.value) == f"Scan failed: {excinfo.value}"

        assert (
            session.execute(select(EventType).where(EventType.project_id == project_id))
            .scalars()
            .all()
            == []
        )


def test_auto_created_event_type_refuses_a_name_longer_than_the_column(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """``event_types.name`` is ``String(100)``; PostgreSQL already enforced it.

    What changes is that the refusal is now sayable and that both backends agree:
    the suite runs on SQLite, which stores an over-long string without complaint,
    so the failure only ever appeared in production.

    The boundary is asserted in both directions — exactly 100 characters is a
    legal name, and a guard that rejected it would refuse data the API accepts
    from a person (``schemas.event_type.EventTypeCreate``).

    DISABLE-THE-FIX: drop the guard and the over-long value is stored instead of
    raising.
    """
    with sync_session_factory() as session:
        project_id, _config = _grouped_config(session)

        with pytest.raises(ScanError) as excinfo:
            ensure_event_type_with_fields(
                session, project_id, _TOO_LONG, _GROUP_COLUMNS, {"screen"}
            )
        message = str(excinfo.value)
        assert f"{EVENT_TYPE_NAME_MAX_LEN + 1}-character value" in message
        # Quoted back ELIDED, and the actionable tail survives the 500-char
        # right-truncation ``user_facing_error`` applies to a curated message
        #: an un-elided value would push it off the end.
        assert _TOO_LONG not in message
        assert user_facing_error(excinfo.value).endswith("pick a different Event type column.")

        # The limit itself is legal.
        at_the_limit = ensure_event_type_with_fields(
            session, project_id, _A_VALID_NAME, _GROUP_COLUMNS, {"screen"}
        )
        assert at_the_limit.name == _A_VALID_NAME

        assert [
            et.name
            for et in session.execute(
                select(EventType).where(EventType.project_id == project_id)
            ).scalars()
        ] == [_A_VALID_NAME]


def test_a_refused_name_is_never_reshaped_into_the_catalog(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Reject, not truncate — and an accepted value is stored character for character.

    ``truncate_event_name`` guards ``Event.name`` and was the obvious thing to
    reuse here. It is the wrong tool, because an event type's name is its
    IDENTITY rather than display text:

    * Truncating COLLIDES. The two values below agree on their first 100
      characters, so a truncating resolver returns one event type for both —
      which then absorbs both groups' events and dedups the second group's
      against the first's, silently and permanently.
    * Truncating with a disambiguating suffix does not collide, but the written
      name then stops equalling the group value — and ``metrics.catalog_sync``
      re-selects ``EventType.name == et_name`` by the RAW value for drift and
      contract detection before it calls this resolver, as does the dry run to
      label a type existing rather than new. Every tick would rediscover the
      type as new.

    So the value is validated and stored VERBATIM, padding included.

    DISABLE-THE-FIX: swap the raise for ``et_name[:100]`` and both halves go red
    — the two long values collapse onto one row, and the padded name loses its
    padding.
    """
    shared = "c" * (EVENT_TYPE_NAME_MAX_LEN + 20)
    with sync_session_factory() as session:
        project_id, _config = _grouped_config(session)

        for value in (f"{shared}-one", f"{shared}-two"):
            with pytest.raises(ScanError):
                ensure_event_type_with_fields(
                    session, project_id, value, _GROUP_COLUMNS, {"screen"}
                )
        assert (
            session.execute(select(EventType).where(EventType.project_id == project_id))
            .scalars()
            .all()
            == []
        )

        padded = ensure_event_type_with_fields(
            session, project_id, "  home  ", _GROUP_COLUMNS, {"screen"}
        )
        assert padded.name == "  home  "
        # The round trip the other call sites depend on: looked up by the raw
        # group value, the created row is found.
        found = session.execute(
            select(EventType).where(
                EventType.project_id == project_id, EventType.name == "  home  "
            )
        ).scalar_one()
        assert found.id == padded.id


def test_manual_grouped_run_refuses_an_unusable_group_value(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """The guard is reached from the runner, not only from the resolver's unit.

    ``_scan_with_grouping`` is the manual half; the scheduled ``catalog_sync``
    calls the same function object, which is why the fix lives in the resolver
    and not in either runner.

    DISABLE-THE-FIX: without the guard the run completes and leaves an event type
    named "" behind.
    """
    with sync_session_factory() as session:
        project_id, config = _grouped_config(session)
        analysis = _grouped_analysis(columns=_GROUP_COLUMNS)
        monkeypatch.setattr(
            scan_tasks,
            "analyze_cardinality_grouped",
            lambda *a, **k: (["home", ""], {"home": analysis, "": analysis}),
        )

        with pytest.raises(ScanError) as excinfo:
            scan_tasks._scan_with_grouping(
                session,
                project_id,
                config,
                adapter=object(),  # type: ignore[arg-type]
                columns=_GROUP_COLUMNS,
                scan_window=None,
                row_limit=50_000,
            )
        assert "blank value" in str(excinfo.value)


def test_dry_run_promises_only_the_group_values_a_run_would_accept(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Preview and run reach the same verdict for every group value.

    This is tripl-0zpq.45's defect pointing the other way. There the dry run
    promised a type the manual run skipped; here it promised types the run
    REFUSES — a blank one and one too long for ``event_types.name`` — and the
    job then failed on values the preview had called fine, after listing their
    events and offering to add their fields.

    A refused value is dropped from the targets outright, so it raises no "would
    be added" warning and contributes no events, no sampled rows and no
    breakdown combinations. It is reported under ``errors`` rather than
    ``warnings`` because it is not a partiality in the preview: it is a run that
    will not finish.

    DISABLE-THE-FIX: remove the filter and the blank and over-long groups come
    back as two more "would be added" warnings with an empty ``errors`` list.
    """
    with sync_session_factory() as session:
        project_id, config = _grouped_config(session)
        adapter = _DryRunAdapter(
            [
                ("home", "en", 10),
                (None, "en", 5),
                (_TOO_LONG, "fr", 3),
            ]
        )

        payload: dict[str, Any] = build_dry_run_payload(
            session,
            adapter,  # type: ignore[arg-type]
            config,
            sample_row_limit=5_000,
        )

        assert {event["event_type"] for event in payload["events"]} == {"home"}
        assert payload["sampled_rows"] == 10
        assert payload["breakdown_combinations"] == 1
        assert [warning for warning in payload["warnings"] if "would be added" in warning] == [
            "Event type 'home' is not in your plan yet and would be added"
        ]
        assert payload["errors"] == [
            event_type_name_rejection(""),
            event_type_name_rejection(_TOO_LONG),
        ]

        # Group order is the same on both sides, so the run raises on the FIRST
        # refused value — and the sentence it persists is the one the preview
        # already showed, word for word.
        with pytest.raises(ScanError) as excinfo:
            ensure_event_type_with_fields(session, project_id, "", _GROUP_COLUMNS, {"screen"})
        assert payload["errors"][0] == str(excinfo.value)

        # A dry run writes nothing, refusals included.
        assert (
            session.execute(select(EventType).where(EventType.project_id == project_id))
            .scalars()
            .all()
            == []
        )


def test_dry_run_stays_non_fatal_when_every_group_value_is_refused(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Reporting is the dry run's whole job; it must not adopt the run's verdict.

    With no target left, nothing was analysed — so ``unmapped_columns`` says
    nothing rather than naming every column, which would tell the operator a run
    skips columns it would in fact create.
    """
    with sync_session_factory() as session:
        _project_id, config = _grouped_config(session)
        adapter = _DryRunAdapter([(None, "en", 5), (_TOO_LONG, "fr", 3)])

        payload: dict[str, Any] = build_dry_run_payload(
            session,
            adapter,  # type: ignore[arg-type]
            config,
            sample_row_limit=5_000,
        )

        assert payload["events"] == []
        assert payload["sampled_rows"] == 0
        assert payload["unmapped_columns"] == []
        assert len(payload["errors"]) == 2


def test_dry_run_totals_the_refusals_it_does_not_spell_out(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A grouping column pointed at a URL refuses hundreds, not two.

    Three spelled out and the rest totalled: an ``errors`` panel a thousand
    lines long says less than one that names three. The first is the one that
    matters — group order is identical on both sides, so it is the value the run
    raises on.

    Pluralised rather than "1 values", the defect tripl-3y7z fixed on the other
    side of the wire and the reason ``unnamed_skip_detail`` keeps its copy in one
    place.
    """
    with sync_session_factory() as session:
        _project_id, config = _grouped_config(session)

        for extra, tail in ((1, "value"), (2, "values")):
            adapter = _DryRunAdapter(
                [(f"{_TOO_LONG}{i}", "en", 1) for i in range(_MAX_REFUSAL_ERRORS + extra)]
            )
            payload: dict[str, Any] = build_dry_run_payload(
                session,
                adapter,  # type: ignore[arg-type]
                config,
                sample_row_limit=5_000,
            )

            assert len(payload["errors"]) == _MAX_REFUSAL_ERRORS + 1
            assert payload["errors"][0] == event_type_name_rejection(f"{_TOO_LONG}0")
            assert payload["errors"][-1] == (
                f"{extra} further Event type column {tail} cannot name an event type either."
            )


# ── the replay snapshot survives later collection jobs ────────


LOGIN_IDENTITY = "event_name=Login|user_id=${user_id}"
CHECKOUT_IDENTITY = "event_name=Checkout|user_id=${user_id}"


def _snapshot_event(
    *,
    event_id: uuid.UUID,
    identity: str = LOGIN_IDENTITY,
    name: str = "Login",
    field_values: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """One entry of the snapshot's ``events`` list, as ``run_scan`` serializes it."""
    return {
        "identity": identity,
        "event_id": str(event_id),
        "name": name,
        "source_name": identity,
        "branch_id": None,
        "status": "implemented",
        "metric_breakdown_columns": [],
        "field_values": field_values or [],
    }


def _snapshot_summary(
    *,
    event_type_id: uuid.UUID,
    version: int = 1,
    events: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "events_created": 1,
        "generation_snapshot": {
            "version": version,
            "single_result": {
                "columns_analyzed": 2,
                "details": [],
                "event_type_id": str(event_type_id),
                "branch_id": None,
                "col_meta": {"user_id": {"is_low": False, "template": "${user_id}"}},
                "events": (
                    events if events is not None else [_snapshot_event(event_id=uuid.uuid4())]
                ),
            },
        },
    }


def _grouped_snapshot_summary(
    *,
    event_type_id: uuid.UUID,
    group_name: str,
    events: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "events_created": 1,
        "generation_snapshot": {
            "version": 1,
            "group_results": {
                group_name: {
                    "columns_analyzed": 2,
                    "details": [],
                    "event_type_id": str(event_type_id),
                    "branch_id": None,
                    "col_meta": {"user_id": {"is_low": False, "template": "${user_id}"}},
                    "events": events,
                }
            },
        },
    }


_COLLECTION_SUMMARY = {
    "mode": "metrics_collection",
    "time_from": "2026-01-01T07:00:00+00:00",
    "time_to": "2026-01-01T08:00:00+00:00",
}


def _seed_snapshot_config(
    session: Session, *, event_type_column: str | None = None
) -> tuple[ScanConfig, uuid.UUID, uuid.UUID]:
    """A replay-capable config plus the live ``Login`` event a snapshot names.

    The live event is the point: a snapshot entry is only replayable while the
    ``events`` row it names still exists, so a fixture that seeds none tests the
    deleted-event path by accident rather than the ordinary one.
    """
    project_id, data_source_id = _seed_project(session)
    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project_id,
        name="pv",
        display_name="Page View",
        description="",
    )
    session.add(event_type)
    session.flush()
    login = Event(
        id=uuid.uuid4(),
        project_id=project_id,
        event_type_id=event_type.id,
        name="Login",
        source_name=LOGIN_IDENTITY,
        description="",
        status="implemented",
    )
    config = ScanConfig(
        id=uuid.uuid4(),
        project_id=project_id,
        data_source_id=data_source_id,
        event_type_id=event_type.id,
        event_type_column=event_type_column,
        name="Hourly",
        base_query="SELECT * FROM events",
        interval="1h",
    )
    session.add_all([login, config])
    session.commit()
    return config, event_type.id, login.id


def _add_completed_job(
    session: Session,
    config: ScanConfig,
    *,
    completed_at: datetime,
    result_summary: dict[str, object],
) -> None:
    session.add(
        ScanJob(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            status=ScanJobStatus.completed.value,
            completed_at=completed_at,
            result_summary=result_summary,
        )
    )


def test_generation_snapshot_survives_newer_collection_jobs(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Only ``run_scan`` writes the snapshot; every other task shadows it.

    ``collect_metrics`` (collection AND replay), ``apply_event_groups`` and the
    demo runtime tick all complete ScanJobs on the same ``scan_config_id`` with
    summaries that carry no ``generation_snapshot``, so "newest completed job"
    made the snapshot reachable only in the gap between a scan and its first
    collection tick — after that, forever, replay fell back to the heuristic
    rebuild, whose ``_field_template`` returns None for a field carrying two
    templates and then matches identities built from raw cells against templated
    ``source_name`` keys.

    DISABLE-THE-FIX: this also pins the SQLite trap. Under the bare indexed form
    ``result_summary["generation_snapshot"].isnot(None)`` the predicate compiles
    to ``JSON_QUOTE(JSON_EXTRACT(...))``, and ``json_quote(NULL)`` is the TEXT
    ``'null'`` — so on SQLite the filter is a silent no-op. That is only visible
    once the shadow rows outnumber ``_SNAPSHOT_JOB_SCAN_LIMIT``, which is why six
    are seeded and not the three the defect itself needs: with five or fewer the
    unfiltered query still returns the snapshot inside its LIMIT and the Python
    walk below finds it, so the test would pass either way. ``.as_string()`` is
    load-bearing for this SQLite fixture, NOT for production — on PostgreSQL the
    bare form compiles to ``(result_summary -> 'generation_snapshot') IS NOT
    NULL`` and ``->`` on a missing key yields SQL NULL, so it filters correctly
    there. What is pinned here is the coverage, not a live Postgres defect.
    """
    with sync_session_factory() as session:
        config, event_type_id, login_id = _seed_snapshot_config(session)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        _add_completed_job(
            session,
            config,
            completed_at=base,
            result_summary=_snapshot_summary(
                event_type_id=event_type_id,
                events=[_snapshot_event(event_id=login_id)],
            ),
        )
        for hours in range(1, 7):
            _add_completed_job(
                session,
                config,
                completed_at=base + timedelta(hours=hours),
                result_summary=dict(_COLLECTION_SUMMARY),
            )
        session.commit()

        group_results, single_result, _branch_id = _load_latest_generation_snapshot(
            session, config=config
        )

        assert group_results == {}
        assert single_result is not None
        assert single_result.event_type_id == event_type_id
        # The template is what a replay matches identities on; losing it is the
        # whole downstream failure.
        assert single_result.col_meta["user_id"]["template"] == "${user_id}"


def test_generation_snapshot_walk_skips_a_malformed_head_row(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A future-version payload at the head must not cost the good one behind it."""
    with sync_session_factory() as session:
        config, event_type_id, login_id = _seed_snapshot_config(session)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        _add_completed_job(
            session,
            config,
            completed_at=base,
            result_summary=_snapshot_summary(
                event_type_id=event_type_id,
                events=[_snapshot_event(event_id=login_id)],
            ),
        )
        _add_completed_job(
            session,
            config,
            completed_at=base + timedelta(hours=1),
            result_summary=_snapshot_summary(
                event_type_id=event_type_id,
                version=2,
                events=[_snapshot_event(event_id=login_id)],
            ),
        )
        session.commit()

        _group_results, single_result, _branch_id = _load_latest_generation_snapshot(
            session, config=config
        )

        assert single_result is not None
        assert single_result.col_meta["user_id"]["template"] == "${user_id}"


# ── tripl-0zpq.19 follow-up: a snapshot never names an event that is gone ────
#
# WHAT THESE TESTS CAN AND CANNOT DO. The defect is a foreign-key violation:
# ``event_metrics.event_id``, ``event_metric_breakdowns.event_id`` and
# ``variable_values.event_id`` all reference ``events.id``, so a replay that
# rebuilds an event from a snapshot and writes its historical id after the row
# has been deleted is refused by PostgreSQL and takes the whole replay down
# mid-window. This suite runs on SQLite, which parses foreign keys and then
# ignores them, and this module's engine does not even set
# ``PRAGMA foreign_keys=ON`` — so no test written here can make that INSERT
# fail, and none of these pretends to.
#
# What they pin instead is the invariant one step upstream, which is where the
# fix lives and is the thing that is actually worth protecting: EVERY event id
# ``_load_latest_generation_snapshot`` hands downstream addresses a row that
# exists. ``events_by_name`` is the single chokepoint — ``chunk_processing``,
# ``metric_rows`` and ``catalog_sync``'s ``replay_events`` all read their ids
# from it and from nowhere else — so asserting on the mapping it returns is
# equivalent to asserting on what the writers receive, and it fails for the
# right reason on any database.


def _live_event_ids(session: Session) -> set[uuid.UUID]:
    return set(session.execute(select(Event.id)).scalars())


def _assert_every_identity_is_live(session: Session, result: GenerationResult) -> None:
    """The writer-facing invariant, stated once."""
    handed_out = {event.id for event in result.events_by_name.values()}
    assert handed_out <= _live_event_ids(session), (
        "the snapshot handed the metric writers an event id with no catalog row; "
        "on PostgreSQL that INSERT is a foreign-key violation"
    )


def test_snapshot_event_deleted_since_the_scan_is_not_handed_to_the_writers(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """An identity with no live row is dropped; the rest of the snapshot stands.

    Checkout was in the plan when the scan ran and has been deleted since — by
    an analyst, a plan-branch merge, or the event fold in ``generate_events``,
    none of which rewrite ``ScanJob.result_summary``. Its replayed per-event
    history has nowhere to go: the row it would need to reference is gone. So
    it is dropped, its volume falls through to the event-type and project-total
    series exactly as it did before tripl-0zpq.19 made this path reachable, and
    Login — which still exists — keeps both its id and the snapshot's historical
    ``col_meta``. Dropping the one dead entry, NOT abandoning the snapshot.
    """
    with sync_session_factory() as session:
        config, event_type_id, login_id = _seed_snapshot_config(session)
        deleted_id = uuid.uuid4()
        _add_completed_job(
            session,
            config,
            completed_at=datetime(2026, 1, 1, tzinfo=UTC),
            result_summary=_snapshot_summary(
                event_type_id=event_type_id,
                events=[
                    _snapshot_event(event_id=login_id),
                    _snapshot_event(
                        event_id=deleted_id,
                        identity=CHECKOUT_IDENTITY,
                        name="Checkout",
                    ),
                ],
            ),
        )
        session.commit()

        _group_results, single_result, _branch_id = _load_latest_generation_snapshot(
            session, config=config
        )

        assert single_result is not None
        _assert_every_identity_is_live(session, single_result)
        assert set(single_result.events_by_name) == {LOGIN_IDENTITY}
        assert single_result.events_by_name[LOGIN_IDENTITY].id == login_id
        assert deleted_id not in {event.id for event in single_result.events_by_name.values()}
        # The snapshot was not abandoned for the live catalog: its historical
        # template is still what a replay matches identities on.
        assert single_result.col_meta["user_id"]["template"] == "${user_id}"


def test_snapshot_event_replaced_under_the_same_identity_is_repointed(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A new row holding the old scan identity inherits the replayed history.

    A plan-branch merge or revert DELETES the main event and INSERTS a
    replacement carrying the same ``source_name``, so the id in a snapshot
    written before the merge is dead while the event itself plainly still
    exists. Discarding here would punch a silent hole in that event's replayed
    series; re-pointing hands the history to the row that holds the identity
    today, which is the same rule ``_merge_event_into_group`` applies when it
    moves ``event_metrics`` to the surviving event rather than dropping them.

    ``uq_event_scan_identity`` is what makes the lookup safe:
    ``(event_type_id, source_name)`` is unique and an event type lives on one
    branch of one project, so a working branch's deep copy cannot answer here.
    """
    with sync_session_factory() as session:
        config, event_type_id, login_id = _seed_snapshot_config(session)
        superseded_id = uuid.uuid4()
        field_definition_id = uuid.uuid4()
        session.add(
            FieldDefinition(
                id=field_definition_id,
                event_type_id=event_type_id,
                name="user_id",
                display_name="User",
                field_type="string",
                order=0,
            )
        )
        _add_completed_job(
            session,
            config,
            completed_at=datetime(2026, 1, 1, tzinfo=UTC),
            result_summary=_snapshot_summary(
                event_type_id=event_type_id,
                events=[
                    _snapshot_event(
                        event_id=superseded_id,
                        field_values=[
                            {
                                "field_definition_id": str(field_definition_id),
                                "value": "${user_id}",
                            }
                        ],
                    )
                ],
            ),
        )
        session.commit()

        _group_results, single_result, _branch_id = _load_latest_generation_snapshot(
            session, config=config
        )

        assert single_result is not None
        _assert_every_identity_is_live(session, single_result)
        replayed = single_result.events_by_name[LOGIN_IDENTITY]
        assert replayed.id == login_id
        # The reconstructed field values follow the event, because
        # ``_accumulate_replay_variable_samples`` reads them off it and
        # ``variable_values.event_id`` is NOT NULL with a key of its own.
        assert [fv.event_id for fv in replayed.field_values] == [login_id]
        # Still the snapshot's own metadata, not a live-catalog rebuild.
        assert single_result.col_meta["user_id"]["template"] == "${user_id}"


def test_snapshot_event_that_still_exists_keeps_its_recorded_id(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The ordinary case must cost nothing and change nothing.

    Guards the fix against over-reach in the other direction: resolution is
    allowed to drop and re-point what is dead, never to rewrite what is alive.
    """
    with sync_session_factory() as session:
        config, event_type_id, login_id = _seed_snapshot_config(session)
        _add_completed_job(
            session,
            config,
            completed_at=datetime(2026, 1, 1, tzinfo=UTC),
            result_summary=_snapshot_summary(
                event_type_id=event_type_id,
                events=[_snapshot_event(event_id=login_id)],
            ),
        )
        session.commit()

        _group_results, single_result, _branch_id = _load_latest_generation_snapshot(
            session, config=config
        )

        assert single_result is not None
        _assert_every_identity_is_live(session, single_result)
        assert {identity: event.id for identity, event in single_result.events_by_name.items()} == {
            LOGIN_IDENTITY: login_id
        }


def test_snapshot_field_value_whose_definition_is_gone_is_dropped(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The second column of the same INSERT, and the second way to kill a replay.

    ``_merge_replay_variable_samples`` writes
    ``(variable_id, event_id, field_definition_id)`` into ``variable_values``,
    where ``field_definition_id`` is NOT NULL with its own foreign key. Deleting
    a field definition does NOT delete its events, so a snapshot can carry a
    perfectly live event whose reconstructed field value names a definition that
    no longer exists — a replay-killing row the event check alone would let
    through. There is no natural key to re-point a field definition by, so the
    only outcome available is to drop the field value; the variable bound to it
    simply collects no sample this run.
    """
    with sync_session_factory() as session:
        config, event_type_id, login_id = _seed_snapshot_config(session)
        live_field_id = uuid.uuid4()
        dropped_field_id = uuid.uuid4()
        session.add(
            FieldDefinition(
                id=live_field_id,
                event_type_id=event_type_id,
                name="user_id",
                display_name="User",
                field_type="string",
                order=0,
            )
        )
        _add_completed_job(
            session,
            config,
            completed_at=datetime(2026, 1, 1, tzinfo=UTC),
            result_summary=_snapshot_summary(
                event_type_id=event_type_id,
                events=[
                    _snapshot_event(
                        event_id=login_id,
                        field_values=[
                            {
                                "field_definition_id": str(live_field_id),
                                "value": "${user_id}",
                            },
                            {
                                "field_definition_id": str(dropped_field_id),
                                "value": "${campaign}",
                            },
                        ],
                    )
                ],
            ),
        )
        session.commit()

        _group_results, single_result, _branch_id = _load_latest_generation_snapshot(
            session, config=config
        )

        assert single_result is not None
        replayed = single_result.events_by_name[LOGIN_IDENTITY]
        assert [fv.field_definition_id for fv in replayed.field_values] == [live_field_id]
        # The event itself is untouched — a dead field definition is not a
        # reason to stop replaying the event that referenced it.
        assert replayed.id == login_id


def test_grouped_snapshot_resolves_identities_on_its_own_return_path(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The grouped branch returns before the single one and needs the same check.

    ``_load_latest_generation_snapshot`` has two exits — ``group_results`` for a
    config with an ``event_type_column`` and ``single_result`` for one without —
    and a fix applied to only the second would leave every grouped replay
    writing dead ids.
    """
    with sync_session_factory() as session:
        config, event_type_id, login_id = _seed_snapshot_config(
            session, event_type_column="event_name"
        )
        _add_completed_job(
            session,
            config,
            completed_at=datetime(2026, 1, 1, tzinfo=UTC),
            result_summary=_grouped_snapshot_summary(
                event_type_id=event_type_id,
                group_name="pv",
                events=[
                    _snapshot_event(event_id=login_id),
                    _snapshot_event(
                        event_id=uuid.uuid4(),
                        identity=CHECKOUT_IDENTITY,
                        name="Checkout",
                    ),
                ],
            ),
        )
        session.commit()

        group_results, single_result, _branch_id = _load_latest_generation_snapshot(
            session, config=config
        )

        assert single_result is None
        assert set(group_results) == {"pv"}
        _assert_every_identity_is_live(session, group_results["pv"])
        assert set(group_results["pv"].events_by_name) == {LOGIN_IDENTITY}


def test_replay_variable_sample_at_a_dead_event_id_is_refused_by_the_database(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The hazard itself, on the one replay writer this database CAN police.

    ``event_metrics`` and ``event_metric_breakdowns`` are written through
    ``pg_insert(...).on_conflict_do_update(...)``, a PostgreSQL construct that
    never compiles here, so the foreign-key refusal those two take is out of
    reach of any test in this suite — the tests above assert the identities
    instead. ``_merge_replay_variable_samples`` is different: it writes through
    the ORM, and ``variable_values.event_id`` is NOT NULL with
    ``ForeignKey("events.id")``. With the fixture's ``PRAGMA foreign_keys=ON``
    that INSERT is refused here exactly as PostgreSQL refuses it in production.

    So this is the consequence spelled out: hand any replay writer an event id
    whose row has been deleted and the statement raises, the replay dies
    mid-window, and because ``process_chunk`` commits per chunk the window is
    left half-rewritten. It is why ``_load_latest_generation_snapshot`` resolves
    its identities before anything downstream can reach this line.
    """
    with sync_session_factory() as session:
        config, event_type_id, _login_id = _seed_snapshot_config(session)
        variable = Variable(
            id=uuid.uuid4(),
            project_id=config.project_id,
            name="user_id",
            source_name="user_id",
        )
        field_definition = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=event_type_id,
            name="user_id",
            display_name="User",
            field_type="string",
            order=0,
        )
        session.add_all([variable, field_definition])
        session.commit()

        deleted_event_id = uuid.uuid4()
        _merge_replay_variable_samples(
            session,
            project_id=config.project_id,
            branch_id=None,
            cardinality_threshold=10,
            accumulated={
                (variable.id, deleted_event_id, field_definition.id): {
                    "variable_id": variable.id,
                    "event_id": deleted_event_id,
                    "field_definition_id": field_definition.id,
                    "source_column": "user_id",
                    "values": ["u1"],
                }
            },
        )

        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


# ── the release verdict describes the CURRENT rollout ─────────


def _seed_version_series(
    session: Session,
    config: ScanConfig,
    *,
    login_id: uuid.UUID,
    filler_id: uuid.UUID,
    start: datetime,
    old_version: str,
    new_version: str,
    login_on_new: int | None,
) -> None:
    """Ten daily buckets of ``old_version``; ``new_version`` ships on day 7.

    ``login_on_new=None`` withholds Login from the new release entirely, which is
    the "missing" regression the analyzer reports.
    """
    days = [start + timedelta(days=offset) for offset in range(10)]
    for day in days:
        for event_id, count in ((login_id, 100), (filler_id, 900)):
            session.add(
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=event_id,
                    event_type_id=None,
                    bucket=day,
                    breakdown_column="app_version",
                    breakdown_value=old_version,
                    is_other=False,
                    count=count,
                )
            )
    for day in days[6:]:
        session.add(
            EventMetricBreakdown(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_id=filler_id,
                event_type_id=None,
                bucket=day,
                breakdown_column="app_version",
                breakdown_value=new_version,
                is_other=False,
                count=500,
            )
        )
        if login_on_new is not None:
            session.add(
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=login_id,
                    event_type_id=None,
                    bucket=day,
                    breakdown_column="app_version",
                    breakdown_value=new_version,
                    is_other=False,
                    count=login_on_new,
                )
            )


def test_recalculate_release_regressions_anchors_on_stored_data_not_a_window(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A replay of a past window must refresh the CURRENT release's verdict.

    The pass wipes the scan's ``ReleaseRegression``/``ReleaseComparability`` rows
    before it computes anything, and ``_prepare_alert_deliveries`` runs
    unconditionally in the same replay over whatever it wrote — with alert state
    keyed on scope, not on version. Anchored on the caller's window, a one-off
    replay of January therefore deleted September's verdict, re-derived January's
    and notified on a release that shipped eight months ago, while resetting the
    acknowledged incidents of the release actually in the field.

    DISABLE-THE-FIX: restoring the ``evaluation_end`` anchor makes this call a
    ``TypeError``; restoring only the anchoring makes the verdict name 2.1.0.
    """
    with sync_session_factory() as session:
        project_id, data_source_id = _seed_project(session)
        event_type = EventType(
            id=uuid.uuid4(),
            project_id=project_id,
            name="pv",
            display_name="Page View",
            description="",
        )
        session.add(event_type)
        session.flush()
        config = ScanConfig(
            id=uuid.uuid4(),
            project_id=project_id,
            data_source_id=data_source_id,
            event_type_id=event_type.id,
            name="Hourly",
            base_query="SELECT * FROM events",
            interval="1h",
            app_version_column="app_version",
        )
        login = Event(
            id=uuid.uuid4(),
            project_id=project_id,
            event_type_id=event_type.id,
            name="event_name=Login",
            description="",
            status="implemented",
        )
        filler = Event(
            id=uuid.uuid4(),
            project_id=project_id,
            event_type_id=event_type.id,
            name="event_name=Filler",
            description="",
            status="implemented",
        )
        session.add_all([config, login, filler])
        session.commit()

        # The historical block a replay would have been pointed at: Login
        # vanished on 2.1.0, so the old code had a regression to report.
        _seed_version_series(
            session,
            config,
            login_id=login.id,
            filler_id=filler.id,
            start=datetime(2026, 1, 1),
            old_version="2.0.0",
            new_version="2.1.0",
            login_on_new=None,
        )
        # What the scan has actually stored since: a healthy current rollout.
        _seed_version_series(
            session,
            config,
            login_id=login.id,
            filler_id=filler.id,
            start=datetime(2026, 9, 1),
            old_version="3.0.0",
            new_version="3.1.0",
            login_on_new=100,
        )
        session.commit()

        detected = _recalculate_release_regressions(session, config)
        session.commit()

        verdicts = (
            session.execute(
                select(ReleaseComparability).where(ReleaseComparability.scan_config_id == config.id)
            )
            .scalars()
            .all()
        )
        assert verdicts
        assert {verdict.version for verdict in verdicts} == {"3.1.0"}
        assert {verdict.previous_version for verdict in verdicts} == {"3.0.0"}

        regressions = (
            session.execute(
                select(ReleaseRegression).where(ReleaseRegression.scan_config_id == config.id)
            )
            .scalars()
            .all()
        )
        assert detected == 0
        assert regressions == []
