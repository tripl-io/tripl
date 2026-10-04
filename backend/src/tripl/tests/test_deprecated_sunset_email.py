"""What the sunset alert's EMAIL says it is.

``check_deprecated_sunset_events`` does not resolve a destination of its own:
it reuses ``alerts_channels._send_digest_to_destination``, the helper written
for ``send_weekly_plan_digest``. That helper hardcoded
``subject=f"[{project.name}] Weekly tripl digest"``, so once the sunset alert
was put on a DAILY beat (celery_app.py's ``check-deprecated-sunset-events``)
every one of its emails arrived titled "Weekly tripl digest" — wrong about the
cadence, wrong about the contents, and threaded by the reader's mail client
into the weekly digest's conversation, which is where an operator looks for
last week's summary rather than for today's work item.

The subject is now the caller's, with the weekly title as the default, so the
three things worth pinning are:

* the weekly digest's subject is byte-identical to the one it always sent —
  this is the path that was NOT meant to change;
* the sunset alert, run end to end, carries its own subject instead;
* Slack, which has no subject at all, is untouched by the new parameter.

The ``[project]`` prefix stays inside the helper, so a caller can only choose
the title. That is deliberate and is asserted below on both paths: two beats
that could each invent a subject SHAPE would eventually disagree about one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from tripl.alert_templates import ALERT_MESSAGE_FORMAT_PLAIN
from tripl.config import SMTP_SECURITY_STARTTLS
from tripl.crypto import encrypt_value
from tripl.models import Base
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.event import Event, EventStatus
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.models.project import Project
from tripl.services import app_settings_service

# Enter the worker package through ``celery_app``. It is what binds the task
# names before the task modules import one another, so importing
# ``tripl.worker.tasks.alerts_digest`` directly walks into the
# alerts <-> alerts_digest cycle (ImportError: partially initialized module).
from tripl.worker.celery_app import celery_app  # noqa: F401
from tripl.worker.tasks import alerts_channels
from tripl.worker.tasks.alerts_channels import DIGEST_SUBJECT_TITLE
from tripl.worker.tasks.alerts_digest import (
    SUNSET_SUBJECT_TITLE,
    check_deprecated_sunset_events,
)

_DIGEST = "tripl.worker.tasks.alerts_digest"

# The subject the weekly plan digest has sent since it shipped, written out
# rather than composed from ``DIGEST_SUBJECT_TITLE``: this file's first job is
# to notice a change to that string, and a constant on both sides of the
# comparison would follow the change instead of catching it.
_WEEKLY_SUBJECT = "[Checkout] Weekly tripl digest"

_SLACK_WEBHOOK = "https://hooks.slack.com/services/T00000000/B00000000/abcdefghijklmnop"

NOW = datetime.now(UTC)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_sunset_email.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _email_config() -> app_settings_service.EmailConfig:
    """An instance whose SMTP settings are configured and beside the point.

    Every destination below carries its own ``email_from_address``, so the
    global Default From never has to be resolved here — which From: address the
    send path accepts is test_alert_send_path.py's question, not this file's.
    """
    return app_settings_service.EmailConfig(
        smtp_host="relay.example.com",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        smtp_security=SMTP_SECURITY_STARTTLS,
        smtp_from_address="digests@example.com",
    )


def _project(name: str = "Checkout") -> Project:
    return Project(
        id=uuid.uuid4(),
        name=name,
        slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}",
        description="",
        is_demo=False,
    )


def _email_destination(project_id: uuid.UUID) -> AlertDestination:
    return AlertDestination(
        id=uuid.uuid4(),
        project_id=project_id,
        type=AlertDestinationType.email.value,
        name="Ops Email",
        enabled=True,
        email_recipients="ops@example.com",
        email_from_address="alerts@example.com",
    )


def _slack_destination(project_id: uuid.UUID) -> AlertDestination:
    return AlertDestination(
        id=uuid.uuid4(),
        project_id=project_id,
        type=AlertDestinationType.slack.value,
        name="Ops Slack",
        enabled=True,
        webhook_url_encrypted=encrypt_value(_SLACK_WEBHOOK),
    )


def _seed_overdue_project(session: Session) -> Project:
    """A project whose main branch holds one deprecated event still receiving data.

    The main branch is inserted explicitly and named by the event, the way
    test_deprecated_sunset_alert.py seeds it: ``_build_sunset_alert_message`` resolves the
    ``kind="main"`` row and filters on it, so an event without one is invisible
    and the task would send nothing at all.
    """
    project = _project()
    main = PlanBranch(
        id=uuid.uuid4(),
        project_id=project.id,
        name="main",
        kind=BranchKind.main.value,
        status=BranchStatus.merged.value,
        description="",
    )
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        branch_id=main.id,
        # FK not enforced under sqlite, and the sunset query never joins it.
        event_type_id=uuid.uuid4(),
        name="app:old_purchase",
        description="",
        status=EventStatus.deprecated,
        sunset_at=NOW - timedelta(days=60),
        last_seen_at=NOW - timedelta(days=1),
    )
    session.add_all([project, main, _email_destination(project.id), event])
    session.commit()
    return project


def test_the_weekly_digests_email_subject_is_the_one_it_always_sent() -> None:
    """The path that was not supposed to move, pinned against the parameter.

    Called directly rather than through ``send_weekly_plan_digest`` because the
    destination and the settings are the whole input to the subject; the task
    adds a SELECT and a message builder, neither of which the subject reads.

    Give ``subject_title`` any other default and this fails — which is the only
    way the weekly digest can be broken by a change made for the sunset alert.
    """
    sent: list[dict[str, object]] = []
    project = _project()

    alerts_channels._send_digest_to_destination(
        destination=_email_destination(project.id),
        message="Weekly tripl digest for Checkout",
        project=project,
        email_config=_email_config(),
        send_slack_message=lambda *_args, **_kwargs: None,
        send_email_message=lambda **kwargs: sent.append(kwargs),
    )

    assert len(sent) == 1
    assert sent[0]["subject"] == _WEEKLY_SUBJECT


def test_the_sunset_alert_does_not_arrive_titled_weekly_tripl_digest(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug, end to end: one overdue event, one email destination, one send.

    Stubbed at ``_channel_send_email_message`` — the last hop before smtplib —
    so everything that builds the subject is real: the task's own call, the
    ``alerts_digest`` wrapper with its egress guard, and the channel helper that
    pairs the title with the project prefix. Reverting
    ``subject_title=SUNSET_SUBJECT_TITLE`` at the call site, or the parameter
    itself, puts "Weekly tripl digest" back in ``subject`` here while the body
    still says the alert's own first line — which is exactly what an operator
    received daily.
    """
    with sync_session_factory() as session:
        _seed_overdue_project(session)

    sent: list[dict[str, object]] = []
    monkeypatch.setattr(f"{_DIGEST}._get_sync_session", sync_session_factory)
    monkeypatch.setattr(
        app_settings_service,
        "get_email_config_sync",
        lambda *_args, **_kwargs: _email_config(),
    )
    monkeypatch.setattr(
        f"{_DIGEST}._channel_send_email_message",
        lambda **kwargs: sent.append(kwargs),
    )

    result = check_deprecated_sunset_events.run()

    assert result == {"destinations_checked": 1, "sent": 1, "failed": 0}
    assert len(sent) == 1
    subject = sent[0]["subject"]
    assert subject == f"[Checkout] {SUNSET_SUBJECT_TITLE}"
    assert subject != _WEEKLY_SUBJECT
    assert "Weekly" not in str(subject)
    # The prefix belongs to the helper, not to the caller's title: both beats
    # address the same reader about the same project.
    assert str(subject).startswith("[Checkout] ")
    assert str(sent[0]["body"]).startswith("Deprecated events still receiving data after sunset")


