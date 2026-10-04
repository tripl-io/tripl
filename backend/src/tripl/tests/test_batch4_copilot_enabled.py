"""The disabled-destination window, measured where it actually is.

The ``_assert_destination_enabled`` check went into the two send tasks and
that closed the case it was filed for: a delivery minted at 09:00 against an
enabled destination, switched off at 09:05, redispatched by the reaper at 09:15.
``test_batch4_send.py`` pins that case, and it flips the toggle BEFORE the task
runs — so the check at the top of the task is enough to make it pass.

Copilot's review of PR #169 pointed out what that leaves open. The check sits at
the top of the task and the egress is 130 to 400 lines below it, with the AI
round-trip, the sparkline and top-mover warehouse reads and the template render
in between; the scheduled-digest sender is worse, because it renders and commits
EVERY member of the batch before the first outbound call. And the helper both
beat-scheduled digests use (``alerts_digest._send_digest_to_destination``) had no
enabled check at all — its two tasks SELECT every enabled destination in the
database up front and then loop, building a dozen queries' worth of message per
project before sending.

So every test here flips the toggle DURING that window — from inside the render
the task is running — which is the only shape of the bug the earlier tests
cannot see. Each one names the line whose revert reddens it.

One mechanism is shared by all of them and is worth stating once. The flip is
issued on the TASK'S OWN session, as a Core UPDATE with
``synchronize_session=False``:

* Not from a second connection, because sqlite serializes writers and the send
  task holds an open write transaction from the moment it flushes its claim —
  a second connection would block on that lock for pysqlite's timeout instead of
  racing the render, which is a hang, not a test.
* ``synchronize_session=False`` because the point is the STALENESS. It leaves
  the ``AlertDestination`` instance the task is holding still reading
  ``enabled=True`` while the row behind it reads disabled, which is exactly what
  a ``PATCH /alert-destinations/{id}`` committed on another connection leaves
  behind — worker sessions are ``expire_on_commit=False`` (worker/db.py), so
  nothing in these tasks would notice on its own. That staleness is the whole
  reason the second check has to RE-READ; re-calling
  ``_assert_destination_enabled`` on the instance already in hand would return
  the answer the first call got and could never disagree with it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session, sessionmaker

# Imported first, and for its side effect — the same import-order note
# test_batch4_messages.py carries: celery_app's bottom-of-file registration is
# what pulls the worker task modules in an order they all survive, and entering
# the package at ``alerts`` or ``alerts_digest`` instead raises ImportError.
import tripl.worker.celery_app  # noqa: F401
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.project import Project
from tripl.tests.test_alert_digest_delivery import _run_digest, _seed
from tripl.tests.test_alerting import _retry_from_inbox, _seed_telegram_length_case
from tripl.worker.tasks import alerts as alerts_module
from tripl.worker.tasks import alerts_messages as messages_module
from tripl.worker.tasks.alerts import (
    check_deprecated_sunset_events,
    send_alert_delivery,
    send_weekly_plan_digest,
)

_DIGEST = "tripl.worker.tasks.alerts_digest"

_TEMPLATE = "[tripl] ${matched_count} alerts\n${items_text}"


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_copilot_enabled.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _switch_off_from_inside(session: Session, destination_id: uuid.UUID) -> None:
    """The operator's PATCH, landing in the middle of a send. See the module docstring."""
    session.execute(
        update(AlertDestination)
        .where(AlertDestination.id == destination_id)
        .values(enabled=False)
        .execution_options(synchronize_session=False)
    )


def _set_enabled(factory: sessionmaker[Session], destination_id: uuid.UUID, *, on: bool) -> str:
    """Flip the toggle from outside any running task; returns the destination name."""
    with factory() as session:
        destination = session.get(AlertDestination, destination_id)
        assert destination is not None
        destination.enabled = on
        session.commit()
        return destination.name


def _destination_id(factory: sessionmaker[Session], delivery_id: str) -> uuid.UUID:
    with factory() as session:
        delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert delivery is not None
        return delivery.destination_id


# ---------------------------------------------------------------------------
# (a) worker/tasks/alerts.py — the per-delivery send task.
# ---------------------------------------------------------------------------


