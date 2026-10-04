from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl.models.implementation_ticket import ImplementationTicket
from tripl.models.project_tracker_config import ProjectTrackerConfig
from tripl.schemas.implementation_ticket import ImplementationTicketResponse
from tripl.services._branch_counterparts import main_counterparts
from tripl.services.event_service import get_event
from tripl.services.org_tracker_defaults_service import (
    NO_DEFAULTS,
    OrgTrackerDefaults,
    defaults_for_project_sync,
)
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.project_lookup import resolve_project_id

logger = logging.getLogger(__name__)

# ``ProjectTrackerConfig.tracker_type`` / ``ImplementationTicket.tracker_type``.
TRACKER_JIRA = "jira"
TRACKER_LINEAR = "linear"


async def list_branch_tickets(
    session: AsyncSession,
    slug: str,
    branch_id: uuid.UUID,
) -> list[ImplementationTicketResponse]:
    """Tracker tickets opened for one branch, oldest first.

    Read-only counterpart to the create-on-merge worker: the
    mapping was persisted but unreachable, so a user who merged a branch had no
    way back to the Jira issue it opened.

    Scoping goes through :func:`plan_branch_service.resolve_branch_id` rather
    than a local ``branch.project_id == project_id`` check: it REUSES an
    existing ownership check instead of adding another one.

    Not the only one, though — the sibling branch routes (detail, diff,
    comments, conflicts, delete, transition, reviewers) go through
    ``plan_branch_service._get_branch``, a second spelling of the same
    predicate, and ``deps.get_branch_id_override`` is a third. Reusing one of
    those beats writing a fourth, which is all this choice claims.
    """
    project_id = await resolve_project_id(session, slug)
    resolved_branch_id = await resolve_branch_id(session, project_id, branch_id)
    rows = (
        (
            await session.execute(
                select(ImplementationTicket)
                .where(ImplementationTicket.branch_id == resolved_branch_id)
                .order_by(ImplementationTicket.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [ImplementationTicketResponse.model_validate(row) for row in rows]


async def list_event_tickets(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> list[ImplementationTicketResponse]:
    """Every tracker ticket that named this event, oldest first.

    The mapping already existed and nothing read it this way:
    ``uq_implementation_ticket_branch`` is one ticket per BRANCH, and
    ``event_ids`` lists the events that branch touched — so an event carried by
    three merged branches is named by three rows, and the only route asked
    "which tickets did this branch open?".

    Scoping borrows :func:`event_service.get_event` with ``strict_branch=False``,
    the predicate ``get_event_history`` already uses for the other per-event
    sub-resource list. That is a REUSE rather than a fourth spelling of the
    branch-ownership check the docstring above warns about — those three are all
    branch predicates and none of them fits a route with no branch in its path.
    ``get_event`` resolves the project itself and 404s on an id from another
    one, which is the leak that matters here.

    A branch copy reads through to its main twin, because ``event_ids`` holds
    MAIN ids and the history belongs to the event, not to one copy of it — the
    same rule the discussion follows.

    Filtering happens in Python on purpose: ``event_ids`` is a JSON column, and
    containment over it is spelled differently on every dialect this project
    runs on. The row count is the project's ticket count, one per merged
    branch, so the scan is bounded by something a person created by hand.
    """
    event = await get_event(session, slug, event_id, branch_id, strict_branch=False)
    project_id = event.project_id
    twins = await main_counterparts(session, project_id=project_id, events=[event])
    target_id = str(twins.get(event.id, event).id)
    rows = (
        (
            await session.execute(
                select(ImplementationTicket)
                .where(ImplementationTicket.project_id == project_id)
                .order_by(ImplementationTicket.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [
        ImplementationTicketResponse.model_validate(row)
        for row in rows
        if target_id in (row.event_ids or [])
    ]


@dataclass(frozen=True)
class _TicketRef:
    """The ticket columns a comment needs, read while a session is at hand.

    The HTTP half runs without one (in a thread, for the async caller), where a
    lazy load of an expired ORM attribute would raise instead of loading.
    """

    id: uuid.UUID
    tracker_type: str
    external_id: str | None
    external_key: str | None


def _ticket_ref(ticket: ImplementationTicket) -> _TicketRef:
    return _TicketRef(
        id=ticket.id,
        tracker_type=ticket.tracker_type or TRACKER_JIRA,
        external_id=ticket.external_id,
        external_key=ticket.external_key,
    )


def _load_comment_inputs(
    session: Session, ticket: ImplementationTicket
) -> tuple[ProjectTrackerConfig | None, _TicketRef, OrgTrackerDefaults]:
    config = session.scalar(
        select(ProjectTrackerConfig).where(ProjectTrackerConfig.project_id == ticket.project_id)
    )
    # The organization's tracker defaults (F20 PR12), under the project's values.
    defaults = defaults_for_project_sync(session, ticket.project_id)
    # Freshly loaded, so every column the HTTP half reads is already populated
    # and nothing it touches can trigger a lazy load.
    return config, _ticket_ref(ticket), defaults


def _post_ticket_comment(
    config: ProjectTrackerConfig | None,
    ticket: _TicketRef,
    text: str,
    defaults: OrgTrackerDefaults = NO_DEFAULTS,
) -> bool:
    """The network half of ``add_ticket_comment``: no session, never raises.

    Posts on the tracker that OPENED the ticket, and only while the project is
    still configured for that same tracker — a project that switched from Jira
    to Linear holds a Linear key, and sending it to Jira (or a Jira token to
    Linear) would hand one vendor the other's credential.

    The worker helpers are imported here rather than at module level: this
    module is on the request path, and ``tripl.worker.tasks`` pulls in the
    whole alert-rendering stack.
    """
    try:
        if config is None or not config.enabled:
            return False
        if ticket.tracker_type != (config.tracker_type or TRACKER_JIRA):
            return False
        if not text.strip():
            return False

        from tripl.worker.tasks.alerts_channels import _post_json, _reject_private_target
        from tripl.worker.tasks.implementation_tickets import (
            _resolve_jira_config,
            _resolve_linear_config,
        )
        from tripl.worker.tasks.tracker_clients import add_jira_comment, add_linear_comment

        if ticket.tracker_type == TRACKER_LINEAR:
            linear = _resolve_linear_config(config, defaults)
            issue_ref = ticket.external_id or ticket.external_key
            if linear is None or not issue_ref:
                return False
            api_key, _team_id = linear
            return add_linear_comment(_post_json, api_key=api_key, issue_id=issue_ref, body=text)

        jira = _resolve_jira_config(config, defaults)
        if jira is None or not ticket.external_key:
            return False
        base_url, auth_email, api_token, _project_key, _issue_type = jira
        # DNS-rebinding defense, as on every other outbound Jira call.
        _reject_private_target(base_url, field="Jira base_url")
        return add_jira_comment(
            _post_json,
            base_url=base_url,
            auth_email=auth_email,
            api_token=api_token,
            issue_key=ticket.external_key,
            body_text=text,
        )
    except Exception:
        # A comment is a courtesy. Whatever triggered it (the scan's auto-live
        # transition) already happened and must not be undone by a tracker
        # outage, so the failure is logged — without the credential; the
        # transport's errors carry scheme and host only — and swallowed.
        logger.warning(
            "Could not comment on %s ticket %s", ticket.tracker_type, ticket.id, exc_info=True
        )
        return False


def add_ticket_comment(session: Session, ticket: ImplementationTicket, text: str) -> bool:
    """Comment ``text`` on ``ticket`` in its tracker (Jira or Linear); True on success.

    SYNC, for the worker (the scan's auto-live path runs on a sync session).
    Best effort: returns False — never raises — when the project has no enabled
    tracker, the config is incomplete, the ticket never came back from the
    tracker, or the call fails. It does blocking HTTP (10s timeout per call), so
    call it after the transaction that decided the transition has committed,
    not while it holds row locks.
    """
    try:
        config, ref, defaults = _load_comment_inputs(session, ticket)
    except Exception:
        logger.warning("Could not load tracker config for ticket comment", exc_info=True)
        return False
    return _post_ticket_comment(config, ref, text, defaults)


async def add_ticket_comment_async(
    session: AsyncSession, ticket: ImplementationTicket, text: str
) -> bool:
    """``add_ticket_comment`` for an async session; the HTTP call runs off the loop."""
    try:
        config, ref, defaults = await session.run_sync(_load_comment_inputs, ticket)
    except Exception:
        logger.warning("Could not load tracker config for ticket comment", exc_info=True)
        return False
    return await asyncio.to_thread(_post_ticket_comment, config, ref, text, defaults)
