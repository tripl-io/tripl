"""The deprecated-sunset alert, both halves.

``check_deprecated_sunset_events`` was registered, callable, tested, and on
nobody's timer: no beat entry named it and nothing else invoked it, so whatever
it built could not reach a reader. It was kept rather than deleted because the
feature is already half-shipped — ``_build_plan_digest_message`` renders the
same counter, from the same predicate, to the same set of destinations, as
"- Deprecated events still receiving data: N". A count cannot name the events;
this task is that line expanded, and it is now on a daily schedule.

Wiring it made the second half urgent. ``_build_sunset_alert_message`` filtered
by project and never by branch, while every event count in the weekly digest is
scoped to the MAIN plan branch with a comment explaining why. Opening a working
branch deep-copies every event row and carries ``status``, ``sunset_at`` and
``last_seen_at`` across unchanged (``plan_branch_service``), so one overdue
event on a project with one open branch produced "Count: 2" and printed the
same event twice — while the digest, on the same data, said 1. Scheduling that
would have shipped the duplicate to real Slack and email destinations daily,
which is why neither half is landable alone.

Both messages now read the sunset watch's open ``sunset_overdue`` findings
(``alerts_messages._sunset_overdue_scope``), which the daily sweep writes for
MAIN-branch events only, so the branch scope is the sweep's and the helpers
below seed a finding exactly where the sweep would. Whether an event's data
stopping clears it is pinned in test_prelaunch_worker_digest_findings.py,
against the real sweep.

The tests below pin each half against its own revert:

* the beat entry exists, names a REGISTERED task, and is daily;
* an open branch's copy neither duplicates a line nor moves the count;
* a branch that is only a PROPOSAL cannot raise an alert about the live plan;
* the alert and the digest agree about one project, which is the property the
  two functions' comments now claim.

Scheduling it also made the message's SIZE matter, which is section (c).
``_build_sunset_alert_message`` renders one line per overdue event, and there is
no cap anywhere between its query and the webhook — not in the builder, not in
``alerts_channels._send_slack_message``, and not in the Telegram splitter, which
this path cannot reach because the task selects only slack and email
destinations. On a daily beat to every enabled destination that made the payload
bounded by nothing but the project's deprecated-event count. Section (c)
pins the cap, the tail that admits to it, and the ceiling its number was chosen
against:

* the list is capped, says how many events it is not showing, and leaves
  ``Count:`` the true total the digest is compared against;
* the capped message fits Slack's ``text`` field even at the widest name the
  ``events.name`` column allows.

The final section belongs to a different fix, about the same outgoing
messages. It covers
what a RESUMED Telegram digest says about itself once the remainder needs more
than one message — the half of that fix that lives in
``alerts_messages.split_telegram_messages`` rather than in the send task, and
which the send-side test deliberately stopped short of (test_alert_send_path.py's
``assert len(posts) == 1`` names it as out of its scope):

* every part of the retry summarises the WHOLE digest, not the leftovers;
* the parts continue the reader's numbering instead of restarting at one;
* a lone remaining message still says it is the last;
* the continued marker is reserved for, so it cannot overflow the ceiling;
* the send task hands the splitter both facts, and the part count it reads
  them from advances once per ACCEPTED message.

The other half — the ``_assert_egress_allowed`` docstring — is a
documentation correction with no behaviour to pin, and the behaviour it now
describes is already covered by test_alert_message_content.py
(``test_the_digest_send_helper_refuses_a_demo_project_on_its_own`` and
``test_the_backstop_refuses_egress_rather_than_refusing_demos``). A second copy
here would go green whatever the docstring said.

``_build_delivery_snapshot``'s ``app_base_url`` paragraph (crosscut-6) is the
same shape of correction. It claimed to be "the SECOND ``_build_item_paths``
call" when ``dispatch._create_deliveries`` freezes the snapshot right after the
flush and BEFORE the loop that mints the typed rows, and it justified the
required argument by the snapshot not "disagree[ing] with the rows beside it
about the same item" — which they always do for an ordinary scope, because the
snapshot passes no ``correlation_group_id`` and the row call does, so the two
take different arms of ``_build_item_paths``. The paragraph now states the
reason that holds (one read of the setting, so the two encodings cannot
disagree about the BASE) and names the shape divergence as deliberate. The
divergence itself is already pinned by test_alert_dispatch_write_path.py's
``test_the_typed_items_and_the_frozen_snapshot_cannot_disagree_about_a_link``,
which counts six links for two items — two typed audit URLs plus the snapshot's
event-details + monitoring pair each — and asserts they share one base. A test
here would restate that and stay green whatever the docstring said.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from tripl.alert_templates import (
    ALERT_MESSAGE_FORMAT_PLAIN,
    ALERT_MESSAGE_FORMAT_TELEGRAM_HTML,
    DIGEST_ALERT_MESSAGE_TEMPLATES,
)
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_rule import AlertRule
from tripl.models.event import Event, EventStatus
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.models.project import Project
from tripl.tests._sunset_findings import open_sunset_finding
from tripl.tests.test_alerting import _seed_telegram_length_case
from tripl.worker.celery_app import celery_app
from tripl.worker.tasks import alerts_messages as am
from tripl.worker.tasks.alerts import (
    TELEGRAM_DELIVERED_ITEM_IDS_KEY,
    TELEGRAM_PARTS_DELIVERED_KEY,
    _read_delivered_part_count,
    _record_delivered_items,
    check_deprecated_sunset_events,
    send_alert_delivery,
)
from tripl.worker.tasks.alerts_messages import (
    _build_plan_digest_message,
    _build_sunset_alert_message,
)

_DIGEST = "tripl.worker.tasks.alerts_digest"
_BEAT_ENTRY = "check-deprecated-sunset-events"
_TASK_NAME = "tripl.worker.tasks.alerts.check_deprecated_sunset_events"

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_sunset.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _seed_project(session: Session, *, name: str = "Checkout") -> tuple[Project, PlanBranch]:
    """A real project, its main branch, and the one destination shape the task sends to.

    The main branch is inserted explicitly rather than left to
    ``models.plan_branch.default_branch_id``: every event below names its branch,
    so that column default never fires and there would otherwise be no
    ``kind="main"`` row for the builder to resolve.
    """
    project = Project(
        id=uuid.uuid4(),
        name=name,
        slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}",
        description="",
        is_demo=False,
    )
    main = PlanBranch(
        id=uuid.uuid4(),
        project_id=project.id,
        name="main",
        kind=BranchKind.main.value,
        status=BranchStatus.merged.value,
        description="",
    )
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type=AlertDestinationType.slack.value,
        name=f"{name} Slack",
        enabled=True,
        webhook_url_encrypted="fake-secret",
    )
    session.add_all([project, main, destination])
    session.commit()
    return project, main


def _open_working_branch(
    session: Session, project: Project, *, name: str = "q4-cleanup"
) -> PlanBranch:
    """A branch under review — draft, never merged, holding its own event rows."""
    branch = PlanBranch(
        id=uuid.uuid4(),
        project_id=project.id,
        name=name,
        kind=BranchKind.working.value,
        status=BranchStatus.draft.value,
        description="",
    )
    session.add(branch)
    session.commit()
    return branch


def _add_event(
    session: Session,
    project: Project,
    branch: PlanBranch,
    *,
    name: str,
    sunset_at: datetime,
    last_seen_at: datetime,
) -> Event:
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        branch_id=branch.id,
        # FK not enforced under sqlite, and neither builder joins event_types.
        event_type_id=uuid.uuid4(),
        name=name,
        description="",
        status=EventStatus.deprecated,
        sunset_at=sunset_at,
        last_seen_at=last_seen_at,
    )
    session.add(event)
    session.commit()
    return event


def _overdue(session: Session, project: Project, branch: PlanBranch, *, name: str) -> Event:
    """Deprecated, sunset two months ago, still receiving data yesterday.

    On the main branch it also gets the open finding the daily sunset watch
    writes for it. A branch copy gets none: the sweep only judges main events.
    """
    event = _add_event(
        session,
        project,
        branch,
        name=name,
        sunset_at=NOW - timedelta(days=60),
        last_seen_at=NOW - timedelta(days=1),
    )
    if branch.kind == BranchKind.main.value:
        session.add(open_sunset_finding(event, at=NOW))
        session.commit()
    return event


def _branch_copy(session: Session, project: Project, branch: PlanBranch, source: Event) -> Event:
    """What opening a branch does to an event row: a new id, everything else carried.

    Mirrors ``plan_branch_service``'s copy, which reproduces ``status``,
    ``sunset_at`` and ``last_seen_at`` verbatim, so a query over events alone
    would count this row again. It carries no finding of its own.
    """
    return _add_event(
        session,
        project,
        branch,
        name=source.name,
        sunset_at=source.sunset_at,
        last_seen_at=source.last_seen_at,
    )


def _capture_sends(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the message at the CHANNEL, one level below the egress guard.

    ``alerts_digest._send_digest_to_destination`` is where the
    ``_assert_egress_allowed`` backstop lives, so stubbing that wrapper — as the
    older sunset tests in test_alerting.py do — would take the guard out of the
    path. Patching the channel function it delegates to leaves every check in
    place and still stops short of a socket.
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


def _digest_sunset_count(message: str) -> int:
    """The weekly digest's own overdue counter, read back out of its line."""
    match = re.search(r"^- Deprecated events still receiving data: (\d+)$", message, re.MULTILINE)
    assert match is not None, f"the digest no longer carries its sunset line:\n{message}"
    return int(match.group(1))