def test_a_destination_switched_off_during_the_render_is_not_posted_to(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The toggle is read at the egress, not only at the top of the task.

    ``send_alert_delivery`` checks ``enabled`` immediately after the egress
    guard and then spends the next several hundred lines rendering: an AI
    round-trip when the rule asks for one, a sparkline and a top-mover query per
    item, the template, and for Telegram the splitter. Only then does it post.
    An operator watching an alert storm reaches the switch during exactly that
    stretch — the storm is what makes the render slow and what makes them want
    the channel off — and the top-of-task check has already passed.

    The line under test is ``_assert_destination_still_enabled(destination)``,
    immediately above the branch dispatch in ``worker/tasks/alerts.py``. Delete
    it and this run comes back ``sent`` with the message sitting in ``posts``.
    Keep the call but drop the ``session.refresh`` inside
    ``_assert_destination_still_enabled`` and it comes back ``sent`` just the
    same, because the instance in hand still says enabled: the re-read is the
    fix, not the second call.

    The top-of-task check is not what saves this run. It ran before the flip and
    passed, which is the point — the flip happens inside the render it precedes.
    """
    delivery_id, _scope_names = _seed_telegram_length_case(
        sync_session_factory,
        item_count=2,
        message_template=_TEMPLATE,
    )
    destination_id = _destination_id(sync_session_factory, delivery_id)

    posts: list[str] = []

    def telegram_post_json(
        url: str,
        body: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        posts.append(str(body["text"]))

    original_render = messages_module._render_delivery_message
    flipped: list[bool] = []

    def render_and_switch_off(delivery: AlertDelivery, **kwargs: object) -> tuple[str, str]:
        """Render exactly as production does, with the toggle moving mid-render."""
        if not flipped:
            session = kwargs["session"]
            assert isinstance(session, Session), "the render lost its session"
            _switch_off_from_inside(session, destination_id)
            flipped.append(True)
        return original_render(delivery, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(alerts_module, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(alerts_module, "_post_json", telegram_post_json)
    monkeypatch.setattr(alerts_module, "_render_delivery_message", render_and_switch_off)

    result = send_alert_delivery.run(delivery_id)

    assert flipped == [True], "the fixture never reached the render it was meant to land in"
    assert posts == [], "an alert reached a destination switched off while it was rendering"
    assert result["status"] == "failed"
    error_message = str(result["error"])
    assert "Ops Bot" in error_message, error_message
    assert "disabled" in error_message, error_message

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        # Failed rather than dropped, for the reason the earlier guard's test
        # gives: a row left `pending` is re-offered by the reaper every fifteen
        # minutes and never tells the operator an alert did not arrive.
        assert persisted.status == AlertDeliveryStatus.failed.value
        assert persisted.sent_at is None
        assert persisted.claimed_at is None
        snapshot = persisted.payload_snapshot
        assert isinstance(snapshot, dict)
        # The render is already paid for when this guard fires, so the body it
        # produced is stamped onto the failed row instead of being thrown away:
        # the Inbox shows the operator the message their toggle stopped. Placing
        # the guard ABOVE the render would save the round-trip and lose this.
        assert snapshot.get("rendered_message"), snapshot

    # The other direction, so a guard that poisoned the row for good could not
    # pass as this one. The flip above rode on the task's transaction and rolled
    # back with it, so this re-enable is belt and braces rather than a repair —
    # stated, not assumed, because the assertion below depends on it.
    _set_enabled(sync_session_factory, destination_id, on=True)
    _retry_from_inbox(sync_session_factory, delivery_id)

    second = send_alert_delivery.run(delivery_id)

    assert second["status"] == "sent"
    assert len(posts) == 1, "a re-enabled destination still refused the delivery"


# ---------------------------------------------------------------------------
# (b) worker/tasks/alert_digest_send.py — the flushed digest's grouped send.
# ---------------------------------------------------------------------------


def _seed_digest_member(session: Session) -> tuple[uuid.UUID, uuid.UUID, str]:
    """One flushed digest member on an ENABLED Slack destination.

    The same seed ``test_batch4_send.py`` uses for the pre-flight case, minus
    its ``destination.enabled = False`` line: here the destination is on when
    the task starts and goes off while the task is rendering.
    """
    config, destination, rule, _event_type = _seed(session, cron=None)
    delivery = AlertDelivery(
        id=uuid.uuid4(),
        project_id=config.project_id,
        scan_config_id=config.id,
        destination_id=destination.id,
        rule_id=rule.id,
        channel="slack",
        status="pending",
        matched_count=1,
        # What the flush stamps: the one record of digest-ness the send task reads.
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
    session.commit()
    return delivery.id, destination.id, destination.name


def test_a_digest_group_whose_destination_went_off_mid_batch_is_not_posted_to(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grouped send has the widest window in the pipeline, so it re-reads too.

    ``send_alert_digest`` checks ``enabled`` in its per-member prepare loop and
    then keeps preparing: every member of the batch is rendered, given its AI
    note, and committed at the ``session.commit()`` that makes the bodies
    durable — and only after all of that does the first outbound call happen. A
    destination carrying four rules therefore waits out four renders between its
    check and its message.

    The line under test is ``_assert_destination_still_enabled(destination)`` at
    the top of the group loop's ``try`` in ``worker/tasks/alert_digest_send.py``.
    Delete it and this run reports one message sent with the digest body in
    ``posts``. Drop the ``session.refresh`` behind it and the same thing happens
    for the sharper reason: the flip below is COMMITTED by the batch's own
    durability commit before the group loop runs, and ``session.get`` still
    hands back an instance reading enabled, because worker sessions do not
    expire on commit.

    Refused as its own group, not as the batch: the failure arm marks this
    group's members `failed` with the toggle named and leaves every other
    destination in the same flush to ship.
    """
    with sync_session_factory() as session:
        delivery_uuid, destination_id, destination_name = _seed_digest_member(session)
    delivery_id = str(delivery_uuid)

    original_render = messages_module._render_delivery_message
    flipped: list[bool] = []

    def render_and_switch_off(delivery: AlertDelivery, **kwargs: object) -> tuple[str, str]:
        if not flipped:
            session = kwargs["session"]
            assert isinstance(session, Session), "the render lost its session"
            _switch_off_from_inside(session, destination_id)
            flipped.append(True)
        return original_render(delivery, **kwargs)  # type: ignore[arg-type]

    # Patched on the module rather than on the task's globals because
    # ``send_alert_digest`` imports the renderer lazily, inside the function, to
    # break the cycle its own comment documents — so the name is resolved from
    # ``alerts_messages`` at call time and this is the binding it reads.
    monkeypatch.setattr(messages_module, "_render_delivery_message", render_and_switch_off)

    result, posts = _run_digest(monkeypatch, sync_session_factory, [delivery_id])

    assert flipped == [True], "the fixture never reached the render it was meant to land in"
    assert posts == [], "a digest reached a destination switched off while it was rendering"
    assert result["messages"] == 0
    assert result["sent"] == 0
    assert result["failed"] == 1

    with sync_session_factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        assert persisted.status == AlertDeliveryStatus.failed.value
        assert persisted.sent_at is None
        assert destination_name in (persisted.error_message or "")
        assert "disabled" in (persisted.error_message or "")
        # Handed back, or the Retry below would be a no-op for the lease's life.
        assert persisted.claimed_at is None

    # This flip WAS committed — by the batch's own durability commit — so the
    # re-enable here is a repair, not a formality.
    _set_enabled(sync_session_factory, destination_id, on=True)
    _retry_from_inbox(sync_session_factory, delivery_id)

    second, posts_after = _run_digest(monkeypatch, sync_session_factory, [delivery_id])

    assert second["sent"] == 1
    assert len(posts_after) == 1, "a re-enabled destination still refused the digest"


# ---------------------------------------------------------------------------
# (c) worker/tasks/alerts_digest.py — the two beat-scheduled digests.
# ---------------------------------------------------------------------------


def _seed_scheduled_digest_project(session: Session) -> tuple[uuid.UUID, str]:
    """A non-demo project holding the one row shape both digest SELECTs admit."""
    project = Project(
        id=uuid.uuid4(),
        name="Checkout",
        slug=f"checkout-{uuid.uuid4().hex[:8]}",
        description="",
        is_demo=False,
    )
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type=AlertDestinationType.slack.value,
        name="Checkout Slack",
        enabled=True,
        webhook_url_encrypted="fake-secret",
    )
    session.add_all([project, destination])
    session.commit()
    return destination.id, destination.name


