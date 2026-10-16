"""The field-contract check of a collection, on its own.

A live collection checks the plan's field contracts against the rows of its
window (``catalog_sync``); a replay skips the check. A demo's first build,
when a history fill follows it, leaves its live collection to the fill
(``services.demo.pipeline.run_history_stage``), so without this the demo would
open without the contract violations the warehouse carries, and the tour's
schema-drift step would wait for the fill. It stores the drifts the check
finds and nothing else: no buckets, no catalog changes, no detection.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.core.json_string_columns import scan_source_query
from tripl.core.plan_scope import main_branch_id
from tripl.models.data_source import DataSource
from tripl.models.event_type import EventType
from tripl.models.scan_config import ScanConfig
from tripl.services import app_settings_service
from tripl.worker.db import _build_adapter
from tripl.worker.tasks.metrics.schema_drift import _detect_field_contract_violations
from tripl.worker.utils.reserved_columns import reserved_catalog_columns
from tripl.worker.utils.scan_naming import scan_group_column
from tripl.worker.utils.scan_preset import preset_scan_columns


def check_field_contracts(
    session: Session, scan_config_id: uuid.UUID, *, time_from: datetime, time_to: datetime
) -> int:
    """Check the scan's field contracts over ``[time_from, time_to)``; returns the violations.

    The columns and the contract check are the live collection's own; a grouped
    scan checks every event type of the main plan, under its group value.
    Commits the drifts it writes.
    """
    config = session.get(ScanConfig, scan_config_id)
    if config is None:
        msg = f"ScanConfig {scan_config_id} not found"
        raise ValueError(msg)
    data_source = session.get(DataSource, config.data_source_id)
    if data_source is None:
        msg = f"DataSource for config {scan_config_id} not found"
        raise ValueError(msg)
    runtime_config = app_settings_service.get_runtime_config_sync(
        session, org_id=app_settings_service.project_org_id_sync(session, config.project_id)
    )
    row_limit = config.metrics_row_limit or runtime_config.metrics_row_limit_default
    group_column = scan_group_column(config)
    if group_column is not None:
        event_types: list[tuple[EventType | None, str | None]] = [
            (event_type, event_type.name)
            for event_type in session.scalars(
                select(EventType).where(
                    EventType.project_id == config.project_id,
                    EventType.branch_id == main_branch_id(session, config.project_id),
                )
            )
        ]
    else:
        single = session.get(EventType, config.event_type_id) if config.event_type_id else None
        event_types = [(single, None)]

    adapter = _build_adapter(data_source)
    try:
        base_query = scan_source_query(adapter, config)
        columns = adapter.get_columns(base_query)
        if config.time_column:
            columns = [c for c in columns if c.name != config.time_column]
        columns = preset_scan_columns(config, columns)
        skip_columns = reserved_catalog_columns(config)
        violations = 0
        for event_type, group_value in event_types:
            outcome = _detect_field_contract_violations(
                session,
                adapter=adapter,
                event_type=event_type,
                base_query=base_query,
                columns=columns,
                skip_columns=skip_columns,
                scan_config_id=config.id,
                time_column=config.time_column,
                time_from=time_from,
                time_to=time_to,
                group_column=group_column if group_value is not None else None,
                group_value=group_value,
                limit=row_limit,
            )
            violations += outcome.violations_detected
    finally:
        adapter.close()
    session.commit()
    return violations
