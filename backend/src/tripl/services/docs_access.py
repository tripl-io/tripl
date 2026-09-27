"""Who may write which docs catalog notes (F22, GH #299; F20 PR4 org roles).

Project notes need nothing here: ``EditorUserDep`` on every write route has
already admitted a caller with an editing role on the path's project (a
``project_members`` editor, or an owner/admin of the project's organization),
through a session or a ``write`` key.

Organization notes reach further than the path's project — they are shown in,
and indexed into, every project of the organization — so they get more rules:

* Only an owner or admin of the note's organization (``organization_members``,
  the organization of the path's project) may create, edit, move, restore or
  delete them. A plain organization member with an editor row on one project
  may not change notes every other project reads. ``users.role`` is not read.
* A project-bound API key is fenced into one project (``_enforce_project_scope``),
  so it may not change notes that other projects read.
* Deleting organization notes in bulk (a folder delete, a ``mirror`` import)
  follows the strict owner gate: organization owner/admin **and** a browser
  session, never an API key, like every other owner-only route
  (``deps.get_owner_user``).

The organization id is always taken from the resolved project row, never from
the caller, so an admin of another organization holds nothing here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.user import User
from tripl.services import project_access
from tripl.services.docs_paths import DocScope

ORG_NOTES_ADMIN_REQUIRED = "Organization owner or admin role required to edit organization notes"


@dataclass(frozen=True)
class DocCaller:
    """The request facts the organization rules need, beside the user."""

    user: User
    via_api_key: bool = False
    key_project_bound: bool = False

    @classmethod
    def from_request_state(cls, user: User, state: Any) -> DocCaller:
        """Read what ``deps._resolve_api_key_user`` stashed on ``request.state``."""
        return cls(
            user=user,
            via_api_key=getattr(state, "api_key_scope", None) is not None,
            key_project_bound=getattr(state, "api_key_project_id", None) is not None,
        )


async def require_doc_writer(
    session: AsyncSession, caller: DocCaller, scope: DocScope, org_id: uuid.UUID
) -> None:
    """Who may create, edit, move, restore or delete a note of ``scope``.

    ``org_id`` is the organization of the path's project, which owns the
    organization notes.
    """
    if scope != "organization":
        return
    if not await project_access.is_org_admin(session, caller.user, org_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ORG_NOTES_ADMIN_REQUIRED)
    if caller.key_project_bound:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A project-scoped API key cannot edit organization notes",
        )


async def require_org_bulk_delete(
    session: AsyncSession, caller: DocCaller, scope: DocScope, org_id: uuid.UUID, what: str
) -> None:
    """Deleting many organization notes at once: org owner/admin, browser session only."""
    await require_doc_writer(session, caller, scope, org_id)
    if scope != "organization":
        return
    if caller.via_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Owner session required to {what} organization notes",
        )
