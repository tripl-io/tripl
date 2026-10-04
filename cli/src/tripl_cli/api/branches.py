"""Plan branches. Merge, revert and branch transitions are not built here.

Not an oversight: neither surface exposes them, because both hand that decision
back to a human in the tripl UI (see ``tripl_mcp.tools.branches``). A builder
would be the first half of shipping them.

Note how the OTHER builders spell "the live plan": by leaving ``branch`` unset.
``deps.get_branch_id_override`` resolves main whenever ``?branch=`` is missing or
empty, and refuses anything that is not a UUID belonging to this project (400) or
names no branch of it (404). So there is no literal for main, here or anywhere.
It IS a declared query parameter, so ``backend/openapi.json`` carries it and the
contract test can see it — typed ``str`` rather than ``format: uuid`` because the
dependency parses the value itself, which is what keeps a malformed id a 400
instead of FastAPI's own 422.
"""

from __future__ import annotations

from tripl_cli.api.request import ApiRequest

LIST = "/projects/{slug}/branches"
DETAIL = "/projects/{slug}/branches/{branch_id}"
DIFF = "/projects/{slug}/branches/{branch_id}/diff"

ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("get", LIST),
    ("get", DETAIL),
    ("get", DIFF),
)


def list_branches(slug: str, *, include_diff_counts: bool = False) -> ApiRequest:
    """Answers ``{items, total}``, not a bare list.

    ``include_diff_counts`` fills ``ahead`` and ``behind_base`` on open branches
    (merged and closed ones, like main, stay null); without it the service
    leaves both null everywhere, because each row costs a snapshot comparison. Off
    by default for that reason, and asked for in exactly one place: ``tripl plan
    branches``, where those two columns are the reason to run the command at
    all. The branch RESOLUTION path must keep the default — it needs only id and
    name, and would otherwise pay that cost on every command taking ``--branch``.

    Passing ``None`` rather than ``False`` when off: ``TriplClient.request``
    drops ``None`` members, which is this layer's way of saying "leave the
    parameter off and let the server apply its own default".
    """
    return ApiRequest(
        "GET",
        LIST.format(slug=slug),
        params={"include_diff_counts": include_diff_counts or None},
    )


def get_branch(slug: str, branch_id: str) -> ApiRequest:
    return ApiRequest("GET", DETAIL.format(slug=slug, branch_id=branch_id))


def get_diff(slug: str, branch_id: str) -> ApiRequest:
    """Answers ``{behind_base, entries, summary}``. No CLI verb reads it yet.

    ``tripl plan`` does not expose it: that payload is not a list, so it does
    not fit the one document shape the seven read verbs share, and
    ``behind_base`` is too load-bearing to smuggle into an ``items`` array.
    ``tripl-mcp``'s ``get_branch_diff`` calls this.
    """
    return ApiRequest("GET", DIFF.format(slug=slug, branch_id=branch_id))
