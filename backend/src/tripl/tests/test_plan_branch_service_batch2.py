"""Plan-branch service regressions from an earlier sweep, batch 2.

- ``?include_diff_counts=true`` counts open branches only, so its
  cost follows the work in flight rather than the project's branch history.
- ``create_branch`` takes the merge base and the deep copy inside
  one snapshot-consistent transaction, so the two describe the same main. When
  Postgres aborts that transaction as unserializable, the creation is retried
  from a clean session a bounded number of times, then answered with 409.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event as sa_event
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError

from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.plan_branch import PlanBranch
from tripl.models.plan_revision import PlanRevision
from tripl.models.project import Project
from tripl.schemas.plan_branch import PlanBranchCreate
from tripl.services import plan_branch_service
from tripl.tests.conftest import TestSessionLocal, engine
from tripl.tests.test_plan_branches import (
    _approve_and_merge,
    _create_branch,
    _seed_plan,
    _transition,
)

# Statuses a review can still move, and the two it cannot.
_OPEN_STATUSES = ("draft", "ready_for_review", "changes_requested", "approved")
_SETTLED_STATUSES = ("merged", "closed")

# The transitions that walk a fresh draft to each status ("merged" goes through
# ``_approve_and_merge`` instead, since it is a merge rather than a transition).
_ACTIONS_TO_STATUS: dict[str, tuple[str, ...]] = {
    "draft": (),
    "ready_for_review": ("submit",),
    "changes_requested": ("submit", "request_changes"),
    "approved": ("submit", "approve"),
    "closed": ("close",),
}


async def _branch_in_status(client: AsyncClient, slug: str, status: str) -> str:
    branch_id = await _create_branch(client, slug, name=f"b-{status}")
    if status == "merged":
        merged = await _approve_and_merge(client, slug, branch_id)
        assert merged.status_code == 200, merged.text
        return branch_id
    for action in _ACTIONS_TO_STATUS[status]:
        moved = await _transition(client, slug, branch_id, action)
        assert "_status" not in moved, (status, action, moved)
    return branch_id


def _record_snapshots(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID | None]:
    """Record the branch of every plan snapshot the branch service builds."""
    built: list[uuid.UUID | None] = []
    real = plan_branch_service.build_plan_snapshot

    async def _recording(
        session: Any, project_id: uuid.UUID, branch_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        built.append(branch_id)
        return await real(session, project_id, branch_id=branch_id)

    monkeypatch.setattr(plan_branch_service, "build_plan_snapshot", _recording)
    return built


async def _list_with_counts(client: AsyncClient, slug: str) -> dict[str, dict[str, Any]]:
    listed = await client.get(f"/api/v1/projects/{slug}/branches?include_diff_counts=true")
    assert listed.status_code == 200, listed.text
    return {item["id"]: item for item in listed.json()["items"]}


# --- diff counts for open branches only ---------------------


@pytest.mark.asyncio
async def test_diff_counts_cover_open_branches_and_skip_settled_ones(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every open status is counted and neither settled one is, nor snapshotted.

    A merged or closed branch keeps its deep copy forever, so counting it cost
    one snapshot per branch in the project's history and still answered nothing
    worth reading. The whole status set is covered: the rule is a class of
    statuses, not a single one.
    """
    slug = "counts-by-status"
    await _seed_plan(client, slug)
    ids = {
        status: await _branch_in_status(client, slug, status)
        for status in (*_OPEN_STATUSES, *_SETTLED_STATUSES)
    }

    plain = (await client.get(f"/api/v1/projects/{slug}/branches")).json()["items"]
    status_of = {item["id"]: item["status"] for item in plain}
    # The setup reached every status it claims to cover.
    assert {status: status_of[branch_id] for status, branch_id in ids.items()} == {
        status: status for status in ids
    }
    main_id = next(item["id"] for item in plain if item["kind"] == "main")

    built = _record_snapshots(monkeypatch)
    rows = await _list_with_counts(client, slug)

    for status in _OPEN_STATUSES:
        row = rows[ids[status]]
        assert isinstance(row["ahead"], int), (status, row)
        assert isinstance(row["behind_base"], bool), (status, row)
    for status in _SETTLED_STATUSES:
        row = rows[ids[status]]
        assert row["ahead"] is None, (status, row)
        assert row["behind_base"] is None, (status, row)

    # The cost: main once and each OPEN branch once. A settled branch's copy is
    # never rebuilt.
    assert sorted(str(branch_id) for branch_id in built) == sorted(
        [main_id, *(ids[status] for status in _OPEN_STATUSES)]
    )


