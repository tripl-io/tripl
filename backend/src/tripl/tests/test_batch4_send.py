"""Batch-4 regressions for the alert SEND path (``worker/tasks/alerts.py``).

Named for its batch, the way ``test_batch3_a1.py``…``test_batch3_g1.py`` are.
The Telegram seeding these tests need already exists in ``test_alerting.py``
and is imported rather than copied: a second copy of a 100-line delivery
fixture stops matching the first the moment the model gains a column — and the
model just gained one. What is covered here is what a send does to the DELIVERY
ROW: what the Inbox is told when a message is refused, which
worker is allowed to send at all when two are handed the same delivery,
and whether the destination's own toggle is still on by the
time the message actually goes out (which covers the scheduled
digest's send task as well as the per-delivery one) — and, once a send is
resumed rather than started, what the message it puts in the chat CLAIMS about
the delivery it is finishing. The scheduled digest's own send
task is here for the same reason, on the question of what it leaves in the row
when one of its groups fails and a later one does not — and on
which of the members it was handed it is allowed to send at all, which is
tripl-0zpq.37 again, asked from the digest side.

One set of cases at the end is about the row's SETTINGS rather than the row:
which From: addresses the send path will accept, against which ones the two
diagnostics and the settings form accept. Those reach past
``alerts.py`` on purpose — the same question is answered in
``alerts_channels._send_digest_to_destination`` for the weekly plan digest and
in ``EmailSettingsUpdate`` for the form that stores the value — because a
single-file test of the answer is exactly what let the four sites disagree.
"""

import ast
import re
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from tripl.alert_templates import (
    ALERT_MESSAGE_FORMAT_PLAIN,
    ALERT_MESSAGE_FORMAT_SLACK_MRKDWN,
    DIGEST_ALERT_MESSAGE_TEMPLATES,
)
from tripl.config import SMTP_SECURITY_STARTTLS
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_rule import AlertRule
from tripl.models.project import Project
from tripl.schemas.app_settings import EmailSettingsUpdate
from tripl.services import app_settings_service
from tripl.tests.test_alert_digest_delivery import _add_rule, _run_digest, _seed
from tripl.tests.test_alerting import (
    _retry_from_inbox,
    _seed_telegram_length_case,
    _telegram_units,
)
from tripl.worker.tasks import alerts, metrics
from tripl.worker.tasks.alerts import TELEGRAM_DELIVERED_ITEM_IDS_KEY, _claim_delivery
from tripl.worker.tasks.alerts_channels import _send_digest_to_destination
from tripl.worker.tasks.maintenance import STRANDED_DELIVERY_MINUTES

# Telegram's over-4096 rejection in the exact shape ``_post_json`` raises it,
# because that string is what ``_is_telegram_message_too_long_error`` matches on.
_TOO_LONG_ERROR = (
    "HTTP 400 from https://api.telegram.org/bot***/sendMessage: Bad Request: message is too long"
)

_ITEMS_SENT = re.compile(r"(\d+) of (\d+) items had already been sent")

_TEMPLATE = "[tripl] ${matched_count} alerts\n${items_text}"


