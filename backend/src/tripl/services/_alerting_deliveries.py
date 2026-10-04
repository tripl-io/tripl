"""Alert deliveries, inbox, and correlation-state management."""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from tripl import cache
from tripl.alert_templates import has_baseline, percent_delta_or_none
from tripl.models.alert_correlation_state import AlertCorrelationState
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_rule import AlertRule
from tripl.models.anomaly_scope_override import (
    RATCHETABLE_SCOPE_TYPES,
    AnomalyScopeOverride,
    ratchet_min_expected_count,
    ratchet_sigma_threshold,
)
from tripl.models.domain_enums import AlertInboxStatus, AnomalyDirection, MetricScopeType
from tripl.models.project_anomaly_settings import (
    DEFAULT_MIN_EXPECTED_COUNT,
    DEFAULT_SIGMA_THRESHOLD,
    ProjectAnomalySettings,
)
from tripl.models.scan_config import ScanConfig
from tripl.models.signal_triage import SignalTriage
from tripl.models.user import User
from tripl.schemas.alerting import (
    AlertDeliveryDetailResponse,
    AlertDeliveryListResponse,
    AlertDeliveryResponse,
    AlertInboxActionRequest,
    AlertInboxActionResponse,
    AlertInboxBulkActionRequest,
    AlertInboxBulkActionResponse,
    AlertInboxGroupResponse,
    AlertInboxListResponse,
    AlertInboxRuleRef,
    AlertInboxStatusCounts,
)
from tripl.services import alert_owner_routing
from tripl.services._alerting_cursors import (
    decode_delivery_cursor,
    decode_inbox_cursor,
    encode_delivery_cursor,
    encode_inbox_cursor,
)
from tripl.services._celery_dispatch import dispatch
from tripl.services._signal_verdict_read import VERDICT_ACTIONS, status_agrees
from tripl.services._signal_verdict_rows import delete_verdict_rows
from tripl.services.project_lookup import resolve_project as _get_project

logger = logging.getLogger(__name__)

INBOX_LOOKBACK_DAYS = 30
INBOX_MAX_SOURCE_ITEMS = 2000

#: How many still-silenced incidents the list holds PAST the window, per status
#:.
#:
#: The window above is a window on DELIVERIES, and silencing an incident is
#: precisely the act of stopping its deliveries. A suppressed group's newest
#: delivery therefore freezes at the moment it was suppressed and, 30 days later,
#: the group contributes no rows, gets no entry in ``groups``, and drops out of
#: the list — while ``_suppressed_correlation_group_ids``
#: (worker/tasks/metrics/dispatch.py) goes on enforcing the suppression with no
#: time bound at all. For an INDEFINITE mute that is terminal: nothing releases
#: it, and the only Unmute control lives on the card that disappeared.
#:
#: PER STATUS, not one shared budget, and that is why this is a rule and not just
#: a number. The orphan population is dominated by ``resolved`` and
#: ``acknowledged`` groups whose scope stopped existing — those never reach
#: ``_reopen_closed_incidents``'s ``closed_keys``, so they accumulate without
#: bound. One shared budget would let them starve every muted one, and
#: ``?status=muted`` would answer "nothing" with an indefinite mute still in
#: force: the filed bug surviving its own fix.
#:
#: 50 because this is a safety net under decisions a human took by hand, not a
#: second inbox. Measured on production 2026-08-15: 12 correlation states, 5 of
#: them indefinitely muted, against 271 delivery items inside the window.
INBOX_MAX_SILENCED_RESCUES = 50

#: Sorts a state that was never acted on to the bottom of the rescue ranking.
_NEVER_ACTED = datetime.min.replace(tzinfo=UTC)

# One joined row behind an inbox group: the item that fired plus the delivery
# that carried it and the three names the card renders.
InboxGroupRow = tuple[AlertDeliveryItem, AlertDelivery, AlertDestination, AlertRule, ScanConfig]

# COLUMNS ONLY, and that is load-bearing rather than incidental.
# ``_build_inbox_group_response`` reads ``row[1].id``/``.created_at``,
# ``row[2].name``, ``row[3].id``/``.name`` and ``row[4].name`` beside the item's
# own columns, and nothing in this module dereferences a RELATIONSHIP off any of
# the five. The select carries no loader options, so a collection one of those
# models declares ``lazy="selectin"`` joins every execution of it — and there are
# four (the list's source rows, the silenced-orphan rescue, the single-card
# rebuild and the per-group unwindowed fallback), each once per request.
# ``ScanConfig.scan_jobs`` was such a collection, and being unbounded it charged
# every inbox request the entire scan history of every config on the page
# (the reasoning now lives on the relationship in
# models/scan_config.py). Want more than a column? Add it to the select, or take
# a second query; do not reach through one of these entities.
_INBOX_GROUP_SELECT = (
    select(AlertDeliveryItem, AlertDelivery, AlertDestination, AlertRule, ScanConfig)
    .options(
        noload(AlertDelivery.items),
        noload(AlertDestination.rules),
        noload(AlertRule.filters),
    )
    .join(AlertDelivery, AlertDelivery.id == AlertDeliveryItem.delivery_id)
    .join(AlertDestination, AlertDestination.id == AlertDelivery.destination_id)
    .join(AlertRule, AlertRule.id == AlertDelivery.rule_id)
    .join(ScanConfig, ScanConfig.id == AlertDelivery.scan_config_id)
)

# Which INBOX_MAX_SOURCE_ITEMS rows the cap admits, decided the same way
# everywhere it is applied. ``created_at`` alone is not a total order — a scan
# writes several deliveries in the same instant — so at the cap boundary two
# queries could admit DIFFERENT rows and the sidebar badge would disagree with
# the page it badges. ``_alerting_health`` already tie-breaks this way.
#
# The item id is the last term because the cap counts ITEM rows, not deliveries:
# one delivery fans out into many items that share its whole delivery-level key,
# so ordering only by delivery leaves the cut INSIDE a delivery undetermined and
# reopens the same disagreement one level down.
_INBOX_SOURCE_ORDER = (
    AlertDelivery.created_at.desc(),
    AlertDelivery.id.desc(),
    AlertDeliveryItem.id.desc(),
)


def delivery_to_response(
    delivery: AlertDelivery,
    *,
    destination_name: str,
    rule_name: str,
    scan_name: str,
) -> AlertDeliveryResponse:
    is_local = delivery.channel == AlertDestinationType.demo_sink
    return AlertDeliveryResponse(
        id=delivery.id,
        project_id=delivery.project_id,
        scan_config_id=delivery.scan_config_id,
        scan_job_id=delivery.scan_job_id,
        destination_id=delivery.destination_id,
        rule_id=delivery.rule_id,
        destination_name=destination_name,
        rule_name=rule_name,
        scan_name=scan_name,
        status=delivery.status,
        channel=delivery.channel,
        matched_count=delivery.matched_count,
        payload_snapshot=delivery.payload_snapshot,
        error_message=delivery.error_message,
        is_local=is_local,
        is_simulated=is_local,
        created_at=delivery.created_at,
        updated_at=delivery.updated_at,
        sent_at=delivery.sent_at,
    )


