import logging
import smtplib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, object_session, selectinload
from sqlalchemy.orm.attributes import set_committed_value

from tripl import realtime
from tripl.alert_templates import (
    ALERT_MESSAGE_FORMAT_PLAIN,
    ALERT_MESSAGE_FORMAT_TELEGRAM_HTML,
    ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2,
    DEMO_SINK_LOCAL_NOTICE,
)
from tripl.alerting_matching import is_project_global_scope
from tripl.alerting_validation import (
    validate_email_recipients,
    validate_jira_api_token,
    validate_jira_auth_email,
    validate_jira_base_url,
    validate_jira_issue_type,
    validate_jira_project_key,
    validate_linear_api_key,
    validate_linear_team_id,
    validate_sender_address,
    validate_slack_webhook_url,
    validate_telegram_bot_token,
    validate_telegram_chat_id,
    validate_webhook_target_url,
)
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.observability.metrics import alert_deliveries_total, alert_delivery_missing_items_total
from tripl.services import app_settings_service
from tripl.worker.celery_app import celery_app
from tripl.worker.db import _get_sync_session
from tripl.worker.tasks.alerts_channels import (
    _decrypt_secret,
    _parse_email_recipients,
    _reject_private_target,
)
from tripl.worker.tasks.alerts_channels import (
    _post_json as _channel_post_json,
)
from tripl.worker.tasks.alerts_channels import (
    _send_email_message as _channel_send_email_message,
)
from tripl.worker.tasks.alerts_channels import (
    _send_jira_issue as _channel_send_jira_issue,
)
from tripl.worker.tasks.alerts_channels import (
    _send_linear_issue as _channel_send_linear_issue,
)
from tripl.worker.tasks.alerts_channels import (
    _send_slack_message as _channel_send_slack_message,
)
from tripl.worker.tasks.alerts_channels import (
    _send_telegram_message as _channel_send_telegram_message,
)
from tripl.worker.tasks.alerts_channels import (
    _send_webhook_message as _channel_send_webhook_message,
)
from tripl.worker.tasks.alerts_digest import (
    check_deprecated_sunset_events,
    send_weekly_plan_digest,
)
from tripl.worker.tasks.alerts_messages import (
    DIGEST_AI_EXPLANATION_MAX_ITEMS,
    _append_ai_explanation,
    _build_ai_explanation,
    _build_email_subject,
    _build_ticket_subject,
    _build_webhook_payload,
    _is_telegram_markdown_parse_error,
    _is_telegram_message_too_long_error,
    _render_delivery_message,
    split_telegram_messages,
)

logger = logging.getLogger(__name__)

# Re-exported, and load-bearing rather than tidy. Both digest tasks are DEFINED
# in alerts_digest.py but registered under ``tripl.worker.tasks.alerts.*``
# names, and celery_app.py's registration block imports this module and never
# that one — so the import above is what puts them in the task registry. Beat
# schedules both by those names (``send-weekly-plan-digest``,
# ``check-deprecated-sunset-events``); drop the import as unused and the
# schedule points at tasks no worker knows, which shows up only as
# "unregistered task" in a worker log.
__all__ = [
    "check_deprecated_sunset_events",
    "send_alert_delivery",
    "send_weekly_plan_digest",
]

# Ids of the AlertDeliveryItems already handed to Telegram, kept in the
# delivery's payload_snapshot. Telegram is the one channel whose delivery may
# take several messages, and the messages already accepted cannot be recalled —
# so what has reached the reader has to survive the failure of a later message,
# the failed status that follows it, and the retry after that.
TELEGRAM_DELIVERED_ITEM_IDS_KEY = "telegram_delivered_item_ids"

# How many Telegram messages of this delivery the reader has ACROSS attempts,
# kept in the same snapshot and written in the same commit as the ids above.
# It is not a resume point (see :func:`_read_delivered_item_ids`); the only
# thing it decides is where the "2/3" marker resumes counting.
TELEGRAM_PARTS_DELIVERED_KEY = "telegram_parts_delivered"


def _read_delivered_item_ids(payload_snapshot: object) -> set[uuid.UUID]:
    """Item ids a previous attempt recorded as delivered, from the snapshot.

    Item ids are the resume point rather than a "skip the first N parts"
    counter because item ids are stable across a retry and part boundaries are
    not: the AI note and each item's sparkline/top-mover context are re-resolved
    on the retry, so the same items can pack into different messages and a part
    counter would either duplicate or drop items.

    A part counter is nonetheless kept beside them
    (:func:`_read_delivered_part_count`), and it does not weaken that argument:
    it never decides what is SENT, only where the "2/3" marker resumes
    counting, and a marker off by one costs a reader a moment's confusion where
    a resume point off by one costs them an item.

    Anything unreadable is treated as "nothing delivered": re-sending a message
    is bad, but silently dropping items because a hand-edited snapshot did not
    parse is worse.
    """
    if not isinstance(payload_snapshot, dict):
        return set()
    raw = payload_snapshot.get(TELEGRAM_DELIVERED_ITEM_IDS_KEY)
    if not isinstance(raw, list):
        return set()
    delivered: set[uuid.UUID] = set()
    for value in raw:
        try:
            delivered.add(uuid.UUID(str(value)))
        except AttributeError, TypeError, ValueError:
            logger.warning("Ignoring unreadable delivered item id %r", value)
    return delivered


def _read_delivered_part_count(payload_snapshot: object) -> int:
    """Telegram messages of this delivery already in the chat, from the snapshot.

    Deliberately NOT ``payload_snapshot['telegram_message_parts']``, which is
    the nearest-looking key and the wrong one: that is the PLAN an attempt made
    (and only written when the plan ran to more than one message), overwritten
    by every later attempt, while this is what previous attempts actually got
    accepted. The two disagree exactly when an attempt fails part-way through,
    which is the only situation either is read in.

    Absent or unreadable means zero — the reverse of
    :func:`_read_delivered_item_ids`'s caution, and for the same reason: a
    wrong offset mislabels a message the reader still receives in full, so
    guessing low here costs less than refusing to send.
    """
    if not isinstance(payload_snapshot, dict):
        return 0
    raw = payload_snapshot.get(TELEGRAM_PARTS_DELIVERED_KEY)
    # ``bool`` is an ``int`` subclass, and ``True`` would silently become an
    # offset of one.
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        return 0
    return raw


def _telegram_too_long_error(
    delivery: AlertDelivery, delivered_ids: set[uuid.UUID], parts_sent: int, cause: Exception
) -> ValueError:
    """Give both the primary and plain fallback loops the same useful failure."""
    return ValueError(
        "Telegram refused a message as too long. "
        f"{len(delivered_ids)} of {len(delivery.items)} items had already been sent, "
        f"{parts_sent} message(s) of them in this attempt. The refused message "
        "carries a single item and cannot be split further, so the rule's "
        "message template, that one item and any AI note exceed Telegram's "
        "4096-character limit together. Shorten the rule's templates."
    )


