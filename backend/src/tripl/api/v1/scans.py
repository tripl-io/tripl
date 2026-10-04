import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from tripl.api.deps import (
    EditorUserDep,
    KeyReachableOwnerUserDep,
    OwnerUserDep,
    SessionDep,
    get_editor_user,
    get_key_reachable_owner_user,
    get_owner_user,
)
from tripl.models.scan_dry_run_job import ScanDryRunJob
from tripl.models.scan_job import ScanJob
from tripl.models.scan_preview_job import ScanPreviewJob
from tripl.schemas.event_metric import PlatformPresenceResponse
from tripl.schemas.scan_config import (
    ScanConfigCreate,
    ScanConfigDetailResponse,
    ScanConfigPreviewRequest,
    ScanConfigResponse,
    ScanConfigUpdate,
    ScanDryRunJobResponse,
    ScanDryRunRequest,
    ScanMetricsReplayRequest,
    SourceFreshnessItem,
)
from tripl.schemas.scan_job import (
    ScanActivityResponse,
    ScanJobResponse,
    ScanPreviewJobResponse,
)
from tripl.services import (
    audit_service,
    metrics_service,
    scan_activity_service,
    scan_config_service,
    scan_service,
)

# Handlers return ORM models; FastAPI serializes them through each route's
# ``response_model=...Response`` (the OpenAPI contract). The return annotations
# below name the ORM type actually returned so mypy matches the runtime, while
# response_model still drives schema generation and output validation.

router = APIRouter(
    prefix="/projects/{slug}/scans",
    tags=["scans"],
)
# A scan config is free-text SQL pointed at a stored warehouse credential, and
# ``base_query`` is interpolated verbatim into ``SELECT * FROM (...) AS _src``.
# Anyone who can author or execute one can read whatever that credential can
# read, so these routes carry the same role as the credentials themselves:
# data sources are org owner/admin-only (api/v1/data_sources.py), and so is the
# SQL run against them. "Owner" in this module means an owner or admin of the
# project's organization (``get_owner_user``). Editors keep every read-only view
# of scans and their jobs, plus the right to run a config an org admin already
# authored (see ``run_scan``).
_owner_required = [Depends(get_owner_user)]
_editor_required = [Depends(get_editor_user)]
# Replaying an existing config over an explicit window is the one owner-only scan
# action an org owner's or admin's ``tk_w_`` key may take. It re-runs SQL an owner
# already authored through the session-only routes above, over a window the caller
# names, and writes nothing but metric values for that config — so the credential
# reach a leaked key gains is bounded by what an owner already approved, unlike
# authoring or editing ``base_query``, which stays browser-only.
_owner_or_owner_key_required = [Depends(get_key_reachable_owner_user)]

# Project-level freshness read (#269). Its own router because it sits beside
# ``/scans`` rather than under it: ``/projects/{slug}/source-freshness``.
source_freshness_router = APIRouter(
    prefix="/projects/{slug}",
    tags=["scans"],
)


@source_freshness_router.get("/source-freshness", response_model=list[SourceFreshnessItem])
async def list_source_freshness(session: SessionDep, slug: str) -> list[SourceFreshnessItem]:
    """Each scan config's freshness: fresh, late, overdue or unknown.

    Computed on read from what the latest collection recorded, so a scan whose
    worker has stopped turns ``overdue`` without any job having to say so.
    """
    return await scan_config_service.list_source_freshness(session, slug)


@router.get("", response_model=list[ScanConfigResponse])
async def list_scan_configs(session: SessionDep, slug: str) -> list[ScanConfigResponse]:
    configs = await scan_service.list_scan_configs(session, slug)
    return await scan_config_service.scan_config_responses(session, configs)


# Declared before ``/{scan_id}``: that template would otherwise claim the literal
# segment and answer 422 for a UUID that does not parse.
@router.get("/activity", response_model=ScanActivityResponse)
async def get_scan_activity(session: SessionDep, slug: str) -> ScanActivityResponse:
    """Each scan's latest job, failing streak and rows read in the last 24 hours.

    One request for the whole Scans list, aggregated in SQL, so the streak and
    the 24h total are exact rather than floors over a capped page of jobs.
    """
    return await scan_activity_service.get_scan_activity(session, slug, now=datetime.now(UTC))


