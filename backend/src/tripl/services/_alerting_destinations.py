"""Alert destination and rule CRUD operations."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import Select

from tripl.alert_templates import validate_template_configuration
from tripl.alerting_validation import (
    validate_slack_webhook_url,
    validate_telegram_bot_token,
    validate_telegram_chat_id,
)
from tripl.core.alert_schedule import next_fire_at
from tripl.core.event_references import plan_alert_filter_change
from tripl.crypto import encrypt_value
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_pending_item import AlertPendingItem
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_filter import AlertRuleFilter
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.domain_enums import AlertRuleFilterField
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.metric_definition import MetricDefinition
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.schemas.alerting import (
    AlertDestinationCreate,
    AlertDestinationResponse,
    AlertDestinationUpdate,
    AlertRuleCreate,
    AlertRuleFilterPayload,
    AlertRuleFilterResponse,
    AlertRuleResponse,
    AlertRuleUpdate,
)
from tripl.services._alerting_from_policy import assert_from_override_allowed
from tripl.services._alerting_health import (
    DestinationHealth,
    RuleHealth,
    load_destination_health,
)
from tripl.services._alerting_monitors import is_rule_muted
from tripl.services.project_lookup import resolve_project as _get_project


def _encrypt_secret(value: str | None) -> str | None:
    if value is None:
        return None
    return encrypt_value(value)


async def _assert_public_destination_host(url: str | None, *, field: str) -> None:
    """SSRF guard for a free-form destination URL, run OFF the event loop.

    ``reject_private_host`` resolves the hostname with ``socket.getaddrinfo``,
    which blocks for as long as the resolver takes and honours no timeout of
    ours. It used to run inside the pydantic validators for ``target_url`` and
    ``jira_base_url``, i.e. during FastAPI's body parsing for an ``async def``
    route — on the event loop, where one degraded lookup stalls every other
    request already in flight on the same uvicorn worker. The
    schema now settles the URL's SHAPE; this settles where it points.

    Nothing is weakened by the move. This runs before the row is written on both
    the create and the update path, so a URL the guard refuses is never stored,
    and send time keeps its own independent re-check
    (``alerts_channels._reject_private_target``) as the DNS-rebinding defence.

    ``_reject_private_target`` is the same helper, reached the same way, as in
    ``_alerting_test_send`` — the one place in this package that already had
    this right. The import is deferred for the reason it is deferred there:
    ``worker.tasks.alerts_channels`` drags in the whole outbound-channel stack
    (urllib, smtplib, ``app_settings_service``), which the request path must not
    pay for at module import.
    """
    if url is None:
        return
    from tripl.worker.tasks.alerts_channels import _reject_private_target

    try:
        await asyncio.to_thread(_reject_private_target, url, field=field)
    except ValueError as exc:
        # The guard signals refusal with ValueError. Raised bare inside a
        # service nothing catches it but main.py's catch-all, which would turn
        # "your webhook points at the metadata endpoint" into a 500.
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _refresh_main_search_index(
    session: AsyncSession, project_id: uuid.UUID, slug: str
) -> None:
    """Refresh the search index after an alert-rule mutation.

    Alert rules are global (they reach their project through their destination,
    not through a branch), so only the MAIN branch index is refreshed eagerly;
    feature-branch indexes pick the change up on their next rebuild. Same rule
    metrics, fact tables and scan configs already follow.

    Without this a rule the user just created stayed unfindable in the command
    palette until some unrelated reindex happened to fire.

    The imports are deferred because the module graph is cyclic here:
    ``search_service`` imports ``project_service``, which imports
    ``alerting_service``, which imports this module. Same reason
    ``plan_branch_merge_service`` defers its own reindex import.
    """
    from tripl.services.plan_branch_service import resolve_branch_id
    from tripl.services.search_service import reindex_project_branch

    main_branch_id = await resolve_branch_id(session, project_id, None)
    await reindex_project_branch(
        session, project_id=project_id, branch_id=main_branch_id, slug=slug
    )


def _destination_query(project_id: uuid.UUID) -> Select[tuple[AlertDestination]]:
    return (
        select(AlertDestination)
        .where(AlertDestination.project_id == project_id)
        .options(
            selectinload(AlertDestination.rules).selectinload(AlertRule.filters),
        )
        .order_by(AlertDestination.created_at.desc())
    )


async def get_destination(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    destination_id: uuid.UUID,
) -> AlertDestination:
    destination = await session.scalar(
        _destination_query(project_id).where(AlertDestination.id == destination_id)
    )
    if destination is None:
        raise HTTPException(status_code=404, detail="Alert destination not found")
    return destination


async def get_rule(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    destination_id: uuid.UUID,
    rule_id: uuid.UUID,
) -> tuple[AlertDestination, AlertRule]:
    destination = await get_destination(
        session,
        project_id=project_id,
        destination_id=destination_id,
    )
    rule = await session.scalar(
        select(AlertRule)
        .where(
            AlertRule.id == rule_id,
            AlertRule.destination_id == destination_id,
        )
        .options(selectinload(AlertRule.filters))
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    return destination, rule


async def validate_filters(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    filters: list[AlertRuleFilterPayload],
) -> None:
    event_type_ids: set[uuid.UUID] = set()
    event_ids: set[uuid.UUID] = set()
    metric_ids: set[uuid.UUID] = set()
    for filter_payload in filters:
        if filter_payload.field == "event_type":
            event_type_ids.update(uuid.UUID(value) for value in filter_payload.values)
        elif filter_payload.field == "event":
            event_ids.update(uuid.UUID(value) for value in filter_payload.values)
        elif filter_payload.field == "metric":
            metric_ids.update(uuid.UUID(value) for value in filter_payload.values)

    if event_type_ids:
        found_ids = set(
            (
                await session.execute(
                    select(EventType.id).where(
                        EventType.project_id == project_id,
                        EventType.id.in_(event_type_ids),
                    )
                )
            ).scalars()
        )
        missing = event_type_ids - found_ids
        if missing:
            raise HTTPException(status_code=404, detail="Filter event type not found")

    if event_ids:
        found_ids = set(
            (
                await session.execute(
                    select(Event.id).where(
                        Event.project_id == project_id,
                        Event.id.in_(event_ids),
                    )
                )
            ).scalars()
        )
        missing = event_ids - found_ids
        if missing:
            raise HTTPException(status_code=404, detail="Filter event not found")

    if metric_ids:
        found_ids = set(
            (
                await session.execute(
                    select(MetricDefinition.id).where(
                        MetricDefinition.project_id == project_id,
                        MetricDefinition.id.in_(metric_ids),
                    )
                )
            ).scalars()
        )
        if metric_ids - found_ids:
            raise HTTPException(status_code=404, detail="Filter metric not found")


async def validate_scan_config(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scan_config_id: uuid.UUID | None,
) -> None:
    """A rule may only be narrowed to a scan of its OWN project.

    Same shape as ``validate_filters``: a 404 rather than a foreign-key error,
    so a stale or cross-project id never reaches the database.
    """
    if scan_config_id is None:
        return
    found = await session.scalar(
        select(ScanConfig.id).where(
            ScanConfig.id == scan_config_id,
            ScanConfig.project_id == project_id,
        )
    )
    if found is None:
        raise HTTPException(status_code=404, detail="Scan configuration not found")


def rule_to_response(rule: AlertRule, health: RuleHealth, *, now: datetime) -> AlertRuleResponse:
    sorted_filters = sorted(rule.filters, key=lambda item: item.position)
    return AlertRuleResponse(
        id=rule.id,
        destination_id=rule.destination_id,
        scan_config_id=rule.scan_config_id,
        name=rule.name,
        enabled=rule.enabled,
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
        notify_on_spike=rule.notify_on_spike,
        notify_on_drop=rule.notify_on_drop,
        ai_explanation_enabled=rule.ai_explanation_enabled,
        notify_owners=rule.notify_owners,
        min_percent_delta=rule.min_percent_delta,
        min_absolute_delta=rule.min_absolute_delta,
        min_expected_count=rule.min_expected_count,
        cooldown_minutes=rule.cooldown_minutes,
        message_template=rule.message_template,
        items_template=rule.items_template,
        message_format=rule.message_format,
        filters=[
            AlertRuleFilterResponse(
                id=filter_row.id,
                field=filter_row.field,
                operator=filter_row.operator,
                values=list(filter_row.values or []),
            )
            for filter_row in sorted_filters
        ],
        # ``muted_until`` is emitted raw, unlike the inbox group's, because the
        # card needs the stored value to offer "unmute" on a rule whose mute has
        # lapsed; ``muted`` is the claim about NOW and is what the badge reads.
        muted=is_rule_muted(rule, now),
        muted_until=rule.muted_until,
        last_delivery_at=health.last_delivery_at,
        last_delivery_status=health.last_delivery_status,
        total_deliveries=health.delivery_count,
        incident_count=health.incident_count,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


async def load_held_counts(
    session: AsyncSession,
    destination_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """How many alerts each destination is holding for its next digest.

    One grouped query for the whole page, the same shape as
    ``load_destination_health`` — a per-card count would be N queries on a
    screen that already batches everything else.
    """
    if not destination_ids:
        return {}
    rows = await session.execute(
        select(AlertPendingItem.destination_id, func.count())
        .where(AlertPendingItem.destination_id.in_(destination_ids))
        .group_by(AlertPendingItem.destination_id)
    )
    return {destination_id: count for destination_id, count in rows.all()}


def _next_digest_at(
    destination: AlertDestination,
    *,
    now: datetime,
    project_timezone: str,
) -> datetime | None:
    """When this destination's next digest is due, or None if it is immediate."""
    cron = destination.delivery_schedule_cron
    if cron is None:
        return None
    try:
        return next_fire_at(cron, tz_name=project_timezone, after=now)
    except ValueError:
        return None


