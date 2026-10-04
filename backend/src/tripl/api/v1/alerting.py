import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query

from tripl.api.deps import EditorUserDep, SessionDep
from tripl.models.alert_delivery import AlertDeliveryStatus
from tripl.models.alert_destination import AlertDestinationType
from tripl.models.anomaly_scope_override import RATCHET_SIGMA_CAP
from tripl.models.domain_enums import AlertInboxStatus, AnomalyDirection, MetricScopeType
from tripl.schemas.alert_owner import NotifyOwnersResponse
from tripl.schemas.alerting import (
    AlertDeliveryDetailResponse,
    AlertDeliveryListResponse,
    AlertDestinationCreate,
    AlertDestinationDraftTestRequest,
    AlertDestinationResponse,
    AlertDestinationTestResponse,
    AlertDestinationUpdate,
    AlertInboxActionRequest,
    AlertInboxActionResponse,
    AlertInboxBulkActionRequest,
    AlertInboxBulkActionResponse,
    AlertInboxGroupResponse,
    AlertInboxListResponse,
    AlertRuleCreate,
    AlertRuleResponse,
    AlertRuleSimulateResponse,
    AlertRuleUpdate,
    MonitorDetailResponse,
    MonitorMuteRequest,
    MonitorsSummaryResponse,
)
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import alert_owner_notify_service, alerting_service, audit_service

router = APIRouter(prefix="/projects/{slug}", tags=["alerting"])


@router.get("/alert-destinations", response_model=list[AlertDestinationResponse])
async def list_alert_destinations(session: SessionDep, slug: str) -> list[AlertDestinationResponse]:
    return await alerting_service.list_destinations(session, slug)


@router.get("/monitors-summary", response_model=MonitorsSummaryResponse)
async def get_monitors_summary(session: SessionDep, slug: str) -> MonitorsSummaryResponse:
    return await alerting_service.get_monitors_summary(session, slug)


@router.post("/alert-destinations", response_model=AlertDestinationResponse, status_code=201)
async def create_alert_destination(
    session: SessionDep,
    slug: str,
    data: AlertDestinationCreate,
    current_user: EditorUserDep,
) -> AlertDestinationResponse:
    dest = await alerting_service.create_destination(session, slug, data)
    await audit_service.record(
        session,
        user=current_user,
        action="alert_destination.create",
        target_type="alert_destination",
        target_id=dest.id,
        target_name=dest.name,
        project_slug=slug,
        payload=data.model_dump(),
    )
    return dest


@router.get("/alert-destinations/{destination_id}", response_model=AlertDestinationResponse)
async def get_alert_destination(
    session: SessionDep, slug: str, destination_id: uuid.UUID
) -> AlertDestinationResponse:
    return await alerting_service.get_destination(session, slug, destination_id)


@router.patch("/alert-destinations/{destination_id}", response_model=AlertDestinationResponse)
async def update_alert_destination(
    session: SessionDep,
    slug: str,
    destination_id: uuid.UUID,
    data: AlertDestinationUpdate,
    current_user: EditorUserDep,
) -> AlertDestinationResponse:
    dest = await alerting_service.update_destination(session, slug, destination_id, data)
    await audit_service.record(
        session,
        user=current_user,
        action="alert_destination.update",
        target_type="alert_destination",
        target_id=dest.id,
        target_name=dest.name,
        project_slug=slug,
        payload=data.model_dump(exclude_unset=True),
    )
    return dest


@router.delete("/alert-destinations/{destination_id}", status_code=204)
async def delete_alert_destination(
    session: SessionDep,
    slug: str,
    destination_id: uuid.UUID,
    current_user: EditorUserDep,
) -> None:
    # The name comes back from the delete itself, which already loaded the row.
    # Asking the service for the destination first meant
    # ``get_destination_response`` — the four delete-impact aggregates of
    # ``load_destination_health`` — to read one string off a row that is about to
    # be deleted.
    name = await alerting_service.delete_destination(session, slug, destination_id)
    await audit_service.record(
        session,
        user=current_user,
        action="alert_destination.delete",
        target_type="alert_destination",
        target_id=destination_id,
        target_name=name,
        project_slug=slug,
    )


