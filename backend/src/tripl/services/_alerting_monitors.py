"""Monitors summary — roll alert rules + per-scope states into firing/warning/healthy."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.domain_enums import AnomalyDirection, MetricScopeType
from tripl.models.scan_config import ScanConfig
from tripl.schemas.alerting import (
    MonitorDetailResponse,
    MonitorFiringScope,
    MonitorsSummaryResponse,
    MonitorSummaryItem,
)
from tripl.services._alerting_scope_readiness import load_scope_readiness
from tripl.services._monitor_state_intervals import load_monitor_state_intervals
from tripl.services.monitoring_utils import firing_monitor_states, summarize_monitor_states
from tripl.services.project_lookup import resolve_project as _get_project


def is_rule_muted(rule: AlertRule, now: datetime) -> bool:
    """Is this rule's mute IN FORCE right now? A lapsed ``muted_until`` is not.

    Shared with ``_alerting_destinations`` rather than kept private here: the
    destination card and the monitors screen describe the SAME AlertRule, and
    the two screens disagreed about its mute state because only one of them had
    any. One definition, imported, is what keeps them equal — a
    second copy of these four lines would drift the moment either is edited.
    """
    muted_until = rule.muted_until
    if muted_until is None:
        return False
    # AlertRule.muted_until uses UtcDateTime, which restores UTC on SQLite too.
    return muted_until > now


async def _get_rule_with_destination(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    rule_id: uuid.UUID,
) -> tuple[AlertRule, AlertDestination, str | None]:
    """The rule, its destination, and the name of the scan it is narrowed to.

    The scan join is an OUTER one and must stay that way: ``scan_config_id`` is
    nullable, null is the default every rule is created with, and the delete path
    sets it back to null (``_alerting_destinations.disable_rules_bound_to_scan``). An
    inner join would 404 every project-wide monitor — which is nearly all of them.
    """
    row = (
        await session.execute(
            select(AlertRule, AlertDestination, ScanConfig.name)
            .join(AlertDestination, AlertDestination.id == AlertRule.destination_id)
            .outerjoin(ScanConfig, ScanConfig.id == AlertRule.scan_config_id)
            .where(
                AlertRule.id == rule_id,
                AlertDestination.project_id == project_id,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    rule, destination, scan_name = row
    return rule, destination, scan_name


async def get_monitors_summary(session: AsyncSession, slug: str) -> MonitorsSummaryResponse:
    project = await _get_project(session, slug)

    rule_rows = (
        await session.execute(
            select(AlertRule, AlertDestination)
            .join(AlertDestination, AlertDestination.id == AlertRule.destination_id)
            .where(AlertDestination.project_id == project.id)
            .order_by(AlertDestination.name, AlertRule.name)
        )
    ).all()

    states_by_rule: dict[uuid.UUID, list[AlertRuleState]] = defaultdict(list)
    rule_ids = [rule.id for rule, _ in rule_rows]
    states: list[AlertRuleState] = []
    if rule_ids:
        states = list(
            (
                await session.execute(
                    select(AlertRuleState).where(AlertRuleState.rule_id.in_(rule_ids))
                )
            )
            .scalars()
            .all()
        )
        for state in states:
            states_by_rule[state.rule_id].append(state)
    # Judge each state on its own grid, as dispatch does.
    interval_of = await load_monitor_state_intervals(session, states)

    now = datetime.now(UTC)
    monitors: list[MonitorSummaryItem] = []
    for rule, destination in rule_rows:
        rollup = summarize_monitor_states(
            states_by_rule.get(rule.id, []), now=now, interval_of=interval_of
        )
        monitors.append(
            MonitorSummaryItem(
                rule_id=rule.id,
                rule_name=rule.name,
                destination_id=destination.id,
                destination_name=destination.name,
                destination_type=destination.type,
                enabled=rule.enabled and destination.enabled,
                status=rollup.status,
                active_scope_count=rollup.active_scope_count,
                firing_scope_count=rollup.firing_scope_count,
                last_anomaly_at=rollup.last_anomaly_at,
                last_notified_at=rollup.last_notified_at,
                notify_on_spike=rule.notify_on_spike,
                notify_on_drop=rule.notify_on_drop,
                min_percent_delta=rule.min_percent_delta,
                min_expected_count=rule.min_expected_count,
                cooldown_minutes=rule.cooldown_minutes,
                muted=is_rule_muted(rule, now),
                muted_until=rule.muted_until,
            )
        )

    return MonitorsSummaryResponse(
        monitors=monitors,
        firing_count=sum(1 for monitor in monitors if monitor.status == "firing"),
        warning_count=sum(1 for monitor in monitors if monitor.status == "warning"),
        healthy_count=sum(1 for monitor in monitors if monitor.status == "healthy"),
        total=len(monitors),
        scope_readiness=await load_scope_readiness(session, project.id),
    )


async def _load_firing_scopes(
    session: AsyncSession, firing: list[AlertRuleState]
) -> list[MonitorFiringScope]:
    """Describe each firing state from the item that last notified it (MO-36).

    ``AlertRuleState`` keeps only the scope's identity and timestamps; its name,
    event and direction live on the delivery item ``last_notified_delivery_id``
    points at. One query for all of them.
    """
    delivery_ids = {
        state.last_notified_delivery_id
        for state in firing
        if state.last_notified_delivery_id is not None
    }
    items: dict[tuple[uuid.UUID, str, str], AlertDeliveryItem] = {}
    if delivery_ids:
        for item in (
            await session.execute(
                select(AlertDeliveryItem).where(AlertDeliveryItem.delivery_id.in_(delivery_ids))
            )
        ).scalars():
            items[(item.delivery_id, str(item.scope_type), item.scope_ref)] = item

    scopes: list[MonitorFiringScope] = []
    for state in firing:
        notified = (
            items.get((state.last_notified_delivery_id, str(state.scope_type), state.scope_ref))
            if state.last_notified_delivery_id is not None
            else None
        )
        assert state.last_anomaly_bucket is not None  # guaranteed by firing_monitor_states
        scopes.append(
            MonitorFiringScope(
                scan_config_id=state.scan_config_id,
                scope_type=MetricScopeType(state.scope_type),
                scope_ref=state.scope_ref,
                scope_name=notified.scope_name if notified is not None else None,
                event_id=notified.event_id if notified is not None else None,
                direction=AnomalyDirection(notified.direction) if notified is not None else None,
                last_anomaly_bucket=state.last_anomaly_bucket,
                last_notified_at=state.last_notified_at,
            )
        )
    scopes.sort(key=lambda scope: _utc(scope.last_anomaly_bucket), reverse=True)
    return scopes


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _build_monitor_detail(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    rule: AlertRule,
    destination: AlertDestination,
    scan_name: str | None,
    now: datetime,
) -> MonitorDetailResponse:
    states = (
        (await session.execute(select(AlertRuleState).where(AlertRuleState.rule_id == rule.id)))
        .scalars()
        .all()
    )
    interval_of = await load_monitor_state_intervals(session, states)
    rollup = summarize_monitor_states(states, now=now, interval_of=interval_of)
    firing_scopes = await _load_firing_scopes(
        session, firing_monitor_states(states, now=now, interval_of=interval_of)
    )

    total_deliveries = (
        await session.execute(
            select(func.count(AlertDelivery.id)).where(AlertDelivery.rule_id == rule.id)
        )
    ).scalar_one()
    last_delivery = (
        await session.execute(
            select(AlertDelivery.created_at, AlertDelivery.status)
            .where(AlertDelivery.rule_id == rule.id)
            # Tie-broken on id, like ``_alerting_health`` does for the very same
            # three numbers on the destination card. A scan writes several
            # deliveries in the same instant, so ``created_at`` alone lets the
            # two screens pick different rows and report different statuses for
            # one rule — the disagreement tripl-oxkt.18 was filed about.
            .order_by(AlertDelivery.created_at.desc(), AlertDelivery.id.desc())
            .limit(1)
        )
    ).first()

    return MonitorDetailResponse(
        rule_id=rule.id,
        rule_name=rule.name,
        destination_id=destination.id,
        destination_name=destination.name,
        destination_type=destination.type,
        enabled=rule.enabled and destination.enabled,
        status=rollup.status,
        active_scope_count=rollup.active_scope_count,
        firing_scope_count=rollup.firing_scope_count,
        last_anomaly_at=rollup.last_anomaly_at,
        last_notified_at=rollup.last_notified_at,
        notify_on_spike=rule.notify_on_spike,
        notify_on_drop=rule.notify_on_drop,
        min_percent_delta=rule.min_percent_delta,
        min_expected_count=rule.min_expected_count,
        cooldown_minutes=rule.cooldown_minutes,
        muted=is_rule_muted(rule, now),
        muted_until=rule.muted_until,
        rule_enabled=rule.enabled,
        destination_enabled=destination.enabled,
        # Which scan this rule can see at all, named so the detail screen can say
        # so — it holds no scans list to resolve the id against. NOT an input to
        # ``scope_readiness`` below, which stays project-level.
        scan_config_id=rule.scan_config_id,
        scan_name=scan_name,
        include_project_total=rule.include_project_total,
        include_event_types=rule.include_event_types,
        include_events=rule.include_events,
        include_schema_drifts=rule.include_schema_drifts,
        include_distribution_drifts=rule.include_distribution_drifts,
        include_release_regressions=rule.include_release_regressions,
        include_variable_value_drifts=rule.include_variable_value_drifts,
        include_metrics=rule.include_metrics,
        include_source_freshness=rule.include_source_freshness,
        include_lifecycle=rule.include_lifecycle,
        include_property_drifts=rule.include_property_drifts,
        total_deliveries=total_deliveries,
        last_delivery_at=last_delivery[0] if last_delivery else None,
        # Coerced at the boundary rather than left to Pydantic, matching
        # ``_alerting_health``: whether the driver returns the enum member or
        # its string depends on the dialect, and this contract must not.
        last_delivery_status=(
            AlertDeliveryStatus(last_delivery[1]) if last_delivery is not None else None
        ),
        firing_scopes=firing_scopes,
        # Project-level, so it is the identical block the monitors list carries
        # — the detail screen renders the same two toggles and must not disagree
        # with the list about whether anything feeds them.
        scope_readiness=await load_scope_readiness(session, project_id),
    )


async def get_monitor(
    session: AsyncSession,
    slug: str,
    rule_id: uuid.UUID,
) -> MonitorDetailResponse:
    project = await _get_project(session, slug)
    rule, destination, scan_name = await _get_rule_with_destination(
        session,
        project_id=project.id,
        rule_id=rule_id,
    )
    return await _build_monitor_detail(
        session,
        project_id=project.id,
        rule=rule,
        destination=destination,
        scan_name=scan_name,
        now=datetime.now(UTC),
    )


async def mute_monitor(
    session: AsyncSession,
    slug: str,
    rule_id: uuid.UUID,
    muted_until: datetime,
) -> MonitorDetailResponse:
    project = await _get_project(session, slug)
    rule, destination, scan_name = await _get_rule_with_destination(
        session,
        project_id=project.id,
        rule_id=rule_id,
    )
    now = datetime.now(UTC)
    # A naive argument is READ AS UTC rather than blowing up the compare below.
    # The route hands this a value ``MonitorMuteRequest.normalize_datetime`` has
    # already normalized, so this guard is for the callers that never see that
    # schema; without it an offset-less instant raises "can't compare
    # offset-naive and offset-aware datetimes" and the catch-all in ``main.py``
    # turns a mute into a 500. UTC is the only reading this
    # codebase gives a bare instant: ``is_rule_muted`` above reaches the same
    # answer from the other side by stripping tzinfo off ``now`` so a
    # SQLite-naive column compares as UTC wall time. Coercing before the write
    # also means the row stores the instant the check passed on, without
    # depending on how the driver resolves a naive value into a ``timestamptz``.
    if muted_until.tzinfo is None:
        muted_until = muted_until.replace(tzinfo=UTC)
    if muted_until <= now:
        raise HTTPException(status_code=422, detail="muted_until must be in the future")
    rule.muted_until = muted_until
    await session.commit()
    return await _build_monitor_detail(
        session,
        project_id=project.id,
        rule=rule,
        destination=destination,
        scan_name=scan_name,
        now=now,
    )


async def unmute_monitor(
    session: AsyncSession,
    slug: str,
    rule_id: uuid.UUID,
) -> MonitorDetailResponse:
    project = await _get_project(session, slug)
    rule, destination, scan_name = await _get_rule_with_destination(
        session,
        project_id=project.id,
        rule_id=rule_id,
    )
    rule.muted_until = None
    await session.commit()
    return await _build_monitor_detail(
        session,
        project_id=project.id,
        rule=rule,
        destination=destination,
        scan_name=scan_name,
        now=datetime.now(UTC),
    )