def test_a_refused_telegram_message_counts_each_delivered_item_once(
    tmp_path,
    monkeypatch,
) -> None:
    """The failed delivery names the items that went out — not twice as many.

    website/docs/use/alerting.md promises a delivery refused for length is
    marked failed "saying how many of its items had already gone out". The send
    loop feeds every landed part to TWO accumulators — ``delivered_items``, and
    ``already_delivered_ids`` which ``_record_delivered_items`` tops up in
    place — so summing their lengths counted this attempt's parts twice. The
    line under test is the numerator of that sentence in
    ``worker/tasks/alerts.py``: reverting it to
    ``len(already_delivered_ids) + len(delivered_items)`` makes ``reported``
    below exactly double ``landed``, and a delivery could announce more items
    delivered than it has (the filed case read "32 of 24").

    The refusal has to arrive AFTER a part has landed or the bug is invisible:
    with nothing delivered yet ``delivered_items`` is empty and both versions
    of the sentence agree. Telegram therefore refuses here on the second call
    rather than on length. The real trigger — one item over 4096 units on its
    own — cannot be seeded through this fixture, because the only oversized
    text it can inject is the rule's message template, which prefixes EVERY
    part and would get the first message refused too.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_too_long.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    delivery_id, scope_names = _seed_telegram_length_case(
        sync_session_factory,
        item_count=14,
        message_template=_TEMPLATE,
    )

    accepted: list[str] = []

    def telegram_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        text = str(body["text"])
        # Telegram's real rule stays in force so a splitter regression still
        # shows up here; the `accepted` clause is what stands in for the one
        # body the splitter cannot shrink.
        if accepted or _telegram_units(text) > 4096:
            raise ValueError(_TOO_LONG_ERROR)
        accepted.append(text)

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        telegram_post_json,
    )

    result = metrics.send_alert_delivery.run(delivery_id)

    assert result["status"] == "failed"
    # Fixture guard, not an assertion about the fix: if the splitter ever stops
    # needing more than one message for 14 items, the run ends "sent" and
    # everything below would be vacuously true.
    assert len(accepted) == 1, "the refusal must land after a part did, or it proves nothing"
    landed = [name for name in scope_names if name in accepted[0]]
    assert landed, "the first message carried no items"

    error_message = str(result["error"])
    match = _ITEMS_SENT.search(error_message)
    assert match is not None, error_message
    reported, total = int(match.group(1)), int(match.group(2))
    assert total == len(scope_names)
    assert reported == len(landed), error_message
    # The shape an operator actually notices: a delivery claiming it sent more
    # items than it has.
    assert reported <= total, error_message
    # ``parts_sent`` is the one number in the sentence that IS per-attempt.
    assert "1 message(s) of them in this attempt" in error_message

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.failed.value
        assert persisted.error_message == error_message
        snapshot = persisted.payload_snapshot
        assert isinstance(snapshot, dict)
        # The count quoted to the operator is the same set the resume reads
        # back, which is the reason it can be quoted on its own.
        assert len(snapshot[TELEGRAM_DELIVERED_ITEM_IDS_KEY]) == reported

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_a_refused_retry_still_counts_what_the_earlier_attempt_sent(
    tmp_path,
    monkeypatch,
) -> None:
    """A resume that is refused immediately still reports the earlier messages.

    The other half of the contract, and the guard against over-correcting the
    double count: the numerator must not shrink to "what this attempt sent"
    either. Here the retry's very first message is refused, so this attempt
    delivered nothing — yet the reader is still holding the part the first
    attempt landed, and the Inbox has to keep saying so. Quoting
    ``len(delivered_items)`` instead of ``len(already_delivered_ids)`` would
    report 0 and invite an operator to shorten a template that had already
    delivered items into the chat.

    This one would stay green against the pre-fix code — it pins the half the
    bug did not break — so it belongs beside, not instead of, the test above.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_too_long_retry.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    delivery_id, scope_names = _seed_telegram_length_case(
        sync_session_factory,
        item_count=14,
        message_template=_TEMPLATE,
    )

    accepted: list[str] = []
    refuse_everything = False

    def telegram_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        text = str(body["text"])
        if refuse_everything or accepted or _telegram_units(text) > 4096:
            raise ValueError(_TOO_LONG_ERROR)
        accepted.append(text)

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        telegram_post_json,
    )

    first = metrics.send_alert_delivery.run(delivery_id)
    assert first["status"] == "failed"
    assert len(accepted) == 1
    landed = [name for name in scope_names if name in accepted[0]]
    assert landed

    # Retry from the Inbox, and let the ceiling refuse the resume outright.
    _retry_from_inbox(sync_session_factory, delivery_id)
    refuse_everything = True

    second = metrics.send_alert_delivery.run(delivery_id)

    assert second["status"] == "failed"
    assert len(accepted) == 1, "the resume must re-send nothing that already landed"
    error_message = str(second["error"])
    match = _ITEMS_SENT.search(error_message)
    assert match is not None, error_message
    reported, total = int(match.group(1)), int(match.group(2))
    assert total == len(scope_names)
    assert reported == len(landed), error_message
    assert "0 message(s) of them in this attempt" in error_message

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_a_second_worker_handed_a_claimed_delivery_sends_nothing(
    tmp_path,
    monkeypatch,
) -> None:
    """Two workers, one delivery: the second must not put it in the chat again.

    ``requeue_stranded_alert_deliveries`` re-enqueues a delivery that has been
    `pending` for fifteen minutes on age alone, and the worker runs prefork with
    no ``--concurrency`` flag — so the backlog that makes a row look stranded is
    exactly the condition under which its first send is still running in another
    process. ``send_alert_delivery`` writes nothing to the row between loading it
    and posting, so before the claim both copies read `pending` and both sent.

    Worker one appears here as its first act and is then held there: the
    committed claim, taken from its own session, which is precisely the state a
    render-and-post still in progress leaves in the database. Worker two is the
    task run below. Delete the ``if not _claim_delivery(...)`` block from
    ``send_alert_delivery`` and this goes red twice over — the run returns
    "sent", and ``posts`` holds the message the reader would have received a
    second time.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_claim.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    delivery_id, _scope_names = _seed_telegram_length_case(
        sync_session_factory,
        item_count=2,
        message_template=_TEMPLATE,
    )

    posts: list[str] = []

    def telegram_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        posts.append(str(body["text"]))

    # Worker one, still rendering and posting.
    with sync_session_factory() as worker_one:
        in_flight = worker_one.get(AlertDelivery, uuid.UUID(delivery_id))
        assert in_flight is not None
        assert _claim_delivery(worker_one, in_flight, now=datetime.now(UTC)) is True
    with sync_session_factory() as session:
        # Read back rather than reused from the claim above, so both sides of
        # the comparison below have been through the same column round-trip.
        held = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert held is not None
        claimed_at = held.claimed_at
        assert claimed_at is not None, "the first worker did not get the claim"

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        telegram_post_json,
    )

    # Worker two: the reaper's redispatch of a row it believes stranded.
    result = metrics.send_alert_delivery.run(delivery_id)

    assert result["status"] == "already_claimed"
    assert posts == [], "the reader was sent the same alert a second time"

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        # The loser leaves the row exactly as it found it: worker one is still
        # sending, so this is neither a failure for the Inbox to show nor a
        # reason to move a lease it does not own.
        assert persisted.status == AlertDeliveryStatus.pending.value
        assert persisted.error_message is None
        assert persisted.claimed_at == claimed_at

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_a_failed_attempt_releases_the_claim_so_a_retry_is_not_swallowed(
    tmp_path,
    monkeypatch,
) -> None:
    """The lease belongs to the attempt, not to the row.

    The Inbox Retry button flips a failed delivery straight back to `pending`
    and re-dispatches this same task, so a claim left behind by the attempt that
    just failed would refuse that send and return a no-op: for the fifteen
    minutes the lease takes to expire, an operator would press Retry and watch
    nothing happen at all. That is a worse outcome than the duplicate the claim
    exists to prevent, and it is the one way this fix could do real harm — so
    drop ``delivery.claimed_at = None`` from the failure handler in
    ``send_alert_delivery`` and the retry below returns "already_claimed" with
    an empty ``posts``.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_claim_release.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    delivery_id, _scope_names = _seed_telegram_length_case(
        sync_session_factory,
        item_count=2,
        message_template=_TEMPLATE,
    )

    posts: list[str] = []
    refuse = True

    def telegram_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        if refuse:
            # Not a ValueError: the MarkdownV2→plain fallback is not what this
            # test is about, so the failure goes straight to the handler that
            # records the row as failed.
            raise RuntimeError("telegram said no")
        posts.append(str(body["text"]))

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        telegram_post_json,
    )

    first = metrics.send_alert_delivery.run(delivery_id)

    assert first["status"] == "failed"
    assert posts == []
    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.failed.value
        assert persisted.claimed_at is None, "a finished attempt kept its lease"

    _retry_from_inbox(sync_session_factory, delivery_id)
    refuse = False

    second = metrics.send_alert_delivery.run(delivery_id)

    assert second["status"] == "sent"
    assert len(posts) == 1, "the retry sent nothing"
    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.sent.value
        # Released on the way out of a successful attempt too, so a row that
        # some later path does hand back to a send task is claimable at once.
        assert persisted.claimed_at is None

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_a_claim_a_dead_worker_left_behind_expires_on_the_reapers_horizon(
    tmp_path,
    monkeypatch,
) -> None:
    """A lease outlives its worker, so it has to expire — on exactly one horizon.

    A worker SIGKILLed between the claim and the terminal status write leaves
    ``claimed_at`` set with no transaction left to roll it back, and nothing
    else would ever clear it. The lease is therefore
    ``maintenance.STRANDED_DELIVERY_MINUTES`` long — the same horizon that
    decides a `pending` row was stranded — so that the reaper's first redispatch
    of such a row is also the first claim that can win.

    The two halves below are that equality, checked from both sides: a claim one
    minute younger than the horizon still refuses a second sender, one a minute
    older lets the redispatch through. Give ``_claim_delivery`` a lease of its
    own instead of the reaper's constant and one half goes red — a shorter lease
    re-opens the race the claim closes, a longer one refuses the reaper's own
    redispatch and burns a dispatch attempt on a no-op.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_claim_lease.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    delivery_id, _scope_names = _seed_telegram_length_case(
        sync_session_factory,
        item_count=2,
        message_template=_TEMPLATE,
    )

    posts: list[str] = []

    def telegram_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        posts.append(str(body["text"]))

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        telegram_post_json,
    )

    def leave_a_claim(age_minutes: float) -> None:
        """What a worker that claimed ``age_minutes`` ago and then died leaves."""
        with sync_session_factory() as session:
            delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
            assert delivery is not None
            delivery.claimed_at = datetime.now(UTC) - timedelta(minutes=age_minutes)
            session.commit()

    leave_a_claim(STRANDED_DELIVERY_MINUTES - 1)
    inside_lease = metrics.send_alert_delivery.run(delivery_id)

    assert inside_lease["status"] == "already_claimed"
    assert posts == [], "a live claim let a second worker send"

    leave_a_claim(STRANDED_DELIVERY_MINUTES + 1)
    past_lease = metrics.send_alert_delivery.run(delivery_id)

    assert past_lease["status"] == "sent"
    assert len(posts) == 1, "an abandoned claim stranded the delivery for good"

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.sent.value
        assert persisted.claimed_at is None

    Base.metadata.drop_all(engine)
    engine.dispose()


def _set_destination_enabled(sync_session_factory, delivery_id: str, *, enabled: bool) -> str:
    """Flip a delivery's destination toggle the way the PATCH endpoint does.

    Returns the destination name, because the whole point of the guard is that
    the operator is told WHICH channel refused the alert.
    """
    with sync_session_factory() as session:
        delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert delivery is not None
        destination = session.get(AlertDestination, delivery.destination_id)
        assert destination is not None
        destination.enabled = enabled
        session.commit()
        return destination.name


def test_a_delivery_is_not_sent_to_a_destination_switched_off_since_it_was_minted(
    tmp_path,
    monkeypatch,
) -> None:
    """The toggle is read when the message goes out, not when it was decided.

    Every path that CREATES a delivery already filters on
    ``AlertDestination.enabled``, so a row exists only because the destination
    was on when the signal fired — and then the row waits. A ``.delay()`` lost
    to a broker restart waits for the reaper, which re-enqueues a `pending`
    delivery on age alone fifteen minutes later; Retry re-dispatches whenever
    the operator presses it. The send task never looked at the toggle, so the
    filed case — minted 09:00, destination disabled 09:05, posted 09:15 —
    reached the chat of a destination the UI shows as off.

    Delete ``_assert_destination_enabled(destination)`` from
    ``send_alert_delivery`` and the first run below comes back "sent" with the
    message sitting in ``posts``: exactly that defect.

    The second half keeps the guard honest in the other direction. The refusal
    is about the toggle's CURRENT value, not a row poisoned for good: switched
    back on and retried, the same delivery ships. Without it a guard that
    failed every retry forever would pass just as well.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_disabled.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    delivery_id, _scope_names = _seed_telegram_length_case(
        sync_session_factory,
        item_count=2,
        message_template=_TEMPLATE,
    )

    posts: list[str] = []

    def telegram_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        posts.append(str(body["text"]))

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        telegram_post_json,
    )

    # The operator switches the destination off while the delivery is still
    # queued — the only way this row can exist at all.
    destination_name = _set_destination_enabled(sync_session_factory, delivery_id, enabled=False)

    result = metrics.send_alert_delivery.run(delivery_id)

    assert result["status"] == "failed"
    assert posts == [], "an alert went to a destination the operator had switched off"
    error_message = str(result["error"])
    assert destination_name in error_message, error_message
    assert "disabled" in error_message, error_message

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        # Failed, not silently dropped: a row left `pending` would be re-queued
        # by the reaper every fifteen minutes and would never tell the operator
        # that an alert they would have received did not arrive.
        assert persisted.status == AlertDeliveryStatus.failed.value
        assert persisted.sent_at is None
        assert persisted.error_message == error_message
        # The claim is taken before the destination is even loaded, so the
        # refusal has to hand it back or the retry below would be a no-op for
        # as long as the lease lasts.
        assert persisted.claimed_at is None

    _set_destination_enabled(sync_session_factory, delivery_id, enabled=True)
    _retry_from_inbox(sync_session_factory, delivery_id)

    second = metrics.send_alert_delivery.run(delivery_id)

    assert second["status"] == "sent"
    assert len(posts) == 1, "a re-enabled destination still refused the delivery"

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_a_digest_member_whose_destination_went_off_fails_without_a_message(
    tmp_path,
    monkeypatch,
) -> None:
    """The scheduled digest had the identical hole, and takes the identical guard.

    ``send_alert_digest``'s members are ordinary `pending` ``AlertDelivery``
    rows. The flush that mints them selects only enabled destinations, but it
    hands the batch to this task over the broker and the toggle can move in the
    meantime — and if the batch is lost, the stranded-delivery reaper re-offers
    each member fifteen minutes later. So "the flush already filtered" is not a
    guard, and before ``_assert_destination_enabled`` reached the prepare loop
    the digest posted to a destination reading "disabled" in the UI.

    Remove that call from ``alert_digest_send`` and the first run below reports
    one message sent and ``posts`` holds the text the disabled Slack channel
    would have received. The re-enabled run afterwards is the fixture's own
    proof that nothing else in this seed was stopping the send.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_disabled_digest.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    with sync_session_factory() as session:
        # Reused rather than rebuilt: this is the Slack destination + rule the
        # digest tests already flush through, and a second hand-rolled copy
        # would stop matching it the moment either model gains a column.
        config, destination, rule, _event_type = _seed(session, cron=None)
        destination_name = destination.name
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=config.project_id,
            scan_config_id=config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="slack",
            status="pending",
            matched_count=1,
            # What the flush stamps, so this member is a digest by the only
            # record of digest-ness the send task consults.
            payload_snapshot={"digest": True},
        )
        session.add(delivery)
        session.add(
            AlertDeliveryItem(
                id=uuid.uuid4(),
                delivery_id=delivery.id,
                scope_type="event",
                scope_ref="event-0",
                scope_name="purchase:success",
                bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
                direction="drop",
                actual_count=10,
                expected_count=20,
                absolute_delta=10,
                percent_delta=50.0,
            )
        )
        # Disabled after the row was minted — the flush would never have
        # selected this destination otherwise.
        destination.enabled = False
        session.commit()
        delivery_id = str(delivery.id)

    result, posts = _run_digest(monkeypatch, sync_session_factory, [delivery_id])

    assert posts == [], "a digest went to a destination the operator had switched off"
    assert result["messages"] == 0
    assert result["sent"] == 0
    # Failed as its own member: the prepare loop collects the failure rather
    # than aborting the batch, so other destinations in the same flush still
    # get their digest.
    assert result["failed"] == 1

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.failed.value
        assert persisted.sent_at is None
        assert destination_name in (persisted.error_message or "")
        assert "disabled" in (persisted.error_message or "")
        assert persisted.claimed_at is None

    _set_destination_enabled(sync_session_factory, delivery_id, enabled=True)
    _retry_from_inbox(sync_session_factory, delivery_id)

    second, posts_after = _run_digest(monkeypatch, sync_session_factory, [delivery_id])

    assert second["sent"] == 1
    assert len(posts_after) == 1, "a re-enabled destination still refused the digest"

    Base.metadata.drop_all(engine)
    engine.dispose()


# A 12-item digest of which a previous attempt already put 8 in the chat. Eight
# makes the remainder's own summary visibly different from the digest's — later
# window, smaller count, different worst mover — and the four that are left are
# few enough that the resume cannot need a second message.
_DIGEST_ITEM_COUNT = 12
_DIGEST_DELIVERED = 8

# ``_seed_telegram_length_case`` stamps ONE instant on every item, which would
# make the whole digest's window and the remainder's identical and the window
# assertion vacuous; the helper below spreads them from here, an hour apart.
_DIGEST_FIRST_BUCKET = datetime(2026, 4, 11, 9, tzinfo=UTC)

# Far enough from the seed's uniform 51.9 that ``_digest_headline`` names this
# item the worst mover, and stamped on an item the reader ALREADY has: the
# resumed message has to name it in the headline while not listing it in the
# body.
_WORST_PERCENT_DELTA = 91.0

# What the model is mocked to write, distinctive enough to find in a body.
_AI_NOTE = "Checkout fell across the whole window; the rest is noise."


def _half_sent_telegram_digest(
    sync_session_factory,
    delivery_id: str,
    *,
    delivered: int,
) -> tuple[list[str], list[str]]:
    """Make the seeded Telegram delivery a digest a first attempt half-sent.

    Three edits, each one something the real pipeline did before this state
    existed: ``payload_snapshot["digest"]`` is what ``metrics/dispatch`` stamps
    at creation and the only record of digest-ness a send consults; the id list
    under ``TELEGRAM_DELIVERED_ITEM_IDS_KEY`` is what ``_record_delivered_items``
    commits after each accepted message, in its shape (sorted ``str(uuid)``);
    and the buckets get spread so the digest has a window with two ends.

    Seeding the resume point instead of failing a real split keeps this off
    Telegram's 4096 arithmetic — what is under test is what the SECOND attempt
    says, and ``test_send_alert_delivery_resumes_a_partly_sent_telegram_split``
    already proves the snapshot round-trip that produces this state.
    ``delivered=0`` is the same digest with nothing sent yet, i.e. a first
    attempt.

    Returns (delivered scope names, still-pending scope names) ordered by
    scope_name — which is seeded order — because ``AlertDelivery.items`` has no
    ORDER BY and a uuid4 primary key makes row order meaningless.
    """
    with sync_session_factory() as session:
        delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert delivery is not None
        items = sorted(delivery.items, key=lambda item: item.scope_name)
        assert 0 <= delivered < len(items)
        for index, item in enumerate(items):
            item.bucket = _DIGEST_FIRST_BUCKET + timedelta(hours=index)
        items[0].percent_delta = _WORST_PERCENT_DELTA
        delivery.payload_snapshot = {
            "digest": True,
            TELEGRAM_DELIVERED_ITEM_IDS_KEY: sorted(str(item.id) for item in items[:delivered]),
        }
        session.commit()
        return (
            [item.scope_name for item in items[:delivered]],
            [item.scope_name for item in items[delivered:]],
        )


def test_a_resumed_telegram_digest_still_summarises_the_whole_digest(
    tmp_path,
    monkeypatch,
) -> None:
    """The retried message describes the digest, not the leftovers it carries.

    website/docs/use/alerting.md: "A digest that needs more than one message is
    still one digest. Every part repeats the same summary and the same window —
    they describe the digest, not the part." A resume is the one caller that
    renders a SUBSET of a delivery, and it used to pass that subset as the only
    items the renderer could see, so ``_build_template_context`` fell back to
    ``summarised_items = rendered_items`` and the header re-derived itself over
    the leftovers: "4 alerts" arriving under two earlier messages that said
    "12", a window opening hours after theirs, and a worst mover that is merely
    the worst of what was left.

    The line under test is the ``summary_items=_digest_summary_items(...)``
    argument on the ``_render_delivery_message`` call in
    ``send_alert_delivery``. Delete it and the three header assertions fail
    together. The body assertions hold either way and are here on purpose: a
    "fix" that summarised the whole digest by re-sending the whole digest would
    be no fix, and this is where that would show.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_digest_resume.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    delivery_id, scope_names = _seed_telegram_length_case(
        sync_session_factory,
        item_count=_DIGEST_ITEM_COUNT,
        # The product's own digest layout rather than a lookalike: ${headline}
        # and ${window_label} are the two variables this issue is about, and a
        # hand-written copy would stop matching the day the default moves.
        message_template=DIGEST_ALERT_MESSAGE_TEMPLATES[ALERT_MESSAGE_FORMAT_PLAIN],
    )
    delivered_names, pending_names = _half_sent_telegram_digest(
        sync_session_factory,
        delivery_id,
        delivered=_DIGEST_DELIVERED,
    )

    posts: list[str] = []

    def telegram_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        text = str(body["text"])
        if _telegram_units(text) > 4096:
            raise ValueError(_TOO_LONG_ERROR)
        posts.append(text)

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        telegram_post_json,
    )

    result = metrics.send_alert_delivery.run(delivery_id)

    assert result["status"] == "sent"
    # Fixture guard, not the contract: four items have to fit in ONE message,
    # or the header being read below comes from ``split_telegram_messages``,
    # whose own summary set is the half of this fix that lives in
    # ``alerts_messages.py``.
    assert len(posts) == 1, [_telegram_units(text) for text in posts]
    headline, window_label, *body_lines = posts[0].splitlines()
    body = "\n".join(body_lines)

    # The header counts the whole digest...
    assert f"{len(scope_names)} alerts" in headline, headline
    assert f"{len(pending_names)} alerts" not in headline, headline
    # ...names the digest's worst mover, which is an item the reader already
    # has and this message therefore does not list...
    assert f"worst {delivered_names[0]} down {_WORST_PERCENT_DELTA:.0f}%" in headline, headline
    # ...and states the window every item falls in. Derived from the same two
    # constants rather than written out, so what this pins is the SPAN — the
    # contract — and not ``_digest_window_label``'s wording or the runner's
    # locale.
    last_bucket = _DIGEST_FIRST_BUCKET + timedelta(hours=_DIGEST_ITEM_COUNT - 1)
    first_pending_bucket = _DIGEST_FIRST_BUCKET + timedelta(hours=_DIGEST_DELIVERED)
    assert window_label == f"{_DIGEST_FIRST_BUCKET:%b %d, %H:%M}–{last_bucket:%H:%M} UTC", (
        window_label
    )
    # The summarised-over-the-remainder window would open here instead.
    assert not window_label.startswith(f"{first_pending_bucket:%b %d, %H:%M}"), window_label

    # ...while the body is still only what the reader has NOT seen.
    assert [name for name in pending_names if name not in body] == []
    assert [name for name in delivered_names if name in body] == []

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.sent.value
        snapshot = persisted.payload_snapshot
        assert isinstance(snapshot, dict)
        assert len(snapshot[TELEGRAM_DELIVERED_ITEM_IDS_KEY]) == len(scope_names)

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_a_resumed_telegram_digest_does_not_regenerate_its_ai_note(
    tmp_path,
    monkeypatch,
) -> None:
    """The note rode out on the first message, so the resume neither asks nor repeats.

    ``send_alert_delivery`` says exactly this about the appended note on the
    immediate path — "the note rides on the first message only and that message
    is already with the reader" — but the digest arm twenty lines above it was
    missing the ``not is_telegram_resume`` clause its neighbour applies. A
    resumed digest therefore paid a fresh LLM round-trip and printed a SECOND
    note, computed over all twelve items, above a body listing four.

    The first delivery is the fixture's own proof that nothing else here
    suppresses the note: same rule settings, same seed, no resume point, and
    the model both gets asked and reaches the chat. Restore the arm to
    ``if is_digest and rule.ai_explanation_enabled and not is_demo_project:``
    and the second delivery's two assertions both fail.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_digest_resume_ai.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    posts: list[str] = []
    asked: list[str] = []

    def telegram_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        text = str(body["text"])
        if _telegram_units(text) > 4096:
            raise ValueError(_TOO_LONG_ERROR)
        posts.append(text)

    def build_ai_explanation(delivery: AlertDelivery, **kwargs: object) -> str:
        asked.append(str(delivery.id))
        return _AI_NOTE

    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_post_json",
        telegram_post_json,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_build_ai_explanation",
        build_ai_explanation,
    )

    fresh_id, _fresh_names = _seed_telegram_length_case(
        sync_session_factory,
        item_count=_DIGEST_ITEM_COUNT,
        message_template=DIGEST_ALERT_MESSAGE_TEMPLATES[ALERT_MESSAGE_FORMAT_PLAIN],
        ai_explanation_enabled=True,
    )
    _half_sent_telegram_digest(sync_session_factory, fresh_id, delivered=0)

    first = metrics.send_alert_delivery.run(fresh_id)

    assert first["status"] == "sent"
    assert asked == [fresh_id], "the fixture never reached the digest's AI arm"
    assert _AI_NOTE in "\n".join(posts), "the note never reached the chat on a first attempt"

    posts.clear()
    resumed_id, _resumed_names = _seed_telegram_length_case(
        sync_session_factory,
        # Second seeding in the same database: the fixture's project slug and
        # data-source name are unique repo-wide, so this one needs its own.
        suffix="-resume",
        item_count=_DIGEST_ITEM_COUNT,
        message_template=DIGEST_ALERT_MESSAGE_TEMPLATES[ALERT_MESSAGE_FORMAT_PLAIN],
        ai_explanation_enabled=True,
    )
    _half_sent_telegram_digest(
        sync_session_factory,
        resumed_id,
        delivered=_DIGEST_DELIVERED,
    )

    second = metrics.send_alert_delivery.run(resumed_id)

    assert second["status"] == "sent"
    assert asked == [fresh_id], "a resume re-asked the model for a note the reader already has"
    assert _AI_NOTE not in "\n".join(posts), "the resume printed the note a second time"

    Base.metadata.drop_all(engine)
    engine.dispose()


# ── a failed digest group must not erase a later group's body ──


def _digest_member(
    session,
    *,
    config,
    destination,
    rule,
    scope_name: str,
    created_at: datetime,
) -> AlertDelivery:
    """One `pending` digest member of ``destination``, carrying one item.

    The shape the flush mints: an ordinary delivery row whose only record of
    digest-ness is ``payload_snapshot["digest"]`` — the key ``send_alert_digest``
    actually reads. ``created_at`` is passed in rather than defaulted because
    the task orders its batch by it, and the defect under test only exists in
    one of the two orders.
    """
    delivery = AlertDelivery(
        id=uuid.uuid4(),
        project_id=config.project_id,
        scan_config_id=config.id,
        destination_id=destination.id,
        rule_id=rule.id,
        channel="slack",
        status="pending",
        matched_count=1,
        created_at=created_at,
        payload_snapshot={"digest": True},
    )
    session.add(delivery)
    session.add(
        AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=delivery.id,
            scope_type="event",
            scope_ref=f"event-{scope_name}",
            scope_name=scope_name,
            bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
            direction="drop",
            actual_count=15403,
            expected_count=32048,
            absolute_delta=16645,
            percent_delta=51.9,
            details_path=f"https://tripl.example.com/p/digest/monitoring/event/{uuid.uuid4()}",
        )
    )
    return delivery


def _run_digest_refusing_one_format(
    monkeypatch,
    sync_session_factory,
    delivery_ids: list[str],
    *,
    refused_format: str,
) -> tuple[dict[str, object], list[tuple[str, str]], list[str]]:
    """The combined send with exactly ONE group's outbound call refused.

    ``_run_digest``'s own ``fail_with`` raises for every Slack call, so it can
    only produce "the whole batch failed" — and the whole batch failing is the
    one arrangement in which this defect is invisible. What it needs is a group
    that fails while a LATER group succeeds, so the refusal is selective.

    Returns the formats attempted as well as the ones that landed, because the
    order of the two groups is the precondition of the test and has to be
    asserted rather than assumed.
    """
    from tripl.worker.tasks import alert_digest_send as digest_module
    from tripl.worker.tasks import alerts as alerts_module

    attempted: list[str] = []
    posts: list[tuple[str, str]] = []

    def fake_slack(webhook_url: str, text: str, *, message_format: str) -> None:
        attempted.append(message_format)
        if message_format == refused_format:
            raise RuntimeError("slack said no")
        posts.append((text, message_format))

    monkeypatch.setattr(digest_module, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(alerts_module, "_send_slack_message", fake_slack)
    monkeypatch.setattr(
        alerts_module, "_resolve_slack_webhook", lambda destination: "https://hooks.slack.com/x"
    )
    return digest_module.send_alert_digest.run(delivery_ids), posts, attempted


def test_a_failed_digest_group_does_not_erase_the_next_groups_rendered_body(
    tmp_path,
    monkeypatch,
) -> None:
    """The member that DID get its message keeps the body it sent.

    ``send_alert_digest`` groups its batch by (destination, message format) —
    not by destination alone, because the format comes from the RULE, so two
    rules on one Slack destination that disagree (`plain` vs `slack_mrkdwn`)
    put two groups in one task run. That is the shape seeded here.

    The prepare loop renders every member of every group and writes the body to
    ``payload_snapshot["rendered_message"]``, and nothing committed those
    writes. The send loop's failure arm then opens with ``session.rollback()``,
    which is not scoped to the group that failed: it threw away the rendered
    snapshot of the group that had not sent yet and expired its instance, so
    when that group's message went out fine the task committed `sent` over the
    pre-render snapshot it re-read from the database.

    Delete the ``session.commit()`` that now sits between the prepare loop and
    the send loop in ``alert_digest_send.py`` and the two assertions on
    ``snapshot["rendered_message"]`` / ``["message_format"]`` below go red with
    ``None``: the Inbox (AlertDeliveryRow reads exactly those keys) shows an
    empty body for a message the reader received, and ``_recent_alert_history``
    tells the model nothing was said about these scopes this morning.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_digest_rollback.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    with sync_session_factory() as session:
        # Reused rather than rebuilt, like the disabled-destination case above:
        # this is the Slack destination the digest tests already flush through.
        config, destination, plain_rule, _event_type = _seed(session, cron=None)
        mrkdwn_rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Second monitor",
            enabled=True,
            include_project_total=False,
            include_event_types=True,
            include_events=False,
            notify_on_spike=True,
            notify_on_drop=True,
            min_percent_delta=0,
            min_absolute_delta=0,
            min_expected_count=0,
            cooldown_minutes=1440,
            # The only thing that makes one task run hold two groups for one
            # destination. Both formats are legal on Slack
            # (ALERT_MESSAGE_FORMATS_BY_DESTINATION), so this is a setting an
            # operator can reach, not a contrivance.
            message_format=ALERT_MESSAGE_FORMAT_SLACK_MRKDWN,
        )
        session.add(mrkdwn_rule)
        session.commit()

        minted_at = datetime.now(UTC) - timedelta(minutes=5)
        refused = _digest_member(
            session,
            config=config,
            destination=destination,
            rule=plain_rule,
            scope_name="checkout:submit",
            created_at=minted_at,
        )
        survivor = _digest_member(
            session,
            config=config,
            destination=destination,
            rule=mrkdwn_rule,
            scope_name="signup:complete",
            # Later, so the batch's `order_by(created_at, id)` puts the group
            # that fails FIRST — the only order in which a rollback can reach a
            # group whose message has not gone out yet.
            created_at=minted_at + timedelta(minutes=1),
        )
        session.commit()
        refused_id = str(refused.id)
        survivor_id = str(survivor.id)

    result, posts, attempted = _run_digest_refusing_one_format(
        monkeypatch,
        sync_session_factory,
        [refused_id, survivor_id],
        refused_format=ALERT_MESSAGE_FORMAT_PLAIN,
    )

    # Fixture guard, not an assertion about the fix: two groups, the failing one
    # attempted FIRST. In the other order the survivor has already committed its
    # own snapshot before the rollback happens and the defect is invisible.
    assert attempted == [
        ALERT_MESSAGE_FORMAT_PLAIN,
        ALERT_MESSAGE_FORMAT_SLACK_MRKDWN,
    ], "the seed stopped producing two groups in the order this test needs"
    assert result["messages"] == 1, "one group failed, the other sent"
    assert result["sent"] == 1
    assert result["failed"] == 1
    assert len(posts) == 1
    delivered_body, delivered_format = posts[0]
    assert delivered_format == ALERT_MESSAGE_FORMAT_SLACK_MRKDWN
    assert "signup:complete" in delivered_body, "the surviving group sent the wrong rule's section"

    with sync_session_factory() as session:
        sent = session.get(AlertDelivery, uuid.UUID(survivor_id))
        assert sent is not None
        assert sent.status == AlertDeliveryStatus.sent.value
        snapshot = sent.payload_snapshot or {}
        # The regression. Before the pre-send commit this was None, on a row
        # whose message the reader is holding.
        assert snapshot.get("rendered_message") == delivered_body
        assert snapshot.get("message_format") == ALERT_MESSAGE_FORMAT_SLACK_MRKDWN
        # ...and the key the dispatcher stamped is still there underneath it.
        assert snapshot.get("digest") is True

        failed = session.get(AlertDelivery, uuid.UUID(refused_id))
        assert failed is not None
        assert failed.status == AlertDeliveryStatus.failed.value
        assert "slack said no" in (failed.error_message or "")
        assert failed.claimed_at is None, "a lease left behind makes Inbox Retry a no-op"
        # The same commit keeps the failed member's own body, which is what
        # Retry re-sends and what the Inbox shows for a delivery nobody got.
        failed_snapshot = failed.payload_snapshot or {}
        assert "checkout:submit" in str(failed_snapshot.get("rendered_message") or "")

    Base.metadata.drop_all(engine)
    engine.dispose()


