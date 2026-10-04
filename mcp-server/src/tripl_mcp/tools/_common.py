"""Shared helpers for tool implementations.

Everything here is a statement about THIS consumer: what a model should be made
to pay for, and what it should be told. That is why the four field projections
below stayed when tripl-i1dt revisited them — ``tripl events list``, ``plan
types``, ``plan fields`` and ``plan search`` all emit their rows VERBATIM, on
purpose (see ``tripl_cli.report.plan_read_document``), because a CLI writes to a
pipe and a trimmed row there is a field the operator has to fetch again. A
projection with one caller is a context-budget policy, not a fact about the API,
and moving one into ``tripl_cli`` would put agent payload policy in a
distribution with nothing to apply it to.

What DID move is the half of these bodies that was never about the consumer: the
``{items, total}`` envelope and ``semantic_used`` are what the routes answer, so
they are read through ``tripl_cli.api``. ``mcp-server/tests/test_contract.py``
pins that nothing here re-derives them.
"""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations
from tripl_cli.api import page_items, page_total

READ_ONLY = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
WRITE_UPDATE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)
# A whole-content replace keyed by path: it overwrites what was there, and
# sending the same content twice changes nothing the second time (no revision).
WRITE_REPLACE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)

EVENT_LIST_FIELDS = (
    "id",
    "name",
    "description",
    "status",
    "reviewed",
    "event_type_id",
    "tags",
    "sunset_at",
    "owner_id",
)

# An event type's own attributes, WITHOUT the embedded field_definitions: a
# project with 40 event types × 25 fields returns the entire field catalogue on
# every list call, which is the bulk of the payload and almost never what the
# caller is after at that point. get_event_type_fields fetches one type's fields
# on demand.
EVENT_TYPE_LIST_FIELDS = (
    "id",
    "name",
    "display_name",
    "description",
    "color",
    "order",
)

# What an agent needs to write a valid field value. The contract_* thresholds are
# a data-quality concern for the scanner, not for composing a payload, and
# event_type_id repeats the parent.
FIELD_DEFINITION_FIELDS = (
    "id",
    "name",
    "display_name",
    "field_type",
    "is_required",
    "enum_options",
    "description",
    "order",
    "sensitivity",
)

SEARCH_RESULT_FIELDS = (
    "entity_type",
    "entity_id",
    "title",
    "subtitle",
    "description",
    "snippet",
    "route_path",
    "score",
    "confidence",
    "event_id",
    "name",
    "implemented",
)

# A docs-catalog note as a listing row: enough to pick one to read, no content.
DOC_LIST_FIELDS = (
    "scope",
    "path",
    "title",
    "description",
    "tags",
    "audience",
    "revision",
    "updated_at",
    # F24: who may read the note, and what the key's user may do with it.
    "visibility",
    "my_permission",
)

# One note as read_doc returns it. `content` is the raw file with its
# frontmatter, so `body` (the same text minus that block) is dropped rather
# than paid for twice; ids and byte counts are for the app, not for an agent.
DOC_READ_FIELDS = (
    "scope",
    "path",
    "title",
    "description",
    "tags",
    "audience",
    "revision",
    "updated_at",
    "updated_by_name",
    "visibility",
    "my_permission",
    # True on an org owner/admin's audited break-glass read of a hidden note.
    "break_glass",
    "lang",
    "requested_lang",
    "translation_fallback",
    "translation_outdated",
    "translations",
    "extra_frontmatter",
    "links",
    "content",
)

# What write_doc answers: the agent just sent the content, so it is not echoed.
DOC_WRITE_FIELDS = (
    "scope",
    "path",
    "title",
    "audience",
    "revision",
    "created",
    "changed",
    "warnings",
    "links",
)


def trim(item: Any, fields: tuple[str, ...]) -> Any:
    """Keep only ``fields`` of a dict item; pass anything else through."""
    if not isinstance(item, dict):
        return item
    return {k: item[k] for k in fields if k in item}


def summarize_collection(data: Any, sample_size: int = 10) -> dict[str, Any]:
    """Reduce a possibly-huge list / {items,total} payload to count + sample.

    Count-plus-sample is an agent budget and stays here; WHERE the rows live is
    the route's business, so the unwrapping is ``tripl_cli.api``'s.
    This was the third hand-written copy of it in this package.

    The membership test is deliberately still spelled here: it asks whether the
    payload IS a page, which ``page_items`` cannot answer — an absent envelope
    and an empty one both unwrap to no rows, and the difference decides between
    a ``{total, sample}`` summary and passing an unrecognised object through.
    """
    if isinstance(data, dict) and "items" in data:
        items = page_items(data)
        total = page_total(data)
        # ``total`` is required on every list envelope this API serves, so the
        # fallback covers a malformed body only — and there the row count is the
        # one number that is certainly true.
        return {"total": len(items) if total is None else total, "sample": items[:sample_size]}
    if isinstance(data, list):
        return {"total": len(data), "sample": data[:sample_size]}
    return {"data": data}


def with_mutation_warnings(data: Any) -> Any:
    """Hoist ``EventMutationResponse.warnings`` to the front of the payload.

    The server may rename an event to its scan-derived canonical name or flag
    unknown ``${variable}`` tokens; the agent must read these and adopt the
    returned name/id instead of its proposed ones.

    Lives beside ``trim`` rather than in the HTTP client, where it used to sit:
    it never touches HTTP (the signature is ``(Any) -> Any`` over an already
    decoded body), its output is a PROMPT rather than data — "do not assume
    *your* proposed values were kept" is second-person address to a model — and
    it is the machinery implementing a rule written in prose three files away
    (server.INSTRUCTIONS, and the create_event/update_event descriptions). The
    shared `tripl` client is consumed by a CLI too, which would print a warning
    line and exit 0, never an ``IMPORTANT_warnings`` dict key.

    Re-checked when the CLI's read verbs landed and still has one caller:
    ``tripl events`` is read-only BY DECISION — a catalog write has
    to land on a plan branch, and reproducing that gate from a shell is a command
    surface of its own (see ``tripl_cli.commands.events``). There is no second
    caller to share with until the CLI can write.
    """
    if isinstance(data, dict) and data.get("warnings"):
        return {
            "IMPORTANT_warnings": data["warnings"],
            "note": (
                "The mutation succeeded WITH warnings. Adopt the server-canonical "
                "name/id from 'result' below; do not assume your proposed values "
                "were kept."
            ),
            "result": {k: v for k, v in data.items() if k != "warnings"},
        }
    return data
