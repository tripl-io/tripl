"""Plan search."""

from __future__ import annotations

from typing import Any

from tripl_cli.api.request import ApiRequest
from tripl_cli.model import as_dict

SEARCH = "/projects/{slug}/search"

ENDPOINTS: tuple[tuple[str, str], ...] = (("get", SEARCH),)

# SearchResult.entity_type, verbatim from the OpenAPI document — the same list
# the route validates `types` against. One spelling, pinned by the contract test.
ENTITY_TYPES: tuple[str, ...] = (
    "event",
    "event_type",
    "field",
    "meta_field",
    "variable",
    "relation",
    "tag",
    "metric",
    "fact_table",
    "scan_config",
    "alert_rule",
    "doc",
)

# The route's own bounds: `q` is Query(min_length=1, max_length=500) and `limit`
# is Query(ge=1, le=100) with a server default of 20. Checked locally so a
# too-long phrase fails at parse time rather than as a 422.
QUERY_MAX_LENGTH = 500
LIMIT_DEFAULT = 20
LIMIT_MAX = 100


def search_plan(
    slug: str,
    query: str,
    *,
    types: list[str] | None = None,
    limit: int | None = None,
    branch: str | None = None,
) -> ApiRequest:
    """Answers ``{items, total, truncated, semantic_used}``.

    NOT offset-paged: ``total`` is the size of THIS response, and ``truncated``
    says whether ranked hits were dropped. Raise ``limit`` to see them.
    """
    return ApiRequest(
        "GET",
        SEARCH.format(slug=slug),
        params={"q": query, "types": types, "limit": limit, "branch": branch},
    )


def semantic_used(payload: Any) -> bool:
    """Did the semantic index answer, or did the route fall back to substring?

    The one envelope key this route adds, and load-bearing on BOTH surfaces for
    the same reason: under a substring fallback a low ``confidence`` means
    something different, so a consumer that cannot tell the two regimes apart
    misreads every score. ``tripl plan search`` publishes it in the document's
    ``meta`` and ``tripl-mcp``'s ``search_plan`` in its envelope — one route, one
    reader, rather than a ``.get`` per surface.

    Absent reads as False rather than as unknown: the backend declares
    ``SearchResponse.semantic_used: bool = False``, so a body without the key IS
    the route saying the index did not answer.

    Only a real boolean counts, for the same reason. ``bool(value)`` would read
    the string ``"false"`` — and any other truthy junk — as True, reporting that
    the semantic index answered when it did not, which inverts what every score
    in the payload means. Anything that is not a boolean is not the route making
    that claim, so it takes the same path as absent (Copilot, PR #78).
    """
    return as_dict(payload).get("semantic_used") is True


def reported_truncated(payload: Any) -> bool | None:
    """The route's own ``truncated``, or ``None`` where it reported none.

    The second envelope fact this route adds, and the one ``total`` cannot give:
    ``total`` is ``len(items)`` after the trim, so on a full page it equals
    ``limit`` whether or not anything was dropped. ``/search``
    takes no ``offset``, so the answer to a True is a bigger ``limit``.

    Absent is ``None`` rather than False HERE because the two surfaces want two
    different things from one key, and this is the one that can express both.
    ``tripl plan search`` uses it as the top rung of ``PlanRead.truncated``'s
    ladder, where "this instance predates the field" must fall through to the
    page-fullness guess instead of being published as "nothing was dropped".
    :func:`truncated` is the projection for the surface that needs a bool.

    Only a real boolean counts, for the reason spelled out on
    :func:`semantic_used`: the string ``"false"`` must not read as True.
    """
    value = as_dict(payload).get("truncated")
    return value if isinstance(value, bool) else None


def truncated(payload: Any) -> bool:
    """Did the route drop ranked hits it could not fit in this response?

    :func:`reported_truncated` collapsed onto a bool, for the surface that
    republishes the fact rather than deciding from it: ``tripl-mcp``'s
    ``search_plan`` puts this in its envelope, and an envelope key that is
    sometimes null is a shape every agent has to test for. The backend declares
    ``SearchResponse.truncated: bool = False``, so a body without the key reads
    as the route saying nothing was dropped.
    """
    return reported_truncated(payload) is True
