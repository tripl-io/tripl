from __future__ import annotations

import logging
import smtplib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.project import Project
from tripl.services import app_settings_service
from tripl.services.active_org_scope import project_in_active_org
from tripl.worker.celery_app import celery_app
from tripl.worker.db import _get_sync_session
from tripl.worker.tasks.alerts_channels import DIGEST_SUBJECT_TITLE
from tripl.worker.tasks.alerts_channels import (
    _post_json as _channel_post_json,
)
from tripl.worker.tasks.alerts_channels import (
    _send_digest_to_destination as _channel_send_digest_to_destination,
)
from tripl.worker.tasks.alerts_channels import (
    _send_email_message as _channel_send_email_message,
)
from tripl.worker.tasks.alerts_channels import (
    _send_slack_message as _channel_send_slack_message,
)
from tripl.worker.tasks.alerts_messages import (
    _build_plan_digest_message,
    _build_sunset_alert_message,
)

logger = logging.getLogger(__name__)

# The sunset alert's own email subject title; the channel helper pairs it with
# the ``[project]`` prefix, exactly as it does the weekly digest's
# ``DIGEST_SUBJECT_TITLE``. Worded as the weekly digest's own counter line
# ("- Deprecated events still receiving data: N"), because this daily message
# is that line expanded and a reader should be able to connect the two from the
# subject alone.
SUNSET_SUBJECT_TITLE = "Deprecated events still receiving data"


def _post_json(
    url: str,
    body: dict[str, object],
    headers: dict[str, str] | None = None,
) -> dict[str, object] | None:
    return _channel_post_json(url, body, headers)


def _send_slack_message(webhook_url: str, text: str, *, message_format: str) -> None:
    _channel_send_slack_message(_post_json, webhook_url, text, message_format=message_format)


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


def _send_digest_to_destination(
    *,
    destination: AlertDestination,
    message: str,
    project: Project,
    email_config: app_settings_service.EmailConfig,
    subject_title: str = DIGEST_SUBJECT_TITLE,
) -> None:
    """Send one digest message, refusing a demo project and a disabled destination first.

    ``subject_title`` is forwarded to the channel helper, which pairs it with
    the ``[project]`` prefix to form the email subject; it defaults to the
    weekly digest's own title, so only a caller that says otherwise moves it.

    Both tasks below already leave demo projects out of their SELECT, so this
    guard is not what stops the send in practice — it is what stops a THIRD
    digest-shaped task added to this module from egressing by forgetting a
    WHERE clause. These two are the only WORKER sends that resolve a
    destination themselves instead of minting an ``AlertDelivery`` and handing
    it to a send task, so they were once the only alert sends
    :func:`_assert_egress_allowed` never saw — which is why that guard's
    docstring no longer claims every dispatch path funnels through a send task,
    and names these two instead. The one other
    destination send that bypasses the send tasks is the **Test** button
    (``services/_alerting_test_send.send_destination_test``), and it stays
    outside this guard on purpose: it answers a question ABOUT a destination
    rather than delivering an alert, so it refuses a demo with an ``ok=False``
    explanation the operator can read instead of raising.

    The ENABLED toggle is re-read here too, and until a later fix
    it was not checked at all: both tasks filter on ``AlertDestination.enabled``
    in their SELECT and that was taken for the guard. It is not one. Each task
    SELECTs every destination in the database up front and then loops, and each
    turn of that loop builds a message out of a dozen plan, drift and anomaly
    queries before reaching this call — so the row a send acts on was last read
    from the database as many message builds ago as there are projects ahead of
    it in the loop. An operator who switches a channel off at 09:00:02, while
    the 09:00 digest is still walking the estate, is not asking to be posted to
    at 09:00:40. :func:`alerts._assert_destination_still_enabled` is what makes
    that a re-read of the toggle rather than a re-check of the instance the
    SELECT loaded, which is a distinction with a difference here: the worker's
    sessions are ``expire_on_commit=False`` and this loop commits nothing.

    Unlike the send tasks BOTH guards run AFTER the message is built rather
    than before, and neither wants moving. These two messages are plain DB
    reads over the project's own rows, so there is no AI round-trip to be saved
    by refusing any earlier — and for the toggle, late is not a compromise but
    the point: what it is looking for is a switch thrown while the message was
    being built.
    """
    # Deferred, for the cycle: ``alerts`` imports this module's two tasks at its
    # own top (they carry ``tripl.worker.tasks.alerts.*`` task names), so a
    # module-level import back into it is a hard cycle. Same workaround, for the
    # same reason, as alert_digest_send.py's.
    from tripl.worker.tasks.alerts import (
        _assert_destination_still_enabled,
        _assert_egress_allowed,
    )

    _assert_egress_allowed(destination, project)
    _assert_destination_still_enabled(destination)
    _channel_send_digest_to_destination(
        destination=destination,
        message=message,
        project=project,
        email_config=email_config,
        send_slack_message=_send_slack_message,
        send_email_message=_send_email_message,
        subject_title=subject_title,
    )