def destination_to_response(
    destination: AlertDestination,
    health: DestinationHealth,
    *,
    now: datetime,
    project_timezone: str = "UTC",
    held_count: int = 0,
) -> AlertDestinationResponse:
    """One destination card. ``now`` is the clock every mute on it is read against.

    Passed in rather than read here: this function is called once per destination
    while building the LIST, so reading the clock inside it gave one response N
    different "now"s and two destinations muted to the same instant could
    disagree about whether that instant had passed. The parameter is required so
    a future caller has to decide which clock it means.
    """
    rules = sorted(destination.rules, key=lambda item: item.created_at, reverse=True)
    return AlertDestinationResponse(
        id=destination.id,
        project_id=destination.project_id,
        type=destination.type,
        name=destination.name,
        enabled=destination.enabled,
        webhook_set=bool(destination.webhook_url_encrypted),
        bot_token_set=bool(destination.bot_token_encrypted),
        chat_id=destination.chat_id,
        target_url_set=bool(destination.target_url_encrypted),
        webhook_header_name=destination.webhook_header_name,
        email_recipients=destination.email_recipients,
        email_from_address=destination.email_from_address,
        email_subject_template=destination.email_subject_template,
        jira_base_url=destination.jira_base_url,
        jira_auth_email=destination.jira_auth_email,
        jira_api_token_set=bool(destination.jira_api_token_encrypted),
        jira_project_key=destination.jira_project_key,
        jira_issue_type=destination.jira_issue_type,
        linear_api_key_set=bool(destination.linear_api_key_encrypted),
        linear_team_id=destination.linear_team_id,
        linear_state_id=destination.linear_state_id,
        linear_label_ids=destination.linear_label_ids,
        delivery_schedule_cron=destination.delivery_schedule_cron,
        project_timezone=project_timezone,
        last_digest_at=destination.last_flushed_at,
        # Computed rather than stored: the answer changes with the clock, and a
        # cached one would be wrong the moment it was read. `next_fire_at`
        # never raises on a stored expression — the API validated it on write —
        # but a row written before that validation existed would, and a card
        # that 500s is worse than one that omits the preview.
        next_digest_at=_next_digest_at(destination, now=now, project_timezone=project_timezone),
        held_count=held_count,
        is_local=destination.type == AlertDestinationType.demo_sink,
        delivery_count=health.delivery_count,
        incident_count=health.incident_count,
        rules=[
            rule_to_response(rule, health.rules.get(rule.id, RuleHealth()), now=now)
            for rule in rules
        ],
        created_at=destination.created_at,
        updated_at=destination.updated_at,
    )