def _record_delivered_items(
    session: Session,
    delivery: AlertDelivery,
    *,
    payload_snapshot: dict[str, object],
    delivered_ids: set[uuid.UUID],
    items: list[AlertDeliveryItem],
) -> dict[str, object]:
    """Commit the items Telegram just accepted, before the next message is posted.

    Committed per message rather than once at the end because the whole point is
    to survive the exception that ends the attempt: a value assigned to
    ``payload_snapshot`` but rolled back with the failure records nothing. The
    ``status=sent`` commit at the end of the task is a separate, later write.

    ``delivered_ids`` accumulates in place, and it is the DELIVERY's running
    set rather than this attempt's: the caller seeds it from the snapshot with
    what earlier attempts landed (:func:`_read_delivered_item_ids`) and each
    call here adds the part that just went out. Its size is therefore the whole
    answer to "how many items does the reader already have", which is what the
    too-long failure in :func:`send_alert_delivery` quotes — adding this
    attempt's own tally to it double-counts.

    The part COUNT written beside them answers a different question — how many
    MESSAGES the reader holds, not how many items — and only the "2/3" marker
    asks it (``alerts_messages.split_telegram_messages``'s ``part_offset``).
    Both are written here because both are facts about the one message that was
    just accepted, and a commit recording either without the other would resume
    with a marker that disagreed with the items printed under it.

    The new snapshot is returned rather than edited in place, because the dict
    handed in has already been committed and mutating that same object would
    leave SQLAlchemy comparing it against itself and skipping the UPDATE.
    """
    delivered_ids.update(item.id for item in items)
    updated = {
        **payload_snapshot,
        TELEGRAM_DELIVERED_ITEM_IDS_KEY: sorted(str(item_id) for item_id in delivered_ids),
        # One call, one accepted message, so +1. Counted off the snapshot rather
        # than off ``delivered_ids`` because items per message vary and the
        # marker counts messages.
        TELEGRAM_PARTS_DELIVERED_KEY: _read_delivered_part_count(payload_snapshot) + 1,
    }
    delivery.payload_snapshot = updated
    session.commit()
    return updated


def _digest_summary_items(
    delivery: AlertDelivery,
    items: list[AlertDeliveryItem] | None,
    *,
    digest: bool,
) -> list[AlertDeliveryItem] | None:
    """What a digest's header must describe when the body lists only part of it.

    ``${matched_count}`` and the list under it describe the MESSAGE in front of
    the reader; ``${headline}`` and ``${window_label}`` describe the DIGEST,
    which is one thing however many messages carry it
    (website/docs/use/alerting.md). ``_build_template_context`` can only keep
    those apart if the caller says what the whole is — handed nothing, it
    summarises whatever it was asked to render.

    A Telegram resume renders the UNDELIVERED SUBSET, and that is exactly the
    caller that used to hand it nothing. The retried part of a 24-item digest
    therefore announced "9 alerts" over a window that opens hours after the one
    the two earlier messages named: three disagreeing summaries of one morning,
    which is the reading the digest layout exists to prevent.
    The MarkdownV2→plain fallback re-renders a remainder for the same reason
    and needs the same answer. So does ``split_telegram_messages``, at both
    call sites: it re-renders every part it packs, and left to itself it
    re-derives the summary from the part it was handed — undoing this answer
    again the moment a remainder needs more than one message.

    None means "nothing to correct", and it is not the same as passing every
    item: a body that already lists the whole delivery keeps ``matched_count``
    — what dispatch counted — as the headline's total instead of recounting the
    rows, and a non-digest has no header making a claim about scope at all (a
    reader looking at message 2 of 2 of a plain alert counts what is in front of
    them, by design).
    """
    if not digest or items is None or len(items) == len(delivery.items):
        return None
    return list(delivery.items)


def _claim_delivery(session: Session, delivery: AlertDelivery, *, now: datetime) -> bool:
    """Take the single-flight claim on a pending delivery; False means someone else has it.

    Nothing else keeps two workers off one delivery. The send tasks leave the
    row untouched from the load at the top until the message is already out —
    on the Slack/webhook paths the first write is ``payload_snapshot`` and the
    first commit is the ``status=sent`` one — so two tasks carrying the same id
    both read `pending` and both send. That is not hypothetical:
    ``requeue_stranded_alert_deliveries`` re-enqueues a row on age alone, and
    the worker runs prefork with no ``--concurrency`` flag (compose.yaml), so
    the backlog that makes a row look stranded is exactly the condition under
    which its first send is still running in another process.

    The paths that DO commit before ``status=sent`` commit no earlier than
    their own send call returning, and none of them closes this race: the
    ticket paths record the external issue id in a ``payload_snapshot`` commit
    of their own, which stops a sequential RE-RUN from filing a second ticket
    but not a concurrent worker holding the copy of the row it loaded before
    that commit landed, and Telegram commits once per accepted message
    (:func:`_record_delivered_items`).

    So the claim is a compare-and-set in its own committed transaction — the
    shape ``alert_flush`` claims a digest window with — and the commit is the
    point: an uncommitted claim is invisible to the other process, which is the
    whole failure mode. Callers run it BEFORE resolving the destination, so a
    duplicate dispatch costs one UPDATE instead of an AI round-trip, a set of
    sparkline queries and a second message.

    It is a LEASE, not a lock: a worker SIGKILLed between the claim and the
    terminal status write leaves ``claimed_at`` set with no transaction left to
    roll it back. The lease is deliberately ``STRANDED_DELIVERY_MINUTES``, the
    reaper's own horizon, so the first redispatch that arm makes is also the
    first claim that can win — a shorter lease would re-open the race this
    closes, a longer one would refuse the reaper's redispatch and burn a
    dispatch attempt on a no-op. The UPDATE also bumps ``updated_at`` through
    the column's ``onupdate``, which keeps the reaper's pending arm (which
    requires ``updated_at < cutoff``) off a row whose send is live.

    A row that is not `pending` cannot be claimed. `sent` is already handled by
    the early return in :func:`send_alert_delivery`; `failed` is not sendable
    until the reaper's failed arm or the Inbox Retry button flips it back, and
    both do that in a committed transaction of their own.
    """
    # Deferred import, for the cycle maintenance.py documents from the other
    # side: entering the task graph at maintenance imports celery_app, which
    # imports this module, which would then be importing a half-initialized
    # maintenance whose constants below the import block do not exist yet.
    from tripl.worker.tasks.maintenance import STRANDED_DELIVERY_MINUTES

    abandoned_before = now - timedelta(minutes=STRANDED_DELIVERY_MINUTES)
    claimed = int(
        getattr(
            session.execute(
                update(AlertDelivery)
                .where(
                    AlertDelivery.id == delivery.id,
                    AlertDelivery.status == AlertDeliveryStatus.pending.value,
                    or_(
                        AlertDelivery.claimed_at.is_(None),
                        AlertDelivery.claimed_at < abandoned_before,
                    ),
                )
                .values(claimed_at=now)
                .execution_options(synchronize_session=False)
            ),
            "rowcount",
            0,
        )
        or 0
    )
    session.commit()
    if claimed != 1:
        return False
    # ``synchronize_session=False`` leaves the loaded row's copy of the column
    # stale, and the release at the end of the attempt is an ORM assignment: if
    # the in-session value still read NULL, assigning NULL would look like no
    # change at all, emit no UPDATE, and leave a finished delivery holding a
    # lease for the rest of the horizon.
    delivery.claimed_at = now
    return True