@router.post("", response_model=ScanConfigResponse, status_code=201)
async def create_scan_config(
    session: SessionDep,
    slug: str,
    data: ScanConfigCreate,
    current_user: OwnerUserDep,
) -> ScanConfigResponse:
    cfg = await scan_service.create_scan_config(session, slug, data)
    await audit_service.record(
        session,
        user=current_user,
        action="scan_config.create",
        target_type="scan_config",
        target_id=cfg.id,
        target_name=cfg.name,
        project_slug=slug,
        payload=data.model_dump(),
    )
    return await scan_config_service.scan_config_response(session, cfg)


@router.post(
    "/preview",
    response_model=ScanPreviewJobResponse,
    status_code=202,
    dependencies=_owner_required,
)
async def preview_scan_config(
    session: SessionDep,
    slug: str,
    data: ScanConfigPreviewRequest,
) -> ScanPreviewJob:
    """Enqueue a preview job; poll GET /preview-jobs/{job_id} for the result."""
    return await scan_service.trigger_preview(session, slug, data)


@router.get(
    "/preview-jobs/{job_id}",
    response_model=ScanPreviewJobResponse,
    dependencies=_owner_required,
)
async def get_scan_preview_job(
    session: SessionDep,
    slug: str,
    job_id: uuid.UUID,
) -> ScanPreviewJob:
    return await scan_service.get_preview_job(session, slug, job_id)


@router.post(
    "/dry-run",
    response_model=ScanDryRunJobResponse,
    status_code=202,
    dependencies=_owner_required,
)
async def dry_run_scan_config(
    session: SessionDep,
    slug: str,
    data: ScanDryRunRequest,
) -> ScanDryRunJob:  # serialized through ScanDryRunJobResponse
    """Enqueue a dry-run; poll GET /dry-run-jobs/{job_id} for the result.

    Owner-only for the same reason preview is: the SQL is free text from a
    draft, executed verbatim against a stored warehouse credential.
    """
    return await scan_service.trigger_dry_run(session, slug, data)


@router.get(
    "/dry-run-jobs/{job_id}",
    response_model=ScanDryRunJobResponse,
    dependencies=_owner_required,
)
async def get_scan_dry_run_job(
    session: SessionDep,
    slug: str,
    job_id: uuid.UUID,
) -> ScanDryRunJob:
    return await scan_service.get_dry_run_job(session, slug, job_id)


@router.get("/{scan_id}", response_model=ScanConfigDetailResponse)
async def get_scan_config(
    session: SessionDep, slug: str, scan_id: uuid.UUID
) -> ScanConfigDetailResponse:
    return await scan_config_service.get_scan_config_detail(session, slug, scan_id)


@router.get("/{scan_id}/platform-presence", response_model=PlatformPresenceResponse)
async def get_platform_presence(
    session: SessionDep, slug: str, scan_id: uuid.UUID
) -> PlatformPresenceResponse:
    """Per-event platform presence matrix for the scan's platform_column.

    Empty when the scan has no platform_column set (the platform dimension is
    inert), so callers can render the panel unconditionally."""
    return await metrics_service.get_platform_presence(session, slug, scan_id)


@router.patch("/{scan_id}", response_model=ScanConfigResponse)
async def update_scan_config(
    session: SessionDep,
    slug: str,
    scan_id: uuid.UUID,
    data: ScanConfigUpdate,
    current_user: OwnerUserDep,
) -> ScanConfigResponse:
    cfg = await scan_service.update_scan_config(session, slug, scan_id, data)
    await audit_service.record(
        session,
        user=current_user,
        action="scan_config.update",
        target_type="scan_config",
        target_id=cfg.id,
        target_name=cfg.name,
        project_slug=slug,
        payload=data.model_dump(exclude_unset=True),
    )
    return await scan_config_service.scan_config_response(session, cfg)


@router.delete("/{scan_id}", status_code=204)
async def delete_scan_config(
    session: SessionDep,
    slug: str,
    scan_id: uuid.UUID,
    current_user: OwnerUserDep,
) -> None:
    existing = await scan_service.get_scan_config(session, slug, scan_id)
    name = getattr(existing, "name", "")
    await scan_service.delete_scan_config(session, slug, scan_id)
    await audit_service.record(
        session,
        user=current_user,
        action="scan_config.delete",
        target_type="scan_config",
        target_id=scan_id,
        target_name=name,
        project_slug=slug,
    )