async def build_destination_response(
    session: AsyncSession,
    destination: AlertDestination,
) -> AlertDestinationResponse:
    """One destination, with the rollups its card and delete confirm need."""
    health = await load_destination_health(session, [destination.id])
    project_timezone = await session.scalar(
        select(Project.timezone).where(Project.id == destination.project_id)
    )
    held = await load_held_counts(session, [destination.id])
    return destination_to_response(
        destination,
        health.get(destination.id, DestinationHealth()),
        now=datetime.now(UTC),
        project_timezone=project_timezone or "UTC",
        held_count=held.get(destination.id, 0),
    )


async def build_rule_response(
    session: AsyncSession,
    rule: AlertRule,
) -> AlertRuleResponse:
    """One rule, with the rollups its card and delete confirm need."""
    health = await load_destination_health(session, [rule.destination_id])
    destination_health = health.get(rule.destination_id, DestinationHealth())
    return rule_to_response(
        rule,
        destination_health.rules.get(rule.id, RuleHealth()),
        now=datetime.now(UTC),
    )


# Credential / channel-config fields on AlertDestinationUpdate — the fields that
# make a destination able to reach the outside world. Two rules reject an update
# that touches them: a ``demo_sink`` must never gain any of them (that would turn
# the local sink into a real external channel), and neither must any destination
# on a demo project, which is zero-egress by construction.
_EXTERNAL_CHANNEL_UPDATE_FIELDS = (
    "webhook_url",
    "bot_token",
    "chat_id",
    "target_url",
    "webhook_header_name",
    "webhook_header_value",
    "email_recipients",
    "email_from_address",
    "email_subject_template",
    "jira_base_url",
    "jira_auth_email",
    "jira_api_token",
    "jira_project_key",
    "jira_issue_type",
    "linear_api_key",
    "linear_team_id",
    "linear_state_id",
    "linear_label_ids",
)


def _reject_demo_ai_explanation(*, is_demo: bool, ai_explanation_enabled: bool | None) -> None:
    """Refuse to arm a rule's AI explanation on a demo project.

    Building the explanation is an outbound LLM call, which a zero-egress demo
    must never make. The worker skips it for demo projects regardless, so without
    this the toggle would just silently do nothing.
    """
    if is_demo and ai_explanation_enabled:
        raise HTTPException(
            status_code=422,
            detail=(
                "AI explanations are disabled for demo projects: generating one "
                "would send demo data to an external model."
            ),
        )


async def replace_rule_filters(
    session: AsyncSession,
    *,
    rule: AlertRule,
    filters: list[AlertRuleFilterPayload],
) -> None:
    """Replace a rule's filter rows THROUGH the relationship, not around it.

    This used to bulk-DELETE the rows and ``session.add`` the replacements keyed
    on ``rule_id``, never touching ``rule.filters`` — which is the collection the
    200 response is rendered from (``rule_to_response``). Nothing put that right
    afterwards: sessions are built with ``expire_on_commit=False`` (see
    ``database``), so the commit left the deleted rows sitting on the instance,
    and the ``selectinload`` in ``get_rule``'s re-read does not overwrite a
    collection that is already loaded on an identity-mapped object. A PATCH that
    changed the filters therefore answered 200 listing the PREVIOUS filters, and
    a client reading its own write back concluded the update had not landed or
    re-applied stale state. The UI never saw it because it
    invalidates and refetches instead of trusting the mutation body.

    Assigning the collection makes the rule in memory and the rule in the
    database the same rule again: ``cascade="all, delete-orphan"`` on
    ``AlertRule.filters`` (see ``models.alert_rule``) deletes whatever the
    assignment displaces, so the hand-rolled DELETE is not needed to do it.
    """
    # The collection must be LOADED before it can be replaced: the ORM reads the
    # old one to decide which rows the assignment orphans. On the update path
    # ``get_rule`` has already selectinloaded it, but on the create path the rule
    # was INSERTed moments earlier and its filters were never touched — there the
    # assignment lazy-loads, and a lazy load is IO from async code, which
    # SQLAlchemy answers with MissingGreenlet (reproduced on the pinned
    # interpreter). One SELECT is what it costs to have a helper that is correct
    # whichever caller holds the rule, on routes that reindex the project anyway.
    await session.refresh(rule, ["filters"])
    rule.filters = [
        AlertRuleFilter(
            field=filter_payload.field,
            operator=filter_payload.operator,
            values=list(filter_payload.values),
            position=position,
        )
        for position, filter_payload in enumerate(filters)
    ]
    # One flush emits the new INSERTs before the orphan DELETEs. Positions may
    # repeat across the two halves while that runs, which is fine: the only index
    # on the table is the non-unique ``ix_alert_rule_filter_rule``.
    await session.flush()


async def disable_rules_bound_to_scan(session: AsyncSession, scan_id: uuid.UUID) -> None:
    """Unbind and DISABLE the alert rules narrowed to a scan about to be deleted.

    The column's ``ondelete`` is SET NULL (see ``models.alert_rule``), and NULL
    means "every scan in the project". Leaving it at that would take a rule
    someone had deliberately narrowed to the noisiest scan and, the moment that
    scan is deleted, re-point it at the whole project — so deleting a scan to
    stop the noise would start paging on every other scan instead.

    Disabling in the same transaction makes the orphan inert and visible: the
    rule keeps its name, thresholds, templates and filters, and the Alerting tab
    shows it switched off rather than silently re-aimed. CASCADE was rejected for
    the opposite reason — it would delete the rule outright and take its delivery
    history with it through ``AlertDelivery.rule_id``, including deliveries made
    for other scans while the rule was still project-wide.

    Lives here rather than in ``scan_service`` because a scan config is deleted
    by TWO paths: ``delete_scan_config``, and the ORM cascade from
    ``DataSource.scan_configs`` when a data source goes. Both must call it.
    """
    bound_rules = (
        (await session.execute(select(AlertRule).where(AlertRule.scan_config_id == scan_id)))
        .scalars()
        .all()
    )
    for rule in bound_rules:
        rule.scan_config_id = None
        rule.enabled = False
    # Same cleanup the ordinary "disable a rule" path does: a rule that is off
    # must not leave open AlertRuleState rows behind reporting it as firing.
    await clear_rule_states(session, [rule.id for rule in bound_rules])


