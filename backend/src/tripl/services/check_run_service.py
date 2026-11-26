"""Uploaded ``tripl check`` runs: record one, list the history, and "used in code" for an event.

The rule the whole module keeps: per project and repository, at most one run
is *current*, the latest run on that repository's default branch, and only a
current run keeps its calls. Recording a default-branch run makes it the
current one and deletes the calls of the run it replaces, in the same
transaction, so "used in code" is always one snapshot per repository and a
call removed from the code disappears from the event. A run on any other ref
is stored as a summary (counts, commit, ref, pipeline) and never touches the
current run. Summaries older than ``CHECK_RUN_RETENTION_DAYS`` are deleted by
the daily ``maintenance.cleanup_check_runs``; a current run is never deleted
for its age, since it is what the event pages show.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl.models.check_run import CheckRun, CheckRunCall
from tripl.models.event import Event
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.schemas.check_run import (
    CheckRunCallLocation,
    CheckRunCounts,
    CheckRunCreate,
    CheckRunFinding,
    CheckRunListResponse,
    CheckRunResponse,
    EventCodeUsageRepository,
    EventCodeUsageResponse,
)


def is_default_branch_run(data: CheckRunCreate) -> bool:
    """Whether the run checked the repository's default branch; unknown counts as not."""
    return data.default_branch is not None and data.ref == data.default_branch


