"""Uploaded ``tripl check`` runs: record one, list the history, and "used in code" for an event.

The rule the whole module keeps: per project, repository and check directory
(``check_root``, the directory the check scanned or its config's path, so
several apps in one repository keep a snapshot each), at most one run is *current*, the newest run
on that repository's default branch, and only a current run keeps its calls.
Recording a default-branch run makes it the current one and deletes the calls
of the run it replaces, in the same transaction, so "used in code" is always
one snapshot per repository and directory and a call removed from the code
disappears from the event. A run on any other ref is stored as a summary
(counts, commit, ref, pipeline) and never touches the current run, and so is a
merge request run whatever its ref: it checks a merged tree, not the default
branch.

"Newest" is by what the CI says, not by arrival: a default-branch run older
than the current one goes to the history only (:func:`is_older_than`), so a
re-run of an old pipeline that finishes last never puts old code back. The
pipeline creation times decide when both runs carry one; else the commit
timestamps, when both carry one; else the later upload wins. Summaries older than
``CHECK_RUN_RETENTION_DAYS`` are deleted by the daily
``maintenance.cleanup_check_runs``; a current run is never deleted for its
age, since it is what the event pages show.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl.core.bucketing import optional_to_utc, to_utc
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
    """Whether the run checked the repository's default branch; unknown counts as not.

    A merge request pipeline never did, even from a source branch with the
    default branch's name.
    """
    return (
        not data.merge_request
        and data.default_branch is not None
        and data.ref == data.default_branch
    )


def is_older_than(data: CheckRunCreate, current: CheckRun) -> bool:
    """Whether the uploaded run is older than the current run it would replace.

    By pipeline creation time when both runs carry one, else by commit
    timestamp when both carry one; a tie, or nothing to compare, is not older,
    so the later upload replaces the current run.
    """
    pairs = (
        (data.pipeline_created_at, current.pipeline_created_at),
        (data.committed_at, current.committed_at),
    )
    for uploaded, stored in pairs:
        if uploaded is not None and stored is not None:
            return uploaded < to_utc(stored)
    return False


async def _lock_repository(
    session: AsyncSession, project_id: uuid.UUID, repository: str, check_root: str
) -> None:
    """Serialize two uploads of one repository directory on PostgreSQL, so one run stays current.

    Two default-branch pipelines finishing together would each demote the
    other's run before inserting their own. A transaction-scoped advisory lock
    makes the second wait for the first to commit. A no-op on SQLite, which
    serializes writers anyway.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"check_runs:{project_id}:{repository}:{check_root}"},
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
    """Store one uploaded run; a newer default-branch run becomes its directory's current one."""
    await _lock_repository(session, project_id, data.repository, data.check_root)
    default_branch = is_default_branch_run(data)
    current = default_branch
    if default_branch:
        replaced_run = await session.scalar(
            select(CheckRun).where(*_current_of(project_id, data.repository, data.check_root))
        )
        current = replaced_run is None or not is_older_than(data, replaced_run)
    counts = Counter(call.status for call in data.calls)
    now = datetime.now(UTC)
    run = CheckRun(
        id=uuid.uuid4(),
        project_id=project_id,
        repository=data.repository,
        check_root=data.check_root,
        repository_url=data.repository_url,
        commit_sha=data.commit_sha.lower(),
        ref=data.ref,
        default_branch=data.default_branch,
        merge_request=data.merge_request,
        is_default_branch=default_branch,
        is_current=current,
        pipeline_url=data.pipeline_url,
        pipeline_created_at=data.pipeline_created_at,
        committed_at=data.committed_at,
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
            *_current_of(project_id, data.repository, data.check_root)
        )
        await session.execute(
            delete(CheckRunCall)
            .where(CheckRunCall.run_id.in_(replaced))
            .execution_options(synchronize_session=False)
        )
        await session.execute(
            update(CheckRun)
            .where(*_current_of(project_id, data.repository, data.check_root))
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


def _current_of(project_id: uuid.UUID, repository: str, check_root: str) -> tuple[Any, ...]:
    """The conditions that pick the current run of one repository directory."""
    return (
        CheckRun.project_id == project_id,
        CheckRun.repository == repository,
        CheckRun.check_root == check_root,
        CheckRun.is_current.is_(True),
    )


def run_response(run: CheckRun) -> CheckRunResponse:
    return CheckRunResponse(
        id=run.id,
        repository=run.repository,
        check_root=run.check_root,
        repository_url=run.repository_url,
        commit_sha=run.commit_sha,
        ref=run.ref,
        default_branch=run.default_branch,
        merge_request=run.merge_request,
        is_default_branch=run.is_default_branch,
        is_current=run.is_current,
        pipeline_url=run.pipeline_url,
        pipeline_created_at=optional_to_utc(run.pipeline_created_at),
        committed_at=optional_to_utc(run.committed_at),
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
    check_root: str | None,
    default_branch_only: bool,
    limit: int,
    offset: int,
) -> CheckRunListResponse:
    """The project's runs, newest first, each as a summary."""
    conditions = [CheckRun.project_id == project_id]
    if repository is not None:
        conditions.append(CheckRun.repository == repository)
    if check_root is not None:
        conditions.append(CheckRun.check_root == check_root)
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
    """Each repository directory's current run, with the calls in it that send ``event_id``.

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
            .order_by(CheckRun.repository, CheckRun.check_root, CheckRun.created_at.desc())
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
                check_root=run.check_root,
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
    shows until a newer default-branch run of its repository directory replaces it.
    Does not commit.
    """
    result = session.execute(
        delete(CheckRun)
        .where(CheckRun.created_at < cutoff, CheckRun.is_current.is_(False))
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)