async def list_deliveries(
    session: AsyncSession,
    slug: str,
    *,
    status: str | None = None,
    channel: str | None = None,
    destination_id: uuid.UUID | None = None,
    rule_id: uuid.UUID | None = None,
    scan_config_id: uuid.UUID | None = None,
    correlation_group_id: uuid.UUID | None = None,
    ungrouped: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    cursor: str | None = None,
) -> AlertDeliveryListResponse:
    project = await _get_project(session, slug)
    # Decoded before any query so a bad cursor is a 422 and costs nothing.
    after = decode_delivery_cursor(cursor) if cursor is not None else None

    filters = [AlertDelivery.project_id == project.id]
    if status is not None:
        filters.append(AlertDelivery.status == status)
    if channel is not None:
        filters.append(AlertDelivery.channel == channel)
    if destination_id is not None:
        filters.append(AlertDelivery.destination_id == destination_id)
    if rule_id is not None:
        filters.append(AlertDelivery.rule_id == rule_id)
    if scan_config_id is not None:
        filters.append(AlertDelivery.scan_config_id == scan_config_id)
    # The alerting page shows deliveries UNDER the incident they belong to, so it
    # asks for one incident's deliveries at a time.
    #
    # The incident is a property of the ITEM, not of the delivery: one message can
    # carry rows from several incidents, so this matches a delivery that has at
    # least one item in the group rather than comparing a column that does not
    # exist on AlertDelivery.
    if correlation_group_id is not None:
        filters.append(
            AlertDelivery.id.in_(
                select(AlertDeliveryItem.delivery_id).where(
                    AlertDeliveryItem.correlation_group_id == correlation_group_id
                )
            )
        )
    # Every item written now carries an incident, so this selects
    # the older rows — invisible to the inbox and impossible to
    # acknowledge. Nesting alone would drop them silently and leave an audit trail
    # that looks complete and is not, so the page gives them their own section.
    if ungrouped:
        filters.append(
            ~AlertDelivery.id.in_(
                select(AlertDeliveryItem.delivery_id).where(
                    AlertDeliveryItem.correlation_group_id.is_not(None)
                )
            )
        )
    if date_from is not None:
        filters.append(AlertDelivery.created_at >= date_from)
    if date_to is not None:
        filters.append(AlertDelivery.created_at <= date_to)

    total = (
        await session.execute(select(func.count(AlertDelivery.id)).where(*filters))
    ).scalar_one()
    # Keyset continuation: strictly after the last row the reader holds
    # in (created_at DESC, id DESC) order. Applied to the page query only —
    # `total` stays the whole filtered set, which is what "of N" means.
    page_filters = list(filters)
    if after is not None:
        after_created, after_id = after
        page_filters.append(
            or_(
                AlertDelivery.created_at < after_created,
                and_(AlertDelivery.created_at == after_created, AlertDelivery.id < after_id),
            )
        )
    rows = (
        await session.execute(
            select(AlertDelivery, AlertDestination.name, AlertRule.name, ScanConfig.name)
            .join(AlertDestination, AlertDestination.id == AlertDelivery.destination_id)
            .join(AlertRule, AlertRule.id == AlertDelivery.rule_id)
            .join(ScanConfig, ScanConfig.id == AlertDelivery.scan_config_id)
            .where(*page_filters)
            # `id` is the tie-break, and it is load-bearing rather than tidy:
            # `created_at` is NOT unique. `_delivery_chunks` mints several
            # deliveries inside one transaction on the immediate path, and a
            # scheduled digest mints one per rule in a single commit, so a page
            # boundary can fall inside a tied group — where OFFSET/LIMIT over an
            # unstable sort silently repeats one row on page 2 and drops
            # another. Any deterministic second key fixes it; `id` is the one
            # already carried by every row.
            .order_by(AlertDelivery.created_at.desc(), AlertDelivery.id.desc())
            .offset(offset)
            # One row past the page answers "is there more?" without a second
            # count under the keyset filter.
            .limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    last_delivery = rows[-1][0] if rows else None

    return AlertDeliveryListResponse(
        items=[
            delivery_to_response(
                delivery,
                destination_name=destination_name,
                rule_name=rule_name,
                scan_name=scan_name,
            )
            for delivery, destination_name, rule_name, scan_name in rows
        ],
        total=total,
        next_cursor=(
            encode_delivery_cursor(last_delivery.created_at, last_delivery.id)
            if has_more and last_delivery is not None
            else None
        ),
    )


async def get_delivery(
    session: AsyncSession,
    slug: str,
    delivery_id: uuid.UUID,
) -> AlertDeliveryDetailResponse:
    project = await _get_project(session, slug)
    row = (
        await session.execute(
            select(AlertDelivery, AlertDestination.name, AlertRule.name, ScanConfig.name)
            .join(AlertDestination, AlertDestination.id == AlertDelivery.destination_id)
            .join(AlertRule, AlertRule.id == AlertDelivery.rule_id)
            .join(ScanConfig, ScanConfig.id == AlertDelivery.scan_config_id)
            .where(
                AlertDelivery.project_id == project.id,
                AlertDelivery.id == delivery_id,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Alert delivery not found")

    delivery, destination_name, rule_name, scan_name = row
    items = (
        (
            await session.execute(
                select(AlertDeliveryItem)
                .where(AlertDeliveryItem.delivery_id == delivery.id)
                .order_by(
                    AlertDeliveryItem.scope_type,
                    AlertDeliveryItem.bucket.desc(),
                )
            )
        )
        .scalars()
        .all()
    )

    return AlertDeliveryDetailResponse(
        **delivery_to_response(
            delivery,
            destination_name=destination_name,
            rule_name=rule_name,
            scan_name=scan_name,
        ).model_dump(),
        items=items,
        owner_notifications=await alert_owner_routing.load_delivery_owner_notifications(
            session, delivery.id
        ),
    )


async def retry_delivery(
    session: AsyncSession,
    slug: str,
    delivery_id: uuid.UUID,
) -> AlertDeliveryDetailResponse:
    """Re-dispatch a failed alert delivery.

    Resets the row to ``pending`` (clearing the prior error and the reaper's
    attempt counter) and re-enqueues the existing ``send_alert_delivery`` task —
    the same dispatch path used when the delivery was first created. Only failed
    deliveries can be retried; a still-``pending`` row is already queued (the
    reaper backstops it) and a ``sent`` row must not be re-sent.
    """
    project = await _get_project(session, slug)
    delivery = await session.scalar(
        select(AlertDelivery).where(
            AlertDelivery.project_id == project.id,
            AlertDelivery.id == delivery_id,
        )
    )
    if delivery is None:
        raise HTTPException(status_code=404, detail="Alert delivery not found")
    if delivery.status != AlertDeliveryStatus.failed.value:
        raise HTTPException(
            status_code=409,
            detail="Only failed deliveries can be retried",
        )
    destination = await session.get(AlertDestination, delivery.destination_id)
    if destination is None or not destination.enabled:
        raise HTTPException(
            status_code=409,
            detail="Alert destination is disabled; enable it before retrying",
        )

    delivery.status = AlertDeliveryStatus.pending.value
    delivery.error_message = None
    delivery.sent_at = None
    # Fresh manual attempt: hand the reaper a clean budget so it backstops this
    # retry if the enqueue below never reaches a worker.
    delivery.dispatch_attempts = 0
    # The row is going back to a send task, so it has to be CLAIMABLE when it
    # gets there: ``alerts._claim_delivery`` is a compare-and-set that refuses a
    # row whose ``claimed_at`` is newer than ``STRANDED_DELIVERY_MINUTES``, and a
    # live lease left on this row would make the ``.delay()`` below a silent
    # no-op — the flip to `pending` and the enqueue both visible, the API
    # answering 200, and nothing going out for fifteen minutes.
    #
    # The identical clear on the reaper's failed arm
    # (``maintenance.requeue_stranded_alert_deliveries``) is the other half of
    # this: the two are the only failed -> pending flips in the system and they
    # must leave the row in the same state, or which arm happened to pick a row
    # up decides whether its next send fires.
    #
    # Unreachable today rather than a live bug, and worth stating which: every
    # send path releases its own lease when the attempt ends (``alerts.py`` on
    # both the sent and failed branches, ``alert_digest_send.py`` on all three),
    # and the one write that marks a row `failed` WITHOUT going through a send —
    # the reaper's exhaustion relabel — can only ever see an already-expired
    # lease, because ``claimed_at`` is set by the same UPDATE that bumps
    # ``updated_at`` and that arm selects on ``updated_at < now - 15min``. So
    # this is defence in depth against a `failed` row that holds a lease for any
    # reason at all (a worker SIGKILLed between its claim and its terminal
    # write, a release whose commit raised, a row written before the lease
    # existed) — not dependent on that release having run, which is exactly the
    # reason maintenance.py gives for its own copy.
    delivery.claimed_at = None
    # Commit as pending BEFORE enqueueing: if dispatch raises (broker down) the
    # row is already pending and requeue_stranded_alert_deliveries will pick it
    # up, mirroring how deliveries are first dispatched.
    await session.commit()

    # Deferred import to avoid pulling the worker task graph into the API
    # process at module load (matches scan_service's dispatch sites).
    #
    # The celery app is imported FIRST because that graph is cyclic: the app
    # module imports every task module, and ``tasks.metrics`` re-exports this
    # very task from ``tasks.alerts``. Entering at ``tasks.alerts`` in a process
    # that has not loaded the app yet therefore lands mid-cycle and raises
    # ImportError, 500ing the retry. Entering at the app loads the task modules
    # in their registration order instead. Reachable since the demo started
    # seeding a failed delivery for Retry to act on.
    import tripl.worker.celery_app  # noqa: F401
    from tripl.worker.tasks.alerts import send_alert_delivery

    await dispatch(send_alert_delivery.delay, str(delivery_id))

    return await get_delivery(session, slug, delivery_id)


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive timestamp so it can be compared with ``now``.

    ``TimestampMixin`` and the inbox correlation-state columns still use plain
    ``DateTime(timezone=True)``. SQLite hands those back naive, unlike the
    separate ``AlertRule.muted_until`` column which uses ``UtcDateTime``.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _effective_inbox_status(state: AlertCorrelationState | None, now: datetime) -> str:
    if state is None:
        return "open"
    if (
        state.status == "muted"
        and state.muted_until is not None
        and _as_utc(state.muted_until) <= now
    ):
        return "open"
    return state.status


def _silenced_orphan_group_ids(
    states: Iterable[AlertCorrelationState],
    *,
    windowed_group_ids: set[uuid.UUID],
    now: datetime,
) -> list[uuid.UUID]:
    """Incidents still under a human decision that the window can no longer see.

    An orphan is a correlation state whose group produced no delivery inside
    ``INBOX_LOOKBACK_DAYS`` — so ``list_alert_inbox`` built no card for it — and
    whose EFFECTIVE status is not ``open``. Both halves matter:

    * Not in ``windowed_group_ids``, because a group the window already covers
      must keep the windowed reading. Rescuing it too would widen every aggregate
      on a card that is on screen, which is the drift ``_build_one_inbox_group``
      documents at length.
    * ``_effective_inbox_status``, not ``state.status``, because a LAPSED mute is
      open again — and an open incident that stopped delivering is exactly the
      resolved-by-time case the window is FOR. Rescuing those would turn the
      inbox into an unbounded archive of everything that ever fired.

    Ranked by ``acted_at`` newest-first and truncated per status to
    {@link INBOX_MAX_SILENCED_RESCUES}; see that constant for why the budget is
    per status rather than shared. There is no status parameter: the per-status
    budget already makes each status's slice of this selection exactly what a
    status-filtered request would pick, so ``list_alert_inbox`` rescues every
    status and filters afterwards. ``correlation_group_id`` is the last sort term
    for the reason ``_INBOX_SOURCE_ORDER`` has one: several incidents acted on in
    a single bulk click share an ``acted_at`` to the microsecond (that is what
    the bulk-acknowledge batch DOES), so without it the cap could admit a different set
    on each request and a card would flicker in and out of the list.

    Pure, and takes the states rather than a session, so the selection rule can
    be tested without a database behind it.
    """
    by_status: dict[str, list[AlertCorrelationState]] = {}
    for state in states:
        if state.correlation_group_id in windowed_group_ids:
            continue
        effective = _effective_inbox_status(state, now)
        if effective == "open":
            continue
        by_status.setdefault(effective, []).append(state)

    def rescue_rank(state: AlertCorrelationState) -> tuple[datetime, str]:
        # A NULL `acted_at` sorts last rather than raising. `note` is the one
        # action that deliberately does not stamp it, so a state can carry a
        # status without one — and a comparison against `None` would take the
        # whole list down instead of ranking that row low.
        acted_at = _as_utc(state.acted_at) if state.acted_at is not None else _NEVER_ACTED
        return (acted_at, str(state.correlation_group_id))

    rescued: list[uuid.UUID] = []
    for candidates in by_status.values():
        candidates.sort(key=rescue_rank, reverse=True)
        rescued.extend(
            state.correlation_group_id for state in candidates[:INBOX_MAX_SILENCED_RESCUES]
        )
    return rescued


async def _load_orphan_group_rows(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    correlation_group_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[InboxGroupRow]]:
    """The delivery rows behind rescued groups, read WITHOUT the window.

    One query for the whole set, not one per group. ``_build_one_inbox_group``
    already knows how to read a single incident unwindowed — that is what the
    deep link uses — but calling it in a loop is two round trips per orphan, and
    ``AlertCorrelationState`` is never pruned, so the orphan population only
    grows. The cost of the rescue has to be bounded by a constant, not by how
    long the instance has been running.

    Capped PER GROUP with a window function rather than by a plain ``LIMIT``, and
    for the reason ``count_open_incidents`` partitions by project: a single global
    limit over the whole id set lets one long-lived incident spend the entire
    budget and return nothing at all for the others. Every column the subquery is
    read back by is labelled, because an unlabelled ``row_number()`` has no name
    to select on.
    """
    if not correlation_group_ids:
        return {}

    ranked = (
        select(
            AlertDeliveryItem.id.label("item_id"),
            func.row_number()
            .over(
                partition_by=AlertDeliveryItem.correlation_group_id,
                order_by=_INBOX_SOURCE_ORDER,
            )
            .label("row_number"),
        )
        .select_from(AlertDeliveryItem)
        .join(AlertDelivery, AlertDelivery.id == AlertDeliveryItem.delivery_id)
        .where(
            AlertDelivery.project_id == project_id,
            AlertDeliveryItem.correlation_group_id.in_(correlation_group_ids),
        )
        .subquery()
    )
    rows = (
        (
            await session.execute(
                _INBOX_GROUP_SELECT.join(ranked, ranked.c.item_id == AlertDeliveryItem.id)
                .where(ranked.c.row_number <= INBOX_MAX_SOURCE_ITEMS)
                .order_by(*_INBOX_SOURCE_ORDER)
            )
        )
        .tuples()
        .all()
    )

    grouped: dict[uuid.UUID, list[InboxGroupRow]] = {}
    for row in rows:
        group_id = row[0].correlation_group_id
        if group_id is None:
            continue
        grouped.setdefault(group_id, []).append(row)
    return grouped


async def _load_acting_user_names(
    session: AsyncSession, user_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Resolve display names for the users who acted on these incidents.

    ONE query for the whole page. Every card can carry an "already handled by X"
    line, so resolving the name per group would put a SELECT behind each row of
    the inbox.
    """
    if not user_ids:
        return {}
    rows = (await session.execute(select(User.id, User.name).where(User.id.in_(user_ids)))).all()
    # ``name`` is nullable on User, and an unnamed operator gets NO name here
    # rather than an email fallback. The inbox is readable by every project
    # member, and this endpoint previously exposed only an opaque acted_by UUID,
    # so falling back to the address would have published colleagues' emails on
    # every incident card they touched. The card renders "handled" without a
    # name.
    return {user_id: name for user_id, name in rows if name is not None}


def _acting_user_ids(states: Iterable[AlertCorrelationState | None]) -> set[uuid.UUID]:
    return {state.acted_by for state in states if state is not None and state.acted_by is not None}


def _acted_by_name(state: AlertCorrelationState | None, names: dict[uuid.UUID, str]) -> str | None:
    if state is None or state.acted_by is None:
        return None
    # A deleted user leaves acted_by NULLed by the FK, but a name can still be
    # missing if the row was written by a user this query did not resolve; the
    # card falls back to "handled" without a name rather than inventing one.
    return names.get(state.acted_by)


def _build_inbox_group_response(
    *,
    correlation_group_id: uuid.UUID,
    state: AlertCorrelationState | None,
    rows: list[InboxGroupRow],
    now: datetime,
    acted_by_name: str | None,
) -> AlertInboxGroupResponse:
    latest_item = max(rows, key=lambda row: row[0].bucket)[0]
    latest_delivery = max(rows, key=lambda row: row[1].created_at)[1]
    first_delivery_at = min(row[1].created_at for row in rows)
    scope_names = sorted({row[0].scope_name for row in rows})
    destination_names = sorted({row[2].name for row in rows})
    rules = sorted(
        {(row[3].id, row[3].name) for row in rows},
        # By NAME, with the id as the tie-break: two rules of one group can share
        # a name, and the pair has to have a stable order across requests.
        key=lambda rule: (rule[1], str(rule[0])),
    )
    rule_names = sorted({row[3].name for row in rows})
    scan_names = sorted({row[4].name for row in rows})
    delivery_ids = {row[1].id for row in rows}
    status = _effective_inbox_status(state, now)
    # Deviations that were MEASURED. A row with no baseline stores 0.0 as a
    # placeholder, and folding it in made a zero-baseline group — the loudest
    # class there is — sort as the smallest deviation in the inbox.
    #
    # ``has_baseline``, not ``> 0``: a signed catalog metric has a real baseline
    # below zero and a real measured percent beside it. Asking
    # ``> 0`` here dropped exactly those rows, so ``max_abs_percent_delta`` came
    # back null on a group whose ``percent_delta`` field — built from the SAME
    # item, in the SAME response object below — reported its 200.0%.
    baselined_deltas = [
        abs(row[0].percent_delta) for row in rows if has_baseline(row[0].expected_count)
    ]
    return AlertInboxGroupResponse(
        correlation_group_id=correlation_group_id,
        status=status,
        # Effective flag, so this payload reads like AlertRuleResponse's.
        muted=status == "muted",
        # Only while the mute is IN FORCE. A lapsed mute reports `open` above but
        # kept emitting the raw column, so the card rendered an "open" badge next
        # to "muted until <a past date>" — two contradictory claims about the
        # same row.
        muted_until=state.muted_until if state is not None and status == "muted" else None,
        note=state.note if state else None,
        false_positive_count=state.false_positive_count if state else 0,
        item_count=len(rows),
        delivery_count=len(delivery_ids),
        latest_bucket=latest_item.bucket,
        latest_delivery_at=latest_delivery.created_at,
        first_delivery_at=first_delivery_at,
        direction=latest_item.direction,
        # Size of the newest firing, from rows already in hand — no extra query.
        actual_count=latest_item.actual_count,
        expected_count=latest_item.expected_count,
        # Through the shared helper, never off the column: the stored 0.0 at a
        # zero baseline is a PLACEHOLDER and nothing may emit it
        # (see worker/tasks/metrics/dispatch.py).
        percent_delta=percent_delta_or_none(latest_item.percent_delta, latest_item.expected_count),
        max_abs_percent_delta=max(baselined_deltas) if baselined_deltas else None,
        # Routable identity of the newest item, so the incident card can offer
        # "go look at it" without expanding its deliveries first.
        scope_type=latest_item.scope_type,
        scope_ref=latest_item.scope_ref,
        event_id=latest_item.event_id,
        scope_types=sorted({row[0].scope_type for row in rows}),
        scope_names=scope_names[:8],
        destination_names=destination_names,
        rule_names=rule_names,
        rules=[AlertInboxRuleRef(id=rule_id, name=name) for rule_id, name in rules],
        scan_names=scan_names,
        acted_at=state.acted_at if state else None,
        acted_by=state.acted_by if state else None,
        acted_by_name=acted_by_name,
    )


async def _with_owners(
    session: AsyncSession,
    project_id: uuid.UUID,
    page: list[AlertInboxGroupResponse],
    rows_by_group: dict[uuid.UUID, list[InboxGroupRow]],
) -> list[AlertInboxGroupResponse]:
    """The page's cards with their owners filled in (F07, #260).

    Resolved off each card's NEWEST item — the one its ``scope_type`` /
    ``scope_ref`` already describe — and for the served page only, in one bulk
    resolution, so paging through the inbox costs at most four extra queries.
    """
    if not page:
        return page
    latest = [
        max(rows_by_group[group.correlation_group_id], key=lambda row: row[0].bucket)[0]
        for group in page
    ]
    owners = await alert_owner_routing.owner_refs_for(session, project_id, latest)
    return [
        group.model_copy(update={"owners": refs}) for group, refs in zip(page, owners, strict=True)
    ]


def _inbox_sort_key(group: AlertInboxGroupResponse) -> tuple[bool, datetime, str]:
    """Order the inbox: OPEN work first, then by activity, then deterministically.

    Effective openness is the PRIMARY term, so an incident nobody has handled can
    never be pushed off page one by one somebody already dealt with. Activity
    alone could not carry that: ``acted_at`` is stamped by acknowledge, resolve,
    mute, reopen and false_positive alike, so ranking on "newest of last-spoke
    and last-acted-on" put the last 20 incidents a human TRIAGED at ranks 1-20
    and buried every untouched one — a worse failure than the sinking it was
    meant to fix. It did not fix that either: at mute time ``acted_at`` is within
    minutes of the last delivery, so the muted group's key is frozen just the
    same and still crosses rank 20 in ~1.25 days of a 7-day mute. The change
    bought minutes.

    Reaching a handled or muted incident is therefore the job of the ``status``
    filter and paging — the frontend half of that work — and NOT of
    displacing open work. ``status`` here is the EFFECTIVE one, so a lapsed mute
    is open again and returns to the top run on its own.

    Within a run, newest activity leads, tie-broken on the correlation group id
    so equal activity never reorders between two requests and paginates a group
    into both pages or neither. Sorted with ``reverse=True``, hence ``True``
    (open) sorting ahead of ``False``.
    """
    activity = _as_utc(group.latest_delivery_at)
    if group.acted_at is not None:
        activity = max(activity, _as_utc(group.acted_at))
    return (group.status == AlertInboxStatus.open, activity, str(group.correlation_group_id))


@dataclass(frozen=True)
class InboxSourceRows:
    """What the inbox can see, and whether that is what it promised to see.

    ``truncated_at`` is ``None`` when the whole ``INBOX_LOOKBACK_DAYS`` window
    fitted under ``INBOX_MAX_SOURCE_ITEMS`` — the only case any deployment has
    ever been in. Otherwise it is the instant the VISIBLE window really starts:
    the ``created_at`` of the oldest delivery the cap admitted. One nullable
    field rather than a bool beside a datetime, so "truncated" and "starts here"
    cannot be reported inconsistently by a future caller.
    """

    rows: list[InboxGroupRow]
    truncated_at: datetime | None


async def _load_inbox_source_rows(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    cutoff: datetime,
) -> InboxSourceRows:
    """The rows the inbox list is built from: windowed, ordered and capped.

    The ONE definition of "what the inbox can see", so the list, the action's
    rebuild of a single card and the sidebar's open-incident count cannot
    disagree about it.

    Selects ONE MORE row than the cap admits, the same probe
    ``analyzers/cardinality`` uses on a warehouse row limit, because the cap and
    the window are two different bounds and only the query knows which one bit.
    The page says "last 30 days"; the code says min(30 days, newest 2000 item
    rows), and until this probe existed a caller could not tell "exactly 2000
    rows in the window" from "the window was cut short" — so a shortened list
    was indistinguishable from a quiet project and nothing on the page said
    otherwise.

    The instant reported is the oldest ADMITTED row's, not the first REJECTED
    one's: an incident is only fully readable if every one of its rows got in,
    and the extra row proves nothing about how many more share its timestamp.
    Reporting the boundary this way makes the claim "the list starts here" true
    at the cost of being one row conservative, which is the direction that
    cannot mislead.
    """
    fetched = list(
        (
            await session.execute(
                _INBOX_GROUP_SELECT.where(
                    AlertDelivery.project_id == project_id,
                    AlertDeliveryItem.correlation_group_id.is_not(None),
                    AlertDelivery.created_at >= cutoff,
                )
                .order_by(*_INBOX_SOURCE_ORDER)
                .limit(INBOX_MAX_SOURCE_ITEMS + 1)
            )
        )
        .tuples()
        .all()
    )
    if len(fetched) <= INBOX_MAX_SOURCE_ITEMS:
        return InboxSourceRows(rows=fetched, truncated_at=None)
    rows = fetched[:INBOX_MAX_SOURCE_ITEMS]
    return InboxSourceRows(rows=rows, truncated_at=_as_utc(rows[-1][1].created_at))


def _inbox_cutoff(now: datetime) -> datetime:
    return now - timedelta(days=INBOX_LOOKBACK_DAYS)


async def count_open_incidents(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Open Alerting Inbox incidents per project, counted the inbox's own way.

    The sidebar badge next to "Alerting" showed the destination count, so it read
    "Alerting 1" beside a page listing 52 open incidents. A badge
    is only worth anything if it agrees with the page it labels, so this lives
    HERE, next to ``list_alert_inbox``, and reuses its rules rather than
    restating them in the caller: the same ``INBOX_LOOKBACK_DAYS`` window on the
    DELIVERY's ``created_at``, the same ``INBOX_MAX_SOURCE_ITEMS`` bound with the
    same tie-break, the same "an item with a correlation_group_id is an incident"
    grouping, and the same ``_effective_inbox_status`` — under which a LAPSED
    mute is open again. It is exposed on ``alerting_service`` so the caller needs
    none of the private helpers, the two constants, or the three models: reaching
    into a module the facade's own docstring calls private is how a badge and its
    page drift apart in the first place.

    The inbox's joins to destination, rule and scan config are NOT replayed,
    because they cannot drop a row: those three columns on AlertDelivery are all
    NOT NULL with ondelete=CASCADE, so every delivery row has all three parents
    and the INNER JOINs are pure fan-out this count would then have to undo.

    Two queries for the WHOLE page of projects, no matter how many: this rides on
    the summary endpoint that every page loads.
    """
    if not project_ids:
        return {}

    now = datetime.now(UTC)
    cutoff = _inbox_cutoff(now)
    # PARTITION BY project, because the list applies the cap to ONE project at a
    # time: a single global LIMIT would let one noisy project spend the whole
    # budget and empty every other project's badge.
    ranked_items = (
        select(
            AlertDelivery.project_id.label("project_id"),
            AlertDeliveryItem.correlation_group_id.label("correlation_group_id"),
            func.row_number()
            .over(partition_by=AlertDelivery.project_id, order_by=_INBOX_SOURCE_ORDER)
            .label("row_number"),
        )
        .select_from(AlertDeliveryItem)
        .join(AlertDelivery, AlertDelivery.id == AlertDeliveryItem.delivery_id)
        .where(
            AlertDelivery.project_id.in_(project_ids),
            AlertDeliveryItem.correlation_group_id.is_not(None),
            AlertDelivery.created_at >= cutoff,
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(ranked_items.c.project_id, ranked_items.c.correlation_group_id)
            .where(ranked_items.c.row_number <= INBOX_MAX_SOURCE_ITEMS)
            .distinct()
        )
    ).all()

    groups_by_project: dict[uuid.UUID, set[uuid.UUID]] = {}
    for project_id, group_id in rows:
        if group_id is None:
            continue
        groups_by_project.setdefault(project_id, set()).add(group_id)
    if not groups_by_project:
        return {}

    states = {
        (state.project_id, state.correlation_group_id): state
        for state in (
            await session.execute(
                select(AlertCorrelationState).where(
                    AlertCorrelationState.project_id.in_(project_ids)
                )
            )
        ).scalars()
    }
    return {
        project_id: sum(
            1
            for group_id in group_ids
            if _effective_inbox_status(states.get((project_id, group_id)), now)
            == AlertInboxStatus.open
        )
        for project_id, group_ids in groups_by_project.items()
    }


def _utc(value: datetime) -> datetime:
    """Read *value* as UTC when it says nothing about its zone.

    Both sides of the date comparison below can be naive. A bare ``?from=
    2026-09-01`` parses to a naive datetime, and SQLite hands back naive columns
    while Postgres hands back aware ones — so an un-normalized ``<=`` is a
    ``TypeError`` on one engine and correct on the other, which is the worst
    shape a comparison can have.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class InboxFilters:
    """What the reader asked the inbox to narrow to, besides ``status``.

    Applied to BUILT group responses rather than to the delivery rows, for the
    same reason ``status`` is: every member here is a property of the incident,
    and an incident does not exist until its rows are grouped. It costs the
    build of a few cards that are then dropped, and buys one filtering site
    whose result the page's own ``total`` counts.

    Two members deliberately read a DERIVED value rather than the widest one
    available:

    * ``direction`` matches the group's ``direction``, which is the NEWEST
      item's — the one the card shows. An incident that dropped five times and
      spiked last is a spike on screen, so ``?direction=drop`` hiding it is the
      answer that matches what the operator is looking at.
    * ``last_fired_from`` / ``last_fired_to`` bound ``latest_delivery_at``, not
      ``first_delivery_at``. "Everything I have not seen since Friday" is the
      triage question; when an incident STARTED is a different one, and the card
      shows both.

    ``scope`` is the exception and must NOT read the response: ``scope_names``
    is truncated to eight entries by ``_build_inbox_group_response``, so a
    search over it would silently fail to find the ninth scope of a wide
    incident. It is matched against the incident's ROWS instead — and only when
    a search is actually being run, because building that text is the one part
    of this that costs anything per group.
    """

    last_fired_from: datetime | None = None
    last_fired_to: datetime | None = None
    scope_type: MetricScopeType | None = None
    direction: AnomalyDirection | None = None
    scope: str | None = None

    @property
    def needle(self) -> str:
        """The scope search, normalized. Empty when there is nothing to search
        for — which whitespace alone is, since a cleared box posts one."""
        return (self.scope or "").strip().casefold()

    @property
    def is_active(self) -> bool:
        """Whether anything here narrows the list at all.

        The router hands the service an ``InboxFilters`` on every request,
        including the overwhelmingly common one that filters by nothing, so the
        caller checks this before entering the loop rather than walking every
        group to decide each one survives (Copilot, PR #162).
        """
        return (
            self.last_fired_from is not None
            or self.last_fired_to is not None
            or self.scope_type is not None
            or self.direction is not None
            or bool(self.needle)
        )

    def matches(self, group: AlertInboxGroupResponse, *, rows: list[InboxGroupRow]) -> bool:
        """Whether *group* survives every filter.

        Takes the incident's ROWS rather than a prepared haystack so the text
        can be built lazily: a keyword argument is evaluated before the call, so
        passing ``_scope_haystack(...)`` in built it for every group of every
        request, including the ones with no search — a set, a sort, a join and a
        casefold over every delivery item, thrown away unread.
        """
        if self.last_fired_from is not None and _utc(group.latest_delivery_at) < _utc(
            self.last_fired_from
        ):
            return False
        if self.last_fired_to is not None and _utc(group.latest_delivery_at) > _utc(
            self.last_fired_to
        ):
            return False
        # `scope_types` is the whole set the incident fired on, unlike
        # `scope_type`, which names only the newest item. An incident holding one
        # release regression among ten event firings IS a release-regression
        # incident to somebody filtering for them.
        if self.scope_type is not None and self.scope_type not in group.scope_types:
            return False
        if self.direction is not None and group.direction != self.direction:
            return False
        # Last, and only now, because this is the expensive one.
        needle = self.needle
        return not needle or needle in _scope_haystack(rows)


def _scope_haystack(rows: list[InboxGroupRow]) -> str:
    """Every scope name and ref an incident fired on, case-folded into one
    string. Built from the ROWS so the eight-name display cap cannot hide a
    match — see :class:`InboxFilters`."""
    parts: set[str] = set()
    for item, *_rest in rows:
        parts.add(item.scope_name or "")
        parts.add(item.scope_ref or "")
    return "\n".join(sorted(parts)).casefold()


async def list_alert_inbox(
    session: AsyncSession,
    slug: str,
    *,
    status: str | None = None,
    filters: InboxFilters | None = None,
    limit: int = 50,
    offset: int = 0,
    cursor: str | None = None,
) -> AlertInboxListResponse:
    project = await _get_project(session, slug)
    after = decode_inbox_cursor(cursor) if cursor is not None else None
    now = datetime.now(UTC)
    source = await _load_inbox_source_rows(
        session, project_id=project.id, cutoff=_inbox_cutoff(now)
    )
    states = {
        state.correlation_group_id: state
        for state in (
            await session.execute(
                select(AlertCorrelationState).where(AlertCorrelationState.project_id == project.id)
            )
        ).scalars()
    }
    groups: dict[uuid.UUID, list[InboxGroupRow]] = {}
    for item, delivery, destination, rule, scan in source.rows:
        if item.correlation_group_id is None:
            continue
        groups.setdefault(item.correlation_group_id, []).append(
            (item, delivery, destination, rule, scan)
        )

    # Incidents the window can no longer see but whose suppression is still in
    # force, merged in BEFORE the status filter so `?status=muted` can reach them.
    # `states` is a dict keyed by group id, so the selection helper
    # gets `.values()` — iterating the dict itself hands it UUIDs.
    #
    # The merge happens here, ahead of `group_states`, so a rescued group goes
    # through exactly the same response build, the same sort and the same paging
    # as every other one. Nothing downstream is allowed to know a card came from
    # the rescue; a second code path for them is how the two readings would drift.
    #
    # Rescued for EVERY status even when one is asked for: the per-status budget
    # makes each status's slice of the selection exactly what a filtered request
    # would pick, and the status chips need the other statuses' counts too.
    # Their delivery rows are loaded as well, not just counted off the
    # states: a chip counts what the OTHER filters leave, and `filters.matches`
    # reads a card's rows. That costs one extra query's worth of rows, bounded by
    # INBOX_MAX_SILENCED_RESCUES per status and INBOX_MAX_SOURCE_ITEMS per group.
    # The status filter below drops the rest.
    orphan_ids = _silenced_orphan_group_ids(
        states.values(),
        windowed_group_ids=set(groups),
        now=now,
    )
    if orphan_ids:
        rescued = await _load_orphan_group_rows(
            session, project_id=project.id, correlation_group_ids=orphan_ids
        )
        for group_id in orphan_ids:
            group_rows = rescued.get(group_id)
            if group_rows is None:
                # A suppressed state whose deliveries are gone entirely — the
                # delivery rows cascade on destination or rule deletion while the
                # state does not. There is nothing to render a card from, so it
                # cannot be shown; log it rather than fail the whole list, since
                # every other incident on the page is still answerable.
                logger.warning(
                    "inbox: correlation group %s is %s but has no deliveries to render",
                    group_id,
                    _effective_inbox_status(states.get(group_id), now),
                )
                continue
            groups[group_id] = group_rows

    group_states = {group_id: states.get(group_id) for group_id in groups}
    acted_by_names = await _load_acting_user_names(session, _acting_user_ids(group_states.values()))
    responses = [
        _build_inbox_group_response(
            correlation_group_id=group_id,
            state=group_states[group_id],
            rows=group_rows,
            now=now,
            acted_by_name=_acted_by_name(group_states[group_id], acted_by_names),
        )
        for group_id, group_rows in groups.items()
    ]
    if filters is not None and filters.is_active:
        responses = [
            group
            for group in responses
            if filters.matches(group, rows=groups[group.correlation_group_id])
        ]
    # Counted after every other filter and before the status one, so each chip
    # says what picking it would list.
    status_counts = AlertInboxStatusCounts.model_validate(
        Counter(str(group.status) for group in responses)
    )
    if status is not None:
        responses = [group for group in responses if group.status == status]
    responses.sort(key=_inbox_sort_key, reverse=True)
    total = len(responses)
    # Keyset continuation. The list is sorted DESCENDING on the key, so
    # "after the cursor" is every group whose key is strictly smaller. A group
    # that sorted down past the seam since the previous page is therefore still
    # served; one that sorted UP is already above the reader and reaches them on
    # the next refetch of page 1.
    remaining = (
        [group for group in responses if _inbox_sort_key(group) < after]
        if after is not None
        else responses[offset:]
    )
    page = await _with_owners(session, project.id, remaining[:limit], groups)
    return AlertInboxListResponse(
        items=page,
        total=total,
        status_counts=status_counts,
        next_cursor=(
            encode_inbox_cursor(_inbox_sort_key(page[-1]))
            if page and len(remaining) > limit
            else None
        ),
        # Reported off the SOURCE load, not off `responses`: the status filter
        # and paging both shrink the list for reasons the reader asked for, and
        # only this one is a bound they did not.
        window_truncated_at=source.truncated_at,
    )


async def get_alert_inbox_group(
    session: AsyncSession,
    slug: str,
    correlation_group_id: uuid.UUID,
) -> AlertInboxGroupResponse:
    """Resolve ONE incident from its WHOLE history, whatever its age.

    Passes ``cutoff=None`` to ``_build_one_inbox_group`` — the parameter that
    selects the unwindowed reading — and it is the only caller that does.
    Alert messages carry a deep link to the incident they describe, and the
    reader taps it when they get round to it; constraining this to the list's
    ``INBOX_LOOKBACK_DAYS`` window would dead-end exactly the links that most
    need to land, the old ones. The counts it reports are
    therefore over every delivery the incident ever made, which for a group
    older than the window is more than the list would show.
    """
    project = await _get_project(session, slug)
    return await _build_one_inbox_group(
        session,
        project_id=project.id,
        correlation_group_id=correlation_group_id,
        now=datetime.now(UTC),
        cutoff=None,
    )


async def _build_one_inbox_group(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    correlation_group_id: uuid.UUID,
    now: datetime,
    cutoff: datetime | None,
) -> AlertInboxGroupResponse:
    """Build a single group's response, over the rows ``cutoff`` selects.

    ``cutoff`` is the window this reading of the incident is taken over, and it
    is EXPLICIT because the two callers need different ones:

    * ``apply_alert_inbox_action`` passes the list's own cutoff, so the card the
      operator is looking at is rebuilt from exactly the rows the list built it
      from. It used to pass none, and every aggregate — item_count,
      delivery_count, first_delivery_at, latest_bucket, max_abs_percent_delta,
      scope_names, rules, scan_names — silently widened the moment the reply
      landed, so clicking Acknowledge redrew the card with different numbers and
      no state change that explained them.
    * ``get_alert_inbox_group`` passes ``None`` for the deep link, on purpose.

    Both readings share ``_build_inbox_group_response`` so one incident cannot be
    rendered two different ways for any reason other than the window.

    Windowed reads take the LIST's capped row set and keep this group's rows,
    rather than re-querying the group under the same cap: ``INBOX_MAX_SOURCE_ITEMS``
    admits the newest N items ACROSS THE PROJECT, so a per-group cap would admit
    a different set at the boundary and reintroduce the drift.

    A windowed read that finds nothing falls back to the unwindowed one. The
    group is then outside the list entirely, so there is no card in the list to
    agree with — the operator reached it through the deep link, and the fallback
    reproduces what THAT showed them. It also keeps an aged incident actionable:
    404ing here after the write had already committed reported an error for a
    change that landed.
    """
    rows: list[InboxGroupRow] = []
    if cutoff is not None:
        source = await _load_inbox_source_rows(session, project_id=project_id, cutoff=cutoff)
        rows = [row for row in source.rows if row[0].correlation_group_id == correlation_group_id]
    if not rows:
        rows = list(
            (
                await session.execute(
                    _INBOX_GROUP_SELECT.where(
                        AlertDelivery.project_id == project_id,
                        AlertDeliveryItem.correlation_group_id == correlation_group_id,
                    )
                    # Capped like every other inbox query: this was the only
                    # unbounded one, and a long-running incident's history has no
                    # ceiling.
                    .order_by(*_INBOX_SOURCE_ORDER)
                    .limit(INBOX_MAX_SOURCE_ITEMS)
                )
            )
            .tuples()
            .all()
        )
    if not rows:
        raise HTTPException(status_code=404, detail="Alert correlation group not found")

    state = await session.scalar(
        select(AlertCorrelationState).where(
            AlertCorrelationState.project_id == project_id,
            AlertCorrelationState.correlation_group_id == correlation_group_id,
        )
    )
    acted_by_names = await _load_acting_user_names(session, _acting_user_ids([state]))
    response = _build_inbox_group_response(
        correlation_group_id=correlation_group_id,
        state=state,
        rows=rows,
        now=now,
        acted_by_name=_acted_by_name(state, acted_by_names),
    )
    (owned,) = await _with_owners(session, project_id, [response], {correlation_group_id: rows})
    return owned


async def _get_or_create_correlation_state(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    correlation_group_id: uuid.UUID,
) -> AlertCorrelationState:
    exists = await session.scalar(
        select(AlertDeliveryItem.id)
        .join(AlertDelivery, AlertDelivery.id == AlertDeliveryItem.delivery_id)
        .where(
            AlertDelivery.project_id == project_id,
            AlertDeliveryItem.correlation_group_id == correlation_group_id,
        )
        .limit(1)
    )
    if exists is None:
        raise HTTPException(status_code=404, detail="Alert correlation group not found")

    state = await session.scalar(
        select(AlertCorrelationState).where(
            AlertCorrelationState.project_id == project_id,
            AlertCorrelationState.correlation_group_id == correlation_group_id,
        )
    )
    if state is None:
        state = AlertCorrelationState(
            project_id=project_id,
            correlation_group_id=correlation_group_id,
            status="open",
        )
        session.add(state)
        await session.flush()
    return state


async def _tune_false_positive_thresholds(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    correlation_group_id: uuid.UUID,
) -> int:
    """Make the detector stricter on the scopes this group actually alerted on.

    Returns the number of scope overrides written or ratcheted, which is 0 for a
    group made only of scope types outside ``RATCHETABLE_SCOPE_TYPES`` (see the
    skip below). The caller reports it so the UI can stop promising a detection
    change that did not happen.

    PER SCOPE, not project-wide. The ratchet used to raise
    ``sigma_threshold`` / ``min_expected_count`` on ``ProjectAnomalySettings``
    AND on every scan the group touched, so one click on one noisy event made
    every other event, event type, project total and catalog metric in the
    project less sensitive — permanently, and with no record of which click
    caused which increment. Per-scope correlation groups put a
    single scope behind that button, so the blast radius had to match it.

    The scope key is ``(scan_config_id, scope_type, scope_ref)`` — how a
    ``MetricAnomaly`` keys itself, and the only key the detection loops can
    honour. ``metric`` scopes are stored with a NULL ``scan_config_id`` because
    catalog metric series are project-global and their anomaly rows carry NULL
    too, even though the DELIVERY that carried the alert is always attributed to
    some scan config.

    Still permanent and still not decaying; the undo is deleting the override
    from Detection settings, which drops the scope straight back to the project
    setting.
    """
    rows = (
        await session.execute(
            select(
                AlertDelivery.scan_config_id,
                AlertDeliveryItem.scope_type,
                AlertDeliveryItem.scope_ref,
                AlertDeliveryItem.scope_name,
            )
            .join(AlertDelivery, AlertDelivery.id == AlertDeliveryItem.delivery_id)
            .where(
                AlertDelivery.project_id == project_id,
                AlertDeliveryItem.correlation_group_id == correlation_group_id,
            )
            .distinct()
            # ``scope_name`` is a display label carried along, not part of the
            # key, so one scope can still come back on several rows with
            # different labels; ordering makes which label wins deterministic
            # and the ``seen`` set below keeps it to ONE ratchet step per scope.
            .order_by(AlertDeliveryItem.scope_name)
        )
    ).all()

    return await tune_false_positive_scopes(
        session,
        project_id=project_id,
        scopes=[
            FalsePositiveScope(
                scan_config_id=scan_config_id,
                scope_type=str(scope_type),
                scope_ref=scope_ref,
                scope_name=scope_name,
            )
            for scan_config_id, scope_type, scope_ref, scope_name in rows
        ],
    )


@dataclass(frozen=True)
class FalsePositiveScope:
    """One scope a false-positive verdict tightens detection on."""

    scan_config_id: uuid.UUID | None
    scope_type: str
    scope_ref: str
    scope_name: str | None = None


async def tune_false_positive_scopes(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scopes: Iterable[FalsePositiveScope],
) -> int:
    """Ratchet each scope's ``AnomalyScopeOverride`` one false-positive step.

    The ONE implementation of "a false positive makes detection stricter",
    shared by an incident marked false positive (``_tune_false_positive_thresholds``
    feeds it the group's delivered scopes) and by a signal verdict of
    ``false_positive`` on a signal no rule routed (``signal_triage_service``
    feeds it that one scope, F01 #254). Returns the number of overrides written
    or ratcheted. DOES NOT COMMIT.
    """
    scopes = list(scopes)
    if not scopes:
        return 0
    settings = await session.scalar(
        select(ProjectAnomalySettings).where(ProjectAnomalySettings.project_id == project_id)
    )
    base_sigma = settings.sigma_threshold if settings is not None else DEFAULT_SIGMA_THRESHOLD
    base_count = settings.min_expected_count if settings is not None else DEFAULT_MIN_EXPECTED_COUNT

    seen: set[tuple[uuid.UUID | None, str, str]] = set()
    for scope in scopes:
        scan_config_id = scope.scan_config_id
        scope_type = str(scope.scope_type)
        scope_ref = scope.scope_ref
        scope_name = scope.scope_name
        if scope_type not in RATCHETABLE_SCOPE_TYPES:
            # Schema/distribution/variable-value drift and release regressions
            # reach the inbox too, but nothing scores them with these two knobs,
            # so a ratchet on them would only have moved unrelated volume
            # scopes. The group is still marked a false positive.
            continue
        # ``metric`` scopes are project-global: the anomaly row carries a NULL
        # config, so the override must too, or detection would never find it.
        config_id = None if scope_type == MetricScopeType.metric.value else scan_config_id
        key = (config_id, scope_type, scope_ref)
        if key in seen:
            continue
        seen.add(key)

        override = await session.scalar(
            select(AnomalyScopeOverride).where(
                AnomalyScopeOverride.project_id == project_id,
                AnomalyScopeOverride.scan_config_id.is_(None)
                if config_id is None
                else AnomalyScopeOverride.scan_config_id == config_id,
                AnomalyScopeOverride.scope_type == scope_type,
                AnomalyScopeOverride.scope_ref == scope_ref,
            )
        )
        if override is None:
            override = AnomalyScopeOverride(
                project_id=project_id,
                scan_config_id=config_id,
                scope_type=scope_type,
                scope_ref=scope_ref,
                scope_name=scope_name or scope_ref,
                sigma_threshold=base_sigma,
                min_expected_count=base_count,
                false_positive_count=0,
            )
            session.add(override)
        # Repeat clicks compound off the scope's own current value, so the
        # second false positive on the same scope is a second step, not a reset.
        override.sigma_threshold = ratchet_sigma_threshold(override.sigma_threshold)
        override.min_expected_count = ratchet_min_expected_count(override.min_expected_count)
        override.false_positive_count = (override.false_positive_count or 0) + 1
        override.scope_name = scope_name or override.scope_name
    await session.flush()
    # ``seen`` holds exactly the scopes that got a ratchet step: one entry per
    # override created or updated, deduplicated the same way the loop is.
    return len(seen)


async def _apply_inbox_action_to_state(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    state: AlertCorrelationState,
    action: str,
    note: str | None,
    muted_until: datetime | None,
    user_id: uuid.UUID,
    now: datetime,
) -> int | None:
    """Apply ONE triage decision to ONE already-loaded state row. DOES NOT COMMIT.

    Extracted from ``apply_alert_inbox_action`` so the bulk route can reuse it.
    The commit that used to sit in the middle of that function was
    the single thing making it undecomposable: a bulk caller needs to mutate N
    rows and commit ONCE, and a helper that commits per group would hand back
    partial success on any failure — which nothing in this repo does. The commit
    now belongs to the caller, and it is the caller's job to make it exactly one.

    Takes the four action fields rather than a request model because the two
    callers pass two DIFFERENT models — ``AlertInboxActionRequest`` and
    ``AlertInboxBulkActionRequest`` — whose only common ground is these fields.
    Typing the parameter as either one would make the other caller build a fake
    instance of it just to call this.

    ``now`` is passed in rather than read here so that a bulk call stamps every
    incident it touched with the SAME ``acted_at``: the batch was one human
    decision and the inbox has to be able to show it as one.

    Returns the number of scope overrides written, which is ``None`` for every
    action that cannot tighten anything — see the assignment below.
    """
    # ``None`` = "this action cannot tighten anything", which is every action but
    # false_positive. Reporting 0 for them let a client render "no scopes
    # tightened" off ``== 0`` after an Acknowledge, announcing a detection
    # decision nobody took.
    overrides_written: int | None = None
    if action == "acknowledge":
        state.status = "acknowledged"
        state.muted_until = None
    elif action == "resolve":
        state.status = "resolved"
        state.muted_until = None
    elif action == "mute":
        state.status = "muted"
        state.muted_until = muted_until
    elif action == "reopen":
        state.status = "open"
        state.muted_until = None
    elif action == "false_positive":
        state.status = "false_positive"
        state.muted_until = None
        state.false_positive_count = (state.false_positive_count or 0) + 1
        overrides_written = await _tune_false_positive_thresholds(
            session,
            project_id=project_id,
            # Off the STATE row, not a separate parameter: the two must name the
            # same incident, and threading the id alongside a row that already
            # carries it is how they would eventually disagree.
            correlation_group_id=state.correlation_group_id,
        )
    elif action == "note":
        # Documents the incident and changes NOTHING else — see the acted_at
        # stamp below.
        pass
    else:
        raise HTTPException(status_code=422, detail="Unsupported alert inbox action")

    # Only a supplied note replaces the stored one. Assigning unconditionally
    # meant every later action — acknowledge, then resolve — silently erased the
    # note written with the previous one, which is the opposite of what a note
    # on an incident is for.
    if note is not None:
        state.note = note.strip() or None
    # Stamped only by an action that actually decided something. The card's
    # "already handled by X on <date>" line is derived from acted_at, so a
    # note-only save that stamped it would forge a decision nobody took and
    # destroy the signal that tells a re-fired incident from a fresh one.
    if action != "note":
        state.acted_at = now
        state.acted_by = user_id
    # NO COMMIT — see this function's docstring. The caller owns it, and the bulk
    # caller owns exactly one for the whole batch.
    return overrides_written


async def prune_disagreeing_signal_verdicts(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    correlation_group_ids: Iterable[uuid.UUID],
) -> int:
    """Delete the verdict rows on these incidents' signals that no longer agree.

    Called by the inbox after it moved incidents, BEFORE its commit. A verdict
    row the incident's new status disagrees with (``tracking_bug`` on an
    incident just reopened, ``expected`` on one marked false positive) is
    ignored at read time anyway; deleting it keeps a later move back — reopen,
    then acknowledge — from resurrecting it in place of the call the inbox just
    made. Goes through ``delete_verdict_rows``, so an ``expected`` row's chart
    annotation goes with it. DOES NOT COMMIT. Returns the number deleted.

    A signal re-delivered into a different incident belongs to the newest one
    (``incident_refs_for_signals``), and only that incident's status is read.
    """
    group_ids = set(correlation_group_ids)
    if not group_ids:
        return 0
    # Pending state changes must be visible to the status lookup below.
    await session.flush()
    item_rows = (
        await session.execute(
            select(
                AlertDelivery.scan_config_id,
                AlertDeliveryItem.scope_type,
                AlertDeliveryItem.scope_ref,
                AlertDeliveryItem.bucket,
            )
            .join(AlertDelivery, AlertDelivery.id == AlertDeliveryItem.delivery_id)
            .where(
                AlertDelivery.project_id == project_id,
                AlertDeliveryItem.correlation_group_id.in_(group_ids),
            )
        )
    ).all()
    keys: set[SignalKey] = set()
    for scan_config_id, scope_type, scope_ref, bucket in item_rows:
        if bucket is None:
            continue
        scope = str(scope_type)
        signal_scan = None if scope == MetricScopeType.metric.value else scan_config_id
        keys.add((signal_scan, scope, scope_ref, _as_utc(bucket)))
    if not keys:
        return 0
    refs = {
        key: ref
        for key, ref in (await incident_refs_for_signals(session, project_id, keys)).items()
        if ref.correlation_group_id in group_ids
    }
    if not refs:
        return 0
    buckets = [key[3] for key in refs]
    verdict_rows = (
        await session.execute(
            select(SignalTriage).where(
                SignalTriage.project_id == project_id,
                SignalTriage.action.in_(sorted(VERDICT_ACTIONS)),
                SignalTriage.scope_ref.in_({key[2] for key in refs}),
                SignalTriage.bucket >= min(buckets),
                SignalTriage.bucket <= max(buckets),
            )
        )
    ).scalars()
    stale: list[SignalTriage] = []
    for row in verdict_rows:
        if row.bucket is None:
            continue
        scope = str(row.scope_type)
        row_scan = None if scope == MetricScopeType.metric.value else row.scan_config_id
        ref = refs.get((row_scan, scope, row.scope_ref, _as_utc(row.bucket)))
        if ref is not None and not status_agrees(ref.status, str(row.action)):
            stale.append(row)
    return await delete_verdict_rows(session, project_id, stale)


async def apply_alert_inbox_action(
    session: AsyncSession,
    slug: str,
    correlation_group_id: uuid.UUID,
    data: AlertInboxActionRequest,
    user_id: uuid.UUID,
) -> AlertInboxActionResponse:
    project = await _get_project(session, slug)
    state = await _get_or_create_correlation_state(
        session,
        project_id=project.id,
        correlation_group_id=correlation_group_id,
    )
    now = datetime.now(UTC)
    overrides_written = await _apply_inbox_action_to_state(
        session,
        project_id=project.id,
        state=state,
        action=data.action,
        note=data.note,
        muted_until=data.muted_until,
        user_id=user_id,
        now=now,
    )
    # A note moves nothing, so it leaves every signal verdict as it was.
    if data.action != "note":
        await prune_disagreeing_signal_verdicts(
            session, project_id=project.id, correlation_group_ids=[correlation_group_id]
        )
    await session.commit()
    # The sidebar badge counts only signals without a verdict, and an incident's
    # status IS the verdict of the signals routed into it (F01, #254).
    await cache.delete_prefix(cache.prefix_projects())

    # Built for THIS group alone, not by re-listing the inbox: rebuilding every
    # group's response on every click was O(project) and 404'd after a SUCCESSFUL
    # commit whenever the group had aged past INBOX_LOOKBACK_DAYS.
    #
    # ...but over the LIST's window, because this response replaces the card the
    # operator just clicked. Rebuilt unwindowed, it answered a different question
    # than the list had asked, and the card redrew with different counts, names
    # and timestamps for no reason the operator could see. See
    # ``_build_one_inbox_group`` for the fallback that keeps an aged incident
    # actionable.
    group = await _build_one_inbox_group(
        session,
        project_id=project.id,
        correlation_group_id=correlation_group_id,
        now=now,
        cutoff=_inbox_cutoff(now),
    )
    return AlertInboxActionResponse(group=group, overrides_written=overrides_written)


async def apply_signal_verdict_to_incident(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    correlation_group_id: uuid.UUID,
    action: str,
    note: str | None,
    user_id: uuid.UUID,
) -> AlertCorrelationState:
    """Apply an inbox ``action`` to the incident a signal verdict was set on.

    A verdict on a signal that belongs to an incident writes through to the
    incident, which stays the source of truth (F01, #254): the same state
    transition, the same false-positive tuning and the same ``acted_*`` stamp
    as a click in the inbox. DOES NOT COMMIT; the caller commits the incident
    change together with the signal's own verdict row.
    """
    state = await _get_or_create_correlation_state(
        session,
        project_id=project_id,
        correlation_group_id=correlation_group_id,
    )
    await _apply_inbox_action_to_state(
        session,
        project_id=project_id,
        state=state,
        action=action,
        note=note,
        muted_until=None,
        user_id=user_id,
        now=datetime.now(UTC),
    )
    return state


def dedupe_correlation_group_ids(correlation_group_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Request order, duplicates dropped — the ONE definition of "what this batch acted on".

    Shared by the service (which mutates, then rebuilds cards, in this order) and
    by the route (which writes one audit row per entry), because those two have to
    agree exactly: a repeated id reaching the route's loop would file two audit
    rows claiming two separate decisions on one incident, and a repeated id
    reaching the rebuild would render the same card twice.

    It is also load-bearing for CORRECTNESS, not only for tidiness. MEASURED by
    neutering this call: ``_load_or_create_correlation_states`` creates a row for
    every id it did not find, so a duplicate becomes two INSERTs for one
    ``(project_id, correlation_group_id)`` and the request dies on that unique
    constraint with a 500. A client that sends one id twice — trivially, a
    double-submitted selection — must get a 200, not a server error.

    Order is PRESERVED rather than going through a ``set`` — which is how
    ``bulk_update_events`` dedupes — because that function only needs membership
    for an ``IN`` clause, while here the order is part of the response contract
    and of the order the audit rows are written in.
    """
    return list(dict.fromkeys(correlation_group_ids))


async def _load_or_create_correlation_states(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    correlation_group_ids: list[uuid.UUID],
) -> dict[uuid.UUID, AlertCorrelationState]:
    """Validate EVERY id, then get-or-create every state row — all BEFORE any mutation.

    **The validation predicate, and why it is this one.** An id is valid iff the
    inbox could SHOW it — some ``AlertDeliveryItem`` in this project carries that
    ``correlation_group_id`` — and NOT iff an ``AlertCorrelationState`` row exists
    for it. The state row is written LAZILY by three separate writers (this
    function, ``_get_or_create_correlation_state``, and the suppression path), so
    an incident nobody has ever acted on has no state row at all. That is the
    overwhelmingly common member of a bulk selection — untouched incidents are
    exactly what this feature exists to clear — so validating on the presence of
    a state row would 404 almost every real batch.

    It is the SAME predicate ``_get_or_create_correlation_state`` uses for the
    single-incident route, and deliberately so: bulk must not be stricter than
    clicking the rows one at a time. It is also deliberately UNWINDOWED — no
    ``INBOX_LOOKBACK_DAYS`` cutoff and no ``INBOX_MAX_SOURCE_ITEMS`` cap.
    Validating against the LIST's windowed, capped row set instead would mean
    that an incident reachable through its deep link, or one that slipped past
    the cap between the page rendering and the operator clicking, would 404 the
    WHOLE batch. ``_build_inbox_group_batch`` carries the matching fallback that
    still renders those groups.

    **Why it runs first.** ONE query covers the whole list, and it runs to
    completion before anything is created or mutated. ``apply_alert_inbox_action``
    validates INSIDE a call that has already done work, which is harmless when
    there is one group and is not when there are 200: it would leave a
    half-applied batch behind a 404. The transaction rule for this
    route is all-or-nothing — validate everything, mutate everything, commit
    once — with no partial success and no per-item error array, because nothing
    in this repo does that.

    The 404 does not name the offending id, matching ``bulk_update_events``'s
    "One or more events were not found". Naming it would also confirm whether a
    given correlation group exists in some OTHER project to a caller who cannot
    read that project.
    """
    known = set(
        (
            await session.execute(
                select(AlertDeliveryItem.correlation_group_id)
                .join(AlertDelivery, AlertDelivery.id == AlertDeliveryItem.delivery_id)
                .where(
                    AlertDelivery.project_id == project_id,
                    AlertDeliveryItem.correlation_group_id.in_(correlation_group_ids),
                )
                .distinct()
            )
        ).scalars()
    )
    if known != set(correlation_group_ids):
        raise HTTPException(
            status_code=404, detail="One or more alert correlation groups were not found"
        )

    states = {
        state.correlation_group_id: state
        for state in (
            await session.execute(
                select(AlertCorrelationState).where(
                    AlertCorrelationState.project_id == project_id,
                    AlertCorrelationState.correlation_group_id.in_(correlation_group_ids),
                )
            )
        ).scalars()
    }
    # Created in ONE flush rather than one per group: the lazily-created rows are
    # the majority of a real batch, so a flush each would be the dominant cost of
    # the whole call.
    missing = [
        AlertCorrelationState(
            project_id=project_id,
            correlation_group_id=group_id,
            status="open",
        )
        for group_id in correlation_group_ids
        if group_id not in states
    ]
    if missing:
        session.add_all(missing)
        await session.flush()
        states.update({state.correlation_group_id: state for state in missing})
    return states


async def _build_inbox_group_batch(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    correlation_group_ids: list[uuid.UUID],
    now: datetime,
) -> list[AlertInboxGroupResponse]:
    """Rebuild N incident cards from ONE load of the inbox's source rows.

    The reason this exists instead of a loop over ``_build_one_inbox_group``:
    that function re-runs ``_load_inbox_source_rows`` — the whole project's
    windowed, capped inbox query — every time it is called with a cutoff. Calling
    it once per selected group would run that query N times for one click, which
    at the 200-group cap is 200 full inbox scans. The rows are loaded
    ONCE here and bucketed by group.

    Every other per-group read is batched the same way: ONE query for all the
    correlation states, and ONE for the acting users' display names via
    ``_load_acting_user_names`` — which already exists precisely so a page of
    cards does not put a SELECT behind each row.

    The window matches ``apply_alert_inbox_action``'s for the same reason it does
    there: these cards REPLACE the ones the operator just clicked, so they must be
    built from the rows the list built them from. An aged group with nothing in
    the window falls back to the unwindowed per-group read, exactly as
    ``_build_one_inbox_group`` does — one query, but only for the aged minority,
    and only because the alternative is a card that cannot be drawn at all.

    Cards are returned in the caller's order (see ``dedupe_correlation_group_ids``).
    A group with no rows under EITHER reading is dropped with a warning rather
    than raising: validation already proved its items existed, so reaching here
    means they were deleted concurrently, and 404ing would report an error for a
    state change that has already committed — a failure already fixed on the single route.
    """
    wanted = set(correlation_group_ids)
    rows_by_group: dict[uuid.UUID, list[InboxGroupRow]] = {}
    for row in (
        await _load_inbox_source_rows(session, project_id=project_id, cutoff=_inbox_cutoff(now))
    ).rows:
        group_id = row[0].correlation_group_id
        if group_id in wanted:
            rows_by_group.setdefault(group_id, []).append(row)

    for group_id in correlation_group_ids:
        if group_id in rows_by_group:
            continue
        # Unwindowed fallback, capped like every other inbox query. Kept PER
        # GROUP rather than folded into one ``IN`` query because
        # ``INBOX_MAX_SOURCE_ITEMS`` counts item rows: a single combined query
        # under one LIMIT would let the first aged group spend the whole budget
        # and return nothing for the rest.
        aged = list(
            (
                await session.execute(
                    _INBOX_GROUP_SELECT.where(
                        AlertDelivery.project_id == project_id,
                        AlertDeliveryItem.correlation_group_id == group_id,
                    )
                    .order_by(*_INBOX_SOURCE_ORDER)
                    .limit(INBOX_MAX_SOURCE_ITEMS)
                )
            )
            .tuples()
            .all()
        )
        if aged:
            rows_by_group[group_id] = aged

    states = {
        state.correlation_group_id: state
        for state in (
            await session.execute(
                select(AlertCorrelationState).where(
                    AlertCorrelationState.project_id == project_id,
                    AlertCorrelationState.correlation_group_id.in_(correlation_group_ids),
                )
            )
        ).scalars()
    }
    group_states = {group_id: states.get(group_id) for group_id in correlation_group_ids}
    acted_by_names = await _load_acting_user_names(session, _acting_user_ids(group_states.values()))

    responses: list[AlertInboxGroupResponse] = []
    for group_id in correlation_group_ids:
        group_rows = rows_by_group.get(group_id)
        if not group_rows:
            logger.warning(
                "alert inbox bulk action: no delivery rows left to rebuild group %s "
                "in project %s; the action committed and was audited",
                group_id,
                project_id,
            )
            continue
        responses.append(
            _build_inbox_group_response(
                correlation_group_id=group_id,
                state=group_states[group_id],
                rows=group_rows,
                now=now,
                acted_by_name=_acted_by_name(group_states[group_id], acted_by_names),
            )
        )
    return responses


async def apply_alert_inbox_bulk_action(
    session: AsyncSession,
    slug: str,
    data: AlertInboxBulkActionRequest,
    user_id: uuid.UUID,
) -> AlertInboxBulkActionResponse:
    """Apply one triage decision to every selected incident, atomically.

    The decision is COPIED into each incident's own state row — there is no group
    object and no new table — so afterwards every selected row carries the same
    note, the same ``acted_at`` and the same ``acted_by``, and is
    indistinguishable from having been clicked individually. See
    ``AlertInboxBulkActionRequest`` for why a persistent supergroup was rejected.

    The shape of the call is fixed by the transaction rule: validate every id in
    ONE query up front and 404 the whole request if any is unknown, mutate all N,
    commit ONCE. There is no partial success and no per-item error array. A single
    ``now`` is taken before the loop so the batch reads as the one decision it
    was.

    ``false_positive`` never reaches here — ``AlertInboxBulkActionRequest``
    refuses it at the schema, so the request cannot be constructed — which is why
    ``overrides_written`` below is unconditionally ``None``.
    """
    project = await _get_project(session, slug)
    group_ids = dedupe_correlation_group_ids(data.correlation_group_ids)
    # Validation and lazy state creation, both batched, both BEFORE the first
    # mutation. A 404 raised from here has changed nothing.
    states = await _load_or_create_correlation_states(
        session,
        project_id=project.id,
        correlation_group_ids=group_ids,
    )
    # ONE timestamp for the whole batch — see ``_apply_inbox_action_to_state``.
    now = datetime.now(UTC)
    for group_id in group_ids:
        await _apply_inbox_action_to_state(
            session,
            project_id=project.id,
            state=states[group_id],
            action=data.action,
            note=data.note,
            muted_until=data.muted_until,
            user_id=user_id,
            now=now,
        )
    # Same transaction: the stale signal verdicts go with the state change.
    if data.action != "note":
        await prune_disagreeing_signal_verdicts(
            session, project_id=project.id, correlation_group_ids=group_ids
        )
    # The one and only commit. Every state row above lands together or none does.
    await session.commit()
    # See ``apply_alert_inbox_action``: incident status feeds the sidebar badge.
    await cache.delete_prefix(cache.prefix_projects())

    groups = await _build_inbox_group_batch(
        session,
        project_id=project.id,
        correlation_group_ids=group_ids,
        now=now,
    )
    return AlertInboxBulkActionResponse(
        groups=groups,
        # Minted HERE, not in the route, so the id the response advertises and the
        # id stamped into all N audit rows cannot drift apart: the route reads it
        # back off this response rather than generating a second one.
        batch_id=uuid.uuid4(),
        # Always ``None``; the only action that can ratchet anything is refused
        # for this route. See ``AlertInboxBulkActionResponse``.
        overrides_written=None,
    )


# (scan_config_id, scope_type, scope_ref, bucket) — a signal's identity as the
# Anomalies page holds it. ``scan_config_id`` is None for a catalog metric.
SignalKey = tuple[uuid.UUID | None, str, str, datetime]


@dataclass(frozen=True)
class SignalIncidentRef:
    correlation_group_id: uuid.UUID
    status: str
    # Who last decided the incident's status, when, and its note; the signal's
    # verdict display falls back to these when the signal carries no verdict
    # row of its own (F01, #254). NULL on an incident nobody has acted on.
    acted_by: uuid.UUID | None = None
    acted_at: datetime | None = None
    note: str | None = None
    # When the signal was first delivered into this incident. A verdict row set
    # on the signal before then keeps winning while the incident is untouched
    # (``_signal_verdict_read.record_prevails``).
    routed_at: datetime | None = None


async def incident_refs_for_signals(
    session: AsyncSession,
    project_id: uuid.UUID,
    keys: Iterable[SignalKey],
) -> dict[SignalKey, SignalIncidentRef]:
    """The inbox incident each signal was routed into, if any.

    A signal becomes an incident only when a rule matched it and a delivery item
    was written for the same scope and bucket, so the lookup reads those items
    — the newest one wins when a scope was re-delivered — and resolves the
    group's status the way the inbox does (``_effective_inbox_status``: a lapsed
    mute is open again). A key with no item is simply absent from the result.

    ``bucket`` is compared in Python after UTC normalisation rather than with an
    ``IN`` on the column: SQLite hands back naive datetimes, and the SQL bound
    is a range over the wanted buckets that keeps the scan to recent deliveries
    (``ix_alert_delivery_project_created``).
    """
    wanted = {
        (scan_config_id, str(scope_type), scope_ref, _as_utc(bucket))
        for scan_config_id, scope_type, scope_ref, bucket in keys
    }
    if not wanted:
        return {}
    buckets = [key[3] for key in wanted]
    rows = (
        await session.execute(
            select(
                AlertDelivery.scan_config_id,
                AlertDeliveryItem.scope_type,
                AlertDeliveryItem.scope_ref,
                AlertDeliveryItem.bucket,
                AlertDeliveryItem.correlation_group_id,
                AlertDelivery.created_at,
            )
            .join(AlertDelivery, AlertDelivery.id == AlertDeliveryItem.delivery_id)
            .where(
                AlertDelivery.project_id == project_id,
                AlertDelivery.created_at >= min(buckets),
                AlertDeliveryItem.bucket >= min(buckets),
                AlertDeliveryItem.bucket <= max(buckets),
                AlertDeliveryItem.correlation_group_id.is_not(None),
            )
            .order_by(AlertDelivery.created_at.asc(), AlertDelivery.id.asc())
        )
    ).all()

    group_by_key: dict[SignalKey, uuid.UUID] = {}
    routed_at_by_key: dict[SignalKey, datetime | None] = {}
    for scan_config_id, scope_type, scope_ref, bucket, group_id, delivered_at in rows:
        # A catalog-metric signal carries no scan, whatever the delivery row
        # was filed under; match it on scope and bucket alone.
        signal_scan = None if scope_type == MetricScopeType.metric.value else scan_config_id
        key = (signal_scan, str(scope_type), scope_ref, _as_utc(bucket))
        if key in wanted and group_id is not None:
            # Ascending order: the newest group wins, and the first delivery
            # into that group is when the signal was routed to it.
            if group_by_key.get(key) != group_id:
                routed_at_by_key[key] = _as_utc(delivered_at) if delivered_at is not None else None
            group_by_key[key] = group_id
    if not group_by_key:
        return {}

    states = {
        state.correlation_group_id: state
        for state in (
            await session.execute(
                select(AlertCorrelationState).where(
                    AlertCorrelationState.project_id == project_id,
                    AlertCorrelationState.correlation_group_id.in_(set(group_by_key.values())),
                )
            )
        ).scalars()
    }
    now = datetime.now(UTC)
    return {
        key: _incident_ref(group_id, states.get(group_id), now, routed_at_by_key.get(key))
        for key, group_id in group_by_key.items()
    }


def _incident_ref(
    group_id: uuid.UUID,
    state: AlertCorrelationState | None,
    now: datetime,
    routed_at: datetime | None = None,
) -> SignalIncidentRef:
    return SignalIncidentRef(
        routed_at=routed_at,
        correlation_group_id=group_id,
        status=_effective_inbox_status(state, now),
        acted_by=state.acted_by if state is not None else None,
        acted_at=_as_utc(state.acted_at) if state is not None and state.acted_at else None,
        note=state.note if state is not None else None,
    )