def _capture_scheduled_sends(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record at the CHANNEL, one level below the guards.

    Stubbing ``alerts_digest._send_digest_to_destination`` itself — as the older
    sunset tests do — would remove the thing under test. Patching the channel
    function that wrapper delegates to leaves both guards in the path and still
    stops short of a socket.
    """
    messages: list[str] = []

    def fake_transport(
        *,
        destination: AlertDestination,
        message: str,
        project: Project,
        email_config: object,
        send_slack_message: object,
        send_email_message: object,
        subject_title: str = "Weekly tripl digest",
    ) -> None:
        messages.append(message)

    monkeypatch.setattr(f"{_DIGEST}._channel_send_digest_to_destination", fake_transport)
    return messages


def test_the_weekly_plan_digest_re_reads_the_toggle_after_building_its_message(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SELECT filtered on ``enabled``; the send is a message-build later.

    ``send_weekly_plan_digest`` SELECTs every enabled Slack/email destination in
    the database in one statement and then loops, and each turn of that loop runs
    a dozen plan, drift and anomaly queries before it sends. Its ``WHERE
    AlertDestination.enabled IS TRUE`` therefore describes the estate as it was
    when the task started, not as it is when a given project's message goes out —
    and until this fix ``_send_digest_to_destination`` had no enabled check at
    all, only the demo-egress backstop, so nothing between the two ever looked
    again.

    The line under test is ``_assert_destination_still_enabled(destination)`` in
    ``alerts_digest._send_digest_to_destination``. Delete it and this run reports
    one sent with the digest in ``sends``. Drop the ``session.refresh`` behind it
    and the same, because the destination instance came out of that opening
    SELECT and nothing in this loop commits or expires it.

    The message builder is stubbed rather than driven: what is being measured is
    which toggle value reaches the send, and a real digest body would only add a
    plan fixture to the failure surface. It is stubbed at the point the real one
    occupies — taking the task's session, returning the text — so the send path
    below it is untouched.
    """
    with sync_session_factory() as session:
        destination_id, destination_name = _seed_scheduled_digest_project(session)

    flipped: list[bool] = []

    def build_and_switch_off(session: Session, *, project: Project, now: datetime) -> str:
        _switch_off_from_inside(session, destination_id)
        flipped.append(True)
        return f"Weekly tripl digest for {project.name}"

    sends = _capture_scheduled_sends(monkeypatch)
    monkeypatch.setattr(f"{_DIGEST}._get_sync_session", sync_session_factory)
    monkeypatch.setattr(f"{_DIGEST}._build_plan_digest_message", build_and_switch_off)

    result = send_weekly_plan_digest.run()

    assert flipped == [True], "the fixture never reached the message build"
    assert sends == [], "the weekly digest posted to a destination switched off mid-run"
    # Checked, attempted, refused: the row is counted because it WAS attempted,
    # which is the honest tally and is how the operator sees that something
    # stopped. A demo project, by contrast, is excluded in the SELECT and never
    # reaches either counter (test_batch4_messages.py).
    assert result == {"destinations_checked": 1, "sent": 0, "failed": 1}

    # And the toggle, not the project: the same seed with the switch left alone
    # must still deliver, or the assertions above would hold for any broken send.
    _set_enabled(sync_session_factory, destination_id, on=True)
    flipped.clear()

    def build_quietly(session: Session, *, project: Project, now: datetime) -> str:
        return f"Weekly tripl digest for {project.name}"

    monkeypatch.setattr(f"{_DIGEST}._build_plan_digest_message", build_quietly)

    enabled_run = send_weekly_plan_digest.run()

    assert enabled_run == {"destinations_checked": 1, "sent": 1, "failed": 0}
    assert sends == ["Weekly tripl digest for Checkout"]


def test_the_daily_sunset_alert_re_reads_the_toggle_after_building_its_message(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second task through the same helper, pinned separately.

    One guard line covers both, but the two tasks reach it through two
    independent calls to ``_send_digest_to_destination`` and beat runs them on
    two different schedules — so a change that keeps the weekly digest's call and
    routes the daily one somewhere else would leave this uncovered. The daily
    cadence is also what makes the window matter here: this alert goes out to
    real destinations every day whether or not anything changed
    (``check_deprecated_sunset_events``'s own docstring), so a channel switched
    off is a channel that would otherwise be posted to again tomorrow.

    Same revert, same two halves: delete
    ``_assert_destination_still_enabled(destination)`` from the helper, or drop
    the refresh behind it, and this reports one sent with the alert in ``sends``.
    """
    with sync_session_factory() as session:
        destination_id, _destination_name = _seed_scheduled_digest_project(session)

    flipped: list[bool] = []

    def build_and_switch_off(
        session: Session,
        *,
        project: Project,
        now: datetime,
    ) -> str | None:
        _switch_off_from_inside(session, destination_id)
        flipped.append(True)
        return "Deprecated events still receiving data\nCount: 1\n- app:old_purchase"

    sends = _capture_scheduled_sends(monkeypatch)
    monkeypatch.setattr(f"{_DIGEST}._get_sync_session", sync_session_factory)
    monkeypatch.setattr(f"{_DIGEST}._build_sunset_alert_message", build_and_switch_off)

    result = check_deprecated_sunset_events.run()

    assert flipped == [True], "the fixture never reached the message build"
    assert sends == [], "the sunset alert posted to a destination switched off mid-run"
    assert result == {"destinations_checked": 1, "sent": 0, "failed": 1}