def _alert_count(message: str) -> int:
    match = re.search(r"^Count: (\d+)$", message, re.MULTILINE)
    assert match is not None, f"the alert no longer carries its count line:\n{message}"
    return int(match.group(1))


# ---------------------------------------------------------------------------
# (a) Scheduling — the task has a caller at all.
# ---------------------------------------------------------------------------


def test_the_sunset_alert_is_scheduled_and_the_name_it_is_scheduled_under_is_registered() -> None:
    """Three separate reverts, three separate assertions.

    Delete the ``check-deprecated-sunset-events`` block from
    ``celery_app.beat_schedule`` and the first assertion fails — that block is
    the entire fix for half (a), and without it the task is unreachable again
    with every other gate still green.

    Delete the ``from tripl.worker.tasks.alerts_digest import ...`` line in
    worker/tasks/alerts.py as an unused re-export and the REGISTRATION
    assertion fails: the task is defined in alerts_digest.py under a
    ``tripl.worker.tasks.alerts.*`` name, and celery_app.py's import block names
    ``tasks.alerts`` and never ``tasks.alerts_digest``, so that import is what
    puts it in the registry. (``test_celery_dispatch`` makes the same check
    across the whole schedule; here it is pinned against this one name, because
    this is the entry that made the indirection load-bearing.)

    Change the cadence and the last assertion fails. It is pinned, not
    bracketed, because the task carries no per-event suppression state — every
    run rebuilds the same message from scratch, the true count and the same
    capped page of names (section (c)) — so the schedule IS how often an
    operator is told, and the argument for daily is written out beside the
    entry. Moving it should mean editing that argument too.
    """
    schedule = celery_app.conf.beat_schedule
    assert _BEAT_ENTRY in schedule, (
        "check_deprecated_sunset_events is registered and callable but nothing "
        "calls it; a task with no caller is dead code"
    )

    entry = schedule[_BEAT_ENTRY]
    assert entry["task"] == _TASK_NAME
    assert _TASK_NAME in celery_app.tasks, (
        "beat names a task the worker will not have registered at run time"
    )
    schedule = entry["schedule"]
    assert schedule.hour == {6}
    assert schedule.minute == {0}
    assert schedule.day_of_week == set(range(7))


