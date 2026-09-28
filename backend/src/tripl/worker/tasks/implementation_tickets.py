"""Celery tasks for branch → implementation ticket automation (tripl-hgez).

``create_implementation_ticket`` opens one Jira or Linear ticket (GH #258) for a
merged branch, covering its added/changed events. ``sync_implementation_tickets``
polls open tickets and, when the tracker reports the issue done (Jira status
category ``done``, Linear state type ``completed``), flips the ticket closed and
marks its covered events ``implemented``. ``comment_seen_in_data`` posts the
auto-live "Seen in production data" comment the scan publishes.

Async-bridge invariant: each sync task builds a THROWAWAY ``NullPool`` async
engine inside the coroutine and disposes it in a ``finally``. asyncpg binds each
connection to the loop that opened it, so the module-global pooled engine cannot
be reused across the fresh ``asyncio.run`` loop each task invocation creates.
The real logic lives in the ``_create_ticket`` / ``_sync_tickets`` coroutines so
tests can drive them against a supplied session without spinning an engine.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.error
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.alerting_validation import (
    validate_jira_api_token,
    validate_jira_auth_email,
    validate_jira_base_url,
    validate_jira_issue_type,
    validate_jira_project_key,
    validate_linear_api_key,
    validate_linear_team_id,
)
from tripl.models.event import Event, EventStatus, event_status_rank
from tripl.models.implementation_ticket import ImplementationTicket
from tripl.models.project_tracker_config import ProjectTrackerConfig
from tripl.services.active_org_scope import in_active_org
from tripl.services.org_tracker_defaults_service import (
    NO_DEFAULTS,
    OrgTrackerDefaults,
    defaults_for_project,
    effective_jira,
    effective_linear,
)
from tripl.worker.celery_app import celery_app
from tripl.worker.db import run_with_async_worker_session
from tripl.worker.tasks.alerts_channels import (
    _find_jira_issue_by_label,
    _get_jira_issue_status,
    _get_json,
    _post_json,
    _reject_private_target,
    _send_jira_issue,
)
from tripl.worker.tasks.tracker_clients import (
    LINEAR_COMPLETED_STATE_TYPE,
    create_linear_issue,
    find_linear_issue_by_marker,
    get_linear_issue_state_type,
)

logger = logging.getLogger(__name__)

# Jira status categories: "new" (to-do), "indeterminate" (in-progress), "done".
_DONE_CATEGORY = "done"

TRACKER_JIRA = "jira"
TRACKER_LINEAR = "linear"


class TransientTrackerError(Exception):
    """A ticket create may succeed after a bounded retry."""


def _is_transient_tracker_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    if isinstance(exc, urllib.error.URLError | TimeoutError | ConnectionError):
        return True
    return isinstance(exc.__cause__, Exception) and _is_transient_tracker_error(exc.__cause__)


def _coerce_uuids(event_ids: list[str]) -> list[uuid.UUID]:
    result: list[uuid.UUID] = []
    for raw in event_ids or []:
        try:
            result.append(uuid.UUID(str(raw)))
        except ValueError, TypeError:
            continue
    return result


async def _load_event_names(session: AsyncSession, event_ids: list[str]) -> list[str]:
    uuids = _coerce_uuids(event_ids)
    if not uuids:
        return []
    rows = await session.execute(select(Event.name).where(Event.id.in_(uuids)).order_by(Event.name))
    return [name for (name,) in rows.all()]


async def _build_ticket_body(session: AsyncSession, event_ids: list[str]) -> str:
    names = await _load_event_names(session, event_ids)
    lines = ["This ticket tracks the implementation of the following events:", ""]
    if names:
        lines.extend(f"- {name}" for name in names)
    else:
        lines.append("- (events pending)")
    return "\n".join(lines)


def _resolve_jira_config(
    config: ProjectTrackerConfig, defaults: OrgTrackerDefaults = NO_DEFAULTS
) -> tuple[str, str, str, str, str] | None:
    """Validate + decrypt a tracker config for outbound use.

    The project's own values, over its organization's defaults (F20 PR12,
    ``org_tracker_defaults_service.effective_jira``: the site, account and
    token come whole from one side). Returns ``(base_url, auth_email,
    api_token, project_key, issue_type)`` or ``None`` when the result is
    incomplete/invalid — the worker logs and skips rather than crashing so one
    bad config can't wedge the beat sweep. The site is re-checked against
    private addresses here, whichever side it came from."""
    target = effective_jira(config, defaults)
    try:
        base_url = validate_jira_base_url(target.base_url)
        auth_email = validate_jira_auth_email(target.auth_email)
        api_token = validate_jira_api_token(target.api_token)
        project_key = validate_jira_project_key(target.project_key)
        issue_type = validate_jira_issue_type(target.issue_type)
    except ValueError:
        return None
    return base_url, auth_email, api_token, project_key, issue_type


def _resolve_linear_config(
    config: ProjectTrackerConfig, defaults: OrgTrackerDefaults = NO_DEFAULTS
) -> tuple[str, str] | None:
    """``(api_key, team_id)`` for a Linear tracker config, or ``None`` when invalid.

    The key is stored exactly as the Jira token is — ``api_token_encrypted``,
    encrypted at rest, owner-gated, never echoed — and the team id rides the
    ``project_key`` column (``project_tracker_config_service``). Either falls
    back to the organization's Linear default (F20 PR12). Same log-and-skip
    contract as ``_resolve_jira_config``.
    """
    target = effective_linear(config, defaults)
    try:
        api_key = validate_linear_api_key(target.api_key)
        team_id = validate_linear_team_id(target.team_id)
    except ValueError:
        return None
    return api_key, team_id


def _branch_marker(branch_uuid: uuid.UUID) -> str:
    """The label a branch's ticket carries, so a later run can find it again.

    Derived from the branch id alone, because that is what "one ticket per
    branch" is keyed on everywhere else — the unique constraint, the existence
    check, the sync sweep. Hex, so it is a single JQL-safe token.
    """
    return f"tripl-branch-{branch_uuid.hex}"


def _find_existing_issue(
    *, base_url: str, auth_email: str, api_token: str, label: str
) -> tuple[str | None, str | None]:
    """Look for an issue this task already created, tolerating a failed search.

    A search error leaves the question unanswered, and the two ways to be wrong
    are not equal: creating a second issue is visible to a human and closable in
    a click, while refusing to create leaves a merged branch with no ticket and
    nothing on any screen to say why. So a failed lookup falls through to the
    create — the behaviour that shipped before this check existed.
    """
    try:
        return _find_jira_issue_by_label(
            _get_json,
            base_url=base_url,
            auth_email=auth_email,
            api_token=api_token,
            label=label,
        )
    except Exception:
        logger.warning(
            "Could not search Jira for an existing issue labelled %s; creating one", label
        )
        return (None, None)


async def _create_ticket(
    session: AsyncSession,
    project_id: str,
    branch_id: str,
    event_ids: list[str],
    summary: str,
) -> None:
    branch_uuid = uuid.UUID(branch_id)
    project_uuid = uuid.UUID(project_id)

    # Idempotency: one ticket per branch. A retry or duplicate enqueue is a no-op.
    existing = await session.scalar(
        select(ImplementationTicket.id).where(ImplementationTicket.branch_id == branch_uuid)
    )
    if existing is not None:
        return

    config = await session.scalar(
        select(ProjectTrackerConfig).where(ProjectTrackerConfig.project_id == project_uuid)
    )
    if config is None or not config.enabled:
        return
    defaults = await defaults_for_project(session, project_uuid)

    if config.tracker_type == TRACKER_LINEAR:
        await _create_linear_ticket(
            session,
            config,
            defaults,
            project_uuid=project_uuid,
            branch_uuid=branch_uuid,
            event_ids=event_ids,
            summary=summary,
        )
        return

    resolved = _resolve_jira_config(config, defaults)
    if resolved is None:
        logger.warning(
            "Skipping implementation ticket for branch %s: tracker config is invalid",
            branch_id,
        )
        return
    base_url, auth_email, api_token, project_key, issue_type = resolved

    # SSRF re-check immediately before the outbound call (DNS-rebinding defense).
    _reject_private_target(base_url, field="Jira base_url")

    # ASK BEFORE CREATING (tripl-l33u.15). The row is committed after the POST,
    # and the worker runs acks_late with a hard time limit that SIGKILLs the
    # child — so a worker killed in between is redelivered, finds no row, and
    # used to open a SECOND Jira issue. Jira's create takes no idempotency key,
    # so nothing local can close that window: the only durable record of the
    # first attempt is the issue itself, and the only way to recognise it is to
    # have labelled it.
    marker = _branch_marker(branch_uuid)
    issue_id, issue_key = _find_existing_issue(
        base_url=base_url, auth_email=auth_email, api_token=api_token, label=marker
    )
    if issue_key is not None:
        logger.info(
            "Adopting existing Jira issue %s for branch %s instead of creating a second one",
            issue_key,
            branch_id,
        )
    else:
        body_text = await _build_ticket_body(session, event_ids)
        try:
            issue_id, issue_key = _send_jira_issue(
                _post_json,
                base_url=base_url,
                auth_email=auth_email,
                api_token=api_token,
                project_key=project_key,
                issue_type=issue_type,
                summary=summary,
                body_text=body_text,
                labels=[marker],
            )
        except Exception as exc:
            if _is_transient_tracker_error(exc):
                raise TransientTrackerError("Temporary Jira ticket creation failure") from exc
            raise

    external_url = f"{base_url}/browse/{issue_key}" if issue_key else ""
    await _persist_ticket(
        session,
        project_uuid=project_uuid,
        branch_uuid=branch_uuid,
        tracker_type=TRACKER_JIRA,
        external_id=issue_id,
        external_key=issue_key,
        external_url=external_url,
        summary=summary,
        event_ids=event_ids,
    )


async def _create_linear_ticket(
    session: AsyncSession,
    config: ProjectTrackerConfig,
    defaults: OrgTrackerDefaults,
    *,
    project_uuid: uuid.UUID,
    branch_uuid: uuid.UUID,
    event_ids: list[str],
    summary: str,
) -> None:
    """The Linear arm of ``_create_ticket``: same claim, same ask-before-create.

    The endpoint is Linear's fixed public GraphQL host, so there is no
    operator-supplied URL to SSRF-check. The branch marker goes into the
    description (Linear labels are workspace objects that would have to be
    created first), and ``find_linear_issue_by_marker`` looks it up on a
    redelivery exactly as the Jira arm looks up its label.
    """
    resolved = _resolve_linear_config(config, defaults)
    if resolved is None:
        logger.warning(
            "Skipping implementation ticket for branch %s: Linear tracker config is invalid",
            branch_uuid,
        )
        return
    api_key, team_id = resolved

    marker = _branch_marker(branch_uuid)
    try:
        issue_id, identifier, url = find_linear_issue_by_marker(
            _post_json, api_key=api_key, team_id=team_id, marker=marker
        )
    except Exception:
        # Same trade as ``_find_existing_issue``: a failed lookup falls through
        # to the create.
        logger.warning(
            "Could not search Linear for an existing issue marked %s; creating one", marker
        )
        issue_id, identifier, url = (None, None, "")

    if identifier is not None:
        logger.info(
            "Adopting existing Linear issue %s for branch %s instead of creating a second one",
            identifier,
            branch_uuid,
        )
    else:
        body_text = await _build_ticket_body(session, event_ids)
        description = f"{body_text}\n\n{marker}"
        try:
            issue_id, identifier, url = create_linear_issue(
                _post_json,
                api_key=api_key,
                team_id=team_id,
                title=summary,
                description=description,
            )
        except Exception as exc:
            if _is_transient_tracker_error(exc):
                raise TransientTrackerError("Temporary Linear ticket creation failure") from exc
            raise

    await _persist_ticket(
        session,
        project_uuid=project_uuid,
        branch_uuid=branch_uuid,
        tracker_type=TRACKER_LINEAR,
        external_id=issue_id,
        external_key=identifier,
        external_url=url,
        summary=summary,
        event_ids=event_ids,
    )


async def _persist_ticket(
    session: AsyncSession,
    *,
    project_uuid: uuid.UUID,
    branch_uuid: uuid.UUID,
    tracker_type: str,
    external_id: str | None,
    external_key: str | None,
    external_url: str,
    summary: str,
    event_ids: list[str],
) -> None:
    branch_id = str(branch_uuid)
    session.add(
        ImplementationTicket(
            project_id=project_uuid,
            branch_id=branch_uuid,
            tracker_type=tracker_type,
            external_id=external_id,
            external_key=external_key,
            external_url=external_url,
            status="open",
            summary=summary,
            event_ids=list(event_ids),
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # uq_implementation_ticket_branch. The check at the top is a
        # check-then-act, so a delivery that enqueued concurrently can insert
        # between it and here; the constraint is what actually holds
        # one-ticket-per-branch. Both tracker issues exist by then — closing that
        # window needs tracker-side idempotency, which Jira's create does not
        # offer (tripl-l33u.11) — but the loser must not also fail the merge that
        # enqueued it.
        await session.rollback()
        logger.warning(
            "Implementation ticket for branch %s was created concurrently; keeping the first",
            branch_id,
        )


async def _mark_events_implemented(session: AsyncSession, event_ids: list[str]) -> None:
    """Flip covered events to ``implemented`` — but never DOWNGRADE. An event
    already at ``live``/``deprecated``/``archived`` outranks ``implemented`` and
    is left untouched."""
    uuids = _coerce_uuids(event_ids)
    if not uuids:
        return
    events = (await session.execute(select(Event).where(Event.id.in_(uuids)))).scalars().all()
    implemented_rank = event_status_rank(EventStatus.implemented)
    for event in events:
        try:
            current = EventStatus(event.status)
        except ValueError:
            continue
        if event_status_rank(current) < implemented_rank:
            event.status = EventStatus.implemented.value


async def _sync_one(session: AsyncSession, ticket: ImplementationTicket) -> None:
    if not ticket.external_key:
        return
    config = await session.scalar(
        select(ProjectTrackerConfig).where(ProjectTrackerConfig.project_id == ticket.project_id)
    )
    if config is None or not config.enabled:
        return
    # A ticket is polled on the tracker that OPENED it. A project that switched
    # trackers since has credentials for the other one, and sending them to
    # this ticket's tracker would be wrong twice over — so it is skipped.
    ticket_tracker = ticket.tracker_type or TRACKER_JIRA
    if ticket_tracker != (config.tracker_type or TRACKER_JIRA):
        return
    defaults = await defaults_for_project(session, ticket.project_id)
    if ticket_tracker == TRACKER_LINEAR:
        if not await _linear_ticket_done(ticket, config, defaults):
            return
    else:
        resolved = _resolve_jira_config(config, defaults)
        if resolved is None:
            logger.warning("Skipping sync for ticket %s: tracker config is invalid", ticket.id)
            return
        base_url, auth_email, api_token, _project_key, _issue_type = resolved

        _reject_private_target(base_url, field="Jira base_url")
        category = _get_jira_issue_status(
            _get_json,
            base_url=base_url,
            auth_email=auth_email,
            api_token=api_token,
            issue_key=ticket.external_key,
        )
        if category != _DONE_CATEGORY:
            return

    ticket.status = "closed"
    ticket.closed_at = datetime.now(UTC)
    await _mark_events_implemented(session, list(ticket.event_ids or []))
    await session.commit()


async def _linear_ticket_done(
    ticket: ImplementationTicket,
    config: ProjectTrackerConfig,
    defaults: OrgTrackerDefaults = NO_DEFAULTS,
) -> bool:
    """Has the Linear issue behind ``ticket`` reached a ``completed`` state?"""
    resolved = _resolve_linear_config(config, defaults)
    if resolved is None:
        logger.warning("Skipping sync for ticket %s: Linear tracker config is invalid", ticket.id)
        return False
    api_key, _team_id = resolved
    # ``issue(id:)`` accepts the uuid or the identifier; prefer the uuid.
    issue_ref = ticket.external_id or ticket.external_key
    if not issue_ref:
        return False
    state_type = get_linear_issue_state_type(_post_json, api_key=api_key, issue_id=issue_ref)
    return state_type == LINEAR_COMPLETED_STATE_TYPE


async def _sync_tickets(session: AsyncSession) -> None:
    """Poll every open ticket, isolating failures to the ticket that caused them.

    IDS, not ORM objects — and that is the whole fix (tripl-l33u.16).
    ``rollback()`` expires every persistent instance in the identity map;
    ``expire_on_commit=False`` suppresses expiry on COMMIT and says nothing about
    rollback. So a loop holding loaded tickets across the handler below had the
    exact opposite of its stated effect: the first tracker failure expired all of
    them, the next iteration's ``ticket.external_key`` became a lazy refresh —
    synchronous IO inside a coroutine, which asyncio SQLAlchemy raises
    MissingGreenlet for — and the exception escaped the per-ticket handler that
    existed to contain it. Ten open tickets and a 500 on the first meant the other
    nine went unpolled, that run and every run after it.

    A uuid cannot expire. Each ticket is loaded inside its own try, after any
    rollback the previous iteration performed.
    """
    ticket_ids = (
        (
            await session.execute(
                select(ImplementationTicket.id).where(
                    ImplementationTicket.status == "open",
                    ImplementationTicket.external_key.is_not(None),
                    in_active_org(ImplementationTicket.project_id),
                )
            )
        )
        .scalars()
        .all()
    )
    for ticket_id in ticket_ids:
        try:
            ticket = await session.get(ImplementationTicket, ticket_id)
            if ticket is None:
                # Deleted between the id sweep and now; nothing to poll.
                continue
            await _sync_one(session, ticket)
        except Exception:
            # Isolate per-ticket failures — one bad ticket must not strand the rest.
            await session.rollback()
            logger.exception("Failed to sync implementation ticket %s", ticket_id)


# Async-bridge helper, shared with the health snapshot task (see worker.db).
_with_worker_session = run_with_async_worker_session


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.implementation_tickets.create_implementation_ticket",
    autoretry_for=(TransientTrackerError,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
)
def create_implementation_ticket(
    project_id: str,
    branch_id: str,
    event_ids: list[str],
    summary: str,
) -> None:
    async def _run() -> None:
        await _with_worker_session(
            lambda session: _create_ticket(session, project_id, branch_id, event_ids, summary)
        )

    asyncio.run(_run())


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.implementation_tickets.sync_implementation_tickets",
)
def sync_implementation_tickets() -> None:
    async def _run() -> None:
        await _with_worker_session(_sync_tickets)

    asyncio.run(_run())


SEEN_IN_DATA_COMMENT = "Seen in production data at {seen_at}; marked live."


def _format_seen_at(seen_at_iso: str) -> str:
    """``2026-09-27T10:05:00+00:00`` -> ``2026-09-27 10:05 UTC``; the raw value if unparsable."""
    try:
        parsed = datetime.fromisoformat(seen_at_iso)
    except ValueError, TypeError:
        return str(seen_at_iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def seen_in_data_comment_text(seen_at_iso: str, event_names: list[str]) -> str:
    """The comment the scan's auto-live posts on the ticket that covered the events."""
    text = SEEN_IN_DATA_COMMENT.format(seen_at=_format_seen_at(seen_at_iso))
    if event_names:
        text += "\n\nEvents: " + ", ".join(event_names)
    return text