# ── the digest's own single-flight claim (from the digest side) ──

# What the reaper's failed arm, or an earlier attempt at this member on its own,
# left in the row. Asserted back byte-for-byte at the end: the one thing worse
# than not re-sending this member is re-sending it and erasing why it failed.
_EARLIER_FAILURE = "slack said no at 09:00"


def test_a_digest_member_another_worker_owns_is_dropped_from_the_batch(
    tmp_path,
    monkeypatch,
) -> None:
    """The digest sends the members it could claim, and only those.

    ``send_alert_digest``'s members are ordinary `pending` ``AlertDelivery``
    rows, so ``requeue_stranded_alert_deliveries`` re-offers any one of them to
    ``send_alert_delivery`` on age alone, and ``task_acks_late`` can re-queue
    the digest batch itself. Either way two workers end up holding one member:
    one posting it by itself, one about to fold it into a digest. The claim is
    what makes the second DROP it rather than fail it — the other worker owns
    that member's outcome, and what is left is still exactly one message per
    (destination, format).

    Three members, one Slack destination, three rules that all take the
    destination's default format — therefore one group, and one body in which
    the two dropped sections would be visible if they were sent. ``held`` is
    claimed from a session of its own, which is precisely the state a
    render-and-post still running in another process leaves in the database.
    ``already_failed`` is the OTHER row the claim keeps out: its
    compare-and-set requires `pending`, while the ``pending`` list above only
    filters out `sent`.

    Replace the ``claimed = [...]`` comprehension in ``alert_digest_send`` with
    ``claimed = pending`` — and drop the ``now =`` assignment it strands, or
    ruff fails the mutant before the tests do — and this goes red four ways
    over: the body carries all three sections instead of one, ``sent`` is 3,
    ``already_failed`` comes back `sent` with
    ``_EARLIER_FAILURE`` erased, and the second run reports "already_sent"
    rather than "already_claimed" because the batch consumed a member it never
    owned — which is the duplicate section in the reader's channel that the
    claim was added for.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_digest_claim.db'}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    with sync_session_factory() as session:
        # Reused rather than rebuilt, like the two digest cases above: this is
        # the Slack destination the digest tests already flush through.
        config, destination, held_rule, _event_type = _seed(session, cron=None)
        survivor_rule = _add_rule(session, destination, "Second monitor")
        failed_rule = _add_rule(session, destination, "Third monitor")

        minted_at = datetime.now(UTC) - timedelta(minutes=5)
        held = _digest_member(
            session,
            config=config,
            destination=destination,
            rule=held_rule,
            scope_name="checkout:submit",
            created_at=minted_at,
        )
        survivor = _digest_member(
            session,
            config=config,
            destination=destination,
            rule=survivor_rule,
            scope_name="signup:complete",
            created_at=minted_at + timedelta(minutes=1),
        )
        already_failed = _digest_member(
            session,
            config=config,
            destination=destination,
            rule=failed_rule,
            scope_name="search:query",
            created_at=minted_at + timedelta(minutes=2),
        )
        # Not sendable until the reaper's failed arm or the Inbox Retry button
        # flips it back, and neither has run: this row is in the batch only
        # because the flush put its id there.
        already_failed.status = AlertDeliveryStatus.failed.value
        already_failed.error_message = _EARLIER_FAILURE
        session.commit()
        held_id = str(held.id)
        survivor_id = str(survivor.id)
        already_failed_id = str(already_failed.id)

    # Worker one, still rendering and posting `held` on its own.
    with sync_session_factory() as worker_one:
        in_flight = worker_one.get(AlertDelivery, uuid.UUID(held_id))
        assert in_flight is not None
        assert _claim_delivery(worker_one, in_flight, now=datetime.now(UTC)) is True
    with sync_session_factory() as session:
        # Read back rather than reused from the claim above, so both sides of
        # the comparison at the end went through the same column round-trip.
        owned = session.get(AlertDelivery, uuid.UUID(held_id))
        assert owned is not None
        claimed_at = owned.claimed_at
        assert claimed_at is not None, "the first worker did not get the claim"

    batch = [held_id, survivor_id, already_failed_id]
    result, posts = _run_digest(monkeypatch, sync_session_factory, batch)

    assert result["status"] == "sent"
    assert result["messages"] == 1
    assert result["sent"] == 1
    # Dropped, not failed: worker one owns `held`'s outcome, and
    # `already_failed` already has a recorded one.
    assert result["failed"] == 0

    # Fixture guard as much as assertion: all three rules take the
    # destination's default format, so one group and one body is the ONLY
    # arrangement in which a section that should not have been sent is visible
    # in the text below rather than hidden in a second post.
    assert len(posts) == 1
    body, _message_format = posts[0]
    assert "signup:complete" in body, "the one claimable member did not reach the chat"
    assert "checkout:submit" not in body, "the digest posted a section another worker is sending"
    assert "search:query" not in body, "the digest re-sent a member already recorded as failed"

    with sync_session_factory() as session:
        still_held = session.get(AlertDelivery, uuid.UUID(held_id))
        assert still_held is not None
        # The loser leaves the row exactly as it found it — the same contract
        # the per-delivery task's loser keeps above: worker one is still
        # sending, so this is neither a failure for the Inbox to show nor a
        # lease for this task to move.
        assert still_held.status == AlertDeliveryStatus.pending.value
        assert still_held.error_message is None
        assert still_held.sent_at is None
        assert still_held.claimed_at == claimed_at

        sent = session.get(AlertDelivery, uuid.UUID(survivor_id))
        assert sent is not None
        assert sent.status == AlertDeliveryStatus.sent.value
        assert sent.claimed_at is None, "a finished attempt kept its lease"

        untouched = session.get(AlertDelivery, uuid.UUID(already_failed_id))
        assert untouched is not None
        assert untouched.status == AlertDeliveryStatus.failed.value
        assert untouched.sent_at is None
        assert untouched.error_message == _EARLIER_FAILURE, "the digest overwrote a recorded cause"

    # The same batch offered a second time — the shape the broker's redelivery
    # of an acks-late task produces. Nothing in it is claimable now: `held` is
    # still worker one's, `already_failed` is still failed, and the survivor is
    # `sent`. The early `already_sent` return cannot answer this, because
    # `held` is genuinely still `pending`.
    second, posts_after = _run_digest(monkeypatch, sync_session_factory, batch)

    assert second["status"] == "already_claimed"
    assert second["messages"] == 0
    assert second["sent"] == 0
    assert posts_after == [], "the reader was sent the same section a second time"

    Base.metadata.drop_all(engine)
    engine.dispose()


# What an operator types into Settings -> Email -> Default From when they want
# the alert to arrive from a name rather than a bare mailbox. ``EmailMessage``
# takes it; ``validate_email_address`` does not ("A display name and angle
# brackets around the email address are not permitted here").
_DISPLAY_NAME_FROM = "Tripl Alerts <no-reply@example.com>"


def _email_config(from_address: str) -> app_settings_service.EmailConfig:
    """The SMTP settings of an instance whose global Default From is ``from_address``."""
    return app_settings_service.EmailConfig(
        smtp_host="relay.example.com",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        smtp_security=SMTP_SECURITY_STARTTLS,
        smtp_from_address=from_address,
    )


def _retarget_at_email_destination(sync_session_factory, delivery_id: str) -> None:
    """Point the seeded delivery at an email destination with NO From: override.

    ``_seed_telegram_length_case`` is the seeding this file shares, and the
    channel is incidental to the question here: the From: address is resolved
    from the destination row plus the global settings, and nothing on the email
    arm reads a Telegram column. Flipping the type in place keeps one fixture
    instead of a second hundred-line copy that stops matching the first the next
    time the model gains a column — which is the rule this file's header states.

    ``email_from_address`` is left NULL deliberately: that is the only way the
    GLOBAL Default From is the value that ships, which is what this case is
    about. A destination override can carry a display name too (tripl-v422
    closed the save-time asymmetry), so the NULL here selects the global path
    rather than the only path a display name can reach.
    """
    with sync_session_factory() as session:
        delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert delivery is not None
        destination = session.get(AlertDestination, delivery.destination_id)
        assert destination is not None
        destination.type = AlertDestinationType.email.value
        destination.email_recipients = "ops@example.com"
        destination.email_from_address = None
        destination.bot_token_encrypted = None
        destination.chat_id = None
        delivery.channel = AlertDestinationType.email.value
        session.commit()


def _email_send_harness(
    tmp_path,
    monkeypatch,
    *,
    db_name: str,
    default_from: str,
):
    """One pending email delivery with the send task wired to it and no socket.

    Returns the engine, the session factory, the delivery id, and the list that
    collects every kwarg set ``_send_email_message`` is called with — empty is
    the assertion that nothing was handed to SMTP at all.
    """
    engine = create_engine(f"sqlite:///{tmp_path / db_name}")
    Base.metadata.create_all(engine)
    sync_session_factory = sessionmaker(engine, expire_on_commit=False)

    delivery_id, _scope_names = _seed_telegram_length_case(
        sync_session_factory,
        item_count=2,
        message_template=_TEMPLATE,
    )
    _retarget_at_email_destination(sync_session_factory, delivery_id)

    sent: list[dict[str, object]] = []
    # Patched on the service module, which is what ``_resolve_email_context``
    # reaches through — the settings themselves live in Postgres, which unit
    # tests do not have.
    monkeypatch.setattr(
        app_settings_service,
        "get_email_config_sync",
        lambda *_args, **_kwargs: _email_config(default_from),
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        metrics.send_alert_delivery.run.__globals__,
        "_send_email_message",
        lambda **kwargs: sent.append(kwargs),
    )
    return engine, sync_session_factory, delivery_id, sent


def test_a_display_name_default_from_delivers_the_alert_instead_of_failing_it(
    tmp_path,
    monkeypatch,
) -> None:
    """The send path accepts the sender both diagnostics told the operator was fine.

    Settings -> Send test email and the destination's own Test both check the
    Default From with ``validate_sender_address``, which parses the display name
    off and validates only the address. The two real send paths used the strict
    ``validate_email_address``, which refuses a display name outright. So
    ``Tripl Alerts <no-reply@example.com>`` passed every check the UI offers and
    then failed EVERY alert to every email destination without an override
     — a diagnostic more permissive than delivery, which is the
    inverse of the fault the earlier From-validation work fixed.

    The line under test is the single call inside
    ``alerts._resolve_email_context``. Put ``validate_email_address`` back and
    this returns "failed" with "Email destination From: address is invalid",
    ``sent`` stays empty, and the row is left for the operator to puzzle over.

    ``from_address`` is asserted byte-for-byte rather than just "did not raise":
    the helper returns the ORIGINAL string, so a future normalising version that
    dropped the display name would still deliver — to a reader who no longer
    sees who it is from.
    """
    engine, sync_session_factory, delivery_id, sent = _email_send_harness(
        tmp_path,
        monkeypatch,
        db_name="batch4_sender_display_name.db",
        default_from=_DISPLAY_NAME_FROM,
    )

    result = metrics.send_alert_delivery.run(delivery_id)

    assert result["status"] == "sent", result
    assert len(sent) == 1, "the alert never reached the transport"
    assert sent[0]["from_address"] == _DISPLAY_NAME_FROM
    assert sent[0]["recipients"] == ["ops@example.com"]

    # The premise the whole fix rests on, stated where it is used rather than
    # taken on trust: the header this value is on its way to accepts it. If this
    # ever raises, the strict validator was right and the fix is wrong.
    header = EmailMessage()
    header["From"] = str(sent[0]["from_address"])
    assert header["From"] == _DISPLAY_NAME_FROM

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.sent.value
        assert persisted.error_message is None
        assert persisted.claimed_at is None

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_a_default_from_with_no_at_sign_still_fails_before_any_smtp_call(
    tmp_path,
    monkeypatch,
) -> None:
    """The check is loosened, not deleted — the other way to make the test above pass.

    Removing the validation entirely would satisfy the display-name case just as
    well and is the cheaper-looking fix, so this pins what must still be caught:
    ``validate_sender_address`` refuses a value with no @-sign, which is the one
    thing a bare string costs. It serialises into the header happily and comes
    back from the relay as an error naming nothing, hours later, on a delivery
    nobody is watching.

    Drop the call from ``_resolve_email_context`` and this goes red twice over:
    the task returns "sent" and ``sent`` holds one message addressed from
    ``not-an-address``. The refusal is also asserted to happen with the
    transport untouched — a check that runs after the SMTP call would leave the
    operator a failed delivery AND a half-open conversation with the relay.
    """
    engine, sync_session_factory, delivery_id, sent = _email_send_harness(
        tmp_path,
        monkeypatch,
        db_name="batch4_sender_no_at_sign.db",
        default_from="not-an-address",
    )

    result = metrics.send_alert_delivery.run(delivery_id)

    assert result["status"] == "failed", result
    assert sent == [], "a From: address with no @-sign was handed to SMTP"
    error_message = str(result["error"])
    # The operator-facing sentence has to keep naming both places the value can
    # come from, because the destination override is empty in exactly this case.
    assert "From: address is invalid" in error_message, error_message
    assert "SMTP_FROM_ADDRESS" in error_message, error_message

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.failed.value
        assert "From: address is invalid" in (persisted.error_message or "")
        assert persisted.claimed_at is None, "a lease left behind makes Inbox Retry a no-op"

    Base.metadata.drop_all(engine)
    engine.dispose()


def _digest_email_destination() -> AlertDestination:
    """An email destination for the weekly digest, with no From: override."""
    return AlertDestination(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        type=AlertDestinationType.email.value,
        name="Ops Email",
        enabled=True,
        email_recipients="ops@example.com",
        email_from_address=None,
    )


def test_the_weekly_digest_sends_from_a_display_name_default_from() -> None:
    """The second real send path, and the one where the failure was invisible.

    ``alerts_channels._send_digest_to_destination`` carried its own copy of the
    strict check, and it serves the weekly plan digest and the sunset alert.
    Both callers wrap it in ``except Exception`` -> ``logger.warning``
    (alerts_digest.py), so a display-name Default From did not surface as a
    failed delivery anywhere in the product: the digests simply stopped
    arriving, every week, and said so only in the worker log.

    Restore ``validate_email_address`` on that line and the first half raises
    "A display name and angle brackets ... are not permitted here" with
    ``sent`` empty. Delete the line instead and the second half goes red.

    Called directly rather than through a task because the destination and the
    settings are the whole input — both callers do nothing to either but read
    them out of a SELECT this test does not need.
    """
    sent: list[dict[str, object]] = []
    project = Project(id=uuid.uuid4(), name="Checkout", slug="checkout", description="")

    _send_digest_to_destination(
        destination=_digest_email_destination(),
        message="Weekly tripl digest for Checkout",
        project=project,
        email_config=_email_config(_DISPLAY_NAME_FROM),
        send_slack_message=lambda *_args, **_kwargs: None,
        send_email_message=lambda **kwargs: sent.append(kwargs),
    )

    assert len(sent) == 1, "the weekly digest never reached the transport"
    assert sent[0]["from_address"] == _DISPLAY_NAME_FROM
    assert sent[0]["subject"] == "[Checkout] Weekly tripl digest"

    # ...and the same value with the @-sign taken away is still refused, before
    # anything is handed to SMTP.
    refused: list[dict[str, object]] = []
    with pytest.raises(ValueError, match="@-sign"):
        _send_digest_to_destination(
            destination=_digest_email_destination(),
            message="Weekly tripl digest for Checkout",
            project=project,
            email_config=_email_config("not-an-address"),
            send_slack_message=lambda *_args, **_kwargs: None,
            send_email_message=lambda **kwargs: refused.append(kwargs),
        )

    assert refused == [], "a From: address with no @-sign was handed to SMTP"


def test_the_settings_form_accepts_exactly_the_senders_the_send_path_accepts() -> None:
    """The front door, which until now stored whatever arrived.

    ``update_service_overrides`` writes ``smtp_from_address`` unvalidated, so a
    typo was reported the next morning by a failed alert rather than by the form
    that took it. ``EmailSettingsUpdate`` now checks it with the SEND PATH's own
    helper, which is what makes "accepted here means deliverable there" a
    property rather than a coincidence.

    Each case pins one way the validator can be wrong. Reach for ``EmailStr`` or
    ``validate_email_address`` instead and the display-name case goes red —
    that is tripl-0zpq.29 rebuilt at the other end of the same pipe, a value the
    operator can never save rather than one they can save but never deliver.
    Delete the validator and the bare string is accepted again. Forget that
    ``None`` and ``""`` are how the setting is CLEARED (a None is dropped from
    the override map, an empty string is stored) and the operator can no longer
    unset a Default From at all — a state the product supports and Settings ->
    Send test email reports on rather than refuses.
    """
    assert (
        EmailSettingsUpdate(smtp_from_address=_DISPLAY_NAME_FROM).smtp_from_address
        == _DISPLAY_NAME_FROM
    )
    assert (
        EmailSettingsUpdate(smtp_from_address="no-reply@example.com").smtp_from_address
        == "no-reply@example.com"
    )

    with pytest.raises(ValidationError, match="@-sign"):
        EmailSettingsUpdate(smtp_from_address="not-an-address")

    assert EmailSettingsUpdate(smtp_from_address=None).smtp_from_address is None
    assert EmailSettingsUpdate(smtp_from_address="").smtp_from_address == ""
    # An update that does not mention the field must stay a partial update: the
    # endpoint flattens with ``exclude_unset``, so a validator that materialised
    # a value here would rewrite a setting nobody touched.
    assert "smtp_from_address" not in EmailSettingsUpdate(smtp_host="relay.example.com").model_dump(
        exclude_unset=True
    )


def _session_commits_in(node: ast.AST, *, via: frozenset[str]) -> list[tuple[str, int]]:
    """Every point inside ``node`` where the delivery's own session is committed.

    One level of indirection is resolved, because not every commit is written
    inline: a call to a module-level function of ``alerts.py`` whose own body
    commits counts as a commit here. That is how Telegram banks each accepted
    message (``_record_delivered_items``), and a scan that only looked for
    ``session.commit()`` would report that branch as writing nothing.
    """
    hits: list[tuple[str, int]] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "commit"
            and isinstance(func.value, ast.Name)
            and func.value.id == "session"
        ):
            hits.append(("session.commit()", sub.lineno))
        elif isinstance(func, ast.Name) and func.id in via:
            hits.append((f"{func.id}()", sub.lineno))
    return hits


def _commits_per_destination_branch() -> dict[str, list[tuple[str, int]]]:
    """What each ``destination.type ==`` branch of ``send_alert_delivery`` commits itself.

    Read off the source rather than by running a send per channel, because the
    question is about every channel at once, including the two that need live
    Jira and Linear credentials to reach their commit at all. Only each
    branch's own body is scanned: the terminal ``status=sent`` commit sits
    after the if/elif chain and belongs to no branch.
    """
    module = ast.parse(Path(alerts.__file__).read_text(encoding="utf-8"))
    committing_helpers = frozenset(
        node.name
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name != "send_alert_delivery"
        and _session_commits_in(node, via=frozenset())
    )
    send = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "send_alert_delivery"
    )
    branches: dict[str, list[tuple[str, int]]] = {}
    for node in ast.walk(send):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        left = node.test.left
        comparators = node.test.comparators
        if (
            isinstance(left, ast.Attribute)
            and left.attr == "type"
            and isinstance(left.value, ast.Name)
            and left.value.id == "destination"
            and len(comparators) == 1
            and isinstance(comparators[0], ast.Attribute)
            and isinstance(comparators[0].value, ast.Name)
            and comparators[0].value.id == "AlertDestinationType"
        ):
            branches[comparators[0].attr] = [
                hit
                for statement in node.body
                for hit in _session_commits_in(statement, via=committing_helpers)
            ]
    return branches


# The word a sentence about the send path uses for each branch: prose about
# what the Jira and Linear branches do says "ticket", not "jira/linear".
_CHANNEL_PROSE_WORDS: dict[str, tuple[str, ...]] = {
    "slack": ("slack",),
    "telegram": ("telegram",),
    "webhook": ("webhook",),
    "email": ("email",),
    "jira": ("jira", "ticket"),
    "linear": ("linear", "ticket"),
    "demo_sink": ("demo_sink", "demo sink"),
}


def test_the_claim_names_only_the_channels_whose_first_commit_is_status_sent() -> None:
    """The argument for the claim is a per-channel fact, and it has to be the true one.

    :func:`_claim_delivery` exists because nothing in the row itself keeps a
    second worker out, and the docstring makes that case by naming the channels
    whose first commit is the terminal ``status=sent`` one. As shipped it named
    the ticket paths among them, and those are the two that commit BEFORE it —
    one ``payload_snapshot`` commit each, immediately after the create call
    returns, holding the external issue id the re-run guard reads. Someone
    auditing duplicate-ticket risk on the strength of that sentence concludes
    the ticket paths have no crash-window protection, or deletes the commit
    that provides it as redundant.

    So the prose is checked against the branches, in both directions. Put
    "ticket" back in the clause ending "and the first commit is the
    ``status=sent`` one" and the first loop goes red on the Jira and Linear
    commits it would then be denying. Delete either of those commits — the move
    the false sentence invites — and the second block goes red instead, because
    the paragraph would then promise a commit that is not there.
    """
    doc = " ".join((_claim_delivery.__doc__ or "").split())
    head, marker, _rest = doc.partition("and the first commit is the ``status=sent`` one")
    assert marker, (
        "the sentence this guard reads has been rewritten — re-point it at the clause "
        "naming the channels that commit nothing before the terminal write"
    )
    # The aside between the em dashes, which is where the channels are named.
    clause = head.rsplit("—", 1)[-1].lower()

    branches = _commits_per_destination_branch()
    assert {"slack", "telegram", "webhook", "jira", "linear"} <= set(branches), (
        f"the branch scan resolved {sorted(branches)}; this guard guards nothing"
    )

    for channel, hits in sorted(branches.items()):
        if not hits:
            continue
        for word in _CHANNEL_PROSE_WORDS.get(channel, (channel,)):
            assert word not in clause, (
                f"the claim's docstring puts the {channel} path among those whose first "
                f"commit is status=sent, but that branch commits at {hits} first — a "
                "reader auditing duplicate sends is told those commits do not exist"
            )

    for ticket_channel in ("jira", "linear"):
        assert branches[ticket_channel], (
            f"the {ticket_channel} branch no longer commits before status=sent, so the "
            "docstring's sentence about the ticket paths recording the external issue id "
            "in a commit of their own has become the false one"
        )
    assert "ticket" in doc.lower() and "external issue id" in doc.lower(), (
        "the ticket paths' own commit is no longer described anywhere in the claim's "
        "docstring — which is the state in which it came to say status=sent was their first"
    )
