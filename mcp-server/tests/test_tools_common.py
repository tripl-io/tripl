"""Agent-payload shapers: mutation-warning hoisting, collection summaries.

Rehomed from tests/test_client.py when the HTTP client moved into the shared
`tripl` distribution — ``with_mutation_warnings`` stayed behind because it is an
MCP prompt concern, not a transport one.
"""

from __future__ import annotations

from tripl_mcp.tools._common import (
    DOC_READ_FIELDS,
    summarize_collection,
    trim,
    with_mutation_warnings,
)


def test_mutation_warnings_are_hoisted() -> None:
    data = {"id": "e1", "name": "purchase:success", "warnings": ["name was derived"]}

    result = with_mutation_warnings(data)

    assert result["IMPORTANT_warnings"] == ["name was derived"]
    assert result["result"]["name"] == "purchase:success"
    assert "warnings" not in result["result"]


def test_no_warnings_passes_through_unchanged() -> None:
    data = {"id": "e1", "warnings": []}

    assert with_mutation_warnings(data) is data


def test_a_summary_reads_the_envelope_the_shared_layer_defines() -> None:
    """Count-plus-sample is this consumer's budget; ``{items, total}`` is not.

    The row filter is the visible half of that split: ``page_items`` keeps
    objects and drops anything else, because every ``*ListResponse.items`` in
    this API is declared ``array[object]``. The hand-written copy that used to
    live in ``_common`` kept whatever the body carried, so one malformed
    response reached an agent as a row and the CLI as nothing — two readings of
    one wire format.
    """
    summary = summarize_collection({"items": [{"id": "a"}, "not an object"], "total": 2})

    assert summary["sample"] == [{"id": "a"}]
    assert summary["total"] == 2


def test_a_bare_array_and_a_non_page_object_are_still_told_apart() -> None:
    """Why the membership test stays spelled out in ``summarize_collection``.

    ``signals`` and ``jobs`` answer bare arrays, the reconciliation routes answer
    envelopes, and a monitors payload is neither. ``page_items`` cannot make that
    distinction — an absent envelope and an empty one unwrap identically — so the
    question of WHETHER this is a page has no shared answer to delegate to.
    """
    assert summarize_collection([1, 2, 3], sample_size=2) == {"total": 3, "sample": [1, 2]}
    assert summarize_collection({"status": "ok"}) == {"data": {"status": "ok"}}
    assert summarize_collection({"items": [], "total": 0}) == {"total": 0, "sample": []}


def test_a_break_glass_read_keeps_its_flag_for_the_agent() -> None:
    """An org admin's key may read a hidden note (audited); the agent must be told (F24)."""
    note = {"path": "diary.md", "content": "# Diary\n", "break_glass": True, "body": "x", "id": "d"}

    trimmed = trim(note, DOC_READ_FIELDS)

    assert trimmed["break_glass"] is True
    assert "body" not in trimmed