def _assert_egress_allowed(destination: AlertDestination, project: Project | None) -> None:
    """Refuse an outbound send from a demo project.

    A demo project is strictly zero-egress: the only sendable destination it may
    have is the local ``demo_sink``. The API refuses to create or enable an
    external destination on one, so what this guard covers is a row that exists
    anyway — written before that API guard, or by hand against the database.

    Every path that MINTS a delivery (scan dispatch, manual retry, stale-pending
    re-dispatch, the flushed digest) funnels through a send task, which is why
    the guard that actually stops the send lives here. "The flushed digest"
    means ``alert_digest_send.send_alert_digest``, which mints deliveries and
    calls this; it is NOT the weekly plan digest, which the older wording "the
    scheduled digest" read as.

    Two worker senders mint nothing and resolve a destination themselves: the
    weekly plan digest and the sunset alert. They are not outside the guard —
    ``alerts_digest._send_digest_to_destination`` calls it directly — but there
    it is a backstop, because both tasks already exclude demo projects in their
    SELECT. The one destination send genuinely outside it is the
    **Test** button, which calls this predicate from
    ``services/_alerting_test_send.send_destination_test``.

    The send tasks run it BEFORE rendering, so a refusal costs no AI round-trip;
    the digest sender runs it after building its message and says why there.
    """
    if (
        project is not None
        and project.is_demo
        and destination.type != AlertDestinationType.demo_sink
    ):
        raise ValueError(
            "External alert delivery is disabled for demo projects: destination "
            f"{destination.name!r} ({destination.type}) is not a local demo sink. "
            "Nothing was sent."
        )


def _assert_destination_enabled(destination: AlertDestination) -> None:
    """Refuse a send through a destination the operator switched off.

    Every path that MINTS a delivery already filters on ``enabled`` — the
    dispatcher's ``_load_enabled_alert_destinations``, both of the flusher's
    destination selects — so a row exists at all only because the toggle was on
    when the signal fired. What none of them can cover is the interval between
    minting and sending, and that interval is not the millisecond it sounds
    like: a ``.delay()`` lost to a broker restart, the stranded-delivery
    reaper's fifteen-minute redispatch, and the Inbox Retry button each hand an
    OLD row to a send task, and the toggle can have moved since. So the check
    belongs beside :func:`_assert_egress_allowed`, for the same reason that one
    does — the send task is the chokepoint every dispatcher funnels through —
    and it reads the toggle at the moment of SENDING rather than the moment of
    deciding, which is what "route no alerts here"
    (website/docs/use/alerting.md) has to mean to be worth anything.

    Called where the send tasks call it — at the top, before the render — this
    is the CHEAP half of that and NOT yet the moment of sending: the render in
    between is an AI round-trip and a set of sparkline/top-mover warehouse
    reads, which on a 24-item digest is where the seconds are. The moment of
    sending is :func:`_assert_destination_still_enabled` below, which both send
    tasks run again immediately before their outbound call and which
    ``alerts_digest._send_digest_to_destination`` — having no render of its own
    to protect — runs instead of this one.

    It raises rather than returning quietly so the Inbox carries a failed
    delivery naming the cause, instead of an alert that simply never arrived.
    Nothing resurrects that row behind the operator's back either: the reaper's
    failed arm excludes disabled destinations by design, and a hand-pressed
    Retry meets this same guard while the toggle is still off.

    The **Test** button is deliberately NOT subject to this. It is a separate
    endpoint that writes no delivery, and checking credentials before switching
    a destination back on is the commonest reason to press it
    (``services/_alerting_test_send.py``); disabled stops alerts, not answers.
    """
    if not destination.enabled:
        raise ValueError(
            f"Alert destination {destination.name!r} is disabled: alerts are not "
            "routed here. Nothing was sent."
        )


def _assert_destination_still_enabled(destination: AlertDestination) -> None:
    """:func:`_assert_destination_enabled`, re-READ, for the line before an egress.

    The check above is placed where a refusal is cheapest; this one is placed
    where a refusal is TRUE. Between them sits everything the send task does
    before it touches the network — the AI round-trip, the sparkline and
    top-mover warehouse reads, the template render, and in the digest sender an
    entire batch of other members rendered and committed. That is the window an
    operator actually flips a toggle in: the alert storm they are switching the
    channel off because of is the same storm that made the render slow.

    Re-READ rather than re-checked, and that is the whole of this function.
    Worker sessions are built ``expire_on_commit=False`` (worker/db.py) and
    nothing between the two calls expires the row, so calling
    :func:`_assert_destination_enabled` a second time on the same instance
    would hand back the value the first call already saw and could never
    disagree with it — a guard that cannot fail. So this reads the column
    itself: one primary-key SELECT of one column per delivery (or per digest
    group), not one per item, and not a second load of the destination's
    secrets.

    NOT ``session.refresh``, and not an ordinary ``session.execute`` either.
    Both FLUSH first, and a flush in the middle of the send is not harmless
    here: it freezes ``delivery.payload_snapshot``'s committed state on the very
    dict the branches below go on to mutate in place, after which re-assigning
    that same object is no longer a change the unit of work can see — and
    ``delivery_mode`` for a demo sink, or ``external_issue_key`` for a ticket,
    never reaches the row. Hence ``no_autoflush`` around the read, and
    ``set_committed_value`` to land the answer on the instance without marking
    it dirty (the idiom ``services/_branch_counterparts.py`` uses for the same
    reason: a read that must not become a write).

    A destination with no session — one a caller built or detached itself —
    keeps the value it was loaded with rather than raising, because the only
    honest answer available is the one already in hand.

    A destination DELETED mid-flight reads back as no row at all. That is
    refused too, by the same ``ValueError`` and into the same ``failed`` row: a
    delivery pointing at a destination that no longer exists is not one to send
    to either, and saying so in the error the operator reads beats an
    ``ObjectDeletedError`` from the ORM.
    """
    session = object_session(destination)
    if session is not None:
        with session.no_autoflush:
            still_enabled = session.execute(
                select(AlertDestination.enabled).where(AlertDestination.id == destination.id)
            ).scalar_one_or_none()
        if still_enabled is None:
            raise ValueError(
                f"Alert destination {destination.name!r} no longer exists; nothing was sent."
            )
        set_committed_value(destination, "enabled", still_enabled)
    _assert_destination_enabled(destination)


def _assert_rule_active(rule: AlertRule, *, now: datetime | None = None) -> None:
    """Refuse queued alerts after an operator disables or mutes their monitor."""
    if not rule.enabled:
        raise ValueError(f"Alert rule {rule.name!r} is disabled; nothing was sent.")
    muted_until = rule.muted_until
    if muted_until is not None and muted_until > (now or datetime.now(UTC)):
        raise ValueError(f"Alert rule {rule.name!r} is muted; nothing was sent.")


def _assert_rule_still_active(rule: AlertRule) -> None:
    """Read the rule switches immediately before outbound delivery."""
    session = object_session(rule)
    if session is not None:
        with session.no_autoflush:
            state = session.execute(
                select(AlertRule.enabled, AlertRule.muted_until).where(AlertRule.id == rule.id)
            ).one_or_none()
        if state is None:
            raise ValueError(f"Alert rule {rule.name!r} no longer exists; nothing was sent.")
        set_committed_value(rule, "enabled", state.enabled)
        set_committed_value(rule, "muted_until", state.muted_until)
    _assert_rule_active(rule)