@router.post(
    "/alert-destinations/test",
    response_model=AlertDestinationTestResponse,
)
async def test_alert_destination_draft(
    session: SessionDep,
    slug: str,
    data: AlertDestinationDraftTestRequest,
    current_user: EditorUserDep,
) -> AlertDestinationTestResponse:
    """Send the test message through settings that are not saved yet (AL-30).

    The destination dialog's "Send test": an unsaved destination, or an edit
    in progress, where ``destination_id`` lends the stored secrets the form
    left blank. Same contract as the saved destination's Test — editor-only,
    always 200 with ``ok``/``error`` — and the same audit action, so "who
    pressed Test" has one place to look whichever button it was.
    """
    outcome = await alerting_service.send_draft_destination_test(session, slug, data)
    result = outcome.response
    await audit_service.record(
        session,
        user=current_user,
        action="alert_destination.test",
        target_type="alert_destination",
        target_id=data.destination_id,
        target_name=outcome.destination_name,
        project_slug=slug,
        payload={
            "ok": result.ok,
            "error": result.error,
            "draft": True,
            "target_origin": outcome.target_origin,
        },
    )
    return result


@router.post(
    "/alert-destinations/{destination_id}/test",
    response_model=AlertDestinationTestResponse,
)
async def test_alert_destination(
    session: SessionDep,
    slug: str,
    destination_id: uuid.UUID,
    current_user: EditorUserDep,
) -> AlertDestinationTestResponse:
    """Send one fixed test message through this destination's real channel.

    Editor-only: it puts a message in somebody's Slack/Telegram/inbox and, for a
    tracker destination, opens a ticket. Always 200 — a channel refusal is the
    answer the caller asked for, not a server fault (see
    ``AlertDestinationTestResponse``).

    Recorded in the audit log rather than as an AlertDelivery, so the Delivery
    log keeps meaning "an alert fired"; see ``services/_alerting_test_send``.
    """
    # The name comes back with the result, read off the load the send already
    # did and taken BEFORE the network call — so the audit entry still names the
    # destination the operator acted on even if the row changes underneath. It
    # used to be fetched by a second call to ``get_destination``, which is
    # ``get_destination_response``: four delete-impact aggregates and a second
    # load of the same row, in front of a request that blocks for up to 10s.
    outcome = await alerting_service.send_destination_test(session, slug, destination_id)
    result = outcome.response
    await audit_service.record(
        session,
        user=current_user,
        action="alert_destination.test",
        target_type="alert_destination",
        target_id=destination_id,
        target_name=outcome.destination_name,
        project_slug=slug,
        payload={"ok": result.ok, "error": result.error},
    )
    return result


@router.post(
    "/alert-destinations/{destination_id}/rules",
    response_model=AlertRuleResponse,
    status_code=201,
)
async def create_alert_rule(
    session: SessionDep,
    slug: str,
    destination_id: uuid.UUID,
    data: AlertRuleCreate,
    current_user: EditorUserDep,
) -> AlertRuleResponse:
    rule = await alerting_service.create_rule(session, slug, destination_id, data)
    await audit_service.record(
        session,
        user=current_user,
        action="alert_rule.create",
        target_type="alert_rule",
        target_id=rule.id,
        target_name=rule.name,
        project_slug=slug,
        payload=data.model_dump(),
    )
    return rule


@router.patch(
    "/alert-destinations/{destination_id}/rules/{rule_id}",
    response_model=AlertRuleResponse,
)
async def update_alert_rule(
    session: SessionDep,
    slug: str,
    destination_id: uuid.UUID,
    rule_id: uuid.UUID,
    data: AlertRuleUpdate,
    current_user: EditorUserDep,
) -> AlertRuleResponse:
    rule = await alerting_service.update_rule(session, slug, destination_id, rule_id, data)
    await audit_service.record(
        session,
        user=current_user,
        action="alert_rule.update",
        target_type="alert_rule",
        target_id=rule.id,
        target_name=rule.name,
        project_slug=slug,
        payload=data.model_dump(exclude_unset=True),
    )
    return rule