async def _comment_seen_in_data(
    session: AsyncSession,
    ticket_id: str,
    event_ids: list[str],
    seen_at_iso: str,
) -> bool:
    """Post the "seen in data" comment on one ticket; True when the tracker took it.

    Published by the scan (``metrics.collect._publish_seen_in_data_comments``)
    AFTER the auto-live transition committed, so there is nothing here to roll
    back: the comment is best effort and never raises for a tracker failure
    (``implementation_ticket_service.add_ticket_comment_async``).
    """
    # Imported here: the service module sits on the request path and imports
    # this module lazily in turn.
    from tripl.services.implementation_ticket_service import add_ticket_comment_async

    try:
        ticket_uuid = uuid.UUID(str(ticket_id))
    except ValueError:
        return False
    ticket = await session.get(ImplementationTicket, ticket_uuid)
    if ticket is None:
        return False
    names = await _load_event_names(session, event_ids)
    return await add_ticket_comment_async(
        session, ticket, seen_in_data_comment_text(seen_at_iso, names)
    )


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.implementation_tickets.comment_seen_in_data",
)
def comment_seen_in_data(ticket_id: str, event_ids: list[str], seen_at_iso: str) -> None:
    """Celery entry for the auto-live ticket comment (GH #258).

    NOT auto-retried: the trackers take no idempotency key for a comment, so a
    retry after a create that actually landed would post it twice.
    """

    async def _comment(session: AsyncSession) -> None:
        await _comment_seen_in_data(session, ticket_id, event_ids, seen_at_iso)

    asyncio.run(_with_worker_session(_comment))