def _resolve_slack_webhook(destination: AlertDestination) -> str:
    try:
        return validate_slack_webhook_url(_decrypt_secret(destination.webhook_url_encrypted))
    except ValueError as exc:
        raise ValueError(
            "Slack destination configuration is invalid. Update the webhook URL."
        ) from exc


def _resolve_email_context(
    session: Session,
    destination: AlertDestination,
) -> tuple[app_settings_service.EmailConfig, list[str], str]:
    """SMTP config, recipients and From: for one email destination, or raise.

    The relay is the destination's project's ORGANIZATION's (F20 PR9).
    """
    email_config = app_settings_service.get_email_config_for_project_sync(
        session, destination.project_id
    )
    if not email_config.smtp_host:
        raise ValueError(
            "Email destination is configured but SMTP is not — set SMTP_HOST "
            "(and SMTP_USERNAME/SMTP_PASSWORD if your relay requires auth)."
        )
    try:
        recipients_csv = validate_email_recipients(destination.email_recipients)
    except ValueError as exc:
        raise ValueError(
            "Email destination configuration is invalid. Update the recipients list."
        ) from exc
    recipients = _parse_email_recipients(recipients_csv)
    # The override only on the organization's own relay (critique #16): through
    # the operator's relay the organization's mail goes out under its sender.
    from_address = app_settings_service.email_sender_for(
        destination.email_from_address, email_config
    )
    if not from_address:
        raise ValueError("Email destination has no From: address and SMTP_FROM_ADDRESS is unset.")
    try:
        # ``validate_sender_address``, not the strict ``validate_email_address``:
        # the global Default From is free text an operator types, and
        # ``Tripl Alerts <no-reply@example.com>`` is what they naturally type
        # there. The strict helper refuses a display name outright, while both
        # diagnostics — Settings → Send test email and the destination's own
        # Test — accept one, so a Default From that passed every check the UI
        # offers then failed EVERY real alert here, with the message below.
        # A diagnostic that is more permissive than delivery is
        # worse than no diagnostic: it certifies a configuration that does not
        # deliver. The ORIGINAL string comes back, display name intact, and goes
        # straight into ``msg["From"]``, which takes it; what is still refused
        # is a value with no @-sign, which serialises happily and comes back
        # from the relay naming nothing.
        #
        # The address also stops being normalised here (lower-cased domain,
        # IDNA-encoded) — nothing reads it back. It is written to one header and
        # never compared or stored, so the normal form had no consumer.
        from_address = validate_sender_address(from_address)
    except ValueError as exc:
        raise ValueError(
            "Email destination From: address is invalid. Update the override or SMTP_FROM_ADDRESS."
        ) from exc
    return email_config, recipients, from_address


def _stamp_rule_state(session: Session, delivery: AlertDelivery) -> None:
    """Record this delivery as the last notification for each item's scope."""
    for item in delivery.items:
        filters = [
            AlertRuleState.rule_id == delivery.rule_id,
            AlertRuleState.scope_type == item.scope_type,
            AlertRuleState.scope_ref == item.scope_ref,
        ]
        # Every scope but the project-global ones keys its state on the scan
        # config that produced the delivery. A ``metric`` scope
        # and a ``lifecycle`` one (GH #258) store NULL there, so they match on
        # the NULL — filter for what the row actually holds, rather than
        # dropping the column.
        #
        # Dropping it is what this used to do, and it stamped EVERY metric state
        # of this rule and scope in the project. While metric states were
        # anchored on the project's lowest config id, a created or deleted scan
        # config moved that anchor and stranded the old row: dropping the filter
        # then kept stamping the stranded row's ``last_notified_at``, so it read
        # as freshly notified forever while nothing could ever load it again.
        if not is_project_global_scope(item.scope_type):
            filters.append(AlertRuleState.scan_config_id == delivery.scan_config_id)
        else:
            filters.append(AlertRuleState.scan_config_id.is_(None))
        # Not scalar_one_or_none — and NOT because a second row is expected.
        # Both arms are single-row by key: the config arm's four equalities are
        # exactly ``uq_alert_rule_state_scope``, and the NULL arm is covered by
        # the partial ``uq_alert_rule_state_metric_scope``, so against this
        # tree's schema the body below runs at most once and the two forms
        # cannot be told apart. What differs is the behaviour when that second
        # key is absent, and it is the fragile one of the pair: SQL treats NULLs
        # as DISTINCT, so the composite constraint stops deduping the moment the
        # column is NULL, and every dialect has to be told about the partial
        # index separately (``sqlite_where`` on the model is the only reason the
        # test database has one at all). Without it, two collections of the same
        # project that both run dispatch's ``scan_config_id IS NULL`` load before
        # either inserts leave two project-global rows, and this query returns
        # them together.
        #
        # The loop stamps both and moves on; scalar_one_or_none would raise
        # MultipleResultsFound — inside the ``try`` whose message is ALREADY in
        # front of the reader, and whose handler rolls the ``sent`` status back
        # and records ``failed`` instead. Retry in the Inbox re-dispatches a
        # failed row (``_alerting_deliveries.retry_delivery``), and the Slack,
        # webhook and email paths keep no delivered-marker to skip on the way
        # Telegram and the ticket channels do: the alert would ship twice
        # because a bookkeeping write raised after the send.
        #
        # It does not reach the row an old worker anchored on a config
        # mid-deploy either: the ``.is_(None)`` arm above excludes that one on
        # purpose, so it is never stamped — and stamping it is what used to keep
        # it looking freshly notified forever. Retiring it is dispatch's job,
        # not this one's: ``metrics.dispatch._retire_config_anchored_metric_states``
        # deletes it on the next collection that dispatches the rule.
        for state in session.execute(select(AlertRuleState).where(*filters)).scalars():
            state.last_notified_at = delivery.sent_at
            state.last_notified_delivery_id = delivery.id


def _post_json(
    url: str,
    body: dict[str, object],
    headers: dict[str, str] | None = None,
) -> dict[str, object] | None:
    return _channel_post_json(url, body, headers)


def _send_slack_message(webhook_url: str, text: str, *, message_format: str) -> None:
    _channel_send_slack_message(_post_json, webhook_url, text, message_format=message_format)


def _send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    message_format: str,
) -> None:
    _channel_send_telegram_message(
        _post_json,
        bot_token,
        chat_id,
        text,
        message_format=message_format,
    )


def _send_webhook_message(
    target_url: str,
    payload: dict[str, object],
    *,
    header_name: str | None = None,
    header_value: str | None = None,
) -> None:
    _channel_send_webhook_message(
        _post_json,
        target_url,
        payload,
        header_name=header_name,
        header_value=header_value,
    )


def _send_email_message(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    smtp_security: str,
    from_address: str,
    recipients: list[str],
    subject: str,
    body: str,
) -> None:
    _channel_send_email_message(
        smtp_module=smtplib,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        smtp_security=smtp_security,
        from_address=from_address,
        recipients=recipients,
        subject=subject,
        body=body,
    )


