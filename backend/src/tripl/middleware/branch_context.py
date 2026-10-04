"""Request-scoped plan-branch context for the audit writer.

``audit_service.record`` must not grow a ``branch=`` argument in the
branch-scoped routes that call it — that is a standing invitation for the next
branch-scoped route to forget it, which is how tripl-wkwv.6 arrived in the first
place. That is a claim about the routes that audit, not about every write route:
whether a write is audited is decided route by route, and some writes record
nothing at all. What this module guarantees is narrower: a row
that IS written from a route that declares ``BranchIdDep``, on a request whose
``?branch=`` resolves to a branch other than main, carries that branch. Two
kinds of request leave ``branch_id`` NULL with a working branch in play: routes
that address the branch by path (``/branches/{id}/revert`` and the like), which
name it in ``target_id`` instead, and routes that deliberately omit
``BranchIdDep`` although the client sends ``?branch=``
(``POST /variables/drifts/{id}/action``, whose write lands on main whatever the
request is scoped to). Deliberately not a census: the counts this paragraph
used to carry were already wrong when they were written, and adding the six
event routes moved them again. ``?branch=`` is already resolved in exactly one
place (:func:`tripl.api.deps.get_branch_id_override`), so it is bound there and
read in exactly one place (:func:`tripl.services.audit_service.record`). Those
two are the only binder and the only reader; each names the other in a comment.

Unlike the request id, this value needs no ASGI-scope mirror. The hazard that
forced one on ``request_id`` — ServerErrorMiddleware serves the catch-all 500
handler from OUTSIDE RequestIDMiddleware, so the contextvar is already reset by
the time that handler runs (see ``request_id._SCOPE_KEY``) — cannot
bite here: the audit row is written mid-handler, long before any unwinding, and
a request that never reaches its handler writes no audit row at all.

The reset in :func:`bound_branch` is load-bearing, not hygiene. Under uvicorn
each request cycle is its own task and therefore its own Context, so a leak
would be invisible in production; the test suite drives the app through
``httpx.ASGITransport`` (tests/conftest.py), which awaits the app in the
CALLER's task, so an unreset ``set()`` bleeds one request's branch into the next
request of the same test.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# The id and the name travel as one value so the two can never drift apart: an
# audit row that names a branch it does not point at is worse than no branch.
_branch_var: ContextVar[tuple[uuid.UUID, str] | None] = ContextVar("tripl_branch", default=None)


def current_branch() -> tuple[uuid.UUID, str] | None:
    """The branch this request is scoped to, as ``(id, name)``.

    ``None`` means "no branch scope": a request with no ``?branch=`` or one
    naming main, a route that does not declare ``BranchIdDep``, or any caller
    outside an HTTP request — the Celery workers write no audit rows today, but
    a future one must degrade to a null branch rather than raise.
    """
    return _branch_var.get()


@contextmanager
def bound_branch(branch_id: uuid.UUID, branch_name: str) -> Iterator[None]:
    """Bind the branch for the enclosed block, then unbind it.

    Bare ``try``/``finally`` with no ``except`` on purpose: FastAPI 0.141 raises
    ``FastAPIError("Response not awaited…")`` when a yield dependency swallows
    the endpoint's exception, and a bare ``finally`` re-raises.
    """
    token = _branch_var.set((branch_id, branch_name))
    try:
        yield
    finally:
        _branch_var.reset(token)