def test_slack_is_untouched_by_the_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Slack destination sent with the sunset title still posts only the message.

    Slack's incoming webhook has no subject field, so the guarantee is a
    negative one: the new parameter must not reach the Slack arm, and nothing
    about the text or the format may move with it. The webhook is decrypted and
    validated for real; only the send-time DNS re-check is stubbed, the way
    test_alerting.py's channel tests stub it — resolving a hostname is not what
    this asserts and would put the suite on the network.
    """
    posted: list[tuple[str, str, str]] = []
    mailed: list[dict[str, object]] = []
    project = _project()

    monkeypatch.setattr(
        alerts_channels,
        "_reject_private_target",
        lambda _url, *, field: None,
    )

    alerts_channels._send_digest_to_destination(
        destination=_slack_destination(project.id),
        message="Deprecated events still receiving data after sunset — Checkout",
        project=project,
        email_config=_email_config(),
        send_slack_message=lambda url, text, *, message_format: posted.append(
            (url, text, message_format)
        ),
        send_email_message=lambda **kwargs: mailed.append(kwargs),
        subject_title=SUNSET_SUBJECT_TITLE,
    )

    assert mailed == [], "a Slack destination reached the email transport"
    assert posted == [
        (
            _SLACK_WEBHOOK,
            "Deprecated events still receiving data after sunset — Checkout",
            ALERT_MESSAGE_FORMAT_PLAIN,
        )
    ]
    # ...and the same destination with the default title posts the same three
    # values, so nothing in the Slack arm reads the parameter at all.
    baseline: list[tuple[str, str, str]] = []
    alerts_channels._send_digest_to_destination(
        destination=_slack_destination(project.id),
        message="Deprecated events still receiving data after sunset — Checkout",
        project=project,
        email_config=_email_config(),
        send_slack_message=lambda url, text, *, message_format: baseline.append(
            (url, text, message_format)
        ),
        send_email_message=lambda **kwargs: mailed.append(kwargs),
        subject_title=DIGEST_SUBJECT_TITLE,
    )

    assert baseline == posted