@pytest.mark.asyncio
async def test_settled_history_costs_no_snapshot_and_a_reopened_branch_counts_again(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Status decides what is counted, not whether the branch ever existed."""
    slug = "counts-history"
    await _seed_plan(client, slug)
    merged_id = await _branch_in_status(client, slug, "merged")
    closed_id = await _branch_in_status(client, slug, "closed")

    built = _record_snapshots(monkeypatch)
    rows = await _list_with_counts(client, slug)
    # Nothing is open, so not even main's snapshot is worth building.
    assert built == []
    for branch_id in (merged_id, closed_id):
        assert rows[branch_id]["ahead"] is None
        assert rows[branch_id]["behind_base"] is None

    reopened = await _transition(client, slug, closed_id, "reopen")
    assert reopened.get("status") == "draft", reopened
    built.clear()
    rows = await _list_with_counts(client, slug)
    main_id = next(branch_id for branch_id, row in rows.items() if row["kind"] == "main")

    # An untouched copy of main is ahead by nothing, and it is counted again.
    assert rows[closed_id]["ahead"] == 0
    assert isinstance(rows[closed_id]["behind_base"], bool)
    assert rows[merged_id]["ahead"] is None
    assert sorted(str(branch_id) for branch_id in built) == sorted([main_id, closed_id])


# --- the merge base and the copy read one main ---------------


def test_branch_creation_reads_main_under_repeatable_read_on_postgres() -> None:
    """Postgres is where concurrent writers exist, and READ COMMITTED is its default.

    REPEATABLE READ is the weakest Postgres level whose transaction reads one MVCC
    snapshot throughout. SQLite must stay unmapped, because SQLAlchemy has no
    REPEATABLE READ for it.
    """
    levels = getattr(plan_branch_service, "_CONSISTENT_READ_ISOLATION", {})
    assert levels.get("postgresql") == "REPEATABLE READ"
    assert "sqlite" not in levels


@pytest.mark.asyncio
async def test_create_branch_takes_base_and_copy_in_one_consistent_read_transaction(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base snapshot and the copy run in ONE transaction opened with the level.

    The race itself cannot be staged here: the suite shares one SQLite
    connection, so there is no second writer to interleave. What the suite can
    see is where the isolation level is applied. SQLite accepts SERIALIZABLE,
    so it stands in for Postgres' REPEATABLE READ below. The level has to ride on
    the BEGIN of the transaction that makes both reads of main, with no commit
    between the first read of the base and the copy's insert. Before the fix the
    request ran on the transaction the auth dependency had autobegun, where no
    level was set and none could be.
    """
    slug = "branch-consistent-read"
    await _seed_plan(client, slug)
    monkeypatch.setattr(
        plan_branch_service,
        "_CONSISTENT_READ_ISOLATION",
        {engine.sync_engine.dialect.name: "SERIALIZABLE"},
        raising=False,
    )

    log: list[tuple[str, str | None]] = []

    def _on_begin(conn: Any) -> None:
        log.append(("begin", conn.get_execution_options().get("isolation_level")))

    def _on_commit(conn: Any) -> None:
        log.append(("commit", None))

    def _on_rollback(conn: Any) -> None:
        log.append(("rollback", None))

    def _on_sql(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        log.append(("sql", " ".join(statement.split())))

    listeners = (
        ("begin", _on_begin),
        ("commit", _on_commit),
        ("rollback", _on_rollback),
        ("before_cursor_execute", _on_sql),
    )
    for name, listener in listeners:
        sa_event.listen(engine.sync_engine, name, listener)
    try:
        created = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    finally:
        for name, listener in listeners:
            sa_event.remove(engine.sync_engine, name, listener)
    assert created.status_code == 201, created.text

    reads_event_types = re.compile(r"^SELECT .*\bFROM event_types\b")
    copy_insert = next(
        index
        for index, (kind, sql) in enumerate(log)
        if kind == "sql" and sql is not None and sql.startswith("INSERT INTO event_types")
    )
    main_reads = [
        index
        for index, (kind, sql) in enumerate(log[:copy_insert])
        if kind == "sql" and sql is not None and reads_event_types.match(sql)
    ]
    # The base snapshot reads main's event types, then the copy reads them again.
    assert len(main_reads) >= 2, log
    opened = max(index for index in range(main_reads[0]) if log[index][0] == "begin")
    assert log[opened] == ("begin", "SERIALIZABLE"), log[opened]
    between = [entry for entry in log[opened:copy_insert] if entry[0] in ("commit", "rollback")]
    assert between == [], between

    # No concurrent writer, so the new branch's base and copy agree exactly.
    diff = await client.get(f"/api/v1/projects/{slug}/branches/{created.json()['id']}/diff")
    assert diff.status_code == 200, diff.text
    assert diff.json()["entries"] == []


# --- an unserializable creation is retried, then 409 ---------
#
# The abort cannot be staged here either: SQLite has no SQLSTATE 40001, and the
# suite has no second writer. So the deep copy is made to fail the way Postgres
# fails it, AFTER it has really written the attempt's base revision, branch row
# and copy, which is everything a retry must neither leave behind nor repeat.


class _DriverError(Exception):
    """A driver error in the shape SQLAlchemy's asyncpg adapter hands one on.

    The adapter re-raises asyncpg's exception as its own ``Error`` and copies the
    server's SQLSTATE onto it as both ``sqlstate`` and ``pgcode``. SQLAlchemy
    then wraps that in a plain ``DBAPIError``, whose ``orig`` it becomes.
    """

    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.pgcode = sqlstate


def _database_error(sqlstate: str) -> DBAPIError:
    return DBAPIError(
        "INSERT INTO plan_revisions ...",
        None,
        _DriverError("could not serialize access due to concurrent update", sqlstate),
    )


def _abort_copies(
    monkeypatch: pytest.MonkeyPatch, *, failures: int, sqlstate: str = "40001"
) -> list[uuid.UUID]:
    """Fail the first ``failures`` deep copies with ``sqlstate``, then copy normally.

    Returns the target branch of every copy the service asked for, so a test can
    count the attempts and check that each one minted a branch row of its own.
    """
    targets: list[uuid.UUID] = []
    real = plan_branch_service.deep_copy_plan_to_branch

    async def _copy(
        session: Any,
        *,
        project_id: uuid.UUID,
        source_branch_id: uuid.UUID,
        target_branch_id: uuid.UUID,
    ) -> None:
        targets.append(target_branch_id)
        await real(
            session,
            project_id=project_id,
            source_branch_id=source_branch_id,
            target_branch_id=target_branch_id,
        )
        if len(targets) <= failures:
            await session.flush()
            raise _database_error(sqlstate)

    monkeypatch.setattr(plan_branch_service, "deep_copy_plan_to_branch", _copy)
    return targets


async def _rows(slug: str, branch_name: str) -> dict[str, int]:
    """Count what a branch creation writes, across the whole project."""
    async with TestSessionLocal() as db:
        project_id = await db.scalar(select(Project.id).where(Project.slug == slug))

        async def _count(model: Any, *where: Any) -> int:
            query = select(func.count()).select_from(model).where(*where)
            return int(await db.scalar(query) or 0)

        return {
            "branches": await _count(PlanBranch, PlanBranch.project_id == project_id),
            "named": await _count(
                PlanBranch,
                PlanBranch.project_id == project_id,
                PlanBranch.name == branch_name,
            ),
            "base_revisions": await _count(
                PlanRevision,
                PlanRevision.project_id == project_id,
                PlanRevision.summary == f"Base snapshot for branch '{branch_name}'",
            ),
            "event_types": await _count(EventType, EventType.project_id == project_id),
            "events": await _count(Event, Event.project_id == project_id),
        }


async def _branch_exists(branch_id: uuid.UUID) -> bool:
    async with TestSessionLocal() as db:
        return await db.get(PlanBranch, branch_id) is not None


@pytest.mark.asyncio
async def test_serialization_failure_is_retried_from_a_clean_session_without_a_second_copy(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry lands exactly one branch, and the failed attempt leaves nothing.

    Through the router on purpose: it audits with the current user AFTER the
    service returns, on the request's session. Rolling that session back to
    retry would have expired the user, and the audit's lazy load would fail.
    """
    slug = "branch-serialization-retry"
    await _seed_plan(client, slug)
    before = await _rows(slug, "feature")
    targets = _abort_copies(monkeypatch, failures=1)

    created = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert created.status_code == 201, created.text

    # One aborted attempt, then one that landed, each on a branch row of its own.
    assert len(targets) == 2
    assert targets[0] != targets[1]
    assert created.json()["id"] == str(targets[1])
    assert not await _branch_exists(targets[0])
    # One branch, one base, one copy: main's rows exactly once more.
    assert await _rows(slug, "feature") == {
        "branches": before["branches"] + 1,
        "named": 1,
        "base_revisions": 1,
        "event_types": 2 * before["event_types"],
        "events": 2 * before["events"],
    }
    diff = await client.get(f"/api/v1/projects/{slug}/branches/{created.json()['id']}/diff")
    assert diff.status_code == 200, diff.text
    assert diff.json()["entries"] == []


@pytest.mark.asyncio
async def test_serialization_failure_on_every_attempt_answers_409_and_leaves_nothing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retries are bounded, the client is told to retry, and a retry then works."""
    slug = "branch-serialization-exhausted"
    await _seed_plan(client, slug)
    before = await _rows(slug, "feature")
    attempts = plan_branch_service._CREATE_BRANCH_ATTEMPTS
    assert attempts > 1
    targets = _abort_copies(monkeypatch, failures=attempts)

    refused = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert refused.status_code == 409, refused.text
    assert "try again" in refused.json()["detail"].lower()
    assert len(targets) == attempts
    assert len(set(targets)) == attempts
    for branch_id in targets:
        assert not await _branch_exists(branch_id)
    assert await _rows(slug, "feature") == before

    # The name is still free and the connection is clean: the client's retry,
    # which no longer collides, creates the branch.
    retried = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert retried.status_code == 201, retried.text
    assert (await _rows(slug, "feature"))["named"] == 1


@pytest.mark.asyncio
async def test_other_database_errors_are_not_retried(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only 40001 is retried. Anything else still surfaces, after one attempt."""
    slug = "branch-other-db-error"
    await _seed_plan(client, slug)
    before = await _rows(slug, "feature")
    targets = _abort_copies(monkeypatch, failures=1, sqlstate="23505")

    async with TestSessionLocal() as session:
        with pytest.raises(DBAPIError) as raised:
            await plan_branch_service.create_branch(session, slug, PlanBranchCreate(name="feature"))
    orig = raised.value.orig
    assert isinstance(orig, _DriverError)
    assert orig.sqlstate == "23505"
    assert len(targets) == 1
    assert await _rows(slug, "feature") == before


class _Psycopg3Error(Exception):
    """psycopg 3 names the code ``sqlstate`` only."""

    sqlstate = "40001"


class _Psycopg2Error(Exception):
    """psycopg2 names the code ``pgcode`` only."""

    pgcode = "40001"


@pytest.mark.parametrize(
    ("orig", "expected"),
    [
        pytest.param(_DriverError("x", "40001"), True, id="asyncpg-adapter"),
        pytest.param(_Psycopg3Error(), True, id="psycopg3"),
        pytest.param(_Psycopg2Error(), True, id="psycopg2"),
        pytest.param(_DriverError("x", "40P01"), False, id="deadlock"),
        pytest.param(Exception("no code"), False, id="no-sqlstate"),
    ],
)
def test_serialization_failure_is_recognised_by_its_sqlstate(
    orig: Exception, expected: bool
) -> None:
    error = DBAPIError("SELECT 1", None, orig)
    assert plan_branch_service._is_serialization_failure(error) is expected


# --- the branch-name race (Copilot on PR #164) -------------------------------


class _Diag:
    def __init__(self, constraint_name: str | None) -> None:
        self.constraint_name = constraint_name


class _UniqueViolation(Exception):
    """A 23505 as psycopg reports one, with the index it was raised on."""

    def __init__(self, message: str, constraint_name: str | None) -> None:
        super().__init__(message)
        self.sqlstate = "23505"
        self.diag = _Diag(constraint_name)


@pytest.mark.parametrize(
    ("orig", "expected"),
    [
        pytest.param(
            _UniqueViolation("duplicate key", "uq_plan_branch_project_name"),
            True,
            id="the-branch-name-index",
        ),
        pytest.param(
            _UniqueViolation("duplicate key", "uq_event_type_project_name"),
            False,
            id="some-other-index",
        ),
        pytest.param(
            _UniqueViolation(
                'duplicate key value violates unique constraint "uq_plan_branch_project_name"',
                None,
            ),
            True,
            id="no-diag-read-off-the-message",
        ),
        pytest.param(
            _DriverError("serialization failure", "40001"),
            False,
            id="not-a-unique-violation",
        ),
    ],
)
def test_a_lost_branch_name_race_is_told_apart_from_other_unique_violations(
    orig: Exception, expected: bool
) -> None:
    """The preflight SELECT holds nothing and the copy that follows takes the
    better part of a second, so two requests naming one branch can both pass it.
    The loser met the index and escaped as a 500; it now answers the 409 the
    preflight would have given. Any OTHER unique violation must still be a 500,
    or a real defect in the copy would be reported to the user as a name clash."""
    error = IntegrityError("INSERT", None, orig)
    assert plan_branch_service._is_duplicate_branch_name(error) is expected
