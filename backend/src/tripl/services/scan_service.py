import logging
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache
from tripl.core.bucketing import floor_to_bucket
from tripl.models.data_source import DataSource, DBType
from tripl.models.event_type import EventType
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_dry_run_job import ScanDryRunJob
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.models.scan_preview_job import ScanPreviewJob
from tripl.schemas.scan_config import (
    ScanConfigCreate,
    ScanConfigPreviewRequest,
    ScanConfigUpdate,
    ScanDryRunRequest,
    ScanMetricsReplayRequest,
    check_replay_chunk_against_interval,
    check_scalar_columns_unreserved,
)
from tripl.services._celery_dispatch import dispatch
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.project_lookup import resolve_project_id
from tripl.services.search_service import reindex_project_branch

logger = logging.getLogger(__name__)


async def _refresh_main_search_index(
    session: AsyncSession, project_id: uuid.UUID, slug: str
) -> None:
    """Refresh the search index after a scan-config mutation.

    Scan configs are global (project-scoped, not branched), so only the MAIN
    branch index is refreshed eagerly; feature-branch indexes pick the change up
    on their next rebuild. Same rule metrics and fact tables already follow.

    Without this a scan the user just created stayed unfindable in the command
    palette until some unrelated reindex happened to fire (tripl-ugrm).
    """
    main_branch_id = await resolve_branch_id(session, project_id, None)
    await reindex_project_branch(
        session, project_id=project_id, branch_id=main_branch_id, slug=slug
    )
    # The event type list carries each type's resolved ``event_name_format``
    # (tripl-kjhi.1), so a scan config edit changes that response too.
    await cache.delete(cache.key_event_types_list(slug))


async def _verify_data_source(
    session: AsyncSession, ds_id: uuid.UUID, project_id: uuid.UUID | None = None
) -> DataSource:
    result = await session.execute(select(DataSource).where(DataSource.id == ds_id))
    ds = result.scalar_one_or_none()
    if ds is None:
        raise HTTPException(status_code=404, detail="Data source not found")
    # A synthetic (local demo) source belongs to exactly one demo project and must
    # never be selected by any other project — that is how an ordinary project is
    # kept from pointing a scan/preview at demo data. Identity is db_type +
    # project ownership, never slug/host/name.
    if ds.db_type == DBType.synthetic and project_id is not None and ds.project_id != project_id:
        raise HTTPException(
            status_code=422,
            detail=(
                "Synthetic data sources belong to their demo project and cannot be selected here."
            ),
        )
    return ds


async def _verify_main_event_type(
    session: AsyncSession, project_id: uuid.UUID, event_type_id: uuid.UUID | None
) -> None:
    if event_type_id is None:
        return
    main_branch_id = await resolve_branch_id(session, project_id, None)
    event_type = await session.scalar(
        select(EventType.id).where(
            EventType.id == event_type_id,
            EventType.project_id == project_id,
            EventType.branch_id == main_branch_id,
        )
    )
    if event_type is None:
        raise HTTPException(
            status_code=422, detail="event_type_id must belong to this project's main branch"
        )


async def _reject_duplicate_name(
    session: AsyncSession,
    data_source_id: uuid.UUID,
    name: str,
    *,
    exclude_scan_id: uuid.UUID | None = None,
) -> None:
    query = select(ScanConfig.id).where(
        ScanConfig.data_source_id == data_source_id, ScanConfig.name == name
    )
    if exclude_scan_id is not None:
        query = query.where(ScanConfig.id != exclude_scan_id)
    if await session.scalar(query) is not None:
        raise HTTPException(status_code=409, detail="Scan config with this name already exists")


async def list_scan_configs(session: AsyncSession, slug: str) -> list[ScanConfig]:
    project_id = await resolve_project_id(session, slug)
    result = await session.execute(
        select(ScanConfig)
        .where(ScanConfig.project_id == project_id)
        .order_by(ScanConfig.created_at.desc())
    )
    return list(result.scalars().all())