async def _lock_repository(session: AsyncSession, project_id: uuid.UUID, repository: str) -> None:
    """Serialize two uploads of one repository on PostgreSQL, so one run stays current.

    Two default-branch pipelines finishing together would each demote the
    other's run before inserting their own. A transaction-scoped advisory lock
    makes the second wait for the first to commit. A no-op on SQLite, which
    serializes writers anyway.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"check_runs:{project_id}:{repository}"},
    )


async def _main_plan_event_ids(
    session: AsyncSession, project_id: uuid.UUID, candidates: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    """The ``candidates`` that are events of this project's main plan.

    An upload is refused on a plan branch, so every id it names is a main-plan
    event; anything else (another project's id, a stale one) is dropped rather
    than trusted, and the call keeps its identity.
    """
    wanted = set(candidates)
    if not wanted:
        return set()
    rows = await session.scalars(
        select(Event.id)
        .join(PlanBranch, Event.branch_id == PlanBranch.id)
        .where(
            Event.project_id == project_id,
            PlanBranch.kind == BranchKind.main.value,
            Event.id.in_(wanted),
        )
    )
    return set(rows.all())


async def record_run(
    session: AsyncSession,
    project_id: uuid.UUID,
    data: CheckRunCreate,
    *,
    user_id: uuid.UUID | None,
) -> CheckRun:
    """Store one uploaded run; a default-branch run becomes its repository's current one."""
    await _lock_repository(session, project_id, data.repository)
    current = is_default_branch_run(data)
    counts = Counter(call.status for call in data.calls)
    now = datetime.now(UTC)
    run = CheckRun(
        id=uuid.uuid4(),
        project_id=project_id,
        repository=data.repository,
        repository_url=data.repository_url,
        commit_sha=data.commit_sha.lower(),
        ref=data.ref,
        default_branch=data.default_branch,
        is_default_branch=current,
        is_current=current,
        pipeline_url=data.pipeline_url,
        strict=data.strict,
        calls_total=len(data.calls),
        calls_ok=counts["ok"],
        calls_warning=counts["warning"],
        calls_error=counts["error"],
        calls_dynamic=counts["dynamic"],
        uploaded_by_user_id=user_id,
        created_at=now,
        updated_at=now,
    )
    if current:
        replaced = select(CheckRun.id).where(
            CheckRun.project_id == project_id,
            CheckRun.repository == data.repository,
            CheckRun.is_current.is_(True),
        )
        await session.execute(
            delete(CheckRunCall)
            .where(CheckRunCall.run_id.in_(replaced))
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            update(CheckRun)
            .where(
                CheckRun.project_id == project_id,
                CheckRun.repository == data.repository,
                CheckRun.is_current.is_(True),
            )
            .values(is_current=False)
            .execution_options(synchronize_session=False)
        )
    session.add(run)
    await session.flush()
    if current:
        known = await _main_plan_event_ids(
            session, project_id, (call.event_id for call in data.calls if call.event_id)
        )
        session.add_all(
            CheckRunCall(
                id=uuid.uuid4(),
                run_id=run.id,
                event_id=call.event_id if call.event_id in known else None,
                event_type=call.event_type,
                name=call.name,
                identity=call.identity,
                path=call.path,
                line=call.line,
                column=call.column,
                status=call.status,
                findings=[finding.model_dump() for finding in call.findings],
            )
            for call in data.calls
        )
    await session.commit()
    return run


def run_response(run: CheckRun) -> CheckRunResponse:
    return CheckRunResponse(
        id=run.id,
        repository=run.repository,
        repository_url=run.repository_url,
        commit_sha=run.commit_sha,
        ref=run.ref,
        default_branch=run.default_branch,
        is_default_branch=run.is_default_branch,
        is_current=run.is_current,
        pipeline_url=run.pipeline_url,
        strict=run.strict,
        calls=CheckRunCounts(
            total=run.calls_total,
            ok=run.calls_ok,
            warning=run.calls_warning,
            error=run.calls_error,
            dynamic=run.calls_dynamic,
        ),
        created_at=run.created_at,
    )


async def list_runs(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    repository: str | None,
    default_branch_only: bool,
    limit: int,
    offset: int,
) -> CheckRunListResponse:
    """The project's runs, newest first, each as a summary."""
    conditions = [CheckRun.project_id == project_id]
    if repository is not None:
        conditions.append(CheckRun.repository == repository)
    if default_branch_only:
        conditions.append(CheckRun.is_default_branch.is_(True))
    total = await session.scalar(select(func.count()).select_from(CheckRun).where(*conditions))
    rows = await session.scalars(
        select(CheckRun)
        .where(*conditions)
        .order_by(CheckRun.created_at.desc(), CheckRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return CheckRunListResponse(items=[run_response(run) for run in rows.all()], total=total or 0)


async def event_code_usage(
    session: AsyncSession, project_id: uuid.UUID, event_id: uuid.UUID
) -> EventCodeUsageResponse:
    """Each repository's current run, with the calls in it that send ``event_id``.

    404 for an event that is not this project's.
    """
    owned = await session.scalar(
        select(Event.id).where(Event.id == event_id, Event.project_id == project_id)
    )
    if owned is None:
        raise HTTPException(status_code=404, detail="Event not found")
    runs: Sequence[CheckRun] = (
        await session.scalars(
            select(CheckRun)
            .where(CheckRun.project_id == project_id, CheckRun.is_current.is_(True))
            .order_by(CheckRun.repository, CheckRun.created_at.desc())
        )
    ).all()
    calls_by_run: dict[uuid.UUID, list[CheckRunCallLocation]] = {run.id: [] for run in runs}
    if runs:
        calls = await session.scalars(
            select(CheckRunCall)
            .where(CheckRunCall.run_id.in_(list(calls_by_run)), CheckRunCall.event_id == event_id)
            .order_by(CheckRunCall.path, CheckRunCall.line, CheckRunCall.column)
        )
        for call in calls.all():
            calls_by_run[call.run_id].append(
                CheckRunCallLocation(
                    path=call.path,
                    line=call.line,
                    column=call.column,
                    status=call.status,
                    findings=[CheckRunFinding.model_validate(item) for item in call.findings],
                )
            )
    return EventCodeUsageResponse(
        event_id=event_id,
        repositories=[
            EventCodeUsageRepository(
                repository=run.repository,
                repository_url=run.repository_url,
                run=run_response(run),
                calls=calls_by_run[run.id],
            )
            for run in runs
        ],
    )


def prune_runs(session: Session, cutoff: datetime) -> int:
    """Delete every run older than ``cutoff`` that is not a current one; the worker's half.

    A current run stays whatever its age: it is the snapshot "used in code"
    shows until a newer default-branch run of its repository replaces it.
    Does not commit.
    """
    result = session.execute(
        delete(CheckRun)
        .where(CheckRun.created_at < cutoff, CheckRun.is_current.is_(False))
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)
