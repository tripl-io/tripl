"""Resolve the grid each alert state's series is scored on, for the monitor rollup.

Alert dispatch keeps a scope open for ``max(24h, 3 x interval)`` of the scope's
OWN grid (``monitoring_utils._freshness_horizon``): a scan config's interval for
scan-backed scopes, the catalog metric's resolved grid for ``metric`` scopes.
``summarize_monitor_states`` has to measure the same states against the same
horizon, or a daily/weekly monitor that is delivering reads "warning".
Shared by the Monitors screen and the project list's
``firing_monitor_count`` so the two cannot disagree.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.metric_grid import metric_grid_stmt, metric_grids
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.domain_enums import MetricScopeType
from tripl.models.metric_definition import MetricDefinition
from tripl.models.scan_config import ScanConfig
from tripl.services.monitoring_utils import MonitorStateInterval, scan_interval_to_timedelta


def _metric_definition_id(scope_ref: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(scope_ref)
    except ValueError:
        return None


async def load_monitor_state_intervals(
    session: AsyncSession,
    states: Iterable[AlertRuleState],
) -> MonitorStateInterval:
    """Return a resolver mapping one ``AlertRuleState`` to its series' grid."""
    states = list(states)
    scan_config_ids = {state.scan_config_id for state in states if state.scan_config_id}
    metric_ids = {
        metric_id
        for state in states
        if state.scope_type == MetricScopeType.metric.value
        and (metric_id := _metric_definition_id(state.scope_ref)) is not None
    }

    scan_intervals: dict[uuid.UUID, timedelta | None] = {}
    if scan_config_ids:
        scan_intervals = {
            scan_config_id: scan_interval_to_timedelta(None if interval is None else str(interval))
            for scan_config_id, interval in (
                await session.execute(
                    select(ScanConfig.id, ScanConfig.interval).where(
                        ScanConfig.id.in_(scan_config_ids)
                    )
                )
            ).all()
        }

    metric_intervals: dict[str, timedelta | None] = {}
    if metric_ids:
        grids = metric_grids(
            (await session.execute(metric_grid_stmt(MetricDefinition.id.in_(metric_ids)))).all()
        )
        metric_intervals = {
            str(metric_id): scan_interval_to_timedelta(grid.interval)
            for metric_id, grid in grids.items()
        }

    def interval_of(state: AlertRuleState) -> timedelta | None:
        # Metric scope first: its row carries a NULL scan_config_id by design,
        # and its grid is the metric's own, never a dispatching scan's.
        if state.scope_type == MetricScopeType.metric.value:
            return metric_intervals.get(state.scope_ref)
        if state.scan_config_id is None:
            return None
        return scan_intervals.get(state.scan_config_id)

    return interval_of
