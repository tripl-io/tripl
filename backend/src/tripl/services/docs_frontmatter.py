"""Frontmatter parsing for docs catalog notes (F22, GH #299).

A note may open with a YAML block::

    ---
    title: Warehouse gotchas
    description: What surprises people about the events table
    tags: [warehouse, sql]
    audience: agent
    ---

The raw content is stored verbatim; this module only READS it, for the columns
the catalog lists, searches and filters on. Recognised keys are ``title``
(falling back to ``name``, which agent-skill files use, then the first ``# H1``,
then the file name), ``description``, ``tags`` (a list or a comma string) and
``audience`` (``human`` | ``agent`` | ``both``, default ``both``). Every other
key is kept as-is and returned as ``extra`` without validation, so a skill's
``allowed-tools`` or ``metadata`` survives a round trip.

YAML is read with a ``SafeLoader`` (no tags, no object construction) that also
refuses aliases: ``*ref`` lets a few hundred bytes expand into gigabytes once
the tree is copied (the "billion laughs"), and ``&r [*r]`` is a cycle. The copy
into JSON is bounded in depth and node count as a second fence.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from tripl.services.docs_paths import (
    MAX_DESCRIPTION_CHARS,
    MAX_FRONTMATTER_BYTES,
    MAX_TAG_CHARS,
    MAX_TAGS,
    MAX_TITLE_CHARS,
    file_stem,
)

AUDIENCES = ("human", "agent", "both")
_RECOGNISED = frozenset({"title", "description", "tags", "audience"})
_TAG = re.compile(r"^[A-Za-z0-9_.:-]+$")
_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)
_CLOSERS = ("\n---\n", "\n...\n")
_CLOSERS_AT_END = ("\n---", "\n...")
MAX_FRONTMATTER_DEPTH = 20
MAX_FRONTMATTER_NODES = 10_000


class DocContentError(ValueError):
    """Frontmatter the catalog refuses; the message is safe to show (422)."""


@dataclass(frozen=True)
class ParsedDoc:
    title: str
    description: str
    tags: list[str]
    audience: str
    body: str
    extra: dict[str, Any] = field(default_factory=dict)


def split_frontmatter(content: str) -> tuple[str | None, str]:
    """``(yaml_text, body)``; ``yaml_text`` is None when the note has no block.

    The block must start at the very first byte with ``---`` on its own line and
    close with ``---`` or ``...`` on its own line. A block that opens but never
    closes is not frontmatter: the whole content is the body, as a Markdown
    renderer would show it (a leading horizontal rule).
    """
    text = content.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return None, content
    rest = text[4:]
    # An empty block: "---\n---\n".
    for closer in ("---\n", "...\n"):
        if rest.startswith(closer):
            return "", rest[len(closer) :]
    if rest in {"---", "..."}:
        return "", ""
    best: tuple[int, int] | None = None
    for closer in _CLOSERS:
        index = rest.find(closer)
        if index >= 0 and (best is None or index < best[0]):
            best = (index, len(closer))
    if best is None:
        for closer in _CLOSERS_AT_END:
            if rest.endswith(closer):
                return rest[: -len(closer)], ""
        return None, content
    index, length = best
    return rest[:index], rest[index + length :]


class _NoAliasSafeLoader(yaml.SafeLoader):
    """``SafeLoader`` that refuses YAML aliases before any node is built."""

    _depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise DocContentError("Frontmatter may not use YAML anchors or aliases")
        self._depth += 1
        try:
            if self._depth > MAX_FRONTMATTER_DEPTH + 1:
                raise DocContentError(
                    f"Frontmatter is nested more than {MAX_FRONTMATTER_DEPTH} levels deep"
                )
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


class _Budget:
    """Nodes left for :func:`_jsonable`, shared across one frontmatter block."""

    def __init__(self) -> None:
        self.nodes = MAX_FRONTMATTER_NODES

    def spend(self, depth: int) -> None:
        self.nodes -= 1
        if self.nodes < 0:
            raise DocContentError(f"Frontmatter holds more than {MAX_FRONTMATTER_NODES} values")
        if depth > MAX_FRONTMATTER_DEPTH:
            raise DocContentError(
                f"Frontmatter is nested more than {MAX_FRONTMATTER_DEPTH} levels deep"
            )


def _jsonable(value: Any, budget: _Budget | None = None, depth: int = 0) -> Any:
    """YAML scalars JSON cannot carry (dates, sets, bytes) as strings."""
    budget = budget if budget is not None else _Budget()
    budget.spend(depth)
    if isinstance(value, dict):
        return {str(key): _jsonable(item, budget, depth + 1) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(item, budget, depth + 1) for item in value]
    if isinstance(value, dt.date | dt.datetime | dt.time):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _load_mapping(yaml_text: str) -> dict[str, Any]:
    if len(yaml_text.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise DocContentError(f"Frontmatter is larger than {MAX_FRONTMATTER_BYTES // 1024} KiB")
    try:
        loaded = yaml.load(yaml_text, Loader=_NoAliasSafeLoader) if yaml_text.strip() else {}
    except RecursionError as exc:
        raise DocContentError("Frontmatter is nested too deeply") from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 2}, column {mark.column + 1})" if mark else ""
        problem = getattr(exc, "problem", None) or "not valid YAML"
        raise DocContentError(f"Frontmatter is not valid YAML{where}: {problem}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise DocContentError("Frontmatter must be a mapping of keys to values")
    return {str(key): value for key, value in loaded.items()}


def load_frontmatter_mapping(yaml_text: str) -> dict[str, Any]:
    """A frontmatter block read the safe way (no aliases, bounded size), as a mapping."""
    return _load_mapping(yaml_text)


def _text(meta: dict[str, Any], key: str, limit: int) -> str | None:
    if key not in meta or meta[key] is None:
        return None
    value = meta[key]
    if isinstance(value, dict | list):
        raise DocContentError(f"Frontmatter '{key}' must be text")
    text = str(value).strip()
    if len(text) > limit:
        raise DocContentError(f"Frontmatter '{key}' is longer than {limit} characters")
    return text


def _tags(meta: dict[str, Any]) -> list[str]:
    raw = meta.get("tags")
    if raw is None:
        return []
    if isinstance(raw, str):
        items: list[Any] = raw.split(",")
    elif isinstance(raw, list):
        items = raw
    else:
        raise DocContentError("Frontmatter 'tags' must be a list or a comma-separated string")
    tags: list[str] = []
    for item in items:
        if isinstance(item, dict | list) or item is None:
            raise DocContentError("Frontmatter 'tags' must hold plain words")
        tag = str(item).strip()
        if not tag:
            continue
        if len(tag) > MAX_TAG_CHARS:
            raise DocContentError(f"Tag '{tag[:20]}...' is longer than {MAX_TAG_CHARS} characters")
        if not _TAG.match(tag):
            raise DocContentError(f"Tag '{tag}' may use only letters, digits and _ . : -")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise DocContentError(f"At most {MAX_TAGS} tags are allowed")
    return tags


def _audience(meta: dict[str, Any]) -> str:
    raw = meta.get("audience")
    if raw is None:
        return "both"
    value = str(raw).strip().lower()
    if value not in AUDIENCES:
        raise DocContentError("Frontmatter 'audience' must be one of: human, agent, both")
    return value


def _first_h1(body: str) -> str | None:
    in_fence = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _H1.match(line)
        if match:
            return match.group(1).strip()
    return None


def parse_frontmatter(content: str, path: str) -> ParsedDoc:
    """Read a note's frontmatter into catalog columns, or :class:`DocContentError`."""
    yaml_text, body = split_frontmatter(content)
    meta = _load_mapping(yaml_text) if yaml_text is not None else {}
    title = _text(meta, "title", MAX_TITLE_CHARS)
    if not title:
        name = _text(meta, "name", 10_000)
        title = name[:MAX_TITLE_CHARS] if name else None
    if not title:
        heading = _first_h1(body)
        title = heading[:MAX_TITLE_CHARS] if heading else None
    if not title:
        title = file_stem(path)[:MAX_TITLE_CHARS]
    description = _text(meta, "description", MAX_DESCRIPTION_CHARS) or ""
    budget = _Budget()
    extra = {key: _jsonable(value, budget) for key, value in meta.items() if key not in _RECOGNISED}
    return ParsedDoc(
        title=title,
        description=description,
        tags=_tags(meta),
        audience=_audience(meta),
        body=body,
        extra=extra,
    )