def _send_jira_issue(
    *,
    base_url: str,
    auth_email: str,
    api_token: str,
    project_key: str,
    issue_type: str,
    summary: str,
    body_text: str,
) -> tuple[str | None, str | None]:
    return _channel_send_jira_issue(
        _post_json,
        base_url=base_url,
        auth_email=auth_email,
        api_token=api_token,
        project_key=project_key,
        issue_type=issue_type,
        summary=summary,
        body_text=body_text,
    )


def _send_linear_issue(
    *,
    api_key: str,
    team_id: str,
    title: str,
    body_text: str,
    state_id: str | None = None,
    label_ids: list[str] | None = None,
) -> tuple[str | None, str | None]:
    return _channel_send_linear_issue(
        _post_json,
        api_key=api_key,
        team_id=team_id,
        title=title,
        body_text=body_text,
        state_id=state_id,
        label_ids=label_ids,
    )


def _enqueue_owner_followup(session: Session, delivery: AlertDelivery) -> None:
    """Queue the owner follow-up (F07, #260) for a delivery committed as sent; never raises.

    Called from the success path AND the ``already_sent`` early return of both
    send tasks: a worker killed after the delivery's commit but before this
    enqueue would otherwise leave its owners untold, and the follow-up's own
    claim makes a repeat harmless. Nothing for a rule without ``notify_owners``
    or a demo project (zero egress). Every caller sits inside a ``try`` whose
    handler marks the delivery failed, so nothing may escape from here.
    """
    try:
        rule = session.get(AlertRule, delivery.rule_id)
        if rule is None or not rule.notify_owners:
            return
        project = session.get(Project, delivery.project_id)
        if project is not None and project.is_demo:
            return
        # Deferred: alert_owner_notify imports this package's message helpers.
        from tripl.worker.tasks.alert_owner_notify import enqueue_owner_notifications

        enqueue_owner_notifications(str(delivery.id))
    except Exception:  # noqa: BLE001 — the delivery is sent; the follow-up is best effort
        logger.exception("Could not queue the owner follow-up for delivery %s", delivery.id)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.alerts.send_alert_delivery",
    bind=True,
)
def send_alert_delivery(self: object, delivery_id: str) -> dict[str, object]:
    session = _get_sync_session()
    message_format: str | None = None
    rendered_message: str | None = None
    try:
        delivery = session.execute(
            select(AlertDelivery)
            .options(selectinload(AlertDelivery.items))
            .where(AlertDelivery.id == uuid.UUID(delivery_id))
        ).scalar_one_or_none()
        if delivery is None:
            raise ValueError(f"AlertDelivery {delivery_id} not found")

        # Idempotency: with task_acks_late a worker SIGKILLed after a successful
        # send but before commit gets the task re-queued. If the delivery already
        # committed as sent, treat the re-run as a no-op so we don't re-send the
        # message or create a duplicate ticket.
        if delivery.status == AlertDeliveryStatus.sent.value:
            # The one thing a re-run still owes: the owner follow-up, in case
            # the run that sent died before queueing it (F07, #260).
            _enqueue_owner_followup(session, delivery)
            return {"status": "already_sent", "delivery_id": delivery_id}

        # Single flight. The early return above only catches a
        # re-run that STARTS after the first one committed; a second worker
        # that starts while this delivery is still being rendered or posted
        # reads the same `pending` row and sends a second copy. That is what
        # the reaper's age-based redispatch produces on a backlogged worker,
        # and it is the one duplicate this pipeline can actually prevent — the
        # other one, a send whose response timed out after the receiver
        # accepted it, is a trade the docs make deliberately.
        if not _claim_delivery(session, delivery, now=datetime.now(UTC)):
            logger.info(
                "Alert delivery %s is already being sent by another worker; skipping",
                delivery_id,
            )
            return {"status": "already_claimed", "delivery_id": delivery_id}

        destination = session.get(AlertDestination, delivery.destination_id)
        rule = session.get(AlertRule, delivery.rule_id)
        scan_config = session.get(ScanConfig, delivery.scan_config_id)
        project = session.get(Project, delivery.project_id)
        if destination is None or rule is None or scan_config is None:
            raise ValueError(f"AlertDelivery {delivery_id} is missing related objects")

        is_demo_project = project is not None and project.is_demo
        _assert_egress_allowed(destination, project)
        # Read here, not at dispatch: this row may have been minted before the
        # operator flipped the toggle. Like the guard above it
        # runs BEFORE the render, so a destination that is off costs no AI
        # round-trip and no sparkline queries either.
        #
        # That saving is ALL this call buys. It is not what makes "a disabled
        # destination is not sent to" true, because the send is still an AI
        # round-trip and a set of warehouse reads away; the re-read immediately
        # before the branch dispatch below is.
        _assert_destination_enabled(destination)
        _assert_rule_active(rule)

        if delivery.matched_count and not delivery.items:
            logger.warning(
                "Alert delivery %s has matched_count=%d but no items; sending existing payload",
                delivery.id,
                delivery.matched_count,
            )
            alert_delivery_missing_items_total.inc()

        # Built once and reused across re-renders (e.g. the MarkdownV2→plain
        # fallback) so the warehouse/DB queries behind sparkline + top-movers
        # don't run a second time when something is already failing.
        item_context_cache: dict[uuid.UUID, tuple[str, ...]] = {}
        # Same idea for metric units: resolved once (one batched query) and
        # reused by the session-less fallback render below.
        metric_units_cache: dict[str, str | None] = {}

        # Resume point for a Telegram delivery that a previous attempt got
        # part-way through. Telegram is the only channel that may need several
        # messages for one delivery, and each accepted message is with the
        # reader for good — so this attempt renders and sends ONLY the items
        # that have not been delivered yet. Every other channel sends the whole
        # delivery in one call, so there is nothing to resume.
        already_delivered_ids = _read_delivered_item_ids(delivery.payload_snapshot)
        # Messages rather than items: what the "2/3" marker has to carry on
        # from, so the retry does not restart the reader's count at one.
        already_delivered_parts = _read_delivered_part_count(delivery.payload_snapshot)
        is_telegram_resume = destination.type == AlertDestinationType.telegram and bool(
            already_delivered_ids
        )
        # None means "the whole delivery", which is what every first attempt and
        # every non-Telegram channel renders.
        pending_items: list[AlertDeliveryItem] | None = None
        telegram_fully_delivered = False
        if is_telegram_resume:
            remaining_items = [
                item for item in delivery.items if item.id not in already_delivered_ids
            ]
            if remaining_items:
                pending_items = remaining_items
            else:
                # Every part landed and only the bookkeeping is missing (the
                # worker died between the last accepted message and the status
                # commit, or that commit itself raised). Retry from the Inbox
                # therefore FINISHES the delivery instead of re-sending it: the
                # reader already has every item, and a second copy of an alert
                # costs their trust in the channel, while the operator's actual
                # complaint — a delivery stuck on "failed" — is fixed either
                # way. The full message is still re-rendered below so the
                # snapshot the Inbox shows describes the whole delivery.
                telegram_fully_delivered = True

        # A digest is stamped at creation (metrics/dispatch.py) because nothing
        # at send time can infer it: the cadence column misses the drain arm and
        # the buffer rows are long gone.
        is_digest = bool(
            isinstance(delivery.payload_snapshot, dict) and delivery.payload_snapshot.get("digest")
        )
        # For a digest the note is part of the LAYOUT, not a tail — it sits
        # above the list so the reader meets the summary first — so it has to
        # exist before the render rather than be appended after it. It is also
        # computed over EVERY item, which is the whole point of one digest
        # instead of three chunks, so the immediate path's 10-item prompt cap
        # would silently describe less than half a 24-item morning.
        #
        # Off on a resume for the reason the appended note below is off on one:
        # the note rides on the FIRST message only and that message is already
        # with the reader, so regenerating it would buy an LLM round-trip and
        # then print a second copy above the remainder. The earlier attempt's
        # text stays in ``payload_snapshot['ai_explanation']`` — nothing below
        # overwrites the key when there is no new note — so the Inbox still
        # shows what was actually sent.
        digest_ai: str | None = None
        if (
            is_digest
            and rule.ai_explanation_enabled
            and not is_demo_project
            and not is_telegram_resume
        ):
            digest_ai = _build_ai_explanation(
                delivery,
                scan_name=scan_config.name,
                project_name=project.name if project else "",
                item_context_cache=item_context_cache,
                session=session,
                max_items=DIGEST_AI_EXPLANATION_MAX_ITEMS,
            )

        text, message_format = _render_delivery_message(
            delivery,
            destination=destination,
            rule=rule,
            scan_name=scan_config.name,
            project=project,
            session=session,
            item_context_cache=item_context_cache,
            metric_units_cache=metric_units_cache,
            items=pending_items,
            # The body is the remainder; the header still describes the whole
            # digest. None on a first attempt, where they are the same set.
            summary_items=_digest_summary_items(delivery, pending_items, digest=is_digest),
            digest=is_digest,
            ai_explanation=digest_ai,
            project_timezone=project.timezone if project else None,
        )
        # AI explanation is generated once (LLM round-trip) and appended after
        # template rendering so custom templates stay untouched; the Telegram
        # plain-format fallback below re-appends the same cached string.
        ai_explanation: str | None = None
        # The AI explanation is an outbound LLM call, so it is off for demo
        # projects for the same zero-egress reason as the send guard above — a
        # demo_sink delivery must stay fully local end to end. It is also off on
        # a resume: the note rides on the first message only and that message is
        # already with the reader, so regenerating it would cost an LLM
        # round-trip to produce something nobody would see.
        if (
            rule.ai_explanation_enabled
            and not is_demo_project
            and not is_telegram_resume
            and not is_digest
        ):
            ai_explanation = _build_ai_explanation(
                delivery,
                scan_name=scan_config.name,
                project_name=project.name if project else "",
                item_context_cache=item_context_cache,
                # Lets the explanation build on what this rule already sent for
                # these scopes rather than restating it.
                session=session,
            )
        if ai_explanation:
            text = _append_ai_explanation(text, ai_explanation, message_format)

        rendered_message = text
        payload_snapshot = (
            dict(delivery.payload_snapshot) if isinstance(delivery.payload_snapshot, dict) else {}
        )
        payload_snapshot["message_format"] = message_format
        payload_snapshot["rendered_message"] = text
        if ai_explanation or digest_ai:
            payload_snapshot["ai_explanation"] = ai_explanation or digest_ai
        delivery.payload_snapshot = payload_snapshot

        # The toggle again, with the render behind us and the next statement
        # the outbound call itself. The check at the top of the
        # task bought the AI round-trip and the sparkline queries; this one
        # buys the window between them and the egress — the only window long
        # enough for an operator to reach the switch, and the one the check at
        # the top cannot see into. It costs a single-column SELECT on a row
        # already in the session, so it is not a second query per item.
        #
        # The render is deliberately NOT thrown away: ``rendered_message`` and
        # ``message_format`` are already set, so the failure arm stamps the
        # body this delivery would have carried onto the `failed` row and the
        # Inbox can show the operator exactly what their toggle stopped.
        _assert_destination_still_enabled(destination)
        _assert_rule_still_active(rule)

        if destination.type == AlertDestinationType.slack:
            _send_slack_message(
                _resolve_slack_webhook(destination), text, message_format=message_format
            )
        elif destination.type == AlertDestinationType.telegram:
            try:
                bot_token = validate_telegram_bot_token(
                    _decrypt_secret(destination.bot_token_encrypted)
                )
                chat_id = validate_telegram_chat_id(destination.chat_id)
            except ValueError as exc:
                raise ValueError(
                    "Telegram destination configuration is invalid. "
                    "Update the bot token or chat id."
                ) from exc
            # Telegram is the one channel with a per-message ceiling, so it is
            # the one channel whose delivery may need more than one message.
            # Nothing is dropped to fit it (website/docs/use/alerting.md): the
            # items are packed into as many messages as they take, each measured
            # assembled and in Telegram's own UTF-16 units. The dispatcher's
            # 8-item chunking is an upstream estimate of the same ceiling and
            # cannot see the rule's template or the AI note, so this is where
            # the promise is actually kept.
            send_items = list(delivery.items) if pending_items is None else pending_items
            parts = (
                []
                if telegram_fully_delivered
                else split_telegram_messages(
                    delivery,
                    destination=destination,
                    rule=rule,
                    scan_name=scan_config.name,
                    project=project,
                    message=text,
                    message_format=message_format,
                    session=session,
                    item_context_cache=item_context_cache,
                    metric_units_cache=metric_units_cache,
                    ai_explanation=ai_explanation or digest_ai,
                    items=pending_items,
                    # The same correction the unsplit render above makes, and
                    # it has to be repeated because the split re-renders every
                    # part: without it a remainder needing two messages
                    # summarises the remainder in both of them.
                    summary_items=_digest_summary_items(delivery, pending_items, digest=is_digest),
                    digest=is_digest,
                    # Zero on a first attempt. On a resume it is what keeps the
                    # marker continuous with the messages already on the
                    # reader's screen.
                    part_offset=already_delivered_parts,
                    project_timezone=project.timezone if project else None,
                )
            )
            if len(parts) > 1:
                # This attempt's PLAN, written only when the plan runs to more
                # than one message. NOT the marker's offset — that is
                # TELEGRAM_PARTS_DELIVERED_KEY, written per message actually
                # accepted; this key is overwritten by every attempt and says
                # nothing about what reached the reader.
                payload_snapshot["telegram_message_parts"] = len(parts)
                delivery.payload_snapshot = payload_snapshot

            # Which items this attempt has put in front of the reader; combined
            # with the ids a previous attempt recorded, it is what the fallback
            # below must NOT re-send.
            delivered_items: list[AlertDeliveryItem] = []
            # Messages this attempt actually got into the chat. ``len(parts)`` is
            # the PLAN, so quoting it in a failure would send an operator looking
            # for messages that were never sent.
            parts_sent = 0
            try:
                for part_text, part_items in parts:
                    _send_telegram_message(
                        bot_token,
                        chat_id,
                        part_text,
                        message_format=message_format,
                    )
                    parts_sent += 1
                    delivered_items.extend(part_items)
                    payload_snapshot = _record_delivered_items(
                        session,
                        delivery,
                        payload_snapshot=payload_snapshot,
                        delivered_ids=already_delivered_ids,
                        items=part_items,
                    )
            except ValueError as exc:
                # HTML belongs here as much as MarkdownV2 does, and only did
                # not because nothing put runtime-built markup in an HTML body
                # before the digest link did. Telegram rejects the WHOLE message
                # on a parse error, and a digest's inputs (a saved template, a
                # scope name, the app base url) do not change between mornings —
                # so without this arm one bad character fails at 10:00 every day
                # forever, silently, and never self-heals.
                if message_format in (
                    ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2,
                    ALERT_MESSAGE_FORMAT_TELEGRAM_HTML,
                ) and _is_telegram_markdown_parse_error(exc):
                    this_attempt_delivered_ids = {item.id for item in delivered_items}
                    remaining = [
                        item for item in send_items if item.id not in this_attempt_delivered_ids
                    ]
                    # Reuse the already-built sparkline/top-movers context and
                    # skip the session so the fallback only re-formats — no
                    # second round of warehouse/DB queries.
                    fallback_text, fallback_format = _render_delivery_message(
                        delivery,
                        destination=destination,
                        rule=rule,
                        scan_name=scan_config.name,
                        project=project,
                        message_format_override=ALERT_MESSAGE_FORMAT_PLAIN,
                        session=None,
                        item_context_cache=item_context_cache,
                        metric_units_cache=metric_units_cache,
                        items=remaining,
                        # ``remaining`` is a remainder twice over here — of a
                        # resume, and of the parts this attempt already landed
                        # in the format that has just been refused — so the
                        # header needs the whole digest even on a first attempt.
                        summary_items=_digest_summary_items(delivery, remaining, digest=is_digest),
                        # Still a digest. Dropping this reverts the reader to
                        # the verbose 317-character-per-item layout precisely
                        # when something has already gone wrong.
                        digest=is_digest,
                        ai_explanation=digest_ai if not delivered_items else None,
                        project_timezone=project.timezone if project else None,
                    )
                    # The note went out with the first message; do not repeat it.
                    fallback_note = None if delivered_items else ai_explanation
                    if fallback_note:
                        fallback_text = _append_ai_explanation(
                            fallback_text,
                            fallback_note,
                            fallback_format,
                        )
                    try:
                        fallback_parts = split_telegram_messages(
                            delivery,
                            destination=destination,
                            rule=rule,
                            scan_name=scan_config.name,
                            project=project,
                            message=fallback_text,
                            message_format=fallback_format,
                            items=remaining,
                            summary_items=_digest_summary_items(
                                delivery, remaining, digest=is_digest
                            ),
                            session=None,
                            item_context_cache=item_context_cache,
                            metric_units_cache=metric_units_cache,
                            ai_explanation=fallback_note,
                            digest=is_digest,
                            # Read from the running snapshot, so it includes the
                            # parts THIS attempt landed before the parse error: the
                            # messages that went out in MarkdownV2 are still in the
                            # chat, and the plain-text remainder continues their
                            # numbering instead of opening a second sequence.
                            part_offset=_read_delivered_part_count(payload_snapshot),
                            project_timezone=project.timezone if project else None,
                        )
                        for part_text, part_items in fallback_parts:
                            _send_telegram_message(
                                bot_token,
                                chat_id,
                                part_text,
                                message_format=fallback_format,
                            )
                            parts_sent += 1
                            # A fallback can fail part-way through too.
                            delivered_items.extend(part_items)
                            payload_snapshot = _record_delivered_items(
                                session,
                                delivery,
                                payload_snapshot=payload_snapshot,
                                delivered_ids=already_delivered_ids,
                                items=part_items,
                            )
                    except ValueError as fallback_exc:
                        if _is_telegram_message_too_long_error(fallback_exc):
                            raise _telegram_too_long_error(
                                delivery, already_delivered_ids, parts_sent, fallback_exc
                            ) from fallback_exc
                        raise
                    # A fresh dict, not an in-place edit, for the same reason
                    # _record_delivered_items returns one: this snapshot may
                    # already be committed, and SQLAlchemy would compare the
                    # mutated object against itself and skip the UPDATE.
                    payload_snapshot = {
                        **payload_snapshot,
                        "requested_message_format": message_format,
                        "fallback_reason": "telegram_markdown_parse_error",
                        "message_format": fallback_format,
                        "rendered_message": fallback_text,
                    }
                    delivery.payload_snapshot = payload_snapshot
                    rendered_message = fallback_text
                    message_format = fallback_format
                elif _is_telegram_message_too_long_error(exc):
                    # The split already measured every message assembled and in
                    # Telegram's units, so the only body it cannot shrink is one
                    # carrying a SINGLE item — re-rendering that at any budget
                    # produces the same message, which is why the retry that
                    # used to live here is gone rather than merely fixed. Say
                    # what happened instead: the raw HTTP 400 in the Inbox names
                    # neither the cause nor how much of the delivery got out.
                    # The numerator is ``already_delivered_ids`` ALONE. That set
                    # is seeded from the snapshot with what earlier attempts
                    # landed and _record_delivered_items adds each part this
                    # attempt lands to it in place, so it is already the union —
                    # exactly "how many of its items had already gone out", the
                    # number website/docs/use/alerting.md promises the Inbox
                    # reports. Adding ``len(delivered_items)`` counted this
                    # attempt's parts a second time: a 24-item delivery that
                    # landed two 8-item parts and lost the third announced
                    # "32 of 24 items had already been sent".
                    # ``parts_sent`` below is this attempt's alone on purpose,
                    # which is why the sentence names the attempt only there.
                    raise _telegram_too_long_error(
                        delivery, already_delivered_ids, parts_sent, exc
                    ) from exc
                else:
                    raise
        elif destination.type == AlertDestinationType.webhook:
            try:
                target_url = validate_webhook_target_url(
                    _decrypt_secret(destination.target_url_encrypted)
                )
            except ValueError as exc:
                raise ValueError(
                    "Webhook destination configuration is invalid. Update the target URL."
                ) from exc
            header_value = (
                _decrypt_secret(destination.webhook_header_value_encrypted)
                if destination.webhook_header_value_encrypted
                else None
            )
            # SSRF re-check at send time (DNS-rebinding defense).
            _reject_private_target(target_url, field="Webhook target_url")
            webhook_payload = _build_webhook_payload(
                delivery,
                destination=destination,
                rule=rule,
                scan_name=scan_config.name,
                project=project,
                message=text,
            )
            _send_webhook_message(
                target_url,
                webhook_payload,
                header_name=destination.webhook_header_name,
                header_value=header_value,
            )
        elif destination.type == AlertDestinationType.email:
            email_config, recipients, from_address = _resolve_email_context(session, destination)
            subject = _build_email_subject(
                template=destination.email_subject_template,
                rule=rule,
                project=project,
                matched_count=delivery.matched_count,
                destination=destination,
                message_format=message_format,
            )
            _send_email_message(
                smtp_host=email_config.smtp_host,
                smtp_port=email_config.smtp_port,
                smtp_username=email_config.smtp_username,
                smtp_password=email_config.smtp_password,
                smtp_security=email_config.smtp_security,
                from_address=from_address,
                recipients=recipients,
                subject=subject,
                body=text,
            )
        elif destination.type == AlertDestinationType.jira:
            try:
                base_url = validate_jira_base_url(destination.jira_base_url)
                auth_email = validate_jira_auth_email(destination.jira_auth_email)
                api_token = validate_jira_api_token(
                    _decrypt_secret(destination.jira_api_token_encrypted)
                )
                project_key = validate_jira_project_key(destination.jira_project_key)
                issue_type = validate_jira_issue_type(destination.jira_issue_type or "Task")
            except ValueError as exc:
                raise ValueError(
                    "Jira destination configuration is invalid. Update the base URL, "
                    "credentials, project key, or issue type."
                ) from exc
            # SSRF re-check at send time (DNS-rebinding defense).
            _reject_private_target(base_url, field="Jira base_url")
            # Idempotency: if a previous attempt already created the ticket but
            # crashed before committing status=sent, the external id is recorded
            # in the snapshot — skip creation to avoid a duplicate ticket.
            if payload_snapshot.get("external_issue_id") or payload_snapshot.get(
                "external_issue_key"
            ):
                logger.info(
                    "Skipping Jira issue creation for delivery %s: already created (%s)",
                    delivery_id,
                    payload_snapshot.get("external_issue_key"),
                )
            else:
                summary = _build_ticket_subject(
                    rule=rule,
                    project=project,
                    matched_count=delivery.matched_count,
                )
                issue_id, issue_key = _send_jira_issue(
                    base_url=base_url,
                    auth_email=auth_email,
                    api_token=api_token,
                    project_key=project_key,
                    issue_type=issue_type,
                    summary=summary,
                    body_text=text,
                )
                if issue_id is not None:
                    payload_snapshot["external_issue_id"] = issue_id
                if issue_key is not None:
                    payload_snapshot["external_issue_key"] = issue_key
                delivery.payload_snapshot = payload_snapshot
                # Persist the external id in its own commit, before the final
                # status=sent commit. If the worker is killed in the window
                # between ticket creation and that final commit, the recorded id
                # survives so the guard above skips re-creation on re-run.
                session.commit()
        elif destination.type == AlertDestinationType.linear:
            try:
                api_key = validate_linear_api_key(
                    _decrypt_secret(destination.linear_api_key_encrypted)
                )
                team_id = validate_linear_team_id(destination.linear_team_id)
            except ValueError as exc:
                raise ValueError(
                    "Linear destination configuration is invalid. Update the API key or team id."
                ) from exc
            # Idempotency: skip creation if a prior attempt already created the
            # ticket (id recorded in snapshot) but crashed before committing.
            if payload_snapshot.get("external_issue_id") or payload_snapshot.get(
                "external_issue_key"
            ):
                logger.info(
                    "Skipping Linear issue creation for delivery %s: already created (%s)",
                    delivery_id,
                    payload_snapshot.get("external_issue_key"),
                )
            else:
                label_ids = (
                    [lid for lid in destination.linear_label_ids.split(",") if lid]
                    if destination.linear_label_ids
                    else None
                )
                title = _build_ticket_subject(
                    rule=rule,
                    project=project,
                    matched_count=delivery.matched_count,
                )
                issue_id, identifier = _send_linear_issue(
                    api_key=api_key,
                    team_id=team_id,
                    title=title,
                    body_text=text,
                    state_id=destination.linear_state_id,
                    label_ids=label_ids,
                )
                if issue_id is not None:
                    payload_snapshot["external_issue_id"] = issue_id
                if identifier is not None:
                    payload_snapshot["external_issue_key"] = identifier
                delivery.payload_snapshot = payload_snapshot
                # Persist the external id in its own commit, before the final
                # status=sent commit, so a crash in between can't create a
                # duplicate ticket on re-run (see the Jira branch above).
                session.commit()
        elif destination.type == AlertDestinationType.demo_sink:
            # Local, non-sendable sink for generated demo projects.
            # The message is already rendered above and stored in
            # payload_snapshot["rendered_message"]; here we ONLY stamp local
            # markers and perform NO network call — no httpx/urllib POST, no
            # SMTP, no SSRF re-check. The delivery then falls through to the
            # shared status=sent block below, so retry and simulate for a
            # demo_sink take this same zero-network path. The is_local /
            # simulated markers (and channel=demo_sink) make the API response
            # clearly a LOCAL SIMULATED delivery that never claims an external
            # Slack/Jira/Linear/email success.
            payload_snapshot["delivery_mode"] = "local_sink"
            payload_snapshot["is_local"] = True
            payload_snapshot["simulated"] = True
            payload_snapshot["local_notice"] = DEMO_SINK_LOCAL_NOTICE
            delivery.payload_snapshot = payload_snapshot
        else:
            raise ValueError(f"Unsupported destination type {destination.type}")

        delivery.status = AlertDeliveryStatus.sent.value
        delivery.sent_at = datetime.now(UTC)
        delivery.error_message = None
        # The attempt is over, so the lease goes with it. `sent` is guarded by
        # the early return at the top rather than by the claim, and a row still
        # holding a lease it no longer needs is a row a later legitimate
        # dispatch would have to wait out.
        delivery.claimed_at = None
        alert_deliveries_total.labels(status=AlertDeliveryStatus.sent.value).inc()
        _stamp_rule_state(session, delivery)
        session.commit()
        if project is not None:
            realtime.publish_project_event(
                project.id,
                project.slug,
                realtime.EVENT_ACTIVITY_CREATED,
                {"delivery_id": delivery_id, "status": AlertDeliveryStatus.sent.value},
            )
        # Owner routing (F07, #260): only once the send is committed, so a
        # retried main delivery that fails never emails an owner. The helper
        # skips demo projects and swallows its own errors.
        _enqueue_owner_followup(session, delivery)
        return {"status": "sent", "delivery_id": delivery_id}
    except Exception as exc:
        logger.exception("Failed to send alert delivery %s", delivery_id)
        session.rollback()
        delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
        if delivery is not None:
            payload_snapshot = (
                dict(delivery.payload_snapshot)
                if isinstance(delivery.payload_snapshot, dict)
                else {}
            )
            if message_format is not None:
                payload_snapshot["message_format"] = message_format
            if rendered_message is not None:
                payload_snapshot["rendered_message"] = rendered_message
            if payload_snapshot:
                delivery.payload_snapshot = payload_snapshot
            delivery.status = AlertDeliveryStatus.failed.value
            delivery.error_message = str(exc)
            # Released with the attempt, and this half matters more than the
            # success one: the Inbox Retry button flips this row straight back
            # to `pending` and re-dispatches it, so a lease left behind would
            # make that send a silent no-op until it expired — an operator
            # pressing Retry and watching nothing happen for fifteen minutes is
            # worse than the duplicate the claim exists to stop.
            delivery.claimed_at = None
            session.commit()
            failed_project = session.get(Project, delivery.project_id)
            if failed_project is not None:
                realtime.publish_project_event(
                    failed_project.id,
                    failed_project.slug,
                    realtime.EVENT_ACTIVITY_CREATED,
                    {"delivery_id": delivery_id, "status": AlertDeliveryStatus.failed.value},
                )
        alert_deliveries_total.labels(status=AlertDeliveryStatus.failed.value).inc()
        return {"status": "failed", "delivery_id": delivery_id, "error": str(exc)}
    finally:
        session.close()