def test_the_sunset_alert_is_not_scheduled_at_the_weekly_digests_own_cadence() -> None:
    """The two tasks send to the same destinations and share a counter.

    At one cadence the detailed alert stops being a follow-up to the digest line
    and becomes a second copy of it in the same week. This states the relation
    the beat comment argues for — strictly more often than the digest — so the
    day someone "aligns" the two schedules the reason they diverge is in front
    of them.
    """
    sunset = celery_app.conf.beat_schedule[_BEAT_ENTRY]["schedule"]
    weekly = celery_app.conf.beat_schedule["send-weekly-plan-digest"]["schedule"]
    assert sunset.day_of_week == set(range(7))
    assert weekly.day_of_week == {1}


# ---------------------------------------------------------------------------
# (b) Branch scope — the alert describes the live plan, once.
# ---------------------------------------------------------------------------


def test_an_open_working_branch_does_not_duplicate_the_overdue_list(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """One event, one open branch, and the count that used to read 2.

    Rebuild ``_build_sunset_alert_message`` over events instead of findings,
    without a branch scope, and both assertions fail together: the header reads
    "Count: 2" and ``app:old_purchase`` is listed twice, from one event that
    exists once in the live plan. Every extra open branch adds another copy.

    The line count is asserted as well as the header because they fail
    independently — a fix that deduplicated the rendered lines while leaving the
    query unscoped would still print the wrong total.
    """
    with sync_session_factory() as session:
        project, main = _seed_project(session)
        source = _overdue(session, project, main, name="app:old_purchase")
        branch = _open_working_branch(session, project)
        _branch_copy(session, project, branch, source)

        message = _build_sunset_alert_message(session, project=project, now=NOW)

    assert message is not None
    assert _alert_count(message) == 1
    assert message.count("app:old_purchase") == 1


def test_a_sunset_pulled_forward_inside_a_branch_raises_no_alert(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A working branch is a PROPOSAL; the alert is about the plan in production.

    Here the branch is the only place the event is overdue: main still sunsets
    it a month from now, and someone reviewing a retirement pulled that date
    back inside their branch. Nothing is wrong with the live plan, so there is
    nothing to alert on — unscoped, the task pages an operator about a date
    nobody has agreed to yet, and the weekly digest (already main-scoped) would
    flatly contradict it.

    Read events across branches instead of the sweep's main-only findings and
    ``message`` is no longer None, which is the whole assertion. This is the
    case a name-based dedup of the test above would not catch.
    """
    with sync_session_factory() as session:
        project, main = _seed_project(session)
        _add_event(
            session,
            project,
            main,
            name="app:legacy_signup",
            sunset_at=NOW + timedelta(days=30),
            last_seen_at=NOW - timedelta(days=1),
        )
        branch = _open_working_branch(session, project)
        _overdue(session, project, branch, name="app:legacy_signup")

        message = _build_sunset_alert_message(session, project=project, now=NOW)

    assert message is None


def test_the_alert_and_the_weekly_digest_report_the_same_overdue_count(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The claim both docstrings now make, on one project, in one session.

    ``_build_plan_digest_message``'s ``sunset_overdue`` and
    ``_build_sunset_alert_message`` read the same open findings through
    ``_sunset_overdue_scope``; the alert exists to name what the digest counts.
    Once only one of them was branch-scoped, so an operator with an open branch
    got a digest saying 1 and an alert saying 2 about the same event — and no
    way to tell which was lying.

    Give either builder a scope of its own and this equality is what fails,
    which is what makes this a test of agreement rather than a second copy of
    the test above.
    """
    with sync_session_factory() as session:
        project, main = _seed_project(session)
        source = _overdue(session, project, main, name="app:old_purchase")
        branch = _open_working_branch(session, project)
        _branch_copy(session, project, branch, source)

        alert = _build_sunset_alert_message(session, project=project, now=NOW)
        digest = _build_plan_digest_message(session, project=project, now=NOW)

    assert alert is not None
    assert _digest_sunset_count(digest) == 1
    assert _alert_count(alert) == _digest_sunset_count(digest)


def test_the_scheduled_task_delivers_the_main_branch_list(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end from beat's entry point, with the branch copy in the database.

    The two halves only matter together: this is the message a real Slack
    destination now receives every day. Running the task rather than the builder
    also keeps the delivery path — the demo filter, the egress backstop, the
    destination type filter — between the fix and the assertion, so a change
    that scoped the query but broke the send still reddens here.

    Lose the branch scope and the delivered text says "Count: 2".
    """
    with sync_session_factory() as session:
        project, main = _seed_project(session)
        source = _overdue(session, project, main, name="app:old_purchase")
        branch = _open_working_branch(session, project)
        _branch_copy(session, project, branch, source)

    messages = _capture_sends(monkeypatch)
    monkeypatch.setattr(f"{_DIGEST}._get_sync_session", sync_session_factory)

    result = check_deprecated_sunset_events.run()

    assert result == {"destinations_checked": 1, "sent": 1, "failed": 0}
    assert len(messages) == 1
    assert _alert_count(messages[0]) == 1
    assert messages[0].count("app:old_purchase") == 1


# ---------------------------------------------------------------------------
# (c) Size — the daily message is bounded, and says when it was cut.
# ---------------------------------------------------------------------------

# Slack refuses a ``text`` field longer than this. It refuses it outright rather
# than truncating, and ``check_deprecated_sunset_events`` turns the resulting
# raise into a ``logger.warning`` and a ``failed`` tally — so an oversized alert
# does not arrive clipped, it does not arrive at all, and only the worker log
# says so. Declared here rather than imported: no production code needs the
# number, only the argument for the cap does.
_SLACK_TEXT_MAX_CHARS = 40_000


def test_the_overdue_list_is_capped_and_says_how_many_it_is_not_showing(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Three events past the cap; three reverts, three assertions.

    Drop ``.limit(_SUNSET_ALERT_MAX_EVENTS)`` from the row query in
    ``_build_sunset_alert_message`` and the line-count assertion fails: the
    message names all 53 again, and with it the last event, which is the
    unbounded render this task now posts to every Slack and email destination
    daily.

    Drop the "… and N more not shown" tail and only the last assertion fails.
    That is the one worth having separately: a capped list with no tail reads
    exactly like a complete one, so the operator acts on 50 of 53 events
    believing they have seen the lot.

    Put ``Count:`` back on ``len(overdue_events)`` instead of its own COUNT(*)
    and the first assertion fails — the header would say 50 — while
    ``test_the_alert_and_the_weekly_digest_report_the_same_overdue_count``
    above stays green, because that project only ever has one event.
    """
    extra = 3
    total = am._SUNSET_ALERT_MAX_EVENTS + extra
    with sync_session_factory() as session:
        project, main = _seed_project(session)
        for index in range(total):
            # Zero-padded so lexical order is numeric order: the builder sorts
            # by name, so the rows the cap drops are exactly the last ``extra``.
            _overdue(session, project, main, name=f"app:legacy_{index:03d}")

        message = _build_sunset_alert_message(session, project=project, now=NOW)

    assert message is not None
    assert _alert_count(message) == total
    named = [line for line in message.splitlines() if line.startswith("- ")]
    assert len(named) == am._SUNSET_ALERT_MAX_EVENTS
    assert f"app:legacy_{total - 1:03d}" not in message
    assert f"… and {extra} more not shown" in message


def test_the_capped_message_fits_slacks_text_field_at_the_widest_event_name(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The cap's NUMBER, checked against the ceiling the constant argues from.

    ``Event.name`` is ``String(500)``, so the widest message the cap can
    produce is its own value times a line of a little over 500 characters.
    Raise ``_SUNSET_ALERT_MAX_EVENTS`` past about seventy and this fails, which
    is the whole point of stating it here: the comment beside the constant
    reasons about this ceiling, and a later "50 is stingy, make it 500" would
    otherwise restore the silent-non-delivery failure with every other test in
    this file still green.

    One event beyond the cap, so the tail is rendered and counted too.
    """
    with sync_session_factory() as session:
        project, main = _seed_project(session)
        for index in range(am._SUNSET_ALERT_MAX_EVENTS + 1):
            _overdue(session, project, main, name=f"{index:03d}{'x' * 497}")

        message = _build_sunset_alert_message(session, project=project, now=NOW)

    assert message is not None
    assert len(message) < _SLACK_TEXT_MAX_CHARS


# ---------------------------------------------------------------------------
# What a RESUMED Telegram digest says about itself.
# ---------------------------------------------------------------------------

_TG = ALERT_MESSAGE_FORMAT_TELEGRAM_HTML

# The production item shape: the long URL rides inside a link label, which
# Telegram counts as free, so items stay compact and the ceilings below stay
# small enough for a reader of this file to hold in their head.
_ITEM_URL = (
    "https://tripl.example.com/p/acme-ios/alerting/"
    "c33ed139-da4d-429f-8ee9-f9c61e67d02c?item=event:12e1e41c&incident=514450c0"
)

# Buckets an hour apart, so the WHOLE digest's window is strictly wider than
# any tail of it. A header summarised over the remainder gets this wrong in a
# way that is visible without counting anything.
_FIRST_BUCKET_HOUR = 8

# 08:00–19:00 UTC over twelve items, which is 11:00–22:00 in Moscow. Written
# out rather than derived because it is the string the reader sees; the
# remainder-scoped window this issue is about starts later on the same day.
_WHOLE_WINDOW = "Sep 06, 11:00–22:00 Europe/Moscow"


def _digest_delivery(count: int):
    """A Telegram digest delivery held in memory, in the layout the product ships."""
    items = [
        AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=uuid.uuid4(),
            scope_type="event",
            scope_ref=str(uuid.uuid4()),
            scope_name=f"main:tap:snippet:{index:03d}",
            bucket=datetime(2026, 9, 6, _FIRST_BUCKET_HOUR + index, tzinfo=UTC),
            direction="drop",
            actual_count=15403,
            expected_count=32048,
            absolute_delta=16645,
            percent_delta=51.9,
            details_path=_ITEM_URL,
            monitoring_path=None,
        )
        for index in range(count)
    ]
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        type=AlertDestinationType.telegram,
        name="All",
    )
    rule = AlertRule(id=uuid.uuid4(), destination_id=destination.id, name="TG", message_format=_TG)
    delivery = AlertDelivery(
        id=uuid.uuid4(),
        project_id=destination.project_id,
        scan_config_id=uuid.uuid4(),
        destination_id=destination.id,
        rule_id=rule.id,
        channel=AlertDestinationType.telegram,
        matched_count=len(items),
    )
    delivery.items = items
    project = Project(id=delivery.project_id, name="acme-ios", slug="acme-ios", description="")
    return delivery, destination, rule, project, items


def _render_remainder(delivery, destination, rule, project, remainder, *, summary_items):
    """The unsplit render ``send_alert_delivery`` performs before it splits.

    Passing ``summary_items`` here is the landed half of that fix; this file is
    about what happens to that answer when the result then has to be split.
    """
    return am._render_delivery_message(
        delivery,
        destination=destination,
        rule=rule,
        scan_name="Snowplow Events (iOS)",
        project=project,
        items=remainder,
        summary_items=summary_items,
        digest=True,
        project_timezone="Europe/Moscow",
    )


def _resume_parts(
    delivery,
    destination,
    rule,
    project,
    remainder,
    *,
    summary_items,
    part_offset,
    max_chars,
):
    """Split that render exactly as the send task's Telegram arm does."""
    message, fmt = _render_remainder(
        delivery, destination, rule, project, remainder, summary_items=summary_items
    )
    return am.split_telegram_messages(
        delivery,
        destination=destination,
        rule=rule,
        scan_name="Snowplow Events (iOS)",
        project=project,
        message=message,
        message_format=fmt,
        items=remainder,
        summary_items=summary_items,
        digest=True,
        part_offset=part_offset,
        max_chars=max_chars,
        project_timezone="Europe/Moscow",
    )


def _visible(text: str) -> str:
    """What Telegram shows once it has parsed the entities away."""
    return unescape(re.sub(r"</?[a-zA-Z][^>]*>", "", text))


def _headlines(parts) -> list[str]:
    return [_visible(text).splitlines()[0] for text, _items in parts]


def _window_lines(parts) -> list[str]:
    return [_visible(text).splitlines()[1] for text, _items in parts]


def _markers(parts) -> list[str]:
    return [line.rsplit(am._WINDOW_LABEL_SEPARATOR, 1)[-1] for line in _window_lines(parts)]


def test_every_part_of_a_resumed_digest_summarises_the_whole_digest() -> None:
    """A retry that needs two messages must not summarise the leftovers twice.

    ``send_alert_delivery`` already works out the whole digest and hands it to
    the unsplit render. ``split_telegram_messages`` then RE-RENDERS every part
    it packs, and it used to derive the summary from the items it was handed —
    which on a resume is the remainder — throwing that answer away again. The
    reader ends up holding "1/3" and "2/3" saying "12 alerts" over an 11:00
    window, then two more messages saying "8 alerts" over a 15:00 one.

    The line under test is ``summary_items=summarised_items if digest else
    None`` in ``render_part``. Put ``delivery_items`` back in its place and both
    header assertions fail: the headline reads "8 alerts" and the window opens
    at 15:00. Deleting the parameter from the signature instead makes this a
    TypeError, which is the same revert caught earlier.

    The body assertion holds either way, deliberately: a "fix" that summarised
    the whole digest by re-listing it would be no fix, and this is where that
    would show.
    """
    delivery, destination, rule, project, items = _digest_delivery(12)
    remainder = items[4:]

    parts = _resume_parts(
        delivery,
        destination,
        rule,
        project,
        remainder,
        summary_items=list(items),
        part_offset=2,
        max_chars=400,
    )

    # Fixture guard, not the contract: with one part the header comes from the
    # caller's own render and this proves nothing about the splitter.
    assert len(parts) > 1, "the remainder must need more than one message"
    assert all("12 alerts" in headline for headline in _headlines(parts)), _headlines(parts)
    assert not any("8 alerts" in headline for headline in _headlines(parts)), _headlines(parts)
    assert all(line.startswith(_WHOLE_WINDOW) for line in _window_lines(parts)), _window_lines(
        parts
    )
    # ...while each part still lists only its own share, and between them the
    # remainder exactly once.
    carried = [item.scope_name for _text, part_items in parts for item in part_items]
    assert carried == [item.scope_name for item in remainder]


def test_a_resumed_digest_numbers_its_parts_after_the_ones_already_sent() -> None:
    """Restarting at "1/2" reads as a second digest, not the rest of one.

    The reader already has two messages of this delivery, numbered 1/3 and 2/3
    by the attempt that failed. The retry's two messages are the third and
    fourth they will hold.

    The line under test is ``part_label=f"{part_offset + index + 1}/{part_offset
    + len(parts)}"``. Drop either ``part_offset`` term and the markers come back
    as ``["1/2", "2/2"]`` — which is what the control below asserts a FIRST
    attempt still produces, so the offset cannot be faked by numbering every
    digest from somewhere else.
    """
    delivery, destination, rule, project, items = _digest_delivery(12)
    remainder = items[4:]

    resumed = _resume_parts(
        delivery,
        destination,
        rule,
        project,
        remainder,
        summary_items=list(items),
        part_offset=2,
        max_chars=400,
    )
    first_attempt = _resume_parts(
        delivery,
        destination,
        rule,
        project,
        remainder,
        summary_items=list(items),
        part_offset=0,
        max_chars=400,
    )

    assert _markers(resumed) == ["3/4", "4/4"]
    assert _markers(first_attempt) == ["1/2", "2/2"]


def test_the_last_remaining_message_of_a_resumed_digest_says_it_is_the_last() -> None:
    """One message is still SOME message once earlier ones exist.

    The no-split shortcut returns the caller's already-rendered text untouched,
    and that text carries no marker — so before this change the final message of
    a recovered digest arrived unnumbered under a "1/3" and a "2/3", leaving the
    reader with no way to know the digest had finished. A first attempt that
    fits in one message must still carry nothing, which is the control: "1/1" is
    noise on the overwhelming majority of digests.

    Two separate returns hand back a lone part, and each gets its own case,
    because reverting either one alone must redden something. Restore the
    unconditional ``return [(message, list(delivery_items))]`` and the first
    marker assertion fails. Restore ``if not digest or len(parts) == 1`` and
    only the LAST case fails — the shortcut is what the first two go through,
    so they stay green and prove nothing about the packer's return.

    Reaching that packer with one part takes the one ceiling where the shortcut
    declines: high enough for the text, too low once the marker is on it. The
    result is over the ceiling and Telegram will refuse it, which is the
    documented fate of any single item that cannot be split — the marker does
    not cause that and must not be dropped to disguise it.
    """
    delivery, destination, rule, project, items = _digest_delivery(12)
    remainder = items[11:]

    resumed = _resume_parts(
        delivery,
        destination,
        rule,
        project,
        remainder,
        summary_items=list(items),
        part_offset=2,
        max_chars=4096,
    )
    first_attempt = _resume_parts(
        delivery,
        destination,
        rule,
        project,
        remainder,
        summary_items=list(items),
        part_offset=0,
        max_chars=4096,
    )

    assert len(resumed) == 1
    assert _markers(resumed) == ["3/3"]
    # The whole digest is still what it describes, unsplit or not.
    assert "12 alerts" in _headlines(resumed)[0]
    assert len(first_attempt) == 1
    assert _window_lines(first_attempt) == [_WHOLE_WINDOW], "a lone first part must carry no marker"

    # The same single item at the one ceiling the shortcut declines, so the
    # packer returns the lone part instead.
    message, fmt = _render_remainder(
        delivery, destination, rule, project, remainder, summary_items=list(items)
    )
    marker_cost = len(am._WINDOW_LABEL_SEPARATOR) + len("3/3")
    packed = _resume_parts(
        delivery,
        destination,
        rule,
        project,
        remainder,
        summary_items=list(items),
        part_offset=2,
        max_chars=am.telegram_visible_length(message, fmt) + marker_cost - 1,
    )
    assert len(packed) == 1, "one item cannot be split, so this is the packer's lone-part return"
    assert _markers(packed) == ["3/3"]


def test_the_continued_marker_never_pushes_a_part_over_the_ceiling() -> None:
    """The reserve has to grow with the offset, or the marker overflows.

    The packer measures each part against a budget short by the widest marker
    it could stamp, because the marker is only stamped once packing has
    finished. ``part_offset`` widens that marker — eight items numbered from 97
    are stamped "98/99" where the item count alone reserved for "8/8" — so an
    offset added without widening the reserve re-opens exactly the hazard
    test_alert_digest_rendering.py's ``test_the_part_marker_never_pushes_a_part
    _over_the_ceiling`` closes for first attempts.

    296 rather than a round number, and offset 97 rather than the 2 the cases
    above use, for the reason that test picks 336. The deficit here is two
    units, so it can only overflow a part that packing left within two units of
    the ceiling. Sweeping ceilings 290-349 at offsets 2, 9, 97 and 998 without
    the widened reserve, exactly 296, 297, 342 and 343 overflow and only at
    offsets of 9 and up — at offsets 2 and 4 the marker never grows past "8/8"
    and there is no deficit to find. A sweep at a small offset, or in steps of
    ten, is green against the broken code and pins nothing; this one was,
    before it was measured.

    Revert ``widest_part = part_offset + len(delivery_items)`` to
    ``len(delivery_items)`` and the pinned assertion fails by those two units.
    """
    delivery, destination, rule, project, items = _digest_delivery(12)
    lone = items[8:]
    message, fmt = _render_remainder(
        delivery, destination, rule, project, lone, summary_items=list(items)
    )
    base = am.telegram_visible_length(message, fmt)
    marker_cost = len(am._WINDOW_LABEL_SEPARATOR) + len("3/3")

    fits = _resume_parts(
        delivery,
        destination,
        rule,
        project,
        lone,
        summary_items=list(items),
        part_offset=2,
        max_chars=base + marker_cost,
    )
    assert _markers(fits) == ["3/3"]
    assert am.telegram_visible_length(fits[0][0], fmt) <= base + marker_cost

    # One unit short: the marker no longer fits, so it splits rather than
    # sending something Telegram would refuse.
    splits = _resume_parts(
        delivery,
        destination,
        rule,
        project,
        lone,
        summary_items=list(items),
        part_offset=2,
        max_chars=base + marker_cost - 1,
    )
    assert len(splits) > 1
    for text, _part_items in splits:
        assert am.telegram_visible_length(text, fmt) <= base + marker_cost - 1

    remainder = items[4:]
    pinned = _resume_parts(
        delivery,
        destination,
        rule,
        project,
        remainder,
        summary_items=list(items),
        part_offset=97,
        max_chars=296,
    )
    assert len(pinned) > 1, "a single part would never have been measured against the budget"
    for text, _part_items in pinned:
        assert am.telegram_visible_length(text, fmt) <= 296, _markers(pinned)

    # Every ceiling in the band, because which ones sit within the deficit of a
    # part boundary is a property of the rendered text, not something to guess.
    for part_offset in (2, 9, 97, 998):
        for max_chars in range(290, 350):
            parts = _resume_parts(
                delivery,
                destination,
                rule,
                project,
                remainder,
                summary_items=list(items),
                part_offset=part_offset,
                max_chars=max_chars,
            )
            for text, _part_items in parts:
                assert am.telegram_visible_length(text, fmt) <= max_chars, (
                    part_offset,
                    max_chars,
                )


def test_the_send_task_tells_the_splitter_the_whole_digest_and_where_to_resume(
    tmp_path,
    monkeypatch,
) -> None:
    """The two facts only ``send_alert_delivery`` can supply, pinned at the call site.

    The cases above prove the splitter USES them; nothing there would notice the
    send task quietly passing neither. Rather than build a remainder big enough
    to need two real messages, the splitter is replaced by a recorder — what is
    under test is the arguments, and the packing is somebody else's test.

    Delete ``summary_items=_digest_summary_items(...)`` from the split call and
    the summary assertion fails; delete ``part_offset=already_delivered_parts``
    and the offset one does. ``telegram_message_parts`` is seeded to a DIFFERENT
    number on purpose: it is the nearest-looking key, it is what the filed issue
    proposed reading, and reading it would make the offset 3.

    The final assertion is the other end of the same wire — the count the next
    resume would read has advanced by the one message this attempt got
    accepted. Drop the ``TELEGRAM_PARTS_DELIVERED_KEY`` line from
    ``_record_delivered_items`` and it stays at 2.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_resume_split.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    delivery_id, scope_names = _seed_telegram_length_case(
        factory,
        item_count=12,
        # The product's own digest layout, not a lookalike.
        message_template=DIGEST_ALERT_MESSAGE_TEMPLATES[ALERT_MESSAGE_FORMAT_PLAIN],
    )
    with factory() as session:
        delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert delivery is not None
        items = sorted(delivery.items, key=lambda item: item.scope_name)
        delivery.payload_snapshot = {
            "digest": True,
            TELEGRAM_DELIVERED_ITEM_IDS_KEY: sorted(str(item.id) for item in items[:8]),
            TELEGRAM_PARTS_DELIVERED_KEY: 2,
            "telegram_message_parts": 3,
        }
        session.commit()
        pending_names = {item.scope_name for item in items[8:]}

    calls: list[dict] = []

    def recording_split(delivery, **kwargs):
        calls.append(kwargs)
        return [("resumed body", list(kwargs["items"]))]

    monkeypatch.setitem(send_alert_delivery.run.__globals__, "_get_sync_session", factory)
    monkeypatch.setitem(
        send_alert_delivery.run.__globals__,
        "split_telegram_messages",
        recording_split,
    )
    monkeypatch.setitem(
        send_alert_delivery.run.__globals__,
        "_post_json",
        lambda url, body, headers=None: None,
    )

    result = send_alert_delivery.run(delivery_id)

    assert result["status"] == "sent"
    assert len(calls) == 1
    kwargs = calls[0]
    # It packs the remainder...
    assert {item.scope_name for item in kwargs["items"]} == pending_names
    # ...and is told the whole delivery is what the header describes.
    assert kwargs["summary_items"] is not None
    assert len(kwargs["summary_items"]) == len(scope_names)
    # ...and where the reader's numbering had got to.
    assert kwargs["part_offset"] == 2

    with factory() as session:
        persisted = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert persisted is not None
        snapshot = persisted.payload_snapshot
        assert isinstance(snapshot, dict)
        assert snapshot[TELEGRAM_PARTS_DELIVERED_KEY] == 3
        assert snapshot["telegram_message_parts"] == 3, "the plan must not be overwritten by it"

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_the_part_count_advances_once_per_accepted_message(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """One call, one accepted message, one increment — whatever it carried.

    The marker counts MESSAGES and the resume point counts ITEMS, and the two
    parts here carry different numbers of items so a count taken off
    ``delivered_ids`` would read 4 rather than 2.

    The reader's guards are pinned beside it because each is a real snapshot a
    hand-edit or an older row can produce, and the wrong answer is a mislabelled
    message rather than a crash: ``telegram_message_parts`` is the plan and not
    this, ``True`` is an ``int`` subclass that would pass a naive check, and a
    negative would number a message backwards.
    """
    with sync_session_factory() as session:
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            scan_config_id=uuid.uuid4(),
            destination_id=uuid.uuid4(),
            rule_id=uuid.uuid4(),
            channel=AlertDestinationType.telegram,
            matched_count=4,
            payload_snapshot={"digest": True},
        )
        session.add(delivery)
        session.commit()

        items = [
            AlertDeliveryItem(
                id=uuid.uuid4(),
                delivery_id=delivery.id,
                scope_type="event",
                scope_ref=f"event-{index}",
                scope_name=f"main:tap:snippet:{index:03d}",
                bucket=NOW,
                direction="drop",
                actual_count=1.0,
                expected_count=2.0,
                absolute_delta=1.0,
                percent_delta=50.0,
            )
            for index in range(4)
        ]
        delivered_ids: set[uuid.UUID] = set()
        snapshot: dict = {"digest": True, "telegram_message_parts": 7}

        snapshot = _record_delivered_items(
            session,
            delivery,
            payload_snapshot=snapshot,
            delivered_ids=delivered_ids,
            items=items[:3],
        )
        assert snapshot[TELEGRAM_PARTS_DELIVERED_KEY] == 1
        assert len(snapshot[TELEGRAM_DELIVERED_ITEM_IDS_KEY]) == 3

        snapshot = _record_delivered_items(
            session,
            delivery,
            payload_snapshot=snapshot,
            delivered_ids=delivered_ids,
            items=items[3:],
        )

    assert snapshot[TELEGRAM_PARTS_DELIVERED_KEY] == 2
    assert len(snapshot[TELEGRAM_DELIVERED_ITEM_IDS_KEY]) == 4
    assert _read_delivered_part_count(snapshot) == 2
    # The plan is left where it was, and is not mistaken for the count.
    assert snapshot["telegram_message_parts"] == 7
    assert _read_delivered_part_count({"telegram_message_parts": 7}) == 0
    assert _read_delivered_part_count({TELEGRAM_PARTS_DELIVERED_KEY: True}) == 0
    assert _read_delivered_part_count({TELEGRAM_PARTS_DELIVERED_KEY: -3}) == 0
    assert _read_delivered_part_count({TELEGRAM_PARTS_DELIVERED_KEY: "2"}) == 0
    assert _read_delivered_part_count(None) == 0
