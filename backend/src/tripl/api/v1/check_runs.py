"""Uploaded ``tripl check`` runs (``tripl check --upload``).

``POST /projects/{slug}/check-runs`` records one run. It is a write, behind
:func:`~tripl.api.deps.get_check_upload_user`: a browser session or a
``write`` key of an editor, or the narrow ``check_upload`` key a CI job holds,
which may do this and read, nothing else. A ``read`` key is refused.

The two reads are what a project member sees: the run history, every run as a
summary, and "used in code" for one event, the calls of each repository's
latest default-branch run that send it. The rules live in
:mod:`tripl.services.check_run_service`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from tripl.api.deps import CheckUploadUserDep, SessionDep
from tripl.schemas.check_run import (
    CheckRunCreate,
    CheckRunListResponse,
    CheckRunResponse,
    EventCodeUsageResponse,
)
from tripl.schemas.pagination import Offset
from tripl.services import check_run_service
from tripl.services.project_lookup import resolve_project_id

router = APIRouter(prefix="/projects/{slug}", tags=["check-runs"])


@router.post("/check-runs", response_model=CheckRunResponse, status_code=201)
async def upload_check_run(
    session: SessionDep, slug: str, data: CheckRunCreate, user: CheckUploadUserDep
) -> CheckRunResponse:
    """Record one ``tripl check`` run of a repository.

    A run whose ``ref`` is its ``default_branch`` becomes the repository's
    current run: its calls replace the previous default-branch run's as what
    "used in code" shows. Any other run is kept as a summary only, for the
    history. At most 5000 calls per run.
    """
    project_id = await resolve_project_id(session, slug)
    run = await check_run_service.record_run(session, project_id, data, user_id=user.id)
    return check_run_service.run_response(run)


@router.get("/check-runs", response_model=CheckRunListResponse)
async def list_check_runs(
    session: SessionDep,
    slug: str,
    repository: Annotated[
        str | None, Query(max_length=255, description="Only the runs of this repository.")
    ] = None,
    default_branch: Annotated[
        bool, Query(description="Only the runs on their repository's default branch.")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Offset = 0,
) -> CheckRunListResponse:
    """The project's uploaded runs, newest first, each as a summary."""
    project_id = await resolve_project_id(session, slug)
    return await check_run_service.list_runs(
        session,
        project_id,
        repository=repository,
        default_branch_only=default_branch,
        limit=limit,
        offset=offset,
    )


@router.get("/events/{event_id}/code-usage", response_model=EventCodeUsageResponse)
async def get_event_code_usage(
    session: SessionDep, slug: str, event_id: uuid.UUID
) -> EventCodeUsageResponse:
    """Where the code sends this event: per repository, its latest default-branch run's calls.

    A repository whose latest run has no call of the event is listed with no
    calls; one that never uploaded a default-branch run is not listed.
    """
    project_id = await resolve_project_id(session, slug)
    return await check_run_service.event_code_usage(session, project_id, event_id)
