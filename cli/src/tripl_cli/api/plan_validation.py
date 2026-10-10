"""Validating tracking calls and captured payloads against the plan (``tripl check``).

A POST that READS: the route writes nothing, and any project member, viewers
included, may call it. It is still a POST because a batch of up to
``MAX_ITEMS`` items does not fit in a query string.

``?branch=`` follows the rule every plan read follows (see ``api/branches.py``):
omitted means the live main plan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tripl_cli.api.request import ApiRequest
from tripl_cli.model import JsonDict, JsonList, page_items

VALIDATE = "/projects/{slug}/plan/validate"

ENDPOINTS: tuple[tuple[str, str], ...] = (("post", VALIDATE),)

# The route's own cap on one request body. The CLI splits a larger run into
# batches of at most this many items rather than letting the route answer 422.
MAX_ITEMS = 5000

# The route's per-item bounds (``PlanValidationItemIn``). The CLI enforces them
# before sending, so one oversize captured value becomes a warning on its own
# line instead of a 422 that fails the whole batch.
NAME_MAX_LENGTH = 500
EVENT_TYPE_MAX_LENGTH = 100
# Separately for ``fields`` and for ``properties``.
MAX_FIELDS = 200
FIELD_KEY_MAX_LENGTH = 255
FIELD_VALUE_MAX_LENGTH = 2000


def validate(
    slug: str,
    items: Sequence[Mapping[str, Any]],
    *,
    branch: str | None = None,
    strict: bool = False,
) -> ApiRequest:
    """One batch. ``items`` is sent as given, so it must already be at most ``MAX_ITEMS``.

    ``strict`` asks the route to note values set at runtime as ``info`` findings;
    it is left off the wire when false, the route's default.
    """
    if len(items) > MAX_ITEMS:
        raise ValueError(f"at most {MAX_ITEMS} items per validate request, got {len(items)}")
    body: JsonDict = {"items": [dict(item) for item in items]}
    if strict:
        body["strict"] = True
    return ApiRequest(
        "POST",
        VALIDATE.format(slug=slug),
        params={"branch": branch},
        json_body=body,
    )


def batches[T](items: Sequence[T], size: int = MAX_ITEMS) -> list[Sequence[T]]:
    """``items`` cut into consecutive runs of at most ``size``. Empty in, empty out."""
    if size < 1:
        raise ValueError("batch size must be at least 1")
    return [items[start : start + size] for start in range(0, len(items), size)]


def verdicts(payload: Any) -> JsonList:
    """The per-item verdicts of one response, in the order the route answered them."""
    return page_items(payload)