async def clear_rule_states(session: AsyncSession, rule_ids: list[uuid.UUID]) -> None:
    if not rule_ids:
        return
    await session.execute(delete(AlertRuleState).where(AlertRuleState.rule_id.in_(rule_ids)))


async def drop_deleted_metric_from_rule_filters(
    session: AsyncSession, *, project_id: uuid.UUID, metric_id: uuid.UUID
) -> None:
    """Take a catalog metric that is being deleted out of every ``metric`` filter.

    The metric counterpart of ``_event_reference_cleanup._apply_alert_filter_changes``
    and the same policy through the same ``plan_alert_filter_change``: a filter
    that still names other metrics keeps them; an emptied exclusive filter goes
    and the rule stays on; an emptied inclusive filter goes and the rule is
    DISABLED, because dropping the row alone would widen a rule narrowed to one
    metric to every signal its destination watches.

    Without this the dead id stayed in the filter, and ``validate_filters``
    re-checks every metric id whenever a rule edit resends its filters — which
    the rule editor always does — so the rule answered every save with "Filter
    metric not found" until someone found the orphan by hand.

    Call BEFORE the delete, inside the caller's transaction; adds no commit.
    """
    dead_refs = {str(metric_id)}
    rows = (
        (
            await session.execute(
                select(AlertRuleFilter)
                .join(AlertRule, AlertRuleFilter.rule_id == AlertRule.id)
                .join(AlertDestination, AlertRule.destination_id == AlertDestination.id)
                .where(
                    AlertDestination.project_id == project_id,
                    AlertRuleFilter.field == AlertRuleFilterField.metric.value,
                )
            )
        )
        .scalars()
        .all()
    )
    disable_rule_ids: list[uuid.UUID] = []
    for row in rows:
        change = plan_alert_filter_change(
            operator=row.operator, values=row.values or [], removed=dead_refs
        )
        if change is None:
            continue
        if not change.delete_filter:
            row.values = change.remaining
            continue
        if change.disable_rule:
            disable_rule_ids.append(row.rule_id)
        await session.delete(row)

    if disable_rule_ids:
        for rule in (
            (await session.execute(select(AlertRule).where(AlertRule.id.in_(disable_rule_ids))))
            .scalars()
            .all()
        ):
            rule.enabled = False
    await session.flush()
    await clear_rule_states(session, disable_rule_ids)


async def list_destinations(session: AsyncSession, slug: str) -> list[AlertDestinationResponse]:
    project = await _get_project(session, slug)
    destinations = (await session.execute(_destination_query(project.id))).scalars().unique().all()
    # One batched load for the whole page — see load_destination_health.
    health = await load_destination_health(session, [dest.id for dest in destinations])
    # ...and one clock for the whole page, for the same reason: every mute in
    # this response is read against the same instant.
    now = datetime.now(UTC)
    held = await load_held_counts(session, [dest.id for dest in destinations])
    return [
        destination_to_response(
            destination,
            health.get(destination.id, DestinationHealth()),
            now=now,
            project_timezone=project.timezone,
            held_count=held.get(destination.id, 0),
        )
        for destination in destinations
    ]


async def create_destination(
    session: AsyncSession,
    slug: str,
    data: AlertDestinationCreate,
) -> AlertDestinationResponse:
    project = await _get_project(session, slug)
    # A ``demo_sink`` is a local, non-sendable sink that only ever belongs to a
    # generated demo project — mirroring how synthetic data sources are
    # demo-only. Block creating one on a real project.
    if data.type == AlertDestinationType.demo_sink and not project.is_demo:
        raise HTTPException(
            status_code=422,
            detail="A demo_sink destination can only be created on a demo project",
        )
    # ...and the mirror of that rule: a demo project is zero-egress, so the local
    # sink is the ONLY destination it may gain. Without this a demo user could add
    # a real Slack/webhook/Jira destination and the next collection would send a
    # genuine outbound message built from synthetic data. The
    # permanently-disabled Slack example a demo ships with is written by the seed
    # builder, not through this API.
    if project.is_demo and data.type != AlertDestinationType.demo_sink:
        raise HTTPException(
            status_code=422,
            detail=(
                "Demo projects cannot send external alerts. Only a local demo_sink "
                "destination is allowed — create a real project to connect Slack, "
                "Telegram, a webhook, email, Jira or Linear."
            ),
        )
    # Where a free-form URL points is decided here rather than in the schema —
    # see ``_assert_public_destination_host``. Only the field this destination
    # type actually uses is resolved: ``validate_channel_config`` checks the
    # channel fields on a per-type branch too, so resolving the others would buy
    # a DNS lookup, and possibly a 422, for a value the model itself never read.
    if data.type == AlertDestinationType.webhook:
        await _assert_public_destination_host(data.target_url, field="Webhook target_url")
    elif data.type == AlertDestinationType.jira:
        await _assert_public_destination_host(data.jira_base_url, field="Jira base_url")
    # Validate and persist the same channel's fields. Other channel values may
    # be present on the request, but must not become unvalidated stored state.
    if data.type == AlertDestinationType.email:
        await assert_from_override_allowed(
            session, project.organization_id, data.email_from_address
        )
    slack = data.type == AlertDestinationType.slack
    telegram = data.type == AlertDestinationType.telegram
    webhook = data.type == AlertDestinationType.webhook
    email = data.type == AlertDestinationType.email
    jira = data.type == AlertDestinationType.jira
    linear = data.type == AlertDestinationType.linear
    destination = AlertDestination(
        project_id=project.id,
        type=data.type,
        name=data.name,
        enabled=data.enabled,
        webhook_url_encrypted=_encrypt_secret(data.webhook_url) if slack else None,
        bot_token_encrypted=_encrypt_secret(data.bot_token) if telegram else None,
        chat_id=data.chat_id if telegram else None,
        target_url_encrypted=_encrypt_secret(data.target_url) if webhook else None,
        webhook_header_name=data.webhook_header_name if webhook else None,
        webhook_header_value_encrypted=(
            _encrypt_secret(data.webhook_header_value) if webhook else None
        ),
        email_recipients=data.email_recipients if email else None,
        email_from_address=data.email_from_address if email else None,
        email_subject_template=data.email_subject_template if email else None,
        jira_base_url=data.jira_base_url if jira else None,
        jira_auth_email=data.jira_auth_email if jira else None,
        jira_api_token_encrypted=_encrypt_secret(data.jira_api_token) if jira else None,
        jira_project_key=data.jira_project_key if jira else None,
        jira_issue_type=data.jira_issue_type if jira else None,
        linear_api_key_encrypted=_encrypt_secret(data.linear_api_key) if linear else None,
        linear_team_id=data.linear_team_id if linear else None,
        linear_state_id=data.linear_state_id if linear else None,
        linear_label_ids=data.linear_label_ids if linear else None,
        delivery_schedule_cron=data.delivery_schedule_cron,
        # A destination born with a cadence adopts the clock immediately, so
        # its first digest is the next real fire rather than a backlog dump.
        last_flushed_at=(datetime.now(UTC) if data.delivery_schedule_cron is not None else None),
    )
    session.add(destination)
    await session.commit()
    destination = await get_destination(
        session,
        project_id=project.id,
        destination_id=destination.id,
    )
    return await build_destination_response(session, destination)


