"""Batch 4, cross-lane: three fixes whose reporter could not reach the file.

Each of the three was found while fixing something else, in a module the finder
did not own, and each lands in a different layer — a service, a demo seeder, a
schema. They are together here because that is the one thing they have in
common; nothing in this file shares a fixture with anything else in it.

``tripl-0zpq.37`` (service lane) — the Inbox **Retry** button has to hand the
send task a CLAIMABLE row. The single-flight lease that fix added refuses a
delivery whose ``claimed_at`` is newer than ``STRANDED_DELIVERY_MINUTES``, and
``retry_delivery`` flips a `failed` row back to `pending` without clearing it.
The reaper's failed arm — the only other failed -> pending flip in the system —
does clear it, and says why: the hand-off must not depend on the send path's own
release having run. This is defence in depth rather than a live bug, and the
test says which: see ``test_retry_hands_the_send_task_a_row_it_can_claim``.

``tripl-0zpq.253`` (demo lane) — ``scope_name`` is a String(255) fed by wider
sources, and ``demo.builders.alerts`` was the last writer family outside the
trim the rest of the batch installed.

``tripl-v422`` (schema lane) — a per-destination From: override that every
reader of the column would happily deliver could not be SAVED, because the save
used the strict validator and the send paths use ``validate_sender_address``.
The mirror image of ``tripl-0zpq.29``, which fixed the same disagreement at the
other end of the same pipe.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from tripl.alerting_validation import validate_sender_address
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import SCOPE_NAME_MAX_LEN, AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_rule import AlertRule
from tripl.models.domain_enums import MetricScopeType
from tripl.models.event import Event
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.schemas.alerting import AlertDestinationCreate, AlertDestinationUpdate
from tripl.schemas.app_settings import EmailSettingsUpdate
from tripl.services import alerting_service
from tripl.services.demo.builders.alerts import _build_firings, _delivery_item

# Import the ``metrics`` package rather than ``tasks.alerts`` directly: the
# celery task graph is cyclic, so entering at ``tasks.alerts`` in a process that
# has not loaded the app yet lands mid-cycle and raises ImportError. Same order
# ``_alerting_deliveries.retry_delivery`` imports in, for the same reason.
from tripl.worker.tasks import metrics as alerts_task
from tripl.worker.tasks.alerts import _claim_delivery
from tripl.worker.tasks.maintenance import STRANDED_DELIVERY_MINUTES

# ---------------------------------------------------------------------------
# Retry has to leave a row the send task it enqueues can claim
# ---------------------------------------------------------------------------


def _seed_failed_delivery(session, *, slug: str, claimed_at: datetime) -> uuid.UUID:
    """One `failed` delivery, holding a lease, with everything ``get_delivery`` joins.

    ``get_delivery`` — which ``retry_delivery`` returns through — inner-joins the
    destination, the rule and the scan config, so all three have to exist or the
    retry 404s for a reason that has nothing to do with what is under test.

    Foreign keys are deliberately NOT enforced on this engine (unlike
    ``conftest``'s), which is what lets ``data_source_id`` be a bare id: a
    DataSource row would add a fourth table to keep in step for no assertion.
    """
    project = Project(
        id=uuid.uuid4(),
        name="Crosslane Retry",
        slug=slug,
        description="",
    )
    scan_config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=uuid.uuid4(),
        project_id=project.id,
        name="Crosslane Scan",
        base_query="SELECT * FROM events",
        time_column="created_at",
        cardinality_threshold=100,
        interval="1h",
    )
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type=AlertDestinationType.demo_sink.value,
        name="Local sink",
        enabled=True,
    )
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name="Spike watch",
        enabled=True,
        message_format="plain",
    )
    delivery = AlertDelivery(
        id=uuid.uuid4(),
        project_id=project.id,
        scan_config_id=scan_config.id,
        destination_id=destination.id,
        rule_id=rule.id,
        channel=AlertDestinationType.demo_sink.value,
        status=AlertDeliveryStatus.failed.value,
        matched_count=1,
        dispatch_attempts=3,
        error_message="the relay said no",
        # The state the send path's own release did NOT produce: a worker
        # SIGKILLed between its claim and its terminal write, a release whose
        # commit raised, or a row written before the lease column existed.
        # Inside the horizon, so no send task may claim it as it stands.
        claimed_at=claimed_at,
    )
    item = AlertDeliveryItem(
        id=uuid.uuid4(),
        delivery_id=delivery.id,
        scope_type=MetricScopeType.event.value,
        scope_ref="event-1",
        scope_name="Checkout Submit",
        bucket=datetime(2026, 9, 14, 9, tzinfo=UTC),
        direction="spike",
        actual_count=120,
        expected_count=40,
        absolute_delta=80,
        percent_delta=200,
        correlation_group_id=uuid.uuid4(),
    )
    session.add_all([project, scan_config, destination, rule, delivery, item])
    return delivery.id


@pytest.mark.asyncio
async def test_retry_hands_the_send_task_a_row_it_can_claim(tmp_path, monkeypatch) -> None:
    """Retry must not report success over a send that cannot happen.

    ``alerts._claim_delivery`` is a compare-and-set that refuses a `pending` row
    whose ``claimed_at`` is newer than ``STRANDED_DELIVERY_MINUTES``. Retry flips
    the row to `pending`, resets the attempt budget and enqueues the task — so a
    lease left behind turns that ``.delay()`` into a silent no-op: the API
    answers 200, the Inbox shows `pending`, and nothing goes out until the lease
    ages out fifteen minutes later.

    The final assertion is the real one and it runs the real claim rather than
    re-deriving the rule: delete ``delivery.claimed_at = None`` from
    ``services/_alerting_deliveries.retry_delivery`` and ``_claim_delivery``
    returns False here, with the two assertions above it still passing — which
    is precisely why this is worth a test. It is the same assertion, on the same
    line of reasoning, that ``test_alerting`` makes for the reaper's failed arm;
    the two flips have to agree.

    NOT a live bug today, and the fix is not sold as one. Every send path
    releases its own lease when the attempt ends (``alerts.py`` on both
    branches, ``alert_digest_send.py`` on all three), and the one write that
    marks a row `failed` without going through a send — the reaper's exhaustion
    relabel — can only ever see an EXPIRED lease, because ``claimed_at`` is set
    by the same UPDATE that bumps ``updated_at`` and that arm selects on
    ``updated_at < now - STRANDED_DELIVERY_MINUTES``. So the row seeded here is
    reachable only through a crash or a write that predates the column. Clearing
    it unconditionally is what makes the hand-off independent of that release
    having run, which is the reason ``maintenance.py`` gives for its own copy.
    """
    slug = "crosslane-retry"
    db_path = tmp_path / "crosslane_retry.db"
    now = datetime.now(UTC)
    # Comfortably inside the horizon: a lease no send task would be allowed to
    # take. Measured off the constant so a change to it moves this with it.
    held_at = now - timedelta(minutes=max(1, STRANDED_DELIVERY_MINUTES // 3))

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    enqueued: list[str] = []
    monkeypatch.setattr(
        alerts_task.send_alert_delivery,
        "delay",
        lambda delivery_id: enqueued.append(delivery_id),
    )

    try:
        async with session_local() as session:
            delivery_id = _seed_failed_delivery(session, slug=slug, claimed_at=held_at)
            await session.commit()

            retried = await alerting_service.retry_delivery(session, slug, delivery_id)

        assert retried.status == AlertDeliveryStatus.pending
        assert enqueued == [str(delivery_id)], "the retry never reached the broker"
    finally:
        # Fully released before the sync engine opens the same file.
        await async_engine.dispose()

    # The worker side, on the row Retry actually left behind.
    engine = create_engine(f"sqlite:///{db_path}")
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == AlertDeliveryStatus.pending.value
        assert delivery.error_message is None
        assert delivery.sent_at is None
        assert delivery.dispatch_attempts == 0

        claimed = _claim_delivery(session, delivery, now=datetime.now(UTC))
        assert claimed is True, (
            "Retry left a live lease on the row it re-enqueued, so the send task "
            "it just dispatched returns 'already_claimed' and nothing is sent"
        )

    Base.metadata.drop_all(engine)
    engine.dispose()


# ---------------------------------------------------------------------------
# the demo seeder is a scope_name writer like any other
# ---------------------------------------------------------------------------

# Longer than the column and longer than the ellipsis budget, in the shape the
# real overflow arrives in: ``Event.name`` is String(500) and every character of
# it is admitted by ``schemas/event.py``.
_LONG_EVENT_NAME = "checkout:" + "submit_button_pressed_" * 20


@pytest.mark.asyncio
async def test_the_demo_seeder_trims_the_scope_label_it_writes() -> None:
    """The demo's firings carry a label that fits the column it is headed for.

    ``_build_firings`` resolves the label out of ``Event.name`` /
    ``EventType.display_name`` / ``MetricDefinition.display_name``, the same
    wider sources ``trim_scope_name`` exists for, and its output feeds BOTH
    ``render_firings_message`` (which becomes the seeded
    ``payload_snapshot["rendered_message"]``) and, through ``_delivery_item``,
    the ``scope_name`` column. Nothing the demo recipe seeds overflows 255
    today — it writes the names it reads back — so this is the guard, not a
    reproduction: a recipe that one day seeds a realistically long event name
    would otherwise hit "value too long for type character varying(255)" inside
    ``create_demo_project``, which is one transaction, and lose the whole seed.

    Drop ``trim_scope_name`` from ``_build_firings``
    (services/demo/builders/alerts.py) and the length assertion reddens. Move it
    from there down into ``_delivery_item`` instead and the LAST assertion
    reddens, which is the one that says why the trim belongs at the firing: the
    message the demo shows and the column it stores have to name the same scope.
    """
    event_id = uuid.uuid4()
    project = Project(
        id=uuid.uuid4(),
        name="Crosslane Demo",
        slug="crosslane-demo",
        description="",
        is_demo=True,
    )
    event = Event(
        id=event_id,
        project_id=project.id,
        branch_id=uuid.uuid4(),
        event_type_id=uuid.uuid4(),
        name=_LONG_EVENT_NAME,
    )
    anomaly = MetricAnomaly(
        id=uuid.uuid4(),
        # NULL is legal here and keeps a ScanConfig out of the fixture; the
        # scope is resolved off ``event_id``, not off the config.
        scan_config_id=None,
        scope_type=MetricScopeType.event.value,
        scope_ref=str(event_id),
        event_id=event_id,
        bucket=datetime(2026, 9, 14, 9, tzinfo=UTC),
        actual_count=120.0,
        expected_count=40.0,
        stddev=5.0,
        effective_stddev=5.0,
        z_score=16.0,
        detector_kind="phase",
        direction="spike",
    )

    # In-memory and without ``enable_sqlite_foreign_keys``: the Event's branch
    # and event type are not read by anything here, and sqlite ignores
    # VARCHAR(n), which is the property that lets the overlong name be stored at
    # all — the same reason the suite never saw tripl-0zpq.253 in the first place.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_local = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_local() as session:
            session.add_all([project, event, anomaly])
            await session.commit()

            firings = await _build_firings(session, project, [anomaly])
    finally:
        await engine.dispose()

    assert len(firings) == 1
    firing = firings[0]
    assert len(firing.scope_name) == SCOPE_NAME_MAX_LEN, (
        f"the demo seeded a {len(firing.scope_name)}-character label into a "
        f"{SCOPE_NAME_MAX_LEN}-character column"
    )
    # An ellipsis, not a hard slice — a name that simply stops is
    # indistinguishable from one authored that way, and this label is read by a
    # person in the Inbox.
    assert firing.scope_name.endswith("...")
    assert _LONG_EVENT_NAME.startswith(firing.scope_name[:-3])

    item = _delivery_item(firing, uuid.uuid4(), uuid.uuid4())
    assert item.scope_name == firing.scope_name, (
        "the label the demo renders into its seeded message and the label it "
        "stores on the item have diverged"
    )


# ---------------------------------------------------------------------------
# the destination override accepts what the destination delivers
# ---------------------------------------------------------------------------

# What an operator types into a From: field when they want the alert to arrive
# from a name rather than a bare mailbox. ``EmailMessage`` takes it;
# ``validate_email_address`` does not.
_DISPLAY_NAME_FROM = "Tripl Alerts <no-reply@example.com>"


def _email_destination_payload(**overrides: object) -> dict[str, object]:
    return {
        "type": "email",
        "name": "Ops Email",
        "email_recipients": "ops@example.com",
        **overrides,
    }


def test_a_destination_can_save_the_from_address_its_own_send_path_delivers() -> None:
    """Save and send have to answer one question the same way.

    Both places that resolve a From: at send time read this exact column —
    ``alerts._resolve_email_context`` for the immediate alert and the combined
    digest, ``alerts_channels._send_digest_to_destination`` for the weekly plan
    digest and the sunset alert — and both run it through
    ``validate_sender_address`` and hand the ORIGINAL string to ``msg["From"]``.
    The destination's own Test button reads the same column into ``_TestTarget``
    and accepts a display name there (``test_smtp_transport``). Only the SAVE
    refused it, so the value was unreachable: not a send-time surprise, a
    configuration the operator simply could not enter.

    That is the mirror of tripl-0zpq.29, where the diagnostics were more
    permissive than delivery. Here the save was stricter than delivery, and the
    pair is only consistent once both ends of it — the global Default From and
    the per-destination override — use the send path's own helper.

    Put ``validate_email_from_address`` back into
    ``schemas/alerting._validate_email_from_override`` and every assertion in
    this test but the last two reddens with "A display name and angle brackets
    around the email address are not permitted here".

    The value is asserted byte-for-byte rather than just "did not raise": the
    helper returns the ORIGINAL string, and a future normalising version would
    still save and still deliver — to a reader who no longer sees who it is from.
    """
    created = AlertDestinationCreate(
        **_email_destination_payload(email_from_address=_DISPLAY_NAME_FROM)
    )
    assert created.email_from_address == _DISPLAY_NAME_FROM

    updated = AlertDestinationUpdate(email_from_address=_DISPLAY_NAME_FROM)
    assert updated.email_from_address == _DISPLAY_NAME_FROM

    # The value this one OVERRIDES already accepted it; that asymmetry inside
    # one pair of fields was the whole defect.
    assert (
        EmailSettingsUpdate(smtp_from_address=_DISPLAY_NAME_FROM).smtp_from_address
        == _DISPLAY_NAME_FROM
    )
    # ...and the send path agrees, which is the claim the save is now making.
    assert validate_sender_address(_DISPLAY_NAME_FROM) == _DISPLAY_NAME_FROM


def test_the_destination_override_still_refuses_a_value_with_no_at_sign() -> None:
    """Loosened, not deleted — the cheaper-looking way to make the test above pass.

    Dropping the validation entirely would satisfy the display-name case just as
    well, so this pins what must still be caught. A bare string with no @-sign
    serialises into the header happily and comes back from the relay as an error
    naming nothing, hours later, on a delivery nobody is watching — and on a
    DESTINATION override it is worse than on the global, because it silently
    overrides a Default From that was fine.
    """
    with pytest.raises(ValidationError):
        AlertDestinationCreate(**_email_destination_payload(email_from_address="not-an-address"))

    with pytest.raises(ValidationError):
        AlertDestinationUpdate(email_from_address="not-an-address")


def test_an_empty_override_still_clears_to_none() -> None:
    """How the override is REMOVED, and the state that falls back to the global.

    ``alerts._resolve_email_context`` reads
    ``destination.email_from_address or email_config.smtp_from_address``, so
    None is not merely tolerated here — it is the switch that hands the
    destination back to the instance-wide Default From. An empty string that
    survived as an empty string would be falsy there too, but it would also
    round-trip to the form as a "set" override; ``None`` is what the column has
    always stored and what ``AlertDestinationResponse`` promises.
    """
    assert AlertDestinationCreate(**_email_destination_payload()).email_from_address is None
    assert (
        AlertDestinationCreate(
            **_email_destination_payload(email_from_address="   ")
        ).email_from_address
        is None
    )
    assert AlertDestinationUpdate(email_from_address=None).email_from_address is None
    assert AlertDestinationUpdate(email_from_address="").email_from_address is None


# The same shape as ``_DISPLAY_NAME_FROM`` — an operator's organisation name in
# front of a real mailbox — padded out to exactly the width of
# ``alert_destinations.email_from_address``.
_FROM_MAILBOX = " <alerts@acme.example>"
_FROM_AT_COLUMN_WIDTH = (
    "Acme Corp Platform Reliability".ljust(255 - len(_FROM_MAILBOX), ".") + _FROM_MAILBOX
)


def test_the_from_override_stops_exactly_where_its_column_does() -> None:
    """Loosening the shape check must not have loosened the length with it.

    ``validate_email_address`` bounded this field by accident: it normalises
    through ``email_validator``, which refuses an address over 254 octets, and
    254 fits ``String(255)``. ``validate_sender_address`` validates only the
    address parsed out of the value and returns the ORIGINAL string, so the
    display name that tripl-v422 exists to allow — and any padding around it —
    was left with no ceiling at all. A long organisation name is an ordinary
    value to type, and on Postgres it became a StringDataRightTruncation out of
    the INSERT: the catch-all 500 in ``main.py`` naming no field, nothing
    written. SQLite ignores VARCHAR widths, so that is invisible to this suite
    and only the 422 below can stand in for it.

    Drop ``max_length=255`` from ``AlertDestinationCreate.email_from_address``
    and the first ``pytest.raises`` finds nothing raised; drop it from
    ``AlertDestinationUpdate`` and the second does.

    The final assertion is why the bound cannot live in the shared helper: the
    send path still accepts the over-long value, because the send path is not
    the thing with a 255-character column.
    """
    assert len(_FROM_AT_COLUMN_WIDTH) == 255
    assert AlertDestination.__table__.c["email_from_address"].type.length == 255

    created = AlertDestinationCreate(
        **_email_destination_payload(email_from_address=_FROM_AT_COLUMN_WIDTH)
    )
    assert created.email_from_address == _FROM_AT_COLUMN_WIDTH
    assert (
        AlertDestinationUpdate(email_from_address=_FROM_AT_COLUMN_WIDTH).email_from_address
        == _FROM_AT_COLUMN_WIDTH
    )

    # One character wider, and still a perfectly well-formed From: — the value
    # is refused for its length alone, which is the only reason it can be.
    too_long = "A" + _FROM_AT_COLUMN_WIDTH
    with pytest.raises(ValidationError) as create_error:
        AlertDestinationCreate(**_email_destination_payload(email_from_address=too_long))
    assert [(error["loc"], error["type"]) for error in create_error.value.errors()] == [
        (("email_from_address",), "string_too_long")
    ]

    with pytest.raises(ValidationError) as update_error:
        AlertDestinationUpdate(email_from_address=too_long)
    assert [(error["loc"], error["type"]) for error in update_error.value.errors()] == [
        (("email_from_address",), "string_too_long")
    ]

    assert validate_sender_address(too_long) == too_long
