"""Uploaded ``tripl check`` runs (``tripl check --upload``).

``POST /projects/{slug}/check-runs`` records one run. It is a write, behind
:func:`~tripl.api.deps.get_check_upload_user` and its own permission
(``check_runs.upload``, not ``plan.edit``): a browser session or a ``write``
key of an editor, or the narrow ``check_upload`` key a CI job holds, which may
do this and read, nothing else, and needs only a viewer's role in the project.
A ``read`` key is refused.

The reads are what a project member sees: the run history, every run as a
summary, the repositories and check directories it can be filtered by, and
"used in code" for one event, the calls that send it in the current run of
each repository and check directory. The rules live in
:mod:`tripl.services.check_run_service`.

``/check-run-settings`` holds the project's link template, which turns each
place "used in code" lists into a link to its file; an editor sets it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from tripl.api.deps import CheckUploadUserDep, EditorUserDep, SessionDep
from tripl.schemas.check_run import (
    CheckRootFilter,
    CheckRunCreate,
    CheckRunDirectoryListResponse,
    CheckRunListResponse,
    CheckRunResponse,
    CheckRunSettings,
    CheckRunSettingsUpdate,
    EventCodeUsageResponse,
)
from tripl.schemas.pagination import Offset
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import audit_service, check_run_service
from tripl.services.project_lookup import resolve_project_id

router = APIRouter(prefix="/projects/{slug}", tags=["check-runs"])


@router.post("/check-runs", response_model=CheckRunResponse, status_code=201)
async def upload_check_run(
    session: SessionDep, slug: str, data: CheckRunCreate, user: CheckUploadUserDep
) -> CheckRunResponse:
    """Record one ``tripl check`` run of a repository directory.

    A run whose ``ref`` is its ``default_branch`` becomes the current run of
    its repository and ``check_root``: its calls replace the previous
    default-branch run's as what "used in code" shows. A default-branch run
    older than the current one (by ``pipeline_created_at`` when both carry it,
    else by ``committed_at``) goes to the history only, and so does any other
    run, kept as a summary. At most 5000 calls per run.
    """
    project_id = await resolve_project_id(session, slug)
    run = await check_run_service.record_run(session, project_id, data, user_id=user.id)
    return check_run_service.run_response(run)


@router.get("/check-runs", response_model=CheckRunListResponse)
async def list_check_runs(
    session: SessionDep,
    slug: str,
    # FreeTextFilter: bound into an equality, and a NUL aborts inside asyncpg
    # before SQL runs, so it is stripped first.
    repository: Annotated[
        FreeTextFilter | None,
        Query(max_length=255, description="Only the runs of this repository."),
    ] = None,
    check_root: Annotated[
        CheckRootFilter | None,
        Query(
            max_length=1000,
            description="Only the runs of this check directory (`.` for the repository root).",
        ),
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
        check_root=check_root,
        default_branch_only=default_branch,
        limit=limit,
        offset=offset,
    )


@router.get("/check-runs/directories", response_model=CheckRunDirectoryListResponse)
async def list_check_run_directories(
    session: SessionDep, slug: str
) -> CheckRunDirectoryListResponse:
    """Every repository and check directory with a run still kept, with its run count.

    What the history's repository and check directory filters offer.
    """
    project_id = await resolve_project_id(session, slug)
    return await check_run_service.list_directories(session, project_id)


@router.get("/events/{event_id}/code-usage", response_model=EventCodeUsageResponse)
async def get_event_code_usage(
    session: SessionDep, slug: str, event_id: uuid.UUID
) -> EventCodeUsageResponse:
    """Where the code sends this event: the calls in each repository directory's current run.

    A directory whose current run has no call of the event is listed with no
    calls; one that never uploaded a default-branch run is not listed.
    ``code_url_template`` is the project's link template, null when unset.
    """
    project_id = await resolve_project_id(session, slug)
    return await check_run_service.event_code_usage(session, project_id, event_id)


@router.get("/check-run-settings", response_model=CheckRunSettings)
async def get_check_run_settings(session: SessionDep, slug: str) -> CheckRunSettings:
    """How the project links a place a check run found to its file."""
    project_id = await resolve_project_id(session, slug)
    return await check_run_service.get_settings(session, project_id)


@router.patch("/check-run-settings", response_model=CheckRunSettings)
async def update_check_run_settings(
    session: SessionDep, slug: str, data: CheckRunSettingsUpdate, user: EditorUserDep
) -> CheckRunSettings:
    """Set or clear the link template; an editor's change (``settings.manage``).

    Not under ``/check-runs``: a ``check_upload`` key, which a viewer may hold,
    may write there, and this is a project setting.
    """
    project_id = await resolve_project_id(session, slug)
    settings = await check_run_service.update_settings(session, project_id, data)
    await audit_service.record(
        session,
        user=user,
        action="check_run_settings.update",
        target_type="check_run_settings",
        target_id=project_id,
        target_name=slug,
        project_slug=slug,
        payload=data.model_dump(exclude_unset=True),
    )
    return settings
