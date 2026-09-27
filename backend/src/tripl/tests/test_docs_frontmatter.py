"""Docs catalog frontmatter parsing (F22, GH #299)."""

import pytest

from tripl.services.docs_frontmatter import (
    DocContentError,
    parse_frontmatter,
    split_frontmatter,
)
from tripl.services.docs_paths import MAX_TAGS


def test_recognised_keys_become_columns_and_the_rest_is_kept() -> None:
    content = (
        "---\n"
        "title: Warehouse gotchas\n"
        "description: What surprises people about the events table\n"
        "tags: [warehouse, sql, warehouse]\n"
        "audience: Agent\n"
        "allowed-tools: [Read, Grep]\n"
        "metadata:\n  owner: data-team\n"
        "reviewed: 2026-01-02\n"
        "---\n"
        "# Heading\n\nBody.\n"
    )
    parsed = parse_frontmatter(content, "guides/warehouse.md")

    assert parsed.title == "Warehouse gotchas"
    assert parsed.description == "What surprises people about the events table"
    assert parsed.tags == ["warehouse", "sql"]
    assert parsed.audience == "agent"
    assert parsed.body == "# Heading\n\nBody.\n"
    assert parsed.extra == {
        "allowed-tools": ["Read", "Grep"],
        "metadata": {"owner": "data-team"},
        # A YAML date is not JSON; it comes back as its ISO string.
        "reviewed": "2026-01-02",
    }


@pytest.mark.parametrize(
    ("content", "title"),
    [
        ("---\nname: event-query-recipes\n---\n# Ignored\n", "event-query-recipes"),
        ("# Event query recipes\n\ntext", "Event query recipes"),
        ("```\n# not a heading\n```\n# Real one\n", "Real one"),
        ("no heading at all", "recipes"),
        ("---\ntitle: ''\n---\n# From heading\n", "From heading"),
    ],
)
def test_title_falls_back_through_name_heading_and_file_name(content: str, title: str) -> None:
    assert parse_frontmatter(content, "a/recipes.md").title == title


def test_tags_accept_a_comma_string() -> None:
    parsed = parse_frontmatter("---\ntags: a, b ,c\n---\n", "x.md")
    assert parsed.tags == ["a", "b", "c"]
    assert parsed.audience == "both"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("---\n- a\n- b\n---\n", "mapping"),
        ("---\nkey: [unclosed\n---\n", "not valid YAML"),
        ("---\naudience: robots\n---\n", "audience"),
        ("---\ntags: ['has space']\n---\n", "Tag 'has space'"),
        ("---\ntags: {a: 1}\n---\n", "list or a comma"),
        ("---\ntitle: [a]\n---\n", "must be text"),
        ("---\ntitle: " + "x" * 301 + "\n---\n", "longer than 300"),
        ("---\ntags: [" + ",".join(f"t{i}" for i in range(MAX_TAGS + 1)) + "]\n---\n", "At most"),
        ("---\nbig: " + "x" * (17 * 1024) + "\n---\n", "larger than 16 KiB"),
    ],
)
def test_invalid_frontmatter_is_refused_precisely(content: str, message: str) -> None:
    with pytest.raises(DocContentError, match=message):
        parse_frontmatter(content, "x.md")


def test_yaml_is_loaded_safely() -> None:
    with pytest.raises(DocContentError):
        parse_frontmatter("---\nx: !!python/object/apply:os.system ['true']\n---\n", "x.md")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("---\na: 1\n---\nbody", ("a: 1", "body")),
        ("---\na: 1\n...\nbody", ("a: 1", "body")),
        ("---\n---\nbody", ("", "body")),
        ("---\na: 1\n---", ("a: 1", "")),
        ("---\r\na: 1\r\n---\r\nbody", ("a: 1", "body")),
        # Not at byte 0, or never closed: not frontmatter.
        ("\n---\na: 1\n---\n", (None, "\n---\na: 1\n---\n")),
        ("---\nno close", (None, "---\nno close")),
    ],
)
def test_split_frontmatter(content: str, expected: tuple[str | None, str]) -> None:
    assert split_frontmatter(content) == expected


def _billion_laughs(levels: int) -> str:
    lines = ["a: &a [x, x, x, x, x, x, x, x, x]"]
    previous = "a"
    for level in range(1, levels + 1):
        lines.append(f"l{level}: &l{level} [" + ", ".join([f"*{previous}"] * 9) + "]")
        previous = f"l{level}"
    return "---\n" + "\n".join(lines) + "\n---\nbody\n"


@pytest.mark.parametrize(
    "content",
    [
        _billion_laughs(8),
        "---\nx: &r [*r]\n---\n",
        "---\nbase: &b {k: 1}\nother: *b\n---\n",
    ],
    ids=["nested-aliases", "self-alias", "plain-alias"],
)
def test_yaml_aliases_are_refused_before_expansion(content: str) -> None:
    with pytest.raises(DocContentError, match="anchors or aliases"):
        parse_frontmatter(content, "x.md")


def test_an_anchor_without_an_alias_is_harmless() -> None:
    parsed = parse_frontmatter("---\nk: &a 1\n---\n", "x.md")
    assert parsed.extra == {"k": 1}


@pytest.mark.parametrize("depth", [25, 5000])
def test_deeply_nested_frontmatter_is_a_content_error(depth: int) -> None:
    content = "---\nt: " + "[" * depth + "]" * depth + "\n---\n"
    with pytest.raises(DocContentError, match="nested"):
        parse_frontmatter(content, "x.md")