@router.delete("/alert-destinations/{destination_id}/rules/{rule_id}", status_code=204)
async def delete_alert_rule(
    session: SessionDep,
    slug: str,
    destination_id: uuid.UUID,
    rule_id: uuid.UUID,
    current_user: EditorUserDep,
) -> None:
    # The name comes back from the delete itself, exactly as it does for a
    # destination above. Reading it here meant a SELECT in the router that knew
    # nothing about ``slug``, and it ran BEFORE the service's project-scoped
    # lookup: a rule id absent from a destination in somebody else's project got
    # this route's "Alert rule not found", while a rule id present under that
    # same foreign destination fell through to the service's "Alert destination
    # not found". Two 404s that, read together, answered "does this rule exist?"
    # for a project the caller cannot see.
    name = await alerting_service.delete_rule(session, slug, destination_id, rule_id)
    await audit_service.record(
        session,
        user=current_user,
        action="alert_rule.delete",
        target_type="alert_rule",
        target_id=rule_id,
        target_name=name,
        project_slug=slug,
    )


@router.post(
    "/alert-destinations/{destination_id}/rules/{rule_id}/simulate",
    response_model=AlertRuleSimulateResponse,
)
async def simulate_alert_rule(
    session: SessionDep,
    slug: str,
    destination_id: uuid.UUID,
    rule_id: uuid.UUID,
    days: int = Query(7, ge=1, le=90),
    cooldown_minutes_override: int | None = Query(None, ge=0, le=10080),
    # The three what-if knobs beside the cooldown one. Omit any of them and the
    # replay uses the rule's saved value; the reply reports both, as
    # ``*_used``/``*_saved``. Without these, asking "would min % 300 cut these
    # incidents" meant saving 300 onto a rule that is live-routing to a real
    # channel (part 3).
    #
    # ``sigma_threshold_override`` is the DETECTOR's sensitivity, not a rule
    # field: it re-reads the recorded anomalies, so it can only ever narrow the
    # set — see ``AlertRuleSimulateResponse``. Bounded above by the same cap the
    # false-positive ratchet respects.
    min_percent_delta_override: float | None = Query(None, ge=0),
    min_expected_count_override: float | None = Query(None, ge=0),
    sigma_threshold_override: float | None = Query(None, gt=0, le=RATCHET_SIGMA_CAP),
    # The editor's unsaved edits, as the PATCH body Save would send. Omitted,
    # the replay is of the saved rule; given, of the saved rule with these laid
    # over it, validated as Save validates them and never written (ALR-12).
    draft: Annotated[AlertRuleUpdate | None, Body()] = None,
) -> AlertRuleSimulateResponse:
    return await alerting_service.simulate_rule(
        session,
        slug,
        destination_id,
        rule_id,
        days,
        cooldown_minutes_override=cooldown_minutes_override,
        min_percent_delta_override=min_percent_delta_override,
        min_expected_count_override=min_expected_count_override,
        sigma_threshold_override=sigma_threshold_override,
        draft=draft,
    )


def _reject_cursor_with_offset(cursor: str | None, offset: int) -> None:
    # A cursor already says where the page starts; an offset on top of it
    # would be a second, conflicting answer.
    if cursor is not None and offset:
        raise HTTPException(status_code=422, detail="cursor and offset are mutually exclusive")