async def get_destination_response(
    session: AsyncSession,
    slug: str,
    destination_id: uuid.UUID,
) -> AlertDestinationResponse:
    project = await _get_project(session, slug)
    destination = await get_destination(
        session,
        project_id=project.id,
        destination_id=destination_id,
    )
    return await build_destination_response(session, destination)


async def update_destination(
    session: AsyncSession,
    slug: str,
    destination_id: uuid.UUID,
    data: AlertDestinationUpdate,
) -> AlertDestinationResponse:
    project = await _get_project(session, slug)
    destination = await get_destination(
        session,
        project_id=project.id,
        destination_id=destination_id,
    )
    update_dict = data.model_dump(exclude_unset=True)
    # A ``demo_sink`` stays a local sink: block editing it into a real external
    # channel by adding a url/token/recipients/credentials. Only name/enabled
    # may change. (The destination ``type`` itself is immutable — there is no
    # ``type`` field on AlertDestinationUpdate — so a demo_sink can never become
    # slack/webhook/... and a real destination can never become a demo_sink.)
    if destination.type == AlertDestinationType.demo_sink:
        forbidden = [field for field in _EXTERNAL_CHANNEL_UPDATE_FIELDS if field in update_dict]
        if forbidden:
            raise HTTPException(
                status_code=422,
                detail=(
                    "A demo_sink destination is a local sink and cannot be given "
                    "credentials or converted into an external channel"
                ),
            )
    # The other half of the demo zero-egress rule. A demo ships one external
    # destination — a permanently disabled Slack example — purely to SHOW what a
    # real channel looks like. Renaming it is fine; enabling it or handing it a
    # webhook/token/recipient is not, because the next collection would then send
    # a real message from synthetic data.
    if project.is_demo and destination.type != AlertDestinationType.demo_sink:
        forbidden = [field for field in _EXTERNAL_CHANNEL_UPDATE_FIELDS if field in update_dict]
        if update_dict.get("enabled") is True:
            forbidden.append("enabled")
        if forbidden:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Demo projects cannot send external alerts. This destination is a "
                    "disabled example: it cannot be enabled or given credentials."
                ),
            )
    # Before the first assignment, not next to the write below: a URL this guard
    # refuses must leave the destination exactly as it was, rather than abandon
    # a half-applied rename on an uncommitted session. Gated on the STORED type
    # because that is what the write sites below are gated on — a ``target_url``
    # sent to a Jira destination is ignored, and resolving an ignored field
    # would 422 a request that succeeds today.
    if destination.type == AlertDestinationType.webhook:
        await _assert_public_destination_host(
            update_dict.get("target_url"), field="Webhook target_url"
        )
    elif destination.type == AlertDestinationType.jira:
        await _assert_public_destination_host(
            update_dict.get("jira_base_url"), field="Jira base_url"
        )
    if "name" in update_dict:
        destination.name = update_dict["name"]
    if "delivery_schedule_cron" in update_dict:
        cadence = update_dict["delivery_schedule_cron"]
        if cadence != destination.delivery_schedule_cron:
            destination.delivery_schedule_cron = cadence
            # Adopt the clock on every cadence change. Without this a
            # destination switched to "daily at 09:00" at 14:00 would carry a
            # watermark from an older cadence, and the flusher would find
            # today's 09:00 already past and unflushed — dumping whatever is
            # buffered within the minute instead of waiting for tomorrow.
            destination.last_flushed_at = datetime.now(UTC) if cadence is not None else None
            if cadence is None:
                # Back to "Immediately": the hold is OVER, so the buffer is
                # SETTLED HERE, in the transaction that clears the column.
                #
                # This is the handoff, and it exists to settle which of the two
                # delivery paths ships each held incident — because both of them
                # can, and the operator must receive each one exactly once.
                # The split is on the scope's
                # ``AlertRuleState.last_notified_at``, because that column is
                # precisely what dispatch's re-send gate reads:
                #
                # * NULL — nobody has ever been told about this scope. The gate
                #   short-circuits on the NULL and fires REGARDLESS of the
                #   cooldown, so the next collection delivers the scope on its
                #   own. Leaving the row would have the flusher's drain arm mint
                #   a digest of the same scope within the minute and the
                #   operator would receive it twice, so the row is DISCARDED and
                #   the immediate path owns it. That path re-reads the scope, so
                #   what arrives is the CURRENT number rather than a reading up
                #   to a whole cadence period old; a scope that fell quiet while
                #   held is therefore not delivered at all, the same trade
                #   ``alert_flush.PENDING_ITEM_MAX_AGE`` already makes over
                #   these rows.
                #
                # * NOT NULL — a digest this destination already sent reported
                #   this scope, and ``alerts._stamp_rule_state`` runs for a
                #   digest send exactly as it does for an immediate one. The
                #   gate then requires BOTH a strictly newer bucket and an
                #   elapsed cooldown, and neither holds at the moment of the
                #   switch: ``last_anomaly_bucket`` is advanced on buffered
                #   collections too, and ``cooldown_minutes`` defaults to 1440.
                #   The immediate path will NOT re-offer these rows, so they are
                #   KEPT and the drain arm delivers them on its next tick — it
                #   beats every 60s against a 300s collection, and it is the one
                #   path that ships a buffered row as it was buffered.
                #
                # The second case is the common one, not a corner, because a
                # state row carries no DIRECTION while a buffered row does: a
                # held DROP sits on the state row an unrelated SPIKE stamped,
                # carrying its own ``correlation_group_id`` and its own Inbox
                # card, and nothing re-offers it. Discarding it destroys an
                # incident that was never delivered and leaves the scope silent
                # for up to ``cooldown_minutes`` — the undeliverable-sibling
                # trap ``dispatch._buffer_pending_items`` argues in full, and
                # the one the drain arm refuses to filter on this same column to
                # avoid.
                #
                # Only on cadence -> NULL. A cadence CHANGE (daily -> hourly)
                # still has a next window, and the watermark above starts its
                # clock fresh, so what is held is delivered on the new schedule
                # rather than settled here at all.
                #
                # Rule states are deliberately NOT cleared, which is the whole
                # difference from the disable branch below. They carry
                # ``last_notified_at``, and off a cadence that column IS the
                # rate limiter (on a cadence, the cadence is) — so a scope the
                # last digest already reported stays quiet until its cooldown
                # lapses instead of being re-announced the moment someone saves.
                # Clearing them to make the NULL case universal was the other
                # candidate on the table and is not available for that same
                # reason: the column is per SCOPE, so nulling it to release a
                # held DROP would also re-announce the SPIKE the last digest
                # just reported.
                already_notified = (
                    select(AlertRuleState.id)
                    .where(
                        AlertRuleState.rule_id == AlertPendingItem.rule_id,
                        AlertRuleState.scope_type == AlertPendingItem.scope_type,
                        AlertRuleState.scope_ref == AlertPendingItem.scope_ref,
                        # A ``metric`` scope stores NULL here on BOTH tables, and
                        # SQL reads NULL = NULL as unknown, so a plain equality
                        # would match no metric row: every one of them would
                        # read as never-notified and be discarded. Same shape,
                        # and same reason, as the partial unique indexes that
                        # key these two tables.
                        or_(
                            AlertRuleState.scan_config_id == AlertPendingItem.scan_config_id,
                            and_(
                                AlertRuleState.scan_config_id.is_(None),
                                AlertPendingItem.scan_config_id.is_(None),
                            ),
                        ),
                        AlertRuleState.last_notified_at.is_not(None),
                    )
                    .correlate(AlertPendingItem)
                    .exists()
                )
                # NOT EXISTS rather than "the state row is NULL", so a buffered
                # row with no state row AT ALL — the rule was disabled and
                # re-enabled, which drops them — is discarded too. Dispatch
                # rebuilds that state on the next collection and sends
                # unconditionally, so the immediate path owns it exactly as it
                # owns the NULL.
                await session.execute(
                    delete(AlertPendingItem)
                    .where(
                        AlertPendingItem.destination_id == destination.id,
                        ~already_notified,
                    )
                    .execution_options(synchronize_session=False)
                )
    if "enabled" in update_dict:
        destination.enabled = update_dict["enabled"]
        if destination.enabled is False:
            await clear_rule_states(session, [rule.id for rule in destination.rules])
            # "Disable a destination" already means "forget its alerting
            # state". A buffer that survived would make a re-enable ship
            # measurements from before the disable as if they were current.
            await session.execute(
                delete(AlertPendingItem).where(AlertPendingItem.destination_id == destination.id)
            )
            destination.last_flushed_at = None
    if destination.type == AlertDestinationType.slack and "webhook_url" in update_dict:
        webhook_url = update_dict["webhook_url"]
        if webhook_url is not None:
            destination.webhook_url_encrypted = _encrypt_secret(
                validate_slack_webhook_url(webhook_url)
            )
    if destination.type == AlertDestinationType.telegram:
        if "bot_token" in update_dict:
            bot_token = update_dict["bot_token"]
            if bot_token is not None:
                destination.bot_token_encrypted = _encrypt_secret(
                    validate_telegram_bot_token(bot_token)
                )
        if "chat_id" in update_dict:
            chat_id = update_dict["chat_id"]
            # An explicit null used to be handed straight to
            # ``validate_telegram_chat_id``, whose "required" arm raises a bare
            # ValueError — raised inside the service, where nothing catches it
            # but the catch-all handler, so a body the schema had accepted came
            # back as ``Internal server error``. Refuse it by name instead: a
            # Telegram destination with no chat id has nowhere to send, so
            # "clear the chat id" is not an operation this channel offers.
            #
            # This is ``bot_token``'s shape immediately above, minus the silent
            # skip, and deliberately so: skipping a null bot_token leaves the
            # destination working with the stored one, whereas skipping a null
            # chat_id would look to the caller like the clear had happened.
            if chat_id is None:
                raise HTTPException(
                    status_code=422,
                    detail="Telegram chat_id cannot be cleared; send a new chat id instead",
                )
            destination.chat_id = validate_telegram_chat_id(chat_id)
    if destination.type == AlertDestinationType.webhook:
        # Field validators already normalized these values and settled their
        # shape; the host they name was resolved above, off the event loop.
        if "target_url" in update_dict and update_dict["target_url"] is not None:
            destination.target_url_encrypted = _encrypt_secret(update_dict["target_url"])
        if "webhook_header_name" in update_dict:
            destination.webhook_header_name = update_dict["webhook_header_name"]
        header_value = update_dict.get("webhook_header_value")
        if header_value is not None:
            destination.webhook_header_value_encrypted = _encrypt_secret(header_value)
        # The header is a pair, and its name is what says it exists: a null name
        # (the "Remove secret header" body sends both halves as null) removes the
        # secret with it. Clearing the name alone used to stop the header going
        # out but left the secret's ciphertext in the row for good. A
        # null or blank VALUE next to a kept name still means "keep the stored
        # secret", which is how an edit that only renames the header works.
        if destination.webhook_header_name is None:
            destination.webhook_header_value_encrypted = None
    if destination.type == AlertDestinationType.email:
        await assert_from_override_allowed(
            session, project.organization_id, update_dict.get("email_from_address")
        )
        # Field validators on AlertDestinationUpdate already normalized these.
        if "email_recipients" in update_dict and update_dict["email_recipients"] is not None:
            destination.email_recipients = update_dict["email_recipients"]
        if "email_from_address" in update_dict:
            destination.email_from_address = update_dict["email_from_address"]
        if "email_subject_template" in update_dict:
            destination.email_subject_template = update_dict["email_subject_template"]
    if destination.type == AlertDestinationType.jira:
        if "jira_base_url" in update_dict and update_dict["jira_base_url"] is not None:
            destination.jira_base_url = update_dict["jira_base_url"]
        if "jira_auth_email" in update_dict and update_dict["jira_auth_email"] is not None:
            destination.jira_auth_email = update_dict["jira_auth_email"]
        jira_token = update_dict.get("jira_api_token")
        if jira_token is not None:
            destination.jira_api_token_encrypted = _encrypt_secret(jira_token)
        if "jira_project_key" in update_dict and update_dict["jira_project_key"] is not None:
            destination.jira_project_key = update_dict["jira_project_key"]
        if "jira_issue_type" in update_dict and update_dict["jira_issue_type"] is not None:
            destination.jira_issue_type = update_dict["jira_issue_type"]
    if destination.type == AlertDestinationType.linear:
        linear_key = update_dict.get("linear_api_key")
        if linear_key is not None:
            destination.linear_api_key_encrypted = _encrypt_secret(linear_key)
        if "linear_team_id" in update_dict and update_dict["linear_team_id"] is not None:
            destination.linear_team_id = update_dict["linear_team_id"]
        if "linear_state_id" in update_dict:
            destination.linear_state_id = update_dict["linear_state_id"]
        if "linear_label_ids" in update_dict:
            destination.linear_label_ids = update_dict["linear_label_ids"]

    await session.commit()
    destination = await get_destination(
        session,
        project_id=project.id,
        destination_id=destination_id,
    )
    return await build_destination_response(session, destination)


