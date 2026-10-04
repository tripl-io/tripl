"""Celery task for the scan dry run: "what would this scan create?".

Split out of ``worker.tasks.scan`` purely for size — that module
crossed the repo's 800-line guideline and the dry-run half is self-contained
behind one entry point, ``build_dry_run_payload``.

Two things did NOT move and must not:

* The Celery task name is still ``tripl.worker.tasks.scan.dry_run_scan_config_async``.
  It is what the broker routes on, so renaming it would strand every in-flight
  job queued by a running deployment. The name is a wire identifier, not a
  filename.
* ``_get_sync_session`` and ``_build_adapter`` stay imported at MODULE level
  here, because that is the namespace ``dry_run_scan_config_async.run`` closes
  over and the tests patch through ``run.__globals__``. Moving either behind a
  function-local import would silently stop the patch taking effect and point
  the test suite at a real database.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.core.adapters.base import BaseAdapter, ColumnInfo
from tripl.core.analyzers.cardinality import (
    BreakdownAnalysis,
    analyze_cardinality,
    analyze_cardinality_grouped,
)
from tripl.core.analyzers.event_plan import (
    DEFAULT_MAX_EVENTS,
    breakdown_row_count,
    plan_events,
    unnamed_skip_detail,
)
from tripl.core.json_string_columns import scan_source_query
from tripl.core.name_template import NameFormatError
from tripl.core.scan_setup_preset import PRESET_EVENT_TYPE_NAME, is_event_properties_preset
from tripl.core.warehouse_types import is_complex_type
from tripl.json_paths import group_json_value_paths
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_dry_run_job import ScanDryRunJob
from tripl.models.scan_job import ScanJobStatus
from tripl.worker.celery_app import celery_app
from tripl.worker.db import _build_adapter, _get_sync_session
from tripl.worker.plan_scope import main_branch_id
from tripl.worker.tasks._errors import NO_EVENT_NAMING_MSG, ScanError, user_facing_error
from tripl.worker.utils.event_types import event_type_name_rejection
from tripl.worker.utils.name_warnings import dry_run_name_warnings
from tripl.worker.utils.query_windows import TimeWindow, resolve_lookback_window
from tripl.worker.utils.reserved_columns import reserved_catalog_columns
from tripl.worker.utils.scan_preset import preset_scan_columns

logger = logging.getLogger(__name__)

# How many refused group values are spelled out before the rest are counted. A
# grouping column pointed at a URL or a free-text label can refuse hundreds, and
# a preview whose ``errors`` panel is a thousand lines long says less than one
# that names three and totals the rest. The FIRST is always spelled out, and
# that is the load-bearing one: group order is identical on both sides, so the
# run raises on exactly that value and persists exactly that sentence.
_MAX_REFUSAL_ERRORS = 3


@dataclass
class _DryRunTarget:
    """One event type a dry-run would write into, and the rows that reach it."""

    name: str
    event_type: EventType | None
    analysis: BreakdownAnalysis
    # True on the grouped path only: BOTH grouped runners — the manual
    # ``_scan_with_grouping`` and the scheduled ``catalog_sync`` — go through
    # ``ensure_event_type_with_fields``, which auto-creates the event type and a
    # FieldDefinition per unreserved column, so those fields WOULD exist by the
    # time events are generated. The single-event-type path creates nothing, so a
    # column with no field definition really is dropped and must be reported as
    # unmapped instead of quietly folded into an event name.
    #
    # This flag was right before the manual path was: the dry run
    # promised a missing event type "would be added" while a real manual run
    # skipped the group outright. Nothing here changed to close that gap — the
    # runner did.
    may_create_fields: bool
    # The only columns a field may be created for, when narrower than "every
    # unreserved column": the preset creates the event and properties fields
    # alone (``ensure_preset_event_type``).
    creatable_columns: frozenset[str] | None = None


def _dry_run_targets(
    session: Session,
    config: ScanConfig,
    adapter: BaseAdapter,
    columns: list[ColumnInfo],
    *,
    scan_window: TimeWindow | None,
    row_limit: int,
) -> tuple[list[_DryRunTarget], list[str]]:
    """Resolve the event type(s) exactly the way a real run resolves them.

    Returns the targets a run would write into, plus the reason for each group
    value it would REFUSE — see ``event_type_name_rejection``. The refusals are
    returned rather than raised because a dry run's job is to report, not to
    fail; ``build_dry_run_payload`` files them under ``errors`` exactly as it
    already files a ``NameFormatError``.
    """
    json_value_paths = group_json_value_paths(config.json_value_paths)
    source = scan_source_query(adapter, config)
    common = {
        "threshold": config.cardinality_threshold,
        "json_value_paths": json_value_paths,
        "time_column": config.time_column if scan_window else None,
        "time_from": scan_window[0] if scan_window else None,
        "time_to": scan_window[1] if scan_window else None,
        "row_limit": row_limit,
    }

    if is_event_properties_preset(config.setup_preset):
        # The run files the events under the chosen event type, or finds or
        # creates the preset's own, and gives it the two fields
        # (``ensure_preset_event_type``). A draft may name no event type yet: the
        # API picks the preset's one when the scan is saved.
        analysis = analyze_cardinality(adapter, source, columns, **common)  # type: ignore[arg-type]
        preset_type = (
            session.get(EventType, config.event_type_id)
            if config.event_type_id is not None
            else session.execute(
                select(EventType).where(
                    EventType.project_id == config.project_id,
                    EventType.branch_id == main_branch_id(session, config.project_id),
                    EventType.name == PRESET_EVENT_TYPE_NAME,
                )
            ).scalar_one_or_none()
        )
        return (
            [
                _DryRunTarget(
                    name=preset_type.name if preset_type is not None else PRESET_EVENT_TYPE_NAME,
                    event_type=preset_type,
                    analysis=analysis,
                    may_create_fields=True,
                    creatable_columns=frozenset(
                        column
                        for column in (config.event_name_column, config.properties_column)
                        if column
                    ),
                )
            ],
            [],
        )

    if config.event_type_id is not None:
        analysis = analyze_cardinality(adapter, source, columns, **common)  # type: ignore[arg-type]
        event_type = session.get(EventType, config.event_type_id)
        if event_type is None:
            msg = f"EventType {config.event_type_id} not found"
            raise ValueError(msg)
        # No refusal on this path: the event type already exists and was named
        # by a person through the API, which enforces the same bound.
        return (
            [
                _DryRunTarget(
                    name=event_type.name,
                    event_type=event_type,
                    analysis=analysis,
                    may_create_fields=False,
                )
            ],
            [],
        )

    if not config.event_type_column:
        raise ScanError(NO_EVENT_NAMING_MSG)

    group_values, grouped = analyze_cardinality_grouped(
        adapter,
        source,
        columns,
        group_column=config.event_type_column,
        **common,  # type: ignore[arg-type]
    )
    plan_branch = main_branch_id(session, config.project_id)
    targets: list[_DryRunTarget] = []
    refusals: list[str] = []
    for value in group_values:
        # The SAME predicate ``ensure_event_type_with_fields`` guards on, so the
        # preview and the run reach the same verdict for every group value. The
        # gap this closes is the mirror of an earlier one: there the dry run
        # promised a type the manual run skipped; here it would promise a type
        # the run REFUSES — a blank one, or one too long for ``event_types.name``
        # — and then the whole job fails on a value the preview called fine.
        #
        # Refused values are dropped from ``targets`` outright, so they raise no
        # "would be added" warning, contribute no events, and count toward
        # neither ``sampled_rows`` nor ``breakdown_combinations``. Nothing about
        # them would be created, so nothing about them is promised.
        rejection = event_type_name_rejection(value)
        if rejection is not None:
            refusals.append(rejection)
            continue
        event_type = session.execute(
            select(EventType).where(
                EventType.project_id == config.project_id,
                EventType.branch_id == plan_branch,
                EventType.name == value,
            )
        ).scalar_one_or_none()
        targets.append(
            _DryRunTarget(
                name=value,
                event_type=event_type,
                analysis=grouped[value],
                may_create_fields=True,
            )
        )
    return targets, refusals


def _existing_event_identities(
    session: Session, project_id: uuid.UUID, event_type_id: uuid.UUID
) -> set[str]:
    """Scan identities already in the plan for this event type.

    Keyed on ``source_name`` for the same reason ``generate_events`` dedups on
    it: a user may rename ``name`` freely, and matching on the display name would
    report an already-catalogued event as new.
    """
    branch = main_branch_id(session, project_id)
    query = select(Event.source_name, Event.name).where(
        Event.project_id == project_id,
        Event.event_type_id == event_type_id,
    )
    if branch is not None:
        query = query.where(Event.branch_id == branch)
    return {
        source_name or name for source_name, name in session.execute(query) if (source_name or name)
    }


def build_dry_run_payload(
    session: Session,
    adapter: BaseAdapter,
    config: ScanConfig,
    *,
    sample_row_limit: int,
) -> dict[str, object]:
    """Answer "what would this scan create?" without writing anything.

    Every event name here comes out of ``plan_events`` — the same function
    ``generate_events`` calls — so the preview cannot promise a name a run would
    not produce. Three separate partialities are echoed rather than smoothed
    over: the lookback window, the sample cap, and the event cap. None of them is
    extrapolated to a table-wide total.
    """
    columns = adapter.get_columns(scan_source_query(adapter, config))
    if config.time_column:
        columns = [c for c in columns if c.name != config.time_column]
    # The preset reads only its own columns, as the run does; a missing or
    # non-JSON column fails the dry run with the run's own message.
    columns = preset_scan_columns(config, columns)
    scan_window = resolve_lookback_window(
        time_column=config.time_column,
        lookback_hours=config.scan_lookback_hours,
    )
    # Computed on the DRAFT and passed down: ``core`` must never import
    # ``worker``, and re-deriving the reserved set is what took a production scan
    # down for 200 consecutive runs.
    skip_cols = reserved_catalog_columns(config)

    targets, refusals = _dry_run_targets(
        session,
        config,
        adapter,
        columns,
        scan_window=scan_window,
        row_limit=sample_row_limit,
    )

    # Keyed on ``(event type name, scan identity)``, never on the identity alone.
    # A run creates one Event PER EVENT TYPE — ``generate_events`` dedups inside
    # an ``existing_by_identity`` scoped to a single ``event_type_id`` — so two
    # groups that both produce the name ``home`` are two Events, not one. Merging
    # them here would undercount the headline and, worse, union the two event
    # types' existing identities, labelling a genuinely new event "already in
    # your plan".
    counts_by_identity: dict[tuple[str, str], int] = {}
    rule_by_identity: dict[tuple[str, str], str] = {}
    display_by_identity: dict[tuple[str, str], str] = {}
    existing_identities: set[tuple[str, str]] = set()
    fields: list[dict[str, object]] = []
    templated: dict[str, dict[str, object]] = {}
    warnings: list[str] = []
    errors: list[str] = []
    known_field_names: set[str] = set()
    max_events_reached = False
    unnamed_total = 0

    # First, and as errors rather than warnings: a refused value is not a
    # partiality in the preview, it is a run that will not finish.
    errors.extend(refusals[:_MAX_REFUSAL_ERRORS])
    if len(refusals) > _MAX_REFUSAL_ERRORS:
        extra = len(refusals) - _MAX_REFUSAL_ERRORS
        noun = "value" if extra == 1 else "values"
        errors.append(f"{extra} further Event type column {noun} cannot name an event type either.")

    for target in targets:
        existing_fds = (
            {fd.name: fd.id for fd in target.event_type.field_definitions}
            if target.event_type is not None
            else {}
        )
        field_ids: dict[str, uuid.UUID] = dict(existing_fds)
        if target.event_type is None:
            warnings.append(
                f"Event type {target.name!r} is not in your plan yet and would be added"
            )
        if target.may_create_fields:
            for column in columns:
                if column.name in skip_cols or column.name in field_ids:
                    continue
                if (
                    target.creatable_columns is not None
                    and column.name not in target.creatable_columns
                ):
                    continue
                # A hypothetical id: nothing is written, but the planner keys
                # field values on one, and a column with no id is dropped from
                # the event name entirely.
                field_ids[column.name] = uuid.uuid4()
                fields.append(
                    {
                        "name": column.name,
                        "type": "json" if is_complex_type(column.type_name) else "string",
                        "status": "new",
                        "event_type": target.name,
                    }
                )
        known_field_names |= set(field_ids)

        try:
            plan = plan_events(
                target.analysis,
                field_ids,
                cardinality_threshold=config.cardinality_threshold,
                event_type_column=config.event_type_column,
                time_column=config.time_column,
                event_name_format=config.event_name_format,
                event_group_rules=config.event_group_rules,
                reserved_columns=skip_cols,
                max_events=DEFAULT_MAX_EVENTS,
            )
        except NameFormatError as exc:
            # Deliberately not fatal. Surfacing the unknown key here, instead of
            # after 200 failed production runs, is the highest-value thing this
            # endpoint does; failing the job would hide it behind a
            # generic error banner.
            errors.append(str(exc))
            continue

        # The skip line is per-PLAN and a grouped scan plans once per event type,
        # so five groups each dropping one row printed "Skipped 1 row whose
        # derived event name was empty" five times. An operator reading that
        # panel concludes five separate one-row problems, not the five rows that
        # were actually dropped. Withhold each plan's own line
        # here and say the total once, below.
        #
        # ``generate_events``' run report keeps the per-plan line on purpose: it
        # renders ``details`` as a per-event-type Log, where one line per type is
        # the established shape and the attribution is the point.
        skip_line = unnamed_skip_detail(plan.events_unnamed) if plan.events_unnamed else None
        unnamed_total += plan.events_unnamed
        warnings.extend(detail for detail in plan.details if detail != skip_line)
        max_events_reached = max_events_reached or plan.truncated
        for col_name, meta in plan.col_meta.items():
            if meta.get("is_json") or meta.get("is_low") or col_name in templated:
                continue
            card = target.analysis.results.get(col_name)
            templated[col_name] = {
                "column": col_name,
                "distinct_values": card.count if card is not None else 0,
                "threshold": config.cardinality_threshold,
            }

        if target.event_type is not None:
            existing_identities |= {
                (target.name, identity)
                for identity in _existing_event_identities(
                    session, config.project_id, target.event_type.id
                )
            }
        for planned in plan.events:
            key = (target.name, planned.name)
            counts_by_identity[key] = counts_by_identity.get(key, 0) + (
                planned.row_count if planned.row_count is not None else 1
            )
            display_by_identity.setdefault(key, planned.name)
            if planned.matched_rule_name is not None:
                rule_by_identity.setdefault(key, planned.matched_rule_name)

    if unnamed_total:
        warnings.append(unnamed_skip_detail(unnamed_total))

    breakdown_combinations = sum(len(t.analysis.rows) for t in targets)
    sampled_rows = sum(
        breakdown_row_count(t.analysis, row) or 1 for t in targets for row in t.analysis.rows
    )
    sample_is_complete = not any(t.analysis.row_limit_reached for t in targets)
    # "exact" needs BOTH: a complete sample AND no lookback window. A window is a
    # second, independent partiality — an event absent from the last 24h is not
    # an event that will not be created.
    count_confidence = "exact" if sample_is_complete and scan_window is None else "sampled"

    # GH #265: combinatorial explosions and near-duplicates among the names a
    # run would CREATE. Best-effort — it returns [] rather than raise — and kept
    # out of ``warnings`` so that list stays the run's own partialities.
    new_names_by_type: dict[str, list[str]] = {}
    for key in counts_by_identity:
        if key not in existing_identities:
            new_names_by_type.setdefault(key[0], []).append(display_by_identity[key])
    name_warnings = dry_run_name_warnings(
        session,
        config.project_id,
        new_names_by_type=new_names_by_type,
        event_type_ids={
            target.name: target.event_type.id if target.event_type is not None else None
            for target in targets
        },
        name_format=config.event_name_format,
        cardinality_threshold=config.cardinality_threshold,
    )

    events = [
        {
            "name": display_by_identity[key],
            "source_name": key[1],
            "event_type": key[0],
            "approx_row_count": count,
            "share_of_sample": (count / sampled_rows) if sampled_rows else 0.0,
            "status": "existing" if key in existing_identities else "new",
            "grouped_by_rule": rule_by_identity.get(key),
            "count_confidence": count_confidence,
        }
        for key, count in sorted(counts_by_identity.items(), key=lambda item: (-item[1], item[0]))
    ]

    return {
        "window_from": scan_window[0].isoformat() if scan_window else None,
        "window_to": scan_window[1].isoformat() if scan_window else None,
        "sampled_rows": sampled_rows,
        "sample_row_limit": sample_row_limit,
        "sample_is_complete": sample_is_complete,
        "breakdown_combinations": breakdown_combinations,
        "events": events,
        # An incomplete sample means distinct events may exist that this pass
        # never looked at, so the caller must say "at least N", never "N".
        "events_truncated": not sample_is_complete,
        "max_events_reached": max_events_reached,
        "fields": fields,
        "templated_columns": list(templated.values()),
        "reserved_columns": sorted(skip_cols),
        # "Unmapped" is a claim about ANALYSED columns: this one was looked at and
        # no field definition covers it. With no targets nothing was analysed at
        # all — an empty window yields no group values, and a grouping column
        # whose every value is refused yields no targets either, so
        # `_dry_run_targets` returns nothing and `known_field_names` stays empty
        # on both. Reporting every column as unmapped there tells the user a run
        # skips columns it would in fact create, and sends the panel down its
        # explicit-event-type branch on a path that has no event type. Say
        # nothing rather than something false.
        "unmapped_columns": sorted(
            c.name for c in columns if c.name not in skip_cols and c.name not in known_field_names
        )
        if targets
        else [],
        "warnings": warnings,
        "errors": errors,
        "name_warnings": name_warnings,
    }


def _dry_run_config(session: Session, job: ScanDryRunJob) -> ScanConfig:
    """The config a dry-run should read: the saved one, or a draft stand-in.

    The draft is a TRANSIENT ``ScanConfig`` — constructed, never ``session.add``ed,
    so it is never flushed. Building it (rather than threading a dozen kwargs
    around) is what lets ``reserved_catalog_columns`` be reused verbatim instead
    of re-derived, which is the invariant that mattered there.
    """
    if job.scan_config_id is not None:
        config = session.get(ScanConfig, job.scan_config_id)
        if config is None:
            msg = f"ScanConfig {job.scan_config_id} not found"
            raise ValueError(msg)
        return config

    if job.data_source_id is None or not job.base_query:
        msg = f"ScanDryRunJob {job.id} carries neither a scan config nor a draft"
        raise ValueError(msg)

    return ScanConfig(
        project_id=job.project_id,
        data_source_id=job.data_source_id,
        name="(dry run)",
        base_query=job.base_query,
        event_type_id=job.event_type_id,
        event_type_column=job.event_type_column,
        time_column=job.time_column,
        event_name_format=job.event_name_format,
        event_group_rules=list(job.event_group_rules or []),
        json_value_paths=list(job.json_value_paths or []),
        setup_preset=job.setup_preset,
        event_name_column=job.event_name_column,
        properties_column=job.properties_column,
        json_string_columns=list(job.json_string_columns or []),
        cardinality_threshold=job.cardinality_threshold,
        app_version_column=job.app_version_column,
        platform_column=job.platform_column,
        scan_lookback_hours=job.scan_lookback_hours,
    )


@celery_app.task(  # type: ignore[untyped-decorator]
    # Wire identifier, NOT a module path: the broker routes on this string, so it
    # keeps the ``...tasks.scan...`` prefix even though the code now lives in
    # ``tasks.scan_dry_run``. Renaming it strands in-flight jobs.
    name="tripl.worker.tasks.scan.dry_run_scan_config_async",
    bind=True,
    max_retries=0,
)
def dry_run_scan_config_async(self: object, job_id: str) -> dict[str, object]:
    """Compute "what would this scan create?" off the request path.

    A dry-run runs the same ``GROUP BY ALL`` a real scan runs, so it is strictly
    slower than the preview that already needed a worker job.
    """
    session = _get_sync_session()
    adapter: BaseAdapter | None = None
    try:
        job = session.get(ScanDryRunJob, uuid.UUID(job_id))
        if job is None:
            msg = f"ScanDryRunJob {job_id} not found"
            raise ValueError(msg)

        config = _dry_run_config(session, job)
        ds = session.get(DataSource, config.data_source_id)
        if ds is None:
            msg = f"DataSource {config.data_source_id} not found"
            raise ValueError(msg)

        job.status = ScanJobStatus.running.value
        job.started_at = datetime.now(UTC)
        session.commit()

        adapter = _build_adapter(ds)
        payload = build_dry_run_payload(
            session,
            adapter,
            config,
            sample_row_limit=job.sample_row_limit,
        )

        job.status = ScanJobStatus.completed.value
        job.completed_at = datetime.now(UTC)
        job.result_summary = payload
        session.commit()
        return payload
    except Exception as e:
        logger.exception(f"Scan dry run failed: {e}")
        session.rollback()
        try:
            job = session.get(ScanDryRunJob, uuid.UUID(job_id))
            if job:
                job.status = ScanJobStatus.failed.value
                job.completed_at = datetime.now(UTC)
                job.error_message = user_facing_error(e)
                session.commit()
        except Exception:
            logger.exception("Failed to update scan dry-run job status after error")
        raise
    finally:
        if adapter is not None:
            adapter.close()
        session.close()