@router.post(
    "/{scan_id}/run",
    response_model=ScanJobResponse,
    status_code=201,
    # Running a STORED config executes only SQL an owner already authored and
    # approved, so it stays editor-level: it grants no new read of the warehouse,
    # and the agent API / MCP server drive it with editor-scoped API keys
    # (mcp-server/src/tripl_mcp/tools/scans.py:27). Authoring and previewing —
    # where the free-text SQL actually comes from — remain owner-only above.
    dependencies=_editor_required,
)
async def run_scan(
    session: SessionDep, slug: str, scan_id: uuid.UUID, current_user: EditorUserDep
) -> ScanJob:
    job = await scan_service.trigger_scan(session, slug, scan_id)
    await audit_service.record(
        session,
        user=current_user,
        action="scan_config.run",
        target_type="scan_config",
        target_id=scan_id,
        project_slug=slug,
        payload={"scan_job_id": str(job.id)},
    )
    return job


@router.post(
    "/{scan_id}/event-groups/apply",
    response_model=ScanJobResponse,
    status_code=201,
)
async def apply_scan_event_groups(
    session: SessionDep,
    slug: str,
    scan_id: uuid.UUID,
    current_user: OwnerUserDep,
) -> ScanJob:
    job = await scan_service.trigger_event_groups_apply(session, slug, scan_id)
    cfg = await scan_service.get_scan_config(session, slug, scan_id)
    await audit_service.record(
        session,
        user=current_user,
        action="scan_config.event_groups.apply",
        target_type="scan_config",
        target_id=scan_id,
        target_name=cfg.name,
        project_slug=slug,
        payload={"scan_job_id": str(job.id)},
    )
    return job


@router.post(
    "/{scan_id}/metrics/replay",
    response_model=ScanJobResponse,
    status_code=201,
    dependencies=_owner_or_owner_key_required,
)
async def replay_scan_metrics(
    session: SessionDep,
    slug: str,
    scan_id: uuid.UUID,
    data: ScanMetricsReplayRequest,
    current_user: KeyReachableOwnerUserDep,
) -> ScanJob:
    job = await scan_service.trigger_metrics_replay(session, slug, scan_id, data)
    await audit_service.record(
        session,
        user=current_user,
        action="scan_config.metrics_replay",
        target_type="scan_config",
        target_id=scan_id,
        project_slug=slug,
        payload={"scan_job_id": str(job.id), **data.model_dump(mode="json")},
    )
    return job


@router.get("/{scan_id}/jobs", response_model=list[ScanJobResponse])
async def list_scan_jobs(
    session: SessionDep,
    slug: str,
    scan_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ScanJob]:
    """Newest jobs first, capped.

    This was uncapped, and the Scans tab fans it out over every scan config on a
    10-second poll: production configs hold 1,366-1,551 jobs each, so an open tab
    pulled roughly 4,400 rows every 10 seconds and rendered them unvirtualized.
    """
    return await scan_service.list_scan_jobs(session, slug, scan_id, limit=limit)


@router.get("/{scan_id}/jobs/{job_id}", response_model=ScanJobResponse)
async def get_scan_job(
    session: SessionDep,
    slug: str,
    scan_id: uuid.UUID,
    job_id: uuid.UUID,
) -> ScanJob:
    return await scan_service.get_scan_job(session, slug, scan_id, job_id)


@router.post("/{scan_id}/jobs/{job_id}/cancel", response_model=ScanJobResponse)
async def cancel_scan_job(
    session: SessionDep,
    slug: str,
    scan_id: uuid.UUID,
    job_id: uuid.UUID,
    current_user: EditorUserDep,
) -> ScanJob:
    job = await scan_service.cancel_scan_job(session, slug, scan_id, job_id)
    await audit_service.record(
        session,
        user=current_user,
        action="scan_job.cancel",
        target_type="scan_job",
        target_id=job_id,
        target_name=str(job_id),
        project_slug=slug,
    )
    return job
