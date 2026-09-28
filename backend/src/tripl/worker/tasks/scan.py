"""Celery tasks for running data source scans.

The dry-run half ("what would this scan create?") lives in the sibling
``scan_dry_run`` module — split out for size in tripl-28g7, no behaviour change.
Its Celery task is still named ``tripl.worker.tasks.scan.dry_run_scan_config_async``
because the broker routes on that string.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl import realtime
from tripl.core.adapters.base import BaseAdapter, ColumnInfo
from tripl.core.analyzers.cardinality import (
    BreakdownAnalysis,
    analyze_cardinality,
    analyze_cardinality_grouped,
)
from tripl.core.analyzers.event_generator import (
    GenerationResult,
    generate_events,
    merge_existing_events_for_group_rules,
)
from tripl.core.analyzers.event_plan import breakdown_row_count
from tripl.core.analyzers.preview import build_json_paths_payload, build_preview_payload
from tripl.json_paths import group_json_value_paths
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.models.scan_preview_job import ScanPreviewJob
from tripl.observability.metrics import scan_runs_total
from tripl.services import app_settings_service
from tripl.worker.celery_app import celery_app
from tripl.worker.db import _build_adapter, _get_sync_session
from tripl.worker.plan_scope import main_branch_id
from tripl.worker.search_reindex import reindex_main_branch_from_worker
from tripl.worker.tasks._errors import NO_EVENT_NAMING_MSG, ScanError, user_facing_error
from tripl.worker.utils.event_types import ensure_event_type_with_fields
from tripl.worker.utils.job_status import (
    TERMINAL_SCAN_JOB_STATUSES,
    closed_by_someone_else,
    job_is_cancelled,
)
from tripl.worker.utils.query_windows import TimeWindow, resolve_lookback_window
from tripl.worker.utils.reserved_columns import reserved_catalog_columns
from tripl.worker.variable_sweep import retire_unused_variables, retired_details_line

logger = logging.getLogger(__name__)

# ``ScanError`` and ``user_facing_error`` now live in ``_errors`` so the metrics
# task can sanitise its user-facing fields with the same logic. The private
# alias is kept for backwards compatibility with existing imports/tests.
_user_facing_error = user_facing_error

__all__ = ["ScanError", "user_facing_error"]


def _publish_scan_job_event(
    session: Session, scan_config_id: str, job_id: str, status: str
) -> None:
    """Emit ``scan_job.updated`` on the project channel AFTER the status commit.

    Best-effort + no-op when Redis is off; a realtime failure must never fail the
    scan. The ScanConfig is already in the session identity map, so slug lookup is
    a cheap cached read.
    """
    config = session.get(ScanConfig, uuid.UUID(scan_config_id))
    if config is None:
        return
    project = session.get(Project, config.project_id)
    if project is None:
        return
    realtime.publish_project_event(
        project.id,
        project.slug,
        realtime.EVENT_SCAN_JOB_UPDATED,
        {"scan_config_id": scan_config_id, "job_id": job_id, "status": status},
    )


def _skip_terminal_job(
    task_name: str, job_id: str, status: str, scan_config_id: str
) -> dict[str, object]:
    """The return value for a job somebody else already closed before start.

    Same shape and same split as ``collect_metrics``: a user cancel is an
    expected outcome and reports ``cancelled``; a ``failed`` the stale reaper
    stamped, or a ``completed`` whose ack was lost under ``task_acks_late``, is a
    redelivery worth a warning and reports ``skipped``.
    """
    if status == ScanJobStatus.cancelled.value:
        logger.info("%s %s was cancelled before start; skipping", task_name, job_id)
        return {"cancelled": True, "job_status": status, "scan_config_id": scan_config_id}
    logger.warning("%s %s is already %s before start; skipping", task_name, job_id, status)
    return {"skipped": True, "job_status": status, "scan_config_id": scan_config_id}


def _task_id(task: object) -> str | None:
    """The Celery task id of the running request, or None outside a worker.

    Recorded on the job so ``cancel_scan_job`` can best-effort revoke a message
    that is still queued — the invariant ``ScanJob.celery_task_id`` documents and
    which, before tripl-0zpq.44, only ``collect_metrics`` honoured, leaving the
    revoke branch unreachable for every catalog run and event-group apply.
    """
    return getattr(getattr(task, "request", None), "id", None)


def _serialize_generation_result(result: GenerationResult) -> dict[str, object]:
    branch_id = None
    events: list[dict[str, object]] = []
    for identity, event in result.events_by_name.items():
        if branch_id is None and event.branch_id is not None:
            branch_id = str(event.branch_id)
        events.append(
            {
                "identity": identity,
                "event_id": str(event.id),
                "name": event.name,
                "source_name": event.source_name,
                "branch_id": str(event.branch_id) if event.branch_id is not None else None,
                "status": event.status,
                "metric_breakdown_columns": list(event.metric_breakdown_columns or []),
                "field_values": [
                    {
                        "field_definition_id": str(field_value.field_definition_id),
                        "value": field_value.value,
                    }
                    for field_value in event.field_values
                ],
            }
        )

    col_meta: dict[str, dict[str, object]] = {}
    for column, meta in result.col_meta.items():
        col_meta[column] = {
            key: value
            for key, value in meta.items()
            if key in {"is_json", "is_low", "template", "json_passthrough_paths"}
        }

    return {
        "columns_analyzed": result.columns_analyzed,
        "details": list(result.details),
        "event_type_id": str(result.event_type_id) if result.event_type_id is not None else None,
        "branch_id": branch_id,
        "col_meta": col_meta,
        "events": events,
    }


def _serialize_generation_snapshot(
    result: GenerationResult,
    *,
    group_results: dict[str, GenerationResult] | None = None,
) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "version": 1,
        "single_result": _serialize_generation_result(result),
    }
    if group_results:
        snapshot["group_results"] = {
            group_name: _serialize_generation_result(group_result)
            for group_name, group_result in group_results.items()
        }
    return snapshot


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.scan.run_scan",
    bind=True,
    max_retries=0,
)
def run_scan(self: object, scan_config_id: str, job_id: str) -> dict[str, object]:
    """Execute a data source scan: connect, analyze columns, detect variables, generate events."""
    session = _get_sync_session()
    adapter: BaseAdapter | None = None

    try:
        # Load scan job and config
        job = session.get(ScanJob, uuid.UUID(job_id))
        if job is None:
            msg = f"ScanJob {job_id} not found"
            raise ValueError(msg)

        # Trustworthy without a re-read: this is the session's first look at the
        # row and nothing has been committed yet. A job can reach a terminal
        # state while its message sits queued — the user stopped it, the stale
        # reaper stamped it failed, or its ``completed`` ack was lost — and
        # ``task_acks_late`` makes redelivery routine rather than a race. A
        # redelivered ``completed`` job matters here for the same reason it does
        # in ``collect_metrics``: re-running it re-queries the warehouse, rewrites
        # the plan, overwrites the first run's delta counters, and leaves the row
        # ``running`` while it still carries the first run's ``completed_at`` —
        # which ``_reject_if_already_running`` then reads as "a scan is already
        # running", 409-ing every user-triggered scan for the duration.
        if job.status in TERMINAL_SCAN_JOB_STATUSES:
            return _skip_terminal_job("run_scan", job_id, job.status, scan_config_id)

        config = session.get(ScanConfig, uuid.UUID(scan_config_id))
        if config is None:
            msg = f"ScanConfig {scan_config_id} not found"
            raise ValueError(msg)

        ds = session.get(DataSource, config.data_source_id)
        if ds is None:
            msg = f"DataSource for config {scan_config_id} not found"
            raise ValueError(msg)

        # Mark job as running
        job.status = ScanJobStatus.running.value
        job.started_at = datetime.now(UTC)
        job.celery_task_id = _task_id(self)
        session.commit()
        _publish_scan_job_event(session, scan_config_id, job_id, ScanJobStatus.running.value)

        # Build adapter and connect
        adapter = _build_adapter(ds)
        adapter.test_connection()

        # Get columns from base query, excluding the time column
        columns = adapter.get_columns(config.base_query)
        if config.time_column:
            columns = [c for c in columns if c.name != config.time_column]
        logger.info(f"Found {len(columns)} columns in base query")
        json_value_paths = group_json_value_paths(config.json_value_paths)
        # The project's organization's row-limit defaults, clamped to the
        # operator's ceiling (F20 PR9).
        runtime_config = app_settings_service.get_runtime_config_sync(
            session, org_id=app_settings_service.project_org_id_sync(session, config.project_id)
        )
        scan_row_limit = config.scan_row_limit or runtime_config.scan_row_limit_default
        scan_window = resolve_lookback_window(
            time_column=config.time_column,
            lookback_hours=config.scan_lookback_hours,
        )

        # Resolve event type: either from config or detect from event_type_column
        event_type_id = config.event_type_id
        logger.info(
            "event_type_id=%s, event_type_column=%r",
            event_type_id,
            config.event_type_column,
        )
        if event_type_id is None and config.event_type_column:
            # Event type column groups rows into different event types.
            # Use GROUPING SETS to get per-group cardinalities in one query.
            logger.info("Using grouped scan with GROUPING SETS")
            (
                result,
                group_results,
                scan_rows_processed,
                catalog_rows_scanned,
            ) = _scan_with_grouping(
                session,
                config.project_id,
                config,
                adapter,
                columns,
                scan_window=scan_window,
                row_limit=scan_row_limit,
            )
        elif event_type_id is not None:
            # Single event type scan — bulk cardinality (no grouping)
            analysis = analyze_cardinality(
                adapter,
                config.base_query,
                columns,
                threshold=config.cardinality_threshold,
                json_value_paths=json_value_paths,
                time_column=config.time_column if scan_window else None,
                time_from=scan_window[0] if scan_window else None,
                time_to=scan_window[1] if scan_window else None,
                row_limit=scan_row_limit,
            )
            if analysis.row_limit_reached:
                msg = (
                    "The scan query reached the configured row limit "
                    f"({scan_row_limit}); increase scan_row_limit to avoid partial generation"
                )
                raise ScanError(msg)

            event_type = session.get(EventType, event_type_id)
            if event_type is None:
                msg = f"EventType {event_type_id} not found"
                raise ValueError(msg)

            field_defs = {fd.name: fd for fd in event_type.field_definitions}
            result = generate_events(
                session,
                config.project_id,
                event_type_id,
                analysis,
                field_defs,
                cardinality_threshold=config.cardinality_threshold,
                event_type_column=config.event_type_column,
                time_column=config.time_column,
                event_name_format=config.event_name_format,
                event_group_rules=config.event_group_rules,
                reserved_columns=reserved_catalog_columns(config),
                scan_config_id=config.id,
            )
            group_results = None
            scan_rows_processed = len(analysis.rows)
            catalog_rows_scanned = _warehouse_rows([analysis])
        else:
            raise ScanError(NO_EVENT_NAMING_MSG)

        # The LAST moment at which a stop is still FREE. Everything the run
        # generated is pending in this session, so the rollback is a real undo;
        # one line later it is durable and the sweep and the reindex follow. A
        # stop landing after this point is still honoured on the job row — the
        # close-out below re-reads the status and leaves a terminal one alone
        # (tripl-0zpq.44) — but it cannot un-write the catalog, which is why the
        # checkpoint is here and not next to the ``completed`` stamp.
        #
        # This is only a true undo because neither ``generate_events`` nor
        # ``merge_existing_events_for_group_rules`` commits internally. A
        # generator that starts committing would turn this guard into a partial
        # catalog.
        if job_is_cancelled(session, job.id):
            session.rollback()
            logger.info("run_scan for %s cancelled mid-run; discarding generation", scan_config_id)
            scan_runs_total.labels(status="cancelled").inc()
            return {"cancelled": True, "scan_config_id": scan_config_id}

        session.commit()
        # Scans mint variables and, before this, never retired one, so a project
        # whose warehouse holds a JSON column keyed by user-typed text grew a
        # permanent row per key (tripl-10h4). Sweeping here — after the commit,
        # before the reindex — keeps the catalog self-healing instead of relying
        # on somebody remembering the danger-zone button. It does not undo the
        # run above: a path enters ``all_paths`` only by appearing in a row, and
        # that row's event stores ``${col.path}``, so the reference check keeps
        # what was just minted. The one exception is a path carried solely by an
        # ARCHIVED event, whose field values a scan deliberately does not
        # rewrite — that variable is minted and swept in the same run, which is
        # the right outcome for a row nothing live refers to.
        variables_retired = retire_unused_variables(
            session,
            project_id=config.project_id,
            branch_id=main_branch_id(session, config.project_id),
        )
        if variables_retired:
            result.details.append(retired_details_line(variables_retired))
        reindex_main_branch_from_worker(session, config.project_id)

        # Mark job as completed — unless somebody closed it while the sweep and
        # the reindex above were running. The stop that landed there could not
        # undo the commit at the checkpoint, but the verdict on the ROW is the
        # closer's: re-opening it as ``completed`` would leave a Succeeded run
        # carrying "Cancelled by user" (tripl-0zpq.44). Only ``status`` and
        # ``completed_at`` are withheld; ``result_summary`` is still recorded
        # below so the run report survives. Same rule, same helper, as
        # ``collect_metrics``.
        closed_status = closed_by_someone_else(session, job.id)
        if closed_status is None:
            job.status = ScanJobStatus.completed.value
            job.completed_at = datetime.now(UTC)
        else:
            logger.warning(
                "run_scan %s finished but the job is already %s; leaving that status in place",
                job_id,
                closed_status,
            )
        job.result_summary = {
            "events_created": result.events_created,
            "events_skipped": result.events_skipped,
            "events_grouped": result.events_grouped,
            "events_merged": result.events_merged,
            "variables_created": result.variables_created,
            # Emitted UNCONDITIONALLY, and a literal 0 is honest here: the sweep
            # above is not gated on anything, so a manual scan always sweeps and
            # 0 means "swept, found nothing". The scheduled path says the same
            # thing on every run but a REPLAY, which sweeps nothing and omits the
            # key precisely so its 0 cannot be read as "found nothing" — the one
            # difference there is that a run with no declared lookback judges
            # only the JSON-derived variables, so its 0 speaks for those alone
            # (``collect_metrics``). Without the key the run
            # reported the sweep only in ``details``, so the frontend's
            # "Variables retired" card — guarded on ``!= null`` — was structurally
            # unreachable for the one run type a user triggers deliberately, and
            # the docs telling the reader to read it against "Variables created"
            # silently failed there.
            "variables_retired": variables_retired,
            "columns_analyzed": result.columns_analyzed,
            "scan_row_limit": scan_row_limit,
            "scan_lookback_hours": config.scan_lookback_hours,
            "scan_window_from": scan_window[0].isoformat() if scan_window else None,
            "scan_window_to": scan_window[1].isoformat() if scan_window else None,
            "scan_rows_processed": scan_rows_processed,
            # The warehouse rows behind those combinations: the sum of the
            # ``_cnt`` every breakdown row carries, the population the dry run
            # reports as "sampled rows". ``scan_rows_processed`` alone put a
            # catalog run ~180x below its own dry run (DA-4). Omitted when a row
            # carries no count, rather than guessed.
            **(
                {"catalog_rows_scanned": catalog_rows_scanned}
                if catalog_rows_scanned is not None
                else {}
            ),
            "details": result.details,
            "generation_snapshot": _serialize_generation_snapshot(
                result,
                group_results=group_results,
            ),
        }
        session.commit()

        logger.info(
            f"Scan completed: {result.events_created} events created, "
            f"{result.events_skipped} skipped, {result.variables_created} variables created"
        )
        # Labelled and published with what the row actually SAYS, not with what
        # this task set out to write: a stop honoured above must not show up as
        # a completed run on the dashboard or push a ``completed`` event to a UI
        # that is already rendering the job as cancelled.
        final_status = closed_status or ScanJobStatus.completed.value
        scan_runs_total.labels(status=final_status).inc()
        _publish_scan_job_event(session, scan_config_id, job_id, final_status)
        return job.result_summary

    except Exception as e:
        logger.exception(f"Scan failed: {e}")
        session.rollback()
        failure_status = ScanJobStatus.failed.value
        try:
            job = session.get(ScanJob, uuid.UUID(job_id))
            if job:
                # The same rule as the success path: a row somebody already
                # closed keeps the status, ``completed_at`` and message its
                # closer wrote. A user who pressed Stop and then watched the run
                # die of the cancel must not be shown "Scan failed due to an
                # internal error" in place of their own cancellation.
                closed_status = closed_by_someone_else(session, job.id)
                if closed_status is None:
                    job.status = ScanJobStatus.failed.value
                    job.completed_at = datetime.now(UTC)
                    job.error_message = user_facing_error(e)
                else:
                    failure_status = closed_status
                    logger.warning(
                        "run_scan %s failed but the job is already %s; "
                        "leaving that status in place",
                        job_id,
                        closed_status,
                    )
                session.commit()
                _publish_scan_job_event(session, scan_config_id, job_id, failure_status)
        except Exception:
            logger.exception("Failed to update job status after error")
        scan_runs_total.labels(status=failure_status).inc()
        raise
    finally:
        if adapter is not None:
            adapter.close()
        session.close()


def _scan_with_grouping(
    session: Session,
    project_id: uuid.UUID,
    config: ScanConfig,
    adapter: BaseAdapter,
    columns: list[ColumnInfo],
    scan_window: TimeWindow | None,
    row_limit: int,
) -> tuple[GenerationResult, dict[str, GenerationResult], int, int | None]:
    """Handle scans where event_type_column groups rows into different event types.

    Returns ``(combined, per_group, scan_rows_processed, catalog_rows_scanned)``:
    the distinct combinations the breakdown returned and the warehouse rows
    behind them (``None`` when a row carries no count).

    Uses GROUPING SETS to compute per-group cardinalities in a single query,
    so a column that is high-cardinality globally may be low-cardinality
    inside a specific group (e.g. event.action within a given event.category).
    """
    col_name = config.event_type_column
    if col_name is None:
        msg = "event_type_column is required for grouped scanning"
        raise ValueError(msg)

    group_values, grouped_results = analyze_cardinality_grouped(
        adapter,
        config.base_query,
        columns,
        group_column=col_name,
        threshold=config.cardinality_threshold,
        json_value_paths=group_json_value_paths(config.json_value_paths),
        time_column=config.time_column if scan_window else None,
        time_from=scan_window[0] if scan_window else None,
        time_to=scan_window[1] if scan_window else None,
        row_limit=row_limit,
    )
    if any(analysis.row_limit_reached for analysis in grouped_results.values()):
        msg = (
            "Grouped scan query reached configured row limit "
            f"({row_limit}); increase scan_row_limit to avoid partial generation"
        )
        raise ScanError(msg)
    logger.info(f"Grouped scan: {len(group_values)} groups found for {col_name!r}")
    scan_rows_processed = sum(len(analysis.rows) for analysis in grouped_results.values())
    catalog_rows_scanned = _warehouse_rows(grouped_results.values())

    combined = GenerationResult()
    per_group_results: dict[str, GenerationResult] = {}

    # The SAME resolver the scheduled catalog sync uses (``catalog_sync`` ->
    # ``ensure_event_type_with_fields``), and it creates rather than skips. A
    # manual run used to only LOOK UP the event type by name and drop the whole
    # group when it was missing, which made a Catalog-only config — the mode
    # whose entire promise is "adds events and fields to your tracking plan when
    # you run it", and which by definition never reaches the scheduler — create
    # zero events forever, while the dry run promised the type "would be added"
    # (tripl-0zpq.45). It also handed ``generate_events`` only the
    # already-declared fields, so a new warehouse column stayed dropped from
    # event identities until the next scheduled tick declared it.
    #
    # ``skip_cols`` is the same set this function already passes to
    # ``generate_events`` as ``reserved_columns``, which reproduces the sync's
    # invariant exactly: a column denied a FieldDefinition is the same column the
    # generator stays quiet about. The resolver scopes to the main plan itself,
    # so no branch lookup is needed here.
    skip_cols = reserved_catalog_columns(config)

    for et_value in group_values:
        et = ensure_event_type_with_fields(session, project_id, et_value, columns, skip_cols)

        field_defs = {fd.name: fd for fd in et.field_definitions}
        # Use per-group cardinality results for this event type
        per_group_analysis = grouped_results[et_value]
        result = generate_events(
            session,
            project_id,
            et.id,
            per_group_analysis,
            field_defs,
            cardinality_threshold=config.cardinality_threshold,
            event_type_column=col_name,
            time_column=config.time_column,
            event_name_format=config.event_name_format,
            event_group_rules=config.event_group_rules,
            reserved_columns=skip_cols,
            scan_config_id=config.id,
        )
        combined.events_created += result.events_created
        combined.events_skipped += result.events_skipped
        combined.events_grouped += result.events_grouped
        combined.events_merged += result.events_merged
        combined.variables_created += result.variables_created
        combined.columns_analyzed = max(combined.columns_analyzed, result.columns_analyzed)
        combined.details.extend(result.details)
        per_group_results[et_value] = result

    return combined, per_group_results, scan_rows_processed, catalog_rows_scanned


def _warehouse_rows(analyses: Iterable[BreakdownAnalysis]) -> int | None:
    """Warehouse rows behind a breakdown: the sum of each row's ``_cnt``.

    ``None`` when any row carries no count column (``breakdown_row_count``'s
    contract), so the run report never prints a column value as a row count.
    """
    total = 0
    for analysis in analyses:
        for row in analysis.rows:
            count = breakdown_row_count(analysis, row)
            if count is None:
                return None
            total += count
    return total


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.scan.apply_event_groups",
    bind=True,
    max_retries=0,
)
def apply_event_groups(self: object, scan_config_id: str, job_id: str) -> dict[str, object]:
    """Apply saved scan group rules to existing catalog events."""
    session = _get_sync_session()
    try:
        job = session.get(ScanJob, uuid.UUID(job_id))
        if job is None:
            msg = f"ScanJob {job_id} not found"
            raise ValueError(msg)

        # See ``run_scan``: a stopped, reaped or already-``completed`` job must
        # not be resurrected by an ``acks_late`` redelivery, and this pass
        # DELETES the events it folds.
        if job.status in TERMINAL_SCAN_JOB_STATUSES:
            return _skip_terminal_job("apply_event_groups", job_id, job.status, scan_config_id)

        config = session.get(ScanConfig, uuid.UUID(scan_config_id))
        if config is None:
            msg = f"ScanConfig {scan_config_id} not found"
            raise ValueError(msg)
        if not config.event_group_rules:
            msg = "The scan config has no event group rules"
            raise ScanError(msg)

        job.status = ScanJobStatus.running.value
        job.started_at = datetime.now(UTC)
        job.celery_task_id = _task_id(self)
        session.commit()

        # Apply-groups MUTATES the catalog — it rewrites rows and DELETES the
        # sources it folds — so like every other scan path it stays on the main
        # plan. A working branch deep-copies every EventType, FieldDefinition and
        # Event under fresh ids, so a DISTINCT over ``Event.event_type_id`` alone
        # returned each open branch's private copies too, and the merge then
        # deleted the branch author's events, analyst edits included, minting a
        # group event there that only main's reindex would ever have indexed
        # (tripl-0zpq.43). ``Event.branch_id`` states the intent;
        # ``EventType.branch_id`` is the column the downstream load actually keys
        # on, since the merge re-selects by ``event_type_id``.
        #
        # NOT narrowed by this: on main the pass still folds every event type
        # that has any event, regardless of which scan config produced it.
        # ``Event`` carries no ``scan_config_id`` and this task has no warehouse
        # adapter, so there is no honest way to learn which event types are this
        # config's; that needs a product decision, not a query change.
        plan_branch = main_branch_id(session, config.project_id)
        if config.event_type_id is not None:
            event_type_ids = [config.event_type_id]
        else:
            event_type_ids = list(
                session.execute(
                    select(Event.event_type_id)
                    .join(EventType, EventType.id == Event.event_type_id)
                    .where(
                        Event.project_id == config.project_id,
                        Event.branch_id == plan_branch,
                        EventType.branch_id == plan_branch,
                    )
                    .distinct()
                ).scalars()
            )

        events_merged = merge_existing_events_for_group_rules(
            session,
            project_id=config.project_id,
            event_type_ids=event_type_ids,
            event_group_rules=config.event_group_rules,
            # The fold that combines two stored contexts demotes on this bound,
            # so it has to be the project's own. The parameter defaults to the
            # column default, which would silently be right for everyone who
            # never moved it and wrong for everyone who did (tripl-3rex).
            cardinality_threshold=config.cardinality_threshold,
        )

        # Same placement and same reasoning as ``run_scan``: the fold is still
        # pending in this session, so a stop here discards it whole. One line
        # later the deletes are durable and the reindex has run — a stop landing
        # then is still honoured on the job row by the close-out below, but it
        # cannot bring the folded sources back.
        if job_is_cancelled(session, job.id):
            session.rollback()
            logger.info(
                "apply_event_groups for %s cancelled mid-run; discarding the merge",
                scan_config_id,
            )
            return {"cancelled": True, "scan_config_id": scan_config_id}

        session.commit()
        # AFTER the commit, exactly as run_scan does and for the same reason:
        # the reindex opens its own connection and cannot see this session's
        # uncommitted work, so running it earlier would index the pre-merge
        # state. Without it the group event a merge had just created carried no
        # search document until some unrelated later task happened to reindex
        # the branch — this was the one catalog-mutating task that never did
        # (tripl-68l3). The source event's documents go with the FK cascade, so
        # it is the survivor's missing row this repairs.
        reindex_main_branch_from_worker(session, config.project_id)

        # Same guard, same helper and same reason as ``run_scan``'s close-out:
        # the reindex above can take seconds, and a Stop that lands in that
        # window owns the row's verdict even though the fold is already durable.
        closed_status = closed_by_someone_else(session, job.id)
        if closed_status is None:
            job.status = ScanJobStatus.completed.value
            job.completed_at = datetime.now(UTC)
        else:
            logger.warning(
                "apply_event_groups %s finished but the job is already %s; "
                "leaving that status in place",
                job_id,
                closed_status,
            )
        job.result_summary = {
            "mode": "event_groups_apply",
            "events_merged": events_merged,
            "event_types_processed": len(event_type_ids),
            "event_group_rules": len(config.event_group_rules),
            "details": [
                (
                    f"Applied {len(config.event_group_rules)} event group rule(s) "
                    f"to {len(event_type_ids)} event type(s); merged {events_merged} event(s)"
                )
            ],
        }
        session.commit()
        _publish_scan_job_event(
            session,
            scan_config_id,
            job_id,
            closed_status or ScanJobStatus.completed.value,
        )
        return job.result_summary
    except Exception as e:
        logger.exception(f"Apply event groups failed: {e}")
        session.rollback()
        try:
            job = session.get(ScanJob, uuid.UUID(job_id))
            if job:
                closed_status = closed_by_someone_else(session, job.id)
                if closed_status is None:
                    job.status = ScanJobStatus.failed.value
                    job.completed_at = datetime.now(UTC)
                    job.error_message = user_facing_error(e)
                else:
                    logger.warning(
                        "apply_event_groups %s failed but the job is already %s; "
                        "leaving that status in place",
                        job_id,
                        closed_status,
                    )
                session.commit()
                _publish_scan_job_event(
                    session,
                    scan_config_id,
                    job_id,
                    closed_status or ScanJobStatus.failed.value,
                )
        except Exception:
            logger.exception("Failed to update event group apply job status after error")
        raise
    finally:
        session.close()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.scan.preview_scan_config_async",
    bind=True,
    max_retries=0,
)
def preview_scan_config_async(self: object, job_id: str) -> dict[str, object]:
    """Compute a scan-config preview for an unsaved draft, off the request path."""
    session = _get_sync_session()
    adapter: BaseAdapter | None = None
    try:
        job = session.get(ScanPreviewJob, uuid.UUID(job_id))
        if job is None:
            msg = f"ScanPreviewJob {job_id} not found"
            raise ValueError(msg)

        ds = session.get(DataSource, job.data_source_id)
        if ds is None:
            msg = f"DataSource {job.data_source_id} not found"
            raise ValueError(msg)

        job.status = ScanJobStatus.running.value
        job.started_at = datetime.now(UTC)
        session.commit()

        adapter = _build_adapter(ds)
        preview_window = resolve_lookback_window(
            time_column=job.time_column,
            lookback_hours=job.scan_lookback_hours,
        )
        if job.include_json_paths:
            # Heavy half: enumerate nested JSON keys for the source query.
            payload = build_json_paths_payload(
                adapter,
                job.base_query,
                list(job.json_value_paths or []),
                time_column=job.time_column if preview_window else None,
                time_from=preview_window[0] if preview_window else None,
                time_to=preview_window[1] if preview_window else None,
            )
        else:
            # Fast half: columns + sample rows only, no JSON path discovery.
            payload = build_preview_payload(
                adapter,
                job.base_query,
                job.row_limit,
                time_column=job.time_column if preview_window else None,
                time_from=preview_window[0] if preview_window else None,
                time_to=preview_window[1] if preview_window else None,
            )

        job.status = ScanJobStatus.completed.value
        job.completed_at = datetime.now(UTC)
        job.result_summary = payload
        session.commit()
        return payload
    except Exception as e:
        logger.exception(f"Scan preview failed: {e}")
        session.rollback()
        try:
            job = session.get(ScanPreviewJob, uuid.UUID(job_id))
            if job:
                job.status = ScanJobStatus.failed.value
                job.completed_at = datetime.now(UTC)
                job.error_message = user_facing_error(e)
                session.commit()
        except Exception:
            logger.exception("Failed to update scan preview job status after error")
        raise
    finally:
        if adapter is not None:
            adapter.close()
        session.close()
