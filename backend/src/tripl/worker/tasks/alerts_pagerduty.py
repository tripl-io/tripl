"""PagerDuty (Events API v2): trigger events for deliveries, resolve on close.

Every other channel is fire-and-forget: a message goes out and the incident's
later life — acknowledged, resolved, closed because the scope stopped firing —
stays inside tripl. PagerDuty is different in kind. What it holds is an open
INCIDENT that pages someone until it is resolved, so a channel that only ever
triggers would leave every page open forever and push the operator into
resolving each one twice. That is why this module has a second half: a small
task that sends ``event_action: "resolve"`` when tripl itself considers the
incident over.

The join between the two halves is the ``dedup_key``. It is derived from the
incident handle tripl already has — ``AlertDeliveryItem.correlation_group_id``,
one rule x scope x direction (``dispatch._correlation_group_id``) — so every
delivery for the same ongoing incident updates ONE PagerDuty incident rather
than opening a new one per collection, and the resolve can name it without any
state of its own beyond what the delivery recorded.

Recorded where: ``payload_snapshot["pagerduty_dedup_keys"]`` on the delivery
that triggered, committed after each accepted event, exactly as the Jira and
Linear branches commit their external issue id before ``status=sent`` — so a
worker killed mid-send does not re-trigger on the re-run. The resolve half
writes ``payload_snapshot["pagerduty_resolved_keys"]`` the same way, which is
what makes an Inbox Resolve followed by the automatic close send ONE resolve.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl.alert_templates import alert_scope_label, scope_has_direction
from tripl.alerting_validation import (
    DEFAULT_PAGERDUTY_SEVERITY,
    PAGERDUTY_SEVERITIES,
    validate_pagerduty_routing_key,
)
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_rule import AlertRule
from tripl.models.project import Project

logger = logging.getLogger(__name__)

# Fixed, never user input: the routing key is what selects the PagerDuty
# service, so there is no URL to configure and nothing here to SSRF-check.
PAGERDUTY_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"
# The Events API v2 caps ``payload.summary`` at 1024 characters and rejects the
# event outright above it, so the summary is cut rather than the event lost.
PAGERDUTY_SUMMARY_MAX_CHARS = 1024
PAGERDUTY_SOURCE = "tripl"
# The only success answer the Events API gives for an accepted event.
PAGERDUTY_ACCEPTED_STATUS = 202

DEDUP_KEYS_SNAPSHOT_KEY = "pagerduty_dedup_keys"
RESOLVED_KEYS_SNAPSHOT_KEY = "pagerduty_resolved_keys"

PostJsonWithStatus = Callable[..., tuple[int, dict[str, object] | None]]


def pagerduty_dedup_key(correlation_group_id: uuid.UUID) -> str:
    """The dedup key for one tripl incident; the resolve rebuilds it from the id."""
    return f"tripl-{correlation_group_id}"


def _delivery_dedup_key(delivery_id: uuid.UUID) -> str:
    # Items with no incident handle (rows from before incidents were recorded)
    # still need a key, and the delivery is the only identity they have. Such a
    # key is never resolved automatically: no inbox card or closing state names it.
    return f"tripl-delivery-{delivery_id}"


def severity_of(destination: AlertDestination) -> str:
    severity = (destination.pagerduty_severity or "").lower()
    return severity if severity in PAGERDUTY_SEVERITIES else DEFAULT_PAGERDUTY_SEVERITY


def first_app_link(items: Sequence[AlertDeliveryItem]) -> str | None:
    """The first absolute app URL any item carries — the incident card, usually.

    Relative or empty on an instance with no ``APP_BASE_URL``, and then there
    is nothing a PagerDuty link or a Teams button could open.
    """
    for item in items:
        for candidate in (item.details_path, item.monitoring_path):
            if candidate and candidate.startswith(("https://", "http://")):
                return candidate
    return None


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _group_summary(
    *,
    project: Project | None,
    rule: AlertRule,
    items: Sequence[AlertDeliveryItem],
    matched_count: int,
) -> str:
    prefix = project.name if project else "tripl"
    if len(items) == 1:
        item = items[0]
        # A drift went neither way, so its kind names it rather than the
        # "spike" its row carries for the rule's Spikes toggle.
        what = (
            f"{item.scope_name} {item.direction}"
            if scope_has_direction(item.scope_type)
            else f"{alert_scope_label(item.scope_type)} {item.scope_name}"
        )
        if item.drift_field:
            what = f"{what} ({item.drift_field})"
        return _truncate(f"[{prefix}] {rule.name}: {what}", PAGERDUTY_SUMMARY_MAX_CHARS)
    count = len(items) or matched_count
    return _truncate(f"[{prefix}] {rule.name} — {count} alert(s)", PAGERDUTY_SUMMARY_MAX_CHARS)


def build_trigger_events(
    delivery: AlertDelivery,
    *,
    destination: AlertDestination,
    rule: AlertRule,
    project: Project | None,
    routing_key: str,
    structured_payload: dict[str, object],
) -> list[tuple[str, dict[str, object]]]:
    """``(dedup_key, event body)`` per distinct incident in this delivery.

    One delivery may carry items of several incidents (several scopes of one
    rule), and each is its own PagerDuty incident: folding them into one event
    would make the first scope's dedup key speak for all of them, and closing
    that scope would resolve pages for scopes still firing. ``custom_details``
    is the generic webhook's structured body narrowed to that incident's items,
    so a PagerDuty rule can match on the same fields a webhook consumer reads.
    """
    groups: dict[str, list[AlertDeliveryItem]] = {}
    for item in delivery.items:
        key = (
            pagerduty_dedup_key(item.correlation_group_id)
            if item.correlation_group_id is not None
            else _delivery_dedup_key(delivery.id)
        )
        groups.setdefault(key, []).append(item)
    if not groups:
        # A delivery with no items (matched_count without rows) still pages:
        # the rendered message says what fired, and silence would hide it.
        groups[_delivery_dedup_key(delivery.id)] = []

    all_items = structured_payload.get("items")
    events: list[tuple[str, dict[str, object]]] = []
    for dedup_key, items in groups.items():
        item_ids = {item.id for item in items}
        details = dict(structured_payload)
        if isinstance(all_items, list) and items:
            # The structured items are built in ``delivery.items`` order, so the
            # positions line up with the ORM rows they came from.
            details["items"] = [
                payload
                for payload, item in zip(all_items, delivery.items, strict=False)
                if item.id in item_ids
            ]
            details["matched_count"] = len(items)
        body: dict[str, object] = {
            "routing_key": routing_key,
            "event_action": "trigger",
            "dedup_key": dedup_key,
            "payload": {
                "summary": _group_summary(
                    project=project,
                    rule=rule,
                    items=items,
                    matched_count=delivery.matched_count,
                ),
                "source": PAGERDUTY_SOURCE,
                "severity": severity_of(destination),
                "component": (project.slug or project.name) if project else PAGERDUTY_SOURCE,
                "custom_details": details,
            },
        }
        link = first_app_link(items)
        if link:
            body["links"] = [{"href": link, "text": "Open in tripl"}]
        events.append((dedup_key, body))
    return events


def resolve_event(*, routing_key: str, dedup_key: str) -> dict[str, object]:
    return {"routing_key": routing_key, "event_action": "resolve", "dedup_key": dedup_key}


def build_test_trigger_and_resolve(
    *, routing_key: str, summary: str, component: str, message: str
) -> list[dict[str, object]]:
    """The Test button's two events: a low-severity trigger and its resolve.

    ``info`` severity so a service whose urgency follows severity does not
    wake anyone; a fresh dedup key per press so a test never folds into, or
    resolves, a real incident.
    """
    dedup_key = f"tripl-test-{uuid.uuid4()}"
    trigger: dict[str, object] = {
        "routing_key": routing_key,
        "event_action": "trigger",
        "dedup_key": dedup_key,
        "payload": {
            "summary": _truncate(summary, PAGERDUTY_SUMMARY_MAX_CHARS),
            "source": PAGERDUTY_SOURCE,
            "severity": "info",
            "component": component,
            "custom_details": {"event": "tripl.destination_test", "message": message},
        },
    }
    return [trigger, resolve_event(routing_key=routing_key, dedup_key=dedup_key)]


def send_pagerduty_event(
    post_json_with_status: PostJsonWithStatus,
    body: dict[str, object],
    *,
    routing_key: str,
) -> None:
    """POST one event; anything but 202 is a failure, and the key never leaks.

    The routing key is the credential and sits in the request BODY, so
    ``_safe_url_for_error`` cannot be what keeps it out of the error: PagerDuty's
    400 answers describe the event, and a description that quoted the field
    back would land in ``alert_deliveries.error_message``, which the API returns.
    Scrubbing the key from whatever text comes back costs nothing and closes that.
    """
    try:
        status, _response = post_json_with_status(PAGERDUTY_EVENTS_URL, body)
    except ValueError as exc:
        raise ValueError(str(exc).replace(routing_key, "[routing key]")) from exc
    if status != PAGERDUTY_ACCEPTED_STATUS:
        raise ValueError(
            f"PagerDuty answered HTTP {status} instead of 202; the event was not accepted"
        )


def recorded_keys(snapshot: object, key: str) -> list[str]:
    if not isinstance(snapshot, dict):
        return []
    value = snapshot.get(key)
    return [entry for entry in value if isinstance(entry, str)] if isinstance(value, list) else []


# --- resolve on close ---------------------------------------------------------
#
# Two places decide an incident is over: the collection that finds a scope no
# longer firing (``dispatch._prepare_alert_deliveries`` closing an
# ``AlertRuleState``) and a person pressing Resolve in the Inbox
# (``_alerting_deliveries._apply_inbox_action_to_state``). Neither may be held
# up by PagerDuty, and neither should page anyone's phone about a decision that
# then rolled back — so both only QUEUE the ids on the session, and the task is
# published after the COMMIT, the pattern ``collect._queue_seen_in_data_comments``
# set for tracker comments.

_PENDING_KEY = "tripl.pagerduty_resolve_pending"
_LISTENING_KEY = "tripl.pagerduty_resolve_listening"
# Registered in ``worker.tasks.alerts`` (which celery_app imports); named here
# so the API side can publish it without importing the worker task graph.
RESOLVE_TASK_NAME = "tripl.worker.tasks.alerts.resolve_pagerduty_incidents"

# Strong references to the publishes scheduled on the API's event loop: the
# loop keeps only a weak one, and a task collected mid-flight is a lost resolve.
_BACKGROUND: set[asyncio.Task[Any]] = set()


def _triggered_here(project_id: uuid.UUID, group_ids: Iterable[uuid.UUID]) -> Any:
    """EXISTS: some delivery of this project paged PagerDuty for one of these ids.

    The cheap filter that keeps every other project — and every close that
    never reached PagerDuty, which is nearly all of them — from publishing a
    task at all. The task re-checks everything that matters (enabled, demo,
    already resolved) when it runs.
    """
    return select(
        exists()
        .where(
            AlertDeliveryItem.delivery_id == AlertDelivery.id,
            AlertDeliveryItem.correlation_group_id.in_(list(group_ids)),
            AlertDelivery.project_id == project_id,
            AlertDelivery.channel == AlertDestinationType.pagerduty.value,
            AlertDelivery.status == AlertDeliveryStatus.sent.value,
        )
        .correlate(None)
    )


def queue_pagerduty_resolves(
    session: Session, project_id: uuid.UUID, group_ids: Iterable[uuid.UUID]
) -> None:
    """Sync (worker) side: resolve these incidents once this session commits."""
    ids = set(group_ids)
    if not ids or not session.execute(_triggered_here(project_id, ids)).scalar():
        return
    _register(session, project_id, ids)


async def queue_pagerduty_resolves_async(
    session: AsyncSession, project_id: uuid.UUID, group_ids: Iterable[uuid.UUID]
) -> None:
    """Async (API) side of :func:`queue_pagerduty_resolves`."""
    ids = set(group_ids)
    if not ids or not await session.scalar(_triggered_here(project_id, ids)):
        return
    _register(session.sync_session, project_id, ids)


def _register(session: Session, project_id: uuid.UUID, ids: set[uuid.UUID]) -> None:
    pending: dict[uuid.UUID, set[uuid.UUID]] | None = session.info.get(_PENDING_KEY)
    if pending is None:
        pending = defaultdict(set)
        session.info[_PENDING_KEY] = pending
    pending[project_id] |= ids
    if not session.info.get(_LISTENING_KEY):
        session.info[_LISTENING_KEY] = True
        sa_event.listen(session, "after_commit", _publish_pending)
        sa_event.listen(session, "after_rollback", _drop_pending)


def _drop_pending(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)


def _publish_pending(session: Session) -> None:
    pending: dict[uuid.UUID, set[uuid.UUID]] = session.info.pop(_PENDING_KEY, None) or {}
    for project_id, ids in pending.items():
        enqueue_resolve(project_id, sorted(ids, key=str))


def enqueue_resolve(project_id: uuid.UUID, group_ids: list[uuid.UUID]) -> None:
    """Publish the resolve task; never raises — the close is already committed.

    On the API side this runs inside the commit of an ``AsyncSession``, i.e. on
    the event loop, where a kombu publish against a hung broker would freeze
    every request on the worker (``services/_celery_dispatch``). There it is
    scheduled onto the loop through the same thread offload; in a worker, where
    there is no loop, it is published inline like every other worker publish.
    """
    from tripl.worker.celery_app import celery_app

    args = [str(project_id), [str(group_id) for group_id in group_ids]]

    def publish() -> None:
        celery_app.send_task(RESOLVE_TASK_NAME, args=args)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    try:
        if loop is None:
            publish()
            return
        task = loop.create_task(asyncio.to_thread(publish))
        _BACKGROUND.add(task)
        task.add_done_callback(_finish_background)
    except Exception:  # noqa: BLE001 — a lost resolve must not fail the close
        logger.exception("Could not queue the PagerDuty resolve for project %s", project_id)


def _finish_background(task: asyncio.Task[Any]) -> None:
    _BACKGROUND.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("Could not queue a PagerDuty resolve", exc_info=task.exception())


def resolve_incidents(
    session: Session,
    *,
    project_id: uuid.UUID,
    group_ids: Sequence[uuid.UUID],
    post_json_with_status: PostJsonWithStatus,
    decrypt: Callable[[str | None], str],
    assert_egress_allowed: Callable[[AlertDestination, Project | None], None],
) -> dict[str, int]:
    """Send one ``resolve`` per (destination, incident) that was triggered and is open.

    Best effort throughout: every failure is logged and the next key is tried,
    because the incident is already closed in tripl and nothing here can or
    should undo that. A destination that is disabled, or on a demo project, is
    skipped — the same two rules every outbound send obeys.
    """
    project = session.get(Project, project_id)
    wanted = {pagerduty_dedup_key(group_id) for group_id in group_ids}
    deliveries = (
        session.execute(
            select(AlertDelivery)
            .join(AlertDestination, AlertDestination.id == AlertDelivery.destination_id)
            .where(
                AlertDelivery.project_id == project_id,
                AlertDelivery.channel == AlertDestinationType.pagerduty.value,
                AlertDelivery.status == AlertDeliveryStatus.sent.value,
                AlertDestination.enabled.is_(True),
                exists().where(
                    AlertDeliveryItem.delivery_id == AlertDelivery.id,
                    AlertDeliveryItem.correlation_group_id.in_(list(group_ids)),
                ),
            )
        )
        .scalars()
        .all()
    )
    # destination -> dedup key -> the deliveries that triggered it and are open.
    open_keys: dict[uuid.UUID, dict[str, list[AlertDelivery]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for delivery in deliveries:
        resolved = set(recorded_keys(delivery.payload_snapshot, RESOLVED_KEYS_SNAPSHOT_KEY))
        for key in recorded_keys(delivery.payload_snapshot, DEDUP_KEYS_SNAPSHOT_KEY):
            if key in wanted and key not in resolved:
                open_keys[delivery.destination_id][key].append(delivery)

    sent = failed = skipped = 0
    for destination_id, keys in open_keys.items():
        destination = session.get(AlertDestination, destination_id)
        if destination is None:
            continue
        try:
            assert_egress_allowed(destination, project)
            routing_key = validate_pagerduty_routing_key(
                decrypt(destination.pagerduty_routing_key_encrypted)
            )
        except ValueError:
            logger.warning(
                "Skipping PagerDuty resolve for destination %s: not sendable", destination_id
            )
            skipped += len(keys)
            continue
        for dedup_key, carriers in keys.items():
            try:
                send_pagerduty_event(
                    post_json_with_status,
                    resolve_event(routing_key=routing_key, dedup_key=dedup_key),
                    routing_key=routing_key,
                )
            except Exception:  # noqa: BLE001 — best effort; the close stands
                logger.warning(
                    "PagerDuty resolve failed for destination %s, key %s",
                    destination_id,
                    dedup_key,
                    exc_info=True,
                )
                failed += 1
                continue
            for delivery in carriers:
                snapshot = dict(delivery.payload_snapshot or {})
                snapshot[RESOLVED_KEYS_SNAPSHOT_KEY] = [
                    *recorded_keys(snapshot, RESOLVED_KEYS_SNAPSHOT_KEY),
                    dedup_key,
                ]
                delivery.payload_snapshot = snapshot
            # Per key, as the trigger side commits per event: a worker killed
            # half-way through does not resolve the finished half twice.
            session.commit()
            sent += 1
    return {"sent": sent, "failed": failed, "skipped": skipped}