@router.get("/alert-deliveries", response_model=AlertDeliveryListResponse)
async def list_alert_deliveries(
    session: SessionDep,
    slug: str,
    # AlertDeliveryStatus / AlertDestinationType (not str): both columns are
    # native Postgres enums, so a garbage value used to travel all the way to
    # the driver and blow up as a 500 ("not among the defined enum values").
    # FastAPI now 422s it at the edge, like GET /events?status= already did.
    status: AlertDeliveryStatus | None = None,
    channel: AlertDestinationType | None = None,
    destination_id: uuid.UUID | None = None,
    rule_id: uuid.UUID | None = None,
    scan_config_id: uuid.UUID | None = None,
    # The page nests deliveries under their incident, so it fetches them one
    # incident at a time; `ungrouped` reaches the rows that have no incident,
    # which no value of `correlation_group_id` can select.
    correlation_group_id: uuid.UUID | None = None,
    ungrouped: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    # Keyset continuation from a previous page's `next_cursor` (ALR-27).
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> AlertDeliveryListResponse:
    _reject_cursor_with_offset(cursor, offset)
    # Contradictory by construction: a delivery with an item in the group
    # necessarily has an item WITH a group, which `ungrouped` excludes, so the
    # pair can only ever return zero rows. Rejecting it is the difference between
    # "you asked for something impossible" and "there is nothing here" — which
    # read identically to a caller holding an empty list.
    if correlation_group_id is not None and ungrouped:
        raise HTTPException(
            status_code=422,
            detail="correlation_group_id and ungrouped are mutually exclusive",
        )
    return await alerting_service.list_deliveries(
        session,
        slug,
        status=status,
        channel=channel,
        destination_id=destination_id,
        rule_id=rule_id,
        scan_config_id=scan_config_id,
        correlation_group_id=correlation_group_id,
        ungrouped=ungrouped,
        date_from=date_from,
        date_to=date_to,
        offset=offset,
        limit=limit,
        cursor=cursor,
    )


@router.get("/alert-deliveries/{delivery_id}", response_model=AlertDeliveryDetailResponse)
async def get_alert_delivery(
    session: SessionDep, slug: str, delivery_id: uuid.UUID
) -> AlertDeliveryDetailResponse:
    return await alerting_service.get_delivery(session, slug, delivery_id)


@router.post(
    "/alert-deliveries/{delivery_id}/retry",
    response_model=AlertDeliveryDetailResponse,
)
async def retry_alert_delivery(
    session: SessionDep,
    slug: str,
    delivery_id: uuid.UUID,
    current_user: EditorUserDep,
) -> AlertDeliveryDetailResponse:
    delivery = await alerting_service.retry_delivery(session, slug, delivery_id)
    await audit_service.record(
        session,
        user=current_user,
        action="alert_delivery.retry",
        target_type="alert_delivery",
        target_id=delivery.id,
        target_name=delivery.rule_name,
        project_slug=slug,
    )
    return delivery


@router.get("/monitors/{rule_id}", response_model=MonitorDetailResponse)
async def get_monitor(session: SessionDep, slug: str, rule_id: uuid.UUID) -> MonitorDetailResponse:
    return await alerting_service.get_monitor(session, slug, rule_id)


@router.post("/monitors/{rule_id}/mute", response_model=MonitorDetailResponse)
async def mute_monitor(
    session: SessionDep,
    slug: str,
    rule_id: uuid.UUID,
    data: MonitorMuteRequest,
    current_user: EditorUserDep,
) -> MonitorDetailResponse:
    monitor = await alerting_service.mute_monitor(session, slug, rule_id, data.muted_until)
    await audit_service.record(
        session,
        user=current_user,
        action="alert_rule.mute",
        target_type="alert_rule",
        target_id=monitor.rule_id,
        target_name=monitor.rule_name,
        project_slug=slug,
        payload=data.model_dump(mode="json"),
    )
    return monitor


@router.post("/monitors/{rule_id}/unmute", response_model=MonitorDetailResponse)
async def unmute_monitor(
    session: SessionDep,
    slug: str,
    rule_id: uuid.UUID,
    current_user: EditorUserDep,
) -> MonitorDetailResponse:
    monitor = await alerting_service.unmute_monitor(session, slug, rule_id)
    await audit_service.record(
        session,
        user=current_user,
        action="alert_rule.unmute",
        target_type="alert_rule",
        target_id=monitor.rule_id,
        target_name=monitor.rule_name,
        project_slug=slug,
    )
    return monitor


@router.get("/alert-inbox", response_model=AlertInboxListResponse)
async def list_alert_inbox(
    session: SessionDep,
    slug: str,
    # Inbox status is filtered in Python against the derived group status, so a
    # typo never reached the driver — it silently matched nothing and reported
    # "no incidents", which is worse than a crash. AlertInboxStatus makes the
    # typo a 422.
    status: AlertInboxStatus | None = None,
    # Bounds on `latest_delivery_at` — when the incident last spoke, which is
    # the column the card leads with and the one triage asks about. Both are
    # INCLUSIVE, and both are narrowings INSIDE the list's own window: nothing
    # here can reach an incident older than INBOX_LOOKBACK_DAYS, or one the
    # source cap dropped. The page states that; the API cannot, so it must not
    # be read as an unbounded date search.
    last_fired_from: datetime | None = None,
    last_fired_to: datetime | None = None,
    # Enums, not bare strings, for the reason `status` is one: a typo has to be
    # a 422 rather than an empty inbox nobody can explain.
    scope_type: MetricScopeType | None = None,
    direction: AnomalyDirection | None = None,
    # Case-insensitive substring over every scope name and ref in the incident,
    # so "the events I already know are fine" can be pulled up by name.
    #
    # FreeTextFilter, like every other free-text filter in the API. This one is
    # compared in PYTHON rather than bound into Postgres, so it cannot hit the
    # asyncpg abort the type was written for — but the type is also what decides
    # what `?scope=%00` MEANS, and the two candidate answers are "match nothing"
    # and "the filter the user plainly meant". schemas/text_filters.py argues
    # for the second, and a filter here that answered differently from the eight
    # beside it would be inconsistent rather than stricter.
    scope: Annotated[FreeTextFilter | None, Query(max_length=200)] = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    # Keyset continuation from a previous page's `next_cursor` (ALR-27).
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> AlertInboxListResponse:
    _reject_cursor_with_offset(cursor, offset)
    return await alerting_service.list_alert_inbox(
        session,
        slug,
        status=status,
        filters=alerting_service.InboxFilters(
            last_fired_from=last_fired_from,
            last_fired_to=last_fired_to,
            scope_type=scope_type,
            direction=direction,
            scope=scope,
        ),
        offset=offset,
        limit=limit,
        cursor=cursor,
    )


# ``audit_log.target_name`` is String(255), and a wide incident's joined scope
# list can exceed it.
_AUDIT_TARGET_NAME_MAX = 255


def incident_audit_name(
    group: AlertInboxGroupResponse | None, correlation_group_id: uuid.UUID
) -> str:
    """What the audit log calls the incident an ``alert_inbox.*`` row is about.

    Both routes used to record ``str(correlation_group_id)``, which made the
    project Audit log a wall of hex — 28 of 31 captured rows on the one project
    with real inbox history — while every other action on the same page named its
    target. Neither affordance on the row rescued it: the title attribute reveals
    the FULL UUID, and expanding shows only the action body.

    Named off the SAME fields the incident card renders — the newest item's
    ``scope_type`` and the group's ``scope_names``, joined the way
    AlertingInbox.tsx joins them — so the trail reads like the thing the operator
    clicked, including the builder's cap of 8 names. ``target_id`` keeps the
    UUID, so lookups are unaffected.

    ``group`` is ``None`` when the batch acted on an incident whose deliveries
    were deleted before the response was rebuilt (see
    ``AlertInboxBulkActionResponse.groups``); that row falls back to the id
    rather than losing its target.
    """
    if group is None or not group.scope_names:
        return str(correlation_group_id)
    label = f"{group.scope_type} · {', '.join(group.scope_names)}"
    if len(label) <= _AUDIT_TARGET_NAME_MAX:
        return label
    return f"{label[: _AUDIT_TARGET_NAME_MAX - 1]}…"


# Registered BEFORE the /{correlation_group_id} routes below, and it must STAY
# there. "bulk-actions" is a literal segment competing with a path parameter that
# FastAPI coerces to a UUID, so whichever route is registered first wins the
# match: registered after, any route on the /{correlation_group_id} template that
# shares this one's method would swallow the request and 422 it as a malformed
# UUID — a rejection of a URL that is spelled exactly right, and one that reads
# like a client bug. Today only the POST .../actions and GET /{id} routes sit on
# that template, so nothing collides on POST yet; the ordering is what keeps that
# true when the next route is added, since nothing else in the file would stop
# it. The variables router has the identical hazard with its literal "drifts"
# segment and solves it the same way (api/v1/variables.py).
@router.post("/alert-inbox/bulk-actions", response_model=AlertInboxBulkActionResponse)
async def apply_alert_inbox_bulk_action(
    session: SessionDep,
    slug: str,
    data: AlertInboxBulkActionRequest,
    current_user: EditorUserDep,
) -> AlertInboxBulkActionResponse:
    """Apply one triage decision to a selection of incidents.

    A shortcut for N clicks on the sibling single-incident route, not a new kind
    of object: the decision is copied into each incident's own state. The service
    validates every id before mutating anything and commits once, so this either
    applies to the whole selection or to none of it.
    """
    result = await alerting_service.apply_alert_inbox_bulk_action(
        session,
        slug,
        data,
        current_user.id,
    )
    # One audit row PER GROUP, reusing the single route's own
    # ``alert_inbox.{action}`` names and its ``alert_correlation_group`` target
    # type, so a bulk mute is searchable by exactly the same query as a
    # hand-clicked one. NOT one batch row with ``target_id=None``:
    # ``audit_log.target_id`` holds a single UUID, and an audit trail that cannot
    # say WHICH incident was muted is worse than no trail at all. ``batch_id`` in
    # the payload is what re-joins the N rows into the one click that wrote them.
    #
    # The id list is excluded from the per-row payload: it would repeat the same
    # (up to 200) ids in all 200 rows, and ``target_id`` already names the one
    # incident this row is about. ``batch_size`` keeps the "how big was that
    # click" question answerable from any single row.
    acted_group_ids = alerting_service.dedupe_correlation_group_ids(data.correlation_group_ids)
    action_payload = data.model_dump(exclude={"correlation_group_ids"})
    # Matched by id, not by position: ``groups`` can be shorter than the batch.
    rebuilt_groups = {group.correlation_group_id: group for group in result.groups}
    for group_id in acted_group_ids:
        await audit_service.record(
            session,
            user=current_user,
            action=f"alert_inbox.{data.action}",
            target_type="alert_correlation_group",
            target_id=group_id,
            target_name=incident_audit_name(rebuilt_groups.get(group_id), group_id),
            project_slug=slug,
            payload={
                **action_payload,
                "batch_id": result.batch_id,
                "batch_size": len(acted_group_ids),
            },
            # One transaction for the whole batch. ``record`` commits per call by
            # default, which for 200 incidents meant 200 separate transactions
            # after the state change was already durable — a failure partway
            # through left incidents silenced with only a partial record of who
            # silenced them, and the response still said 200 OK.
            commit=False,
        )
    await session.commit()
    return result


@router.get("/alert-inbox/{correlation_group_id}", response_model=AlertInboxGroupResponse)
async def get_alert_inbox_group(
    session: SessionDep,
    slug: str,
    correlation_group_id: uuid.UUID,
) -> AlertInboxGroupResponse:
    """Resolve one incident by id, ignoring the list's lookback window.

    Alert messages deep-link the incident they describe, and the reader opens
    them late; before this route the link landed on a page of 20 unrelated
    incidents with no explanation.
    """
    return await alerting_service.get_alert_inbox_group(session, slug, correlation_group_id)


@router.post("/alert-inbox/{correlation_group_id}/actions", response_model=AlertInboxActionResponse)
async def apply_alert_inbox_action(
    session: SessionDep,
    slug: str,
    correlation_group_id: uuid.UUID,
    data: AlertInboxActionRequest,
    current_user: EditorUserDep,
) -> AlertInboxActionResponse:
    result = await alerting_service.apply_alert_inbox_action(
        session,
        slug,
        correlation_group_id,
        data,
        current_user.id,
    )
    await audit_service.record(
        session,
        user=current_user,
        action=f"alert_inbox.{data.action}",
        target_type="alert_correlation_group",
        target_id=correlation_group_id,
        target_name=incident_audit_name(result.group, correlation_group_id),
        project_slug=slug,
        payload=data.model_dump(),
    )
    return result


@router.post(
    "/alert-inbox/{correlation_group_id}/notify-owners",
    response_model=NotifyOwnersResponse,
)
async def notify_alert_inbox_owners(
    session: SessionDep,
    slug: str,
    correlation_group_id: uuid.UUID,
    current_user: EditorUserDep,
) -> NotifyOwnersResponse:
    """Email the owners of this incident's event type / metric once, now (F07, #260).

    Answers with one row per owner tried; a ``skipped`` or ``failed`` owner is
    part of the answer, not an error. No owners is an empty list. An owner
    emailed about this incident in the last 10 minutes is ``skipped`` ("notified
    N minutes ago"); at most 20 owners are contacted per request.
    """
    result = await alert_owner_notify_service.notify_incident_owners(
        session, slug, correlation_group_id, current_user
    )
    await audit_service.record(
        session,
        user=current_user,
        action="alert_inbox.notify_owners",
        target_type="alert_correlation_group",
        target_id=correlation_group_id,
        target_name=result.target_name,
        project=result.project,
        payload={
            "owners": [
                {"user_id": str(owner.user_id) if owner.user_id else None, "status": owner.status}
                for owner in result.response.owners
            ],
        },
    )
    return result.response