async def delete_destination(
    session: AsyncSession,
    slug: str,
    destination_id: uuid.UUID,
) -> str:
    """Delete a destination and return the NAME it had, for the audit entry.

    Returned rather than left to the caller to fetch: the row is gone once this
    commits, so a route that wanted to name it had to load the destination a
    second time beforehand — and the only loader exposed on the service facade is
    ``get_destination_response``, which drags in the four delete-impact aggregates
    of ``load_destination_health`` purely to read one string.
    """
    project = await _get_project(session, slug)
    destination = await get_destination(
        session,
        project_id=project.id,
        destination_id=destination_id,
    )
    name = destination.name
    await clear_rule_states(session, [rule.id for rule in destination.rules])
    await session.delete(destination)
    await session.commit()
    # ``AlertDestination.rules`` cascades, so this delete took every rule the
    # destination owned with it — and each of those was an indexed document.
    # Creating or renaming a destination needs no such refresh: no document kind
    # carries a destination's own text.
    await _refresh_main_search_index(session, project.id, slug)
    return name


async def create_rule(
    session: AsyncSession,
    slug: str,
    destination_id: uuid.UUID,
    data: AlertRuleCreate,
) -> AlertRuleResponse:
    project = await _get_project(session, slug)
    _reject_demo_ai_explanation(
        is_demo=project.is_demo,
        ai_explanation_enabled=data.ai_explanation_enabled,
    )
    destination = await get_destination(
        session,
        project_id=project.id,
        destination_id=destination_id,
    )
    await validate_filters(
        session,
        project_id=project.id,
        filters=data.filters,
    )
    await validate_scan_config(
        session,
        project_id=project.id,
        scan_config_id=data.scan_config_id,
    )
    try:
        message_format, message_template, items_template = validate_template_configuration(
            destination_type=destination.type,
            message_format=data.message_format,
            message_template=data.message_template,
            items_template=data.items_template,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    rule = AlertRule(
        destination_id=destination.id,
        scan_config_id=data.scan_config_id,
        name=data.name,
        enabled=data.enabled,
        include_project_total=data.include_project_total,
        include_event_types=data.include_event_types,
        include_events=data.include_events,
        include_schema_drifts=data.include_schema_drifts,
        include_distribution_drifts=data.include_distribution_drifts,
        include_release_regressions=data.include_release_regressions,
        include_variable_value_drifts=data.include_variable_value_drifts,
        include_metrics=data.include_metrics,
        include_source_freshness=data.include_source_freshness,
        include_lifecycle=data.include_lifecycle,
        include_property_drifts=data.include_property_drifts,
        notify_on_spike=data.notify_on_spike,
        notify_on_drop=data.notify_on_drop,
        ai_explanation_enabled=data.ai_explanation_enabled,
        notify_owners=data.notify_owners,
        min_percent_delta=data.min_percent_delta,
        min_absolute_delta=data.min_absolute_delta,
        min_expected_count=data.min_expected_count,
        cooldown_minutes=data.cooldown_minutes,
        message_template=message_template,
        items_template=items_template,
        message_format=message_format,
    )
    session.add(rule)
    await session.flush()
    await replace_rule_filters(
        session,
        rule=rule,
        filters=data.filters,
    )
    await session.commit()
    await _refresh_main_search_index(session, project.id, slug)
    _destination, refreshed_rule = await get_rule(
        session,
        project_id=project.id,
        destination_id=destination.id,
        rule_id=rule.id,
    )
    return await build_rule_response(session, refreshed_rule)


async def _validated_rule_changes(
    session: AsyncSession,
    *,
    project: Project,
    destination: AlertDestination,
    rule: AlertRule,
    data: AlertRuleUpdate,
) -> tuple[dict[str, Any], list[AlertRuleFilterPayload] | None]:
    """A rule PATCH body checked against the stored rule, without applying it.

    Returns the column changes and the replacement filters (None when the body
    leaves them alone). Shared by ``update_rule``, which writes them, and
    ``draft_rule``, which replays them without writing, so a draft the
    replay accepted is one Save would accept too.
    """
    update_dict = data.model_dump(exclude_unset=True)
    _reject_demo_ai_explanation(
        is_demo=project.is_demo,
        ai_explanation_enabled=update_dict.get("ai_explanation_enabled"),
    )

    # Re-check "at least one direction" against the MERGED row rather than
    # against the body alone. ``AlertRuleBase.validate_direction`` only ever
    # sees the fields the request mentioned, so PATCHing
    # ``{"notify_on_spike": false}`` onto a rule whose ``notify_on_drop`` is
    # already off sails past it: the validator reads the missing side as None,
    # not as the false that is actually stored. Both columns then end up false,
    # the direction gates in ``alerting_matching.rule_matches_anomaly`` reject
    # every anomaly whatever its direction, and the result is a rule that is
    # stored enabled, renders as enabled and can never fire again.
    #
    # This has to live here and not in the schema because only the service has
    # the stored row. The create path needs no equivalent — both columns default
    # to true, and a body that turns both off in one go is still caught by the
    # schema validator, which is why that one stays.
    notify_on_spike = update_dict.get("notify_on_spike", rule.notify_on_spike)
    notify_on_drop = update_dict.get("notify_on_drop", rule.notify_on_drop)
    if not notify_on_spike and not notify_on_drop:
        raise HTTPException(
            status_code=422,
            detail="At least one alert direction must be enabled",
        )

    filters_payload = data.filters if "filters" in update_dict else None
    update_dict.pop("filters", None)
    if filters_payload is not None:
        await validate_filters(
            session,
            project_id=project.id,
            filters=filters_payload,
        )
    if "scan_config_id" in update_dict:
        await validate_scan_config(
            session,
            project_id=project.id,
            scan_config_id=update_dict["scan_config_id"],
        )
    if (
        "message_format" in update_dict
        or "message_template" in update_dict
        or "items_template" in update_dict
    ):
        try:
            message_format, message_template, items_template = validate_template_configuration(
                destination_type=destination.type,
                message_format=update_dict.get("message_format", rule.message_format),
                message_template=update_dict.get("message_template", rule.message_template),
                items_template=update_dict.get("items_template", rule.items_template),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        update_dict["message_format"] = message_format
        update_dict["message_template"] = message_template
        update_dict["items_template"] = items_template

    return update_dict, filters_payload


async def draft_rule(
    session: AsyncSession,
    *,
    project: Project,
    destination: AlertDestination,
    rule: AlertRule,
    data: AlertRuleUpdate,
) -> AlertRule:
    """The stored rule with a draft's changes laid over it, never persisted.

    The draft is the editor's unsaved PATCH body, checked exactly as Save checks
    it. The result is a TRANSIENT copy: it is not added to the session and is
    linked to nothing persistent, so no flush can write it, and the stored rule
    is untouched — the replay of unsaved edits must not become the edit.
    """
    update_dict, filters_payload = await _validated_rule_changes(
        session, project=project, destination=destination, rule=rule, data=data
    )
    columns = {attr.key: getattr(rule, attr.key) for attr in sa_inspect(AlertRule).column_attrs}
    columns.update(update_dict)
    draft = AlertRule(**columns)
    source_filters = (
        [(payload.field, payload.operator, list(payload.values)) for payload in filters_payload]
        if filters_payload is not None
        else [(row.field, row.operator, list(row.values or [])) for row in rule.filters]
    )
    draft.filters = [
        AlertRuleFilter(field=field, operator=operator, values=values, position=position)
        for position, (field, operator, values) in enumerate(source_filters)
    ]
    return draft


async def update_rule(
    session: AsyncSession,
    slug: str,
    destination_id: uuid.UUID,
    rule_id: uuid.UUID,
    data: AlertRuleUpdate,
) -> AlertRuleResponse:
    project = await _get_project(session, slug)
    destination, rule = await get_rule(
        session,
        project_id=project.id,
        destination_id=destination_id,
        rule_id=rule_id,
    )
    update_dict, filters_payload = await _validated_rule_changes(
        session, project=project, destination=destination, rule=rule, data=data
    )

    if "enabled" in update_dict and update_dict["enabled"] is False:
        await clear_rule_states(session, [rule.id])

    for key, value in update_dict.items():
        setattr(rule, key, value)

    if filters_payload is not None:
        await replace_rule_filters(
            session,
            rule=rule,
            filters=filters_payload,
        )

    await session.commit()
    await _refresh_main_search_index(session, project.id, slug)
    _destination, refreshed_rule = await get_rule(
        session,
        project_id=project.id,
        destination_id=destination_id,
        rule_id=rule_id,
    )
    return await build_rule_response(session, refreshed_rule)


async def delete_rule(
    session: AsyncSession,
    slug: str,
    destination_id: uuid.UUID,
    rule_id: uuid.UUID,
) -> str:
    """Delete a rule and return the NAME it had, for the audit entry.

    The twin of ``delete_destination`` above, returned for the twin reason: the
    row is gone once this commits, so a route that wanted to name it had to find
    it first — and the only lookup a router can reach without a project is a bare
    ``AlertRule.id``/``destination_id`` select. That select answers BEFORE
    ``get_rule`` has checked that ``slug`` owns the destination, which is how the
    two 404s of one cross-project delete came to disagree.
    """
    project = await _get_project(session, slug)
    _destination, rule = await get_rule(
        session,
        project_id=project.id,
        destination_id=destination_id,
        rule_id=rule_id,
    )
    # Read while the instance is still live: after the delete commits, touching
    # an attribute would try to refresh a row that no longer exists.
    name = rule.name
    await clear_rule_states(session, [rule.id])
    await session.delete(rule)
    await session.commit()
    await _refresh_main_search_index(session, project.id, slug)
    return name