async def get_scan_config(session: AsyncSession, slug: str, scan_id: uuid.UUID) -> ScanConfig:
    project_id = await resolve_project_id(session, slug)
    result = await session.execute(
        select(ScanConfig).where(ScanConfig.id == scan_id, ScanConfig.project_id == project_id)
    )
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="Scan config not found")
    return config


async def create_scan_config(
    session: AsyncSession, slug: str, data: ScanConfigCreate
) -> ScanConfig:
    project_id = await resolve_project_id(session, slug)
    await _verify_data_source(session, data.data_source_id, project_id)
    await _verify_main_event_type(session, project_id, data.event_type_id)
    await _reject_duplicate_name(session, data.data_source_id, data.name)

    payload = data.model_dump()
    project_keep_releases = (
        await session.execute(
            select(Project.app_version_keep_releases).where(Project.id == project_id)
        )
    ).scalar_one()
    payload["app_version_keep_releases"] = (
        project_keep_releases if payload.get("app_version_column") else None
    )
    config = ScanConfig(project_id=project_id, **payload)
    session.add(config)
    await session.commit()
    await session.refresh(config)
    await _refresh_main_search_index(session, project_id, slug)
    return config


async def update_scan_config(
    session: AsyncSession,
    slug: str,
    scan_id: uuid.UUID,
    data: ScanConfigUpdate,
) -> ScanConfig:
    config = await get_scan_config(session, slug, scan_id)
    update_dict = data.model_dump(exclude_unset=True)
    await _verify_main_event_type(
        session, config.project_id, update_dict.get("event_type_id", config.event_type_id)
    )
    if "name" in update_dict:
        await _reject_duplicate_name(
            session, config.data_source_id, update_dict["name"], exclude_scan_id=config.id
        )
    project_keep_releases = (
        await session.execute(
            select(Project.app_version_keep_releases).where(Project.id == config.project_id)
        )
    ).scalar_one()
    resulting_app_version_column = update_dict.get("app_version_column", config.app_version_column)
    update_dict["app_version_keep_releases"] = (
        project_keep_releases if resulting_app_version_column else None
    )
    # PATCH semantics: merge the partial payload onto the live config first so
    # cross-field checks see the post-update state, not just the diff.
    try:
        check_scalar_columns_unreserved(
            metric_breakdown_columns=update_dict.get(
                "metric_breakdown_columns", config.metric_breakdown_columns
            )
            or [],
            distribution_drift_fields=update_dict.get(
                "distribution_drift_fields", config.distribution_drift_fields
            )
            or [],
            event_type_column=update_dict.get("event_type_column", config.event_type_column),
            time_column=update_dict.get("time_column", config.time_column),
            app_version_column=update_dict.get("app_version_column", config.app_version_column),
            platform_column=update_dict.get("platform_column", config.platform_column),
        )
        check_replay_chunk_against_interval(
            interval=update_dict.get("interval", config.interval),
            replay_chunk_interval=update_dict.get(
                "replay_chunk_interval", config.replay_chunk_interval
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    for key, value in update_dict.items():
        setattr(config, key, value)
    await session.commit()
    await session.refresh(config)
    await _refresh_main_search_index(session, config.project_id, slug)
    return config


async def trigger_event_groups_apply(
    session: AsyncSession,
    slug: str,
    scan_id: uuid.UUID,
) -> ScanJob:
    config = await get_scan_config(session, slug, scan_id)
    if not config.event_group_rules:
        raise HTTPException(status_code=400, detail="Scan config has no event group rules")
    await _reject_if_already_running(session, config.id)

    job = ScanJob(
        scan_config_id=config.id,
        status=ScanJobStatus.pending.value,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    from tripl.worker.tasks.scan import apply_event_groups

    try:
        await dispatch(apply_event_groups.delay, str(config.id), str(job.id))
    except Exception:
        job.status = ScanJobStatus.failed.value
        job.error_message = "Failed to dispatch task to worker (broker unavailable)"
        await session.commit()
        await session.refresh(job)
    return job


async def trigger_preview(
    session: AsyncSession,
    slug: str,
    data: ScanConfigPreviewRequest,
) -> ScanPreviewJob:
    """Create a ScanPreviewJob for an unsaved draft and dispatch the worker task.

    Preview queries the warehouse and can exceed the gateway timeout, so the
    work runs in the worker; the client polls ``get_preview_job`` for the result.
    """
    project_id = await resolve_project_id(session, slug)
    await _verify_data_source(session, data.data_source_id, project_id)

    job = ScanPreviewJob(
        project_id=project_id,
        data_source_id=data.data_source_id,
        base_query=data.base_query,
        json_value_paths=data.json_value_paths,
        row_limit=data.limit,
        time_column=data.time_column,
        scan_lookback_hours=data.scan_lookback_hours,
        include_json_paths=data.include_json_paths,
        status=ScanJobStatus.pending.value,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    from tripl.worker.tasks.scan import preview_scan_config_async

    try:
        await dispatch(preview_scan_config_async.delay, str(job.id))
    except Exception:
        job.status = ScanJobStatus.failed.value
        job.error_message = "Failed to dispatch task to worker (broker unavailable)"
        await session.commit()
        await session.refresh(job)
    return job


async def get_preview_job(
    session: AsyncSession,
    slug: str,
    job_id: uuid.UUID,
) -> ScanPreviewJob:
    project_id = await resolve_project_id(session, slug)
    result = await session.execute(
        select(ScanPreviewJob).where(
            ScanPreviewJob.id == job_id,
            ScanPreviewJob.project_id == project_id,
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Scan preview job not found")
    return job


async def trigger_dry_run(
    session: AsyncSession,
    slug: str,
    data: ScanDryRunRequest,
) -> ScanDryRunJob:
    """Create a ScanDryRunJob and dispatch the worker task.

    A dry-run runs the same ``GROUP BY ALL`` a real scan runs, so it is strictly
    slower than the preview that already needed a worker job — hence 202 + poll
    rather than an in-request answer.
    """
    project_id = await resolve_project_id(session, slug)

    if data.scan_config_id is not None:
        # Saved-config path. Scoping the config to the project is the check that
        # keeps a dry-run from executing another project's SQL.
        config = await get_scan_config(session, slug, data.scan_config_id)
        await _verify_main_event_type(session, project_id, config.event_type_id)
        job = ScanDryRunJob(
            project_id=project_id,
            scan_config_id=config.id,
            sample_row_limit=data.sample_row_limit,
            status=ScanJobStatus.pending.value,
        )
    elif data.data_source_id is None or not data.base_query:
        # Unreachable through the API — ``ScanDryRunRequest.validate_target``
        # already rejects this as 422. Spelled as a real check rather than an
        # ``assert`` so it survives ``python -O`` and so a future direct caller
        # gets an error instead of a NULL base_query reaching the worker.
        raise HTTPException(
            status_code=422,
            detail="either scan_config_id, or both data_source_id and base_query, must be provided",
        )
    else:
        await _verify_data_source(session, data.data_source_id, project_id)
        await _verify_main_event_type(session, project_id, data.event_type_id)
        job = ScanDryRunJob(
            project_id=project_id,
            data_source_id=data.data_source_id,
            base_query=data.base_query,
            event_type_id=data.event_type_id,
            event_type_column=data.event_type_column,
            time_column=data.time_column,
            event_name_format=data.event_name_format,
            event_group_rules=[rule.model_dump() for rule in data.event_group_rules],
            json_value_paths=list(data.json_value_paths),
            cardinality_threshold=data.cardinality_threshold,
            app_version_column=data.app_version_column,
            platform_column=data.platform_column,
            scan_lookback_hours=data.scan_lookback_hours,
            sample_row_limit=data.sample_row_limit,
            status=ScanJobStatus.pending.value,
        )

    session.add(job)
    await session.commit()
    await session.refresh(job)

    from tripl.worker.tasks.scan_dry_run import dry_run_scan_config_async

    try:
        await dispatch(dry_run_scan_config_async.delay, str(job.id))
    except Exception:
        job.status = ScanJobStatus.failed.value
        job.error_message = "Failed to dispatch task to worker (broker unavailable)"
        await session.commit()
        await session.refresh(job)
    return job


async def get_dry_run_job(
    session: AsyncSession,
    slug: str,
    job_id: uuid.UUID,
) -> ScanDryRunJob:
    project_id = await resolve_project_id(session, slug)
    result = await session.execute(
        select(ScanDryRunJob).where(
            ScanDryRunJob.id == job_id,
            ScanDryRunJob.project_id == project_id,
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Scan dry-run job not found")
    return job


async def delete_scan_config(session: AsyncSession, slug: str, scan_id: uuid.UUID) -> None:
    from tripl.services._alerting_destinations import disable_rules_bound_to_scan

    config = await get_scan_config(session, slug, scan_id)
    project_id = config.project_id
    await disable_rules_bound_to_scan(session, scan_id)
    await session.delete(config)
    await session.commit()
    # Covers both document kinds this delete moved: the scan_config row is gone,
    # and every alert_rule that was narrowed to it lost its subtitle above.
    await _refresh_main_search_index(session, project_id, slug)


async def _reject_if_already_running(session: AsyncSession, scan_config_id: uuid.UUID) -> None:
    """409 rather than a second collection over the same metric windows.

    The scheduler already skips dispatch while a live job exists
    (worker/tasks/metrics/schedule.py), but the manual trigger had no guard at
    all: a double-click, or Run pressed during the hourly run, started a second
    scan on the same config. Collection deletes a chunk window and rewrites it,
    so two interleaved runs lose rows (tripl-jfm3.100).

    "Live" uses the same staleness rule as the scheduler: a job whose newest
    activity marker is older than STALE_ACTIVE_SCAN_JOB_TIMEOUT is a corpse the
    reaper will clear, and must not block a new run forever.
    """
    from tripl.worker.tasks.metrics._helpers import STALE_ACTIVE_SCAN_JOB_TIMEOUT

    now = datetime.now(UTC)
    active = (
        await session.execute(
            select(ScanJob).where(
                ScanJob.scan_config_id == scan_config_id,
                ScanJob.status.in_((ScanJobStatus.pending.value, ScanJobStatus.running.value)),
            )
        )
    ).scalars()
    for job in active:
        markers = [m for m in (job.updated_at, job.started_at, job.created_at) if m is not None]
        if not markers:
            continue
        latest = max(m if m.tzinfo else m.replace(tzinfo=UTC) for m in markers)
        if now - latest < STALE_ACTIVE_SCAN_JOB_TIMEOUT:
            raise HTTPException(
                status_code=409,
                detail="A scan is already running for this configuration.",
            )


async def trigger_scan(session: AsyncSession, slug: str, scan_id: uuid.UUID) -> ScanJob:
    """Create a ScanJob and dispatch the Celery task."""
    config = await get_scan_config(session, slug, scan_id)
    await _reject_if_already_running(session, config.id)

    job = ScanJob(
        scan_config_id=config.id,
        status=ScanJobStatus.pending.value,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    # Import here to avoid circular imports at module level
    from tripl.worker.tasks.scan import run_scan

    try:
        await dispatch(run_scan.delay, str(config.id), str(job.id))
    except Exception:
        job.status = ScanJobStatus.failed.value
        job.error_message = "Failed to dispatch task to worker (broker unavailable)"
        await session.commit()
        await session.refresh(job)
    return job


async def trigger_metrics_replay(
    session: AsyncSession,
    slug: str,
    scan_id: uuid.UUID,
    data: ScanMetricsReplayRequest,
) -> ScanJob:
    """Create a ScanJob and dispatch metrics collection for an explicit window."""
    config = await get_scan_config(session, slug, scan_id)
    if not config.time_column or not config.interval:
        raise HTTPException(
            status_code=400,
            detail="Scan config requires time_column and interval to replay metrics",
        )
    await _reject_if_already_running(session, config.id)

    # The worker refuses a period reaching into the interval that is still
    # filling — it holds no complete bucket to replay — and it refuses it AFTER
    # the job exists, so the caller got a 201 and then an unexplained failed run.
    # Answer it here instead, before any ScanJob row is created. This is the same
    # boundary the worker computes: ``_floor_to_interval`` is
    # ``floor_to_bucket``'s grid, and the worker's ``now`` is never earlier than
    # this one, so a window accepted here cannot be refused there.
    latest_complete = floor_to_bucket(datetime.now(UTC), config.interval)
    if data.time_to > latest_complete:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Replay period must end at or before {latest_complete:%Y-%m-%d %H:%M} UTC: "
                f"the current {config.interval} interval has not finished, so it holds no "
                f"complete bucket to replay."
            ),
        )

    job = ScanJob(
        scan_config_id=config.id,
        status=ScanJobStatus.pending.value,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    from tripl.worker.tasks.metrics import collect_metrics

    try:
        await dispatch(
            collect_metrics.delay,
            str(config.id),
            str(job.id),
            data.time_from.isoformat(),
            data.time_to.isoformat(),
        )
    except Exception:
        job.status = ScanJobStatus.failed.value
        job.error_message = "Failed to dispatch task to worker (broker unavailable)"
        await session.commit()
        await session.refresh(job)
    return job


async def list_scan_jobs(
    session: AsyncSession,
    slug: str,
    scan_id: uuid.UUID,
    *,
    limit: int = 50,
) -> list[ScanJob]:
    """Newest jobs first. Capped — see the route docstring (tripl-jfm3.107)."""
    await get_scan_config(session, slug, scan_id)
    result = await session.execute(
        select(ScanJob)
        .where(ScanJob.scan_config_id == scan_id)
        .order_by(ScanJob.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_scan_job(
    session: AsyncSession,
    slug: str,
    scan_id: uuid.UUID,
    job_id: uuid.UUID,
) -> ScanJob:
    await get_scan_config(session, slug, scan_id)
    result = await session.execute(
        select(ScanJob).where(ScanJob.id == job_id, ScanJob.scan_config_id == scan_id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Scan job not found")
    return job


async def cancel_scan_job(
    session: AsyncSession,
    slug: str,
    scan_id: uuid.UUID,
    job_id: uuid.UUID,
) -> ScanJob:
    """Cancel an active (pending/running) scan job.

    Marks the job ``cancelled`` and best-effort revokes the Celery task. A
    running task is not killed; it stops cooperatively at its next checkpoint,
    and every scan task has one: a metrics collection or replay polls this status
    at each chunk boundary and keeps the points it already wrote, while a catalog
    run or an event-group apply looks once, immediately before the commit that
    makes its work durable — stopped THERE it rolls the whole generation back and
    writes nothing at all.

    How much work survives therefore depends on when the stop lands; what does
    not depend on it is the status. A catalog run stopped after that commit still
    finishes its variable sweep and its reindex, and their output stays, but every
    scan task re-reads the row through the database before its closing write and
    leaves a terminal status alone. So the job stays ``cancelled`` — it never
    turns itself back into ``completed``, and never overwrites the cancellation
    with the ``failed`` of an error the cancel itself provoked.
    """
    job = await get_scan_job(session, slug, scan_id, job_id)
    if job.status not in (ScanJobStatus.pending.value, ScanJobStatus.running.value):
        raise HTTPException(
            status_code=409,
            detail=f"Scan job is not active (status: {job.status})",
        )

    if job.celery_task_id:
        try:
            from tripl.worker.celery_app import celery_app

            # Off the event loop, like every other broker call in this module: a
            # revoke is a synchronous kombu broadcast, and against a hung broker
            # an inline call holds the uvicorn thread for seconds. Harmless while
            # ``celery_task_id`` was almost always NULL — which is exactly what
            # tripl-0zpq.44 stopped being true, since the scan tasks now record
            # it and every Stop run reaches this branch.
            await dispatch(celery_app.control.revoke, job.celery_task_id)
        except Exception:  # noqa: BLE001 — revoke is best-effort; cooperative stop is the backstop
            logger.warning(
                "Failed to revoke celery task %s for scan job %s",
                job.celery_task_id,
                job.id,
                exc_info=True,
            )

    job.status = ScanJobStatus.cancelled.value
    job.completed_at = datetime.now(UTC)
    job.error_message = "Cancelled by user"
    await session.commit()
    await session.refresh(job)
    return job
