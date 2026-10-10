"""The tool tables and the PyPI README are held to the registered tools.

``website/docs/integrate/mcp-server.md`` lists every tool with its arguments, and
those cells drifted: ``list_events`` lost ``has_open_questions`` and ``property``,
``list_event_types`` and ``get_event_type_fields`` never showed ``branch_id``,
and ``list_variables`` never showed ``offset``/``limit``. The README (the PyPI
page) said "18 curated tools (15 read, 3 write)" when there were 23 with four
writes, right above its own note that it carries no tool counts. Both are now
read back against ``build_server()``, the schema an agent is actually held to.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from tripl_mcp.contract import TOOL_ENDPOINTS
from tripl_mcp.server import build_server

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_PATH = REPO_ROOT / "website" / "docs" / "integrate" / "mcp-server.md"
README_PATH = REPO_ROOT / "mcp-server" / "README.md"

# The two sections whose tables list the tools, by heading.
TOOL_TABLE_HEADINGS = ("### Read tools", "### Write tools")

# `| `tool` | `arguments` | backed by |`
_TOOL_ROW = re.compile(r"^\|\s*`(?P<tool>[a-z_]+)`\s*\|(?P<arguments>[^|]*)\|", re.MULTILINE)


@pytest.fixture(scope="module")
def tools() -> dict[str, Any]:
    return {tool.name: tool for tool in asyncio.run(build_server().list_tools())}


def _section(text: str, heading: str) -> str:
    start = text.index(f"\n{heading}\n")
    end = text.find("\n#", start + len(heading) + 2)
    return text[start : end if end != -1 else len(text)]


def _documented_arguments() -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """Each documented tool's (required, optional) arguments, read off its row.

    A cell is ``slug, event_id, branch_id?`` (``?`` marks optional), ``patch{...}``
    for an object argument, or ``—`` for none.
    """
    text = DOCS_PATH.read_text(encoding="utf-8")
    documented: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    for heading in TOOL_TABLE_HEADINGS:
        for row in _TOOL_ROW.finditer(_section(text, heading)):
            cell = row.group("arguments").strip().strip("`").strip()
            names = [] if cell in ("", "—", "-") else [part.strip() for part in cell.split(",")]
            names = [re.sub(r"\{.*\}", "", name) for name in names]
            required = frozenset(name for name in names if not name.endswith("?"))
            optional = frozenset(name.rstrip("?") for name in names if name.endswith("?"))
            documented[row.group("tool")] = (required, optional)
    return documented


def test_the_tool_tables_list_exactly_the_registered_tools() -> None:
    documented = set(_documented_arguments())
    assert documented == set(TOOL_ENDPOINTS), (
        f"{DOCS_PATH.name}'s tool tables and the registered tools disagree: "
        f"only documented {sorted(documented - set(TOOL_ENDPOINTS))}, "
        f"only registered {sorted(set(TOOL_ENDPOINTS) - documented)}"
    )


def test_every_documented_argument_cell_matches_the_tool_schema(tools: dict[str, Any]) -> None:
    wrong: list[str] = []
    for name, (required, optional) in sorted(_documented_arguments().items()):
        schema = dict(tools[name].input_schema)
        properties = set(schema.get("properties", {}))
        schema_required = set(schema.get("required", []))
        schema_optional = properties - schema_required
        if required != schema_required or optional != schema_optional:
            wrong.append(
                f"{name}: documented required {sorted(required)} optional {sorted(optional)}, "
                f"schema required {sorted(schema_required)} optional {sorted(schema_optional)}"
            )
    assert not wrong, f"{DOCS_PATH.name}'s Arguments cells have drifted:\n  " + "\n  ".join(wrong)


def test_the_readme_carries_no_tool_count() -> None:
    """Its own note says so: a hand-kept count went stale the moment a release shipped."""
    readme = README_PATH.read_text(encoding="utf-8")
    counts = re.findall(r"\b\d+\s+(?:curated\s+)?tools?\b|\(\d+\s+read,\s*\d+\s+write\)", readme)
    assert not counts, f"mcp-server/README.md states a tool count again: {counts}"


def test_the_readme_names_every_write_tool(tools: dict[str, Any]) -> None:
    """What an agent with a tk_w_ key can change, on the page that sells the package."""
    readme = README_PATH.read_text(encoding="utf-8")
    writes = sorted(
        name
        for name, tool in tools.items()
        if tool.annotations is not None and tool.annotations.read_only_hint is False
    )
    assert writes, "found no write tools - the derivation is broken"
    unnamed = [name for name in writes if f"`{name}`" not in readme]
    assert not unnamed, f"mcp-server/README.md never names these write tools: {unnamed}"


def test_the_readme_links_resolve_on_pypi() -> None:
    """PyPI renders the README with no repository around it: a relative link 404s."""
    readme = README_PATH.read_text(encoding="utf-8")
    relative = [
        target
        for target in re.findall(r"\]\(([^)]+)\)", readme)
        if not target.startswith(("http://", "https://", "#", "mailto:"))
    ]
    assert not relative, f"mcp-server/README.md has links PyPI cannot follow: {relative}"
