"""Who may write which docs catalog notes (F22, GH #299).

``EditorUserDep`` on every write route has already admitted an instance editor
or owner with an editing role on the path's project, through a session or a
``write`` key. Organization notes reach further than the path's project — they
are shown in, and indexed into, every project of the organization — so they get
two more rules here:

* A project-bound API key is fenced into one project (``_enforce_project_scope``),
  so it may not change notes that other projects read.
* Deleting organization notes in bulk (a folder delete, a ``mirror`` import)
  follows the strict owner gate: owner role **and** a browser session, never an
  API key, like every other owner-only route (``deps.get_owner_user``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status

from tripl.models.user import User
from tripl.services.docs_paths import DocScope


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


def require_doc_writer(caller: DocCaller, scope: DocScope) -> None:
    """Who may create, edit, move, restore or delete a note of ``scope``.

    TODO(F20 PR4): organization_members.role in ('owner', 'admin').
    """
    if scope != "organization":
        return
    if caller.user.role not in ("owner", "editor"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Editor role required to edit organization notes",
        )
    if caller.key_project_bound:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A project-scoped API key cannot edit organization notes",
        )


def require_org_bulk_delete(caller: DocCaller, scope: DocScope, what: str) -> None:
    """Deleting many organization notes at once: owner role, browser session only."""
    require_doc_writer(caller, scope)
    if scope != "organization":
        return
    if caller.user.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Owner role required to {what} organization notes",
        )
    if caller.via_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Owner session required to {what} organization notes",
        )
