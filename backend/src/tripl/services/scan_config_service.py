"""Read-side extras for scan configs: metrics schedule and freshness (#269).

``scan_service`` owns the config CRUD; this module answers "when did metrics
collection last run for this scan, and when is it next due" for the scan detail
page, with the scheduler's own pure functions rather than a port of its rule,
and builds every ``ScanConfigResponse`` so each carries its computed
``freshness`` (which needs the project's settling window).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.event_metric import EventMetric
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.schemas.scan_config import (
    ScanConfigDetailResponse,
    ScanConfigResponse,
    SourceFreshness,
    SourceFreshnessItem,
)
from tripl.services import scan_service
from tripl.services.monitoring_utils import scan_interval_to_timedelta
from tripl.services.project_lookup import resolve_project_id
from tripl.services.source_freshness import DEFAULT_SETTLING, compute_freshness


async def project_settling_delay(session: AsyncSession, project_id: uuid.UUID) -> timedelta:
    """The project's ingestion-settling allowance, the default when unset."""
    minutes = await session.scalar(
        select(ProjectAnomalySettings.anomaly_ingestion_settling_minutes).where(
            ProjectAnomalySettings.project_id == project_id
        )
    )
    return DEFAULT_SETTLING if minutes is None else timedelta(minutes=minutes)


def _build_response[ResponseT: ScanConfigResponse](
    model: type[ResponseT], config: ScanConfig, freshness: SourceFreshness
) -> ResponseT:
    """``model`` from the ORM row plus its computed ``freshness``.

    Read field by field rather than ``model_validate(config)``: ``freshness`` is
    not a column, so attribute validation would find nothing there. Fields the
    row does not carry (the detail response's schedule) keep their defaults.
    """
    data: dict[str, object] = {
        name: getattr(config, name)
        for name in model.model_fields
        if name != "freshness" and hasattr(config, name)
    }
    data["freshness"] = freshness
    return model.model_validate(data)


async def scan_config_responses(
    session: AsyncSession,
    configs: list[ScanConfig],
    *,
    now: datetime | None = None,
) -> list[ScanConfigResponse]:
    """``ScanConfigResponse`` rows with freshness, one settling read per project."""
    moment = now or datetime.now(UTC)
    settling_by_project: dict[uuid.UUID, timedelta] = {}
    responses: list[ScanConfigResponse] = []
    for config in configs:
        settling = settling_by_project.get(config.project_id)
        if settling is None:
            settling = await project_settling_delay(session, config.project_id)
            settling_by_project[config.project_id] = settling
        freshness = compute_freshness(config, moment, settling=settling)
        responses.append(_build_response(ScanConfigResponse, config, freshness))
    return responses


async def scan_config_response(
    session: AsyncSession,
    config: ScanConfig,
    *,
    now: datetime | None = None,
) -> ScanConfigResponse:
    """One ``ScanConfigResponse`` with its freshness."""
    (response,) = await scan_config_responses(session, [config], now=now)
    return response


async def list_source_freshness(
    session: AsyncSession,
    slug: str,
    *,
    now: datetime | None = None,
) -> list[SourceFreshnessItem]:
    """Every scan config of the project with its freshness (Overview, source cards)."""
    project_id = await resolve_project_id(session, slug)
    result = await session.execute(
        select(ScanConfig)
        .where(ScanConfig.project_id == project_id)
        .order_by(ScanConfig.name, ScanConfig.id)
    )
    settling = await project_settling_delay(session, project_id)
    moment = now or datetime.now(UTC)
    return [
        SourceFreshnessItem(
            id=config.id,
            name=config.name,
            data_source_id=config.data_source_id,
            freshness=compute_freshness(config, moment, settling=settling),
        )
        for config in result.scalars().all()
    ]


async def get_scan_config_detail(
    session: AsyncSession,
    slug: str,
    scan_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> ScanConfigDetailResponse:
    """The config plus ``last_metrics_run_at`` / ``next_metrics_run_at``."""
    config = await scan_service.get_scan_config(session, slug, scan_id)
    moment = now or datetime.now(UTC)
    last_run_at, next_run_at = await _metrics_schedule(session, config, now=moment)
    freshness = compute_freshness(
        config, moment, settling=await project_settling_delay(session, config.project_id)
    )
    return _build_response(ScanConfigDetailResponse, config, freshness).model_copy(
        update={"last_metrics_run_at": last_run_at, "next_metrics_run_at": next_run_at}
    )


async def _metrics_schedule(
    session: AsyncSession,
    config: ScanConfig,
    *,
    now: datetime,
) -> tuple[datetime | None, datetime | None]:
    """``(last_metrics_run_at, next_metrics_run_at)`` for one scan config.

    The dispatcher collects only a config with an interval AND a time column
    (``check_metrics_due``), so either missing means no next run. The next run
    is the scheduler's bucket-half due check
    (``schedule.scan_config_collection_schedule``): the earliest moment the
    config is due, which the dispatcher may still hold back (a live job, the
    failure backoff, a demo's cooldown) — the same promise the monitoring
    drilldown's ``next_collection_at`` makes. Imported lazily, like
    ``metrics_service``'s scheduler read, to keep the request path out of the
    Celery import graph until it is used.
    """
    from tripl.worker.tasks.metrics.schedule import (
        scan_config_collection_progress,
        scan_config_collection_schedule,
    )
    from tripl.worker.tasks.metrics.tasks import _RECENT_JOB_SCAN_LIMIT

    rows = await session.execute(
        select(ScanJob.result_summary, ScanJob.completed_at)
        .where(
            ScanJob.scan_config_id == config.id,
            ScanJob.status == ScanJobStatus.completed.value,
        )
        .order_by(ScanJob.created_at.desc())
        .limit(_RECENT_JOB_SCAN_LIMIT)
    )
    last_run_at, watermark = scan_config_collection_progress(rows.tuples().all())

    delta = scan_interval_to_timedelta(config.interval)
    if delta is None or not config.time_column:
        return last_run_at, None

    # Bounded like every ``event_metrics`` read: a newest bucket older than two
    # intervals leaves the config due whatever its exact value, so the floor
    # changes no answer.
    last_bucket = await session.scalar(
        select(func.max(EventMetric.bucket)).where(
            EventMetric.scan_config_id == config.id,
            EventMetric.bucket >= now - 2 * delta,
        )
    )
    next_run_at, _due = scan_config_collection_schedule(
        last_bucket=last_bucket,
        watermark=watermark,
        delta=delta,
        now=now,
    )
    return last_run_at, next_run_at
