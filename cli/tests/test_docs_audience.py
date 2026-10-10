"""The docs audience rule, held once in the shared layer.

`tripl docs ls --audience` and tripl-mcp's `list_docs` each carried their own
copy of the table; a change to what `both` matches had to land in two
distributions. Both now call `docs.filter_by_audience`.
"""

from __future__ import annotations

from tripl_cli.api import docs

ROWS = [
    {"path": "h.md", "audience": "human"},
    {"path": "a.md", "audience": "agent"},
    {"path": "b.md", "audience": "both"},
]


def test_the_table_covers_exactly_the_declared_audiences() -> None:
    """`--audience` takes `AUDIENCES` as its choices, so every choice needs a row."""
    assert set(docs.AUDIENCE_MATCHES) == set(docs.AUDIENCES)


def test_a_both_note_matches_either_reader() -> None:
    def paths(audience: str | None) -> list[str]:
        return [row["path"] for row in docs.filter_by_audience(ROWS, audience)]

    assert paths("human") == ["h.md", "b.md"]
    assert paths("agent") == ["a.md", "b.md"]
    assert paths("both") == ["b.md"]
    assert paths(None) == ["h.md", "a.md", "b.md"]