class _OrgEmailConfigs:
    """Each project's ORGANIZATION's relay (F20 PR9), read once per organization per run."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._by_org: dict[uuid.UUID, app_settings_service.EmailConfig] = {}

    def for_project(self, project: Project) -> app_settings_service.EmailConfig:
        org_id = project.organization_id
        if org_id not in self._by_org:
            self._by_org[org_id] = app_settings_service.get_email_config_sync(
                self._session, org_id=org_id
            )
        return self._by_org[org_id]


@celery_app.task(name="tripl.worker.tasks.alerts.send_weekly_plan_digest")  # type: ignore[untyped-decorator]
def send_weekly_plan_digest() -> dict[str, int]:
    session = _get_sync_session()
    sent = 0
    failed = 0
    try:
        email_configs = _OrgEmailConfigs(session)
        rows = session.execute(
            select(Project, AlertDestination)
            .join(AlertDestination, AlertDestination.project_id == Project.id)
            .where(
                # A demo project is zero-egress by construction
                # (website/docs/use/demo-workspace.md): the API refuses to
                # create or enable an external destination on one. This task
                # inherits none of that — it sends to whatever enabled rows
                # exist, including any written before that API guard or by hand
                # against the database. Excluding demos here rather than
                # refusing them row by row also keeps them out of
                # ``destinations_checked`` and off the weekly ``failed`` tally,
                # which is the honest tally: nothing was attempted, and nothing
                # is wrong.
                Project.is_demo.is_(False),
                project_in_active_org(),
                AlertDestination.enabled.is_(True),
                AlertDestination.type.in_(
                    [AlertDestinationType.slack.value, AlertDestinationType.email.value]
                ),
            )
            .order_by(Project.name, AlertDestination.name)
        ).all()
        now = datetime.now(UTC)
        for project, destination in rows:
            message = _build_plan_digest_message(session, project=project, now=now)
            try:
                _send_digest_to_destination(
                    destination=destination,
                    message=message,
                    project=project,
                    email_config=email_configs.for_project(project),
                )
                sent += 1
            except Exception:  # noqa: BLE001
                failed += 1
                logger.warning(
                    "Failed to send weekly digest to destination %s",
                    destination.id,
                    exc_info=True,
                )
        return {"destinations_checked": len(rows), "sent": sent, "failed": failed}
    finally:
        session.close()


@celery_app.task(name="tripl.worker.tasks.alerts.check_deprecated_sunset_events")  # type: ignore[untyped-decorator]
def check_deprecated_sunset_events() -> dict[str, int]:
    """Alert on deprecated events that keep receiving data past their sunset_at.

    Runs daily from beat (``check-deprecated-sunset-events`` in celery_app.py,
    where the cadence is argued). It keeps no per-event state, so a project
    whose overdue list has not changed receives the same message every day —
    intended, not an oversight: the list IS the work item.

    The list is the sunset watch's open ``sunset_overdue`` findings
    (``tasks.lifecycle``, swept daily just before this runs), so an event leaves
    it the way it leaves the catalog chip and the Lifecycle alerts: when its data
    stops for 24 hours, or when the plan retires it or moves its sunset out.

    Its "Count:" agrees with the ``sunset_overdue`` line of the weekly digest
    above, because both read the same findings
    (:func:`_build_sunset_alert_message`). This message is that line expanded:
    the count, then the first ``_SUNSET_ALERT_MAX_EVENTS`` events by name, then
    a tail saying how many it is not showing.

    Its email carries its own subject (``subject_title`` above). Sharing the
    send helper with the weekly digest used to mean sharing its subject too, so
    a daily alert arrived titled "Weekly tripl digest".
    """
    session = _get_sync_session()
    checked = 0
    sent = 0
    failed = 0
    try:
        email_configs = _OrgEmailConfigs(session)
        rows = session.execute(
            select(Project, AlertDestination)
            .join(AlertDestination, AlertDestination.project_id == Project.id)
            .where(
                # Same reasoning as the weekly digest above: this task resolves
                # its destinations the same way and is equally outside the API's
                # demo guard.
                Project.is_demo.is_(False),
                project_in_active_org(),
                AlertDestination.enabled.is_(True),
                AlertDestination.type.in_(
                    [AlertDestinationType.slack.value, AlertDestinationType.email.value]
                ),
            )
            .order_by(Project.name, AlertDestination.name)
        ).all()
        now = datetime.now(UTC)
        for project, destination in rows:
            checked += 1
            message = _build_sunset_alert_message(session, project=project, now=now)
            if message is None:
                continue
            try:
                _send_digest_to_destination(
                    destination=destination,
                    message=message,
                    project=project,
                    email_config=email_configs.for_project(project),
                    subject_title=SUNSET_SUBJECT_TITLE,
                )
                sent += 1
            except Exception:  # noqa: BLE001
                failed += 1
                logger.warning(
                    "Failed to send sunset alert to destination %s",
                    destination.id,
                    exc_info=True,
                )
        return {"destinations_checked": checked, "sent": sent, "failed": failed}
    finally:
        session.close()
