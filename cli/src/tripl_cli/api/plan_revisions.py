"""Plan revisions: the stored copies of a project's whole plan.

Only the listing is built here. Taking a revision, reverting to one and reading
one's full payload stay in the tripl UI, like the branch transitions
``api/branches.py`` leaves out.

The listing covers the whole project and has no ``?branch=``: a revision is a
fixed copy, not a view of a plan that moves. A row made for a plan branch (its
merge base, or the merge itself) names that branch in ``branch_id``.
"""

from __future__ import annotations

from tripl_cli.api.request import ApiRequest

LIST = "/projects/{slug}/revisions"

ENDPOINTS: tuple[tuple[str, str], ...] = (("get", LIST),)

# The route's own bounds: `limit` is Query(ge=1, le=200) with a server default
# of 50. Pinned to the OpenAPI document by the contract test.
LIMIT_DEFAULT = 50
LIMIT_MAX = 200


def list_revisions(slug: str, *, offset: int | None = None, limit: int | None = None) -> ApiRequest:
    """Paged, newest first: answers ``{items, total}``.

    Each item is a summary (id, created_at, kind, summary, branch_id and
    entity_counts) without the stored plan itself.
    """
    return ApiRequest("GET", LIST.format(slug=slug), params={"offset": offset, "limit": limit})
