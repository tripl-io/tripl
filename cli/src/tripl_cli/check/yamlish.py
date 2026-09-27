"""A reader for the small YAML subset ``.tripl/check.yml`` is written in.

Hand-rolled on purpose: ``pyproject.toml`` holds this distribution to httpx as
its only dependency, because tripl-mcp installs everything this package pulls
in. A tracking config needs very little of YAML, and what it needs is here:

* block mappings and block sequences, nested by indentation (spaces only);
* ``- key: value`` sequence items that open a mapping;
* flow collections, ``{a: b, c: [d, e]}``, which may span lines;
* single- and double-quoted scalars (double quotes take the usual escapes);
* plain scalars, resolved as YAML 1.2's core schema resolves them:
  ``null``/``~``, ``true``/``false``, integers, floats, else a string;
* ``#`` comments.

Anchors, aliases, tags, multi-document streams and block scalars (``|``/``>``)
are refused with a line number rather than misread. A config that needs them
can be written as JSON instead: the loader takes ``.json`` as well.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class YamlError(ValueError):
    """The text is not in the supported subset. ``line`` is 1-based, or 0 when unknown."""

    def __init__(self, message: str, line: int = 0) -> None:
        super().__init__(f"line {line}: {message}" if line else message)
        self.line = line


@dataclass(frozen=True, slots=True)
class _Line:
    indent: int
    text: str
    number: int


_INT = re.compile(r"[-+]?(?:0|[1-9][0-9_]*)")
_FLOAT = re.compile(
    r"[-+]?(?:[0-9][0-9_]*)?\.[0-9]+(?:[eE][-+]?[0-9]+)?"
    r"|[-+]?[0-9]+[eE][-+]?[0-9]+"
)
_UNSUPPORTED_START = ("&", "*", "!", "|", ">", "%", "@", "`")
_DOUBLE_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "0": "\0",
    '"': '"',
    "\\": "\\",
    "/": "/",
    " ": " ",
}


def loads(text: str) -> Any:
    """Parse ``text``. An empty document is ``None``."""
    lines = _lines(text)
    if not lines:
        return None
    value, index = _block(lines, 0, lines[0].indent)
    if index != len(lines):
        raise YamlError("unexpected indentation", lines[index].number)
    return value


def _lines(text: str) -> list[_Line]:
    """Comment-free, non-blank logical lines, with flow collections joined up."""
    physical: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlError("indent with spaces, not tabs", number)
        stripped = _strip_comment(raw).rstrip()
        if not stripped.strip():
            continue
        if stripped.strip() in ("---", "..."):
            if physical:
                raise YamlError("only one YAML document is supported", number)
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        physical.append(_Line(indent, stripped.strip(), number))
    joined: list[_Line] = []
    pending: _Line | None = None
    for line in physical:
        pending = (
            line
            if pending is None
            else _Line(pending.indent, f"{pending.text} {line.text}", pending.number)
        )
        if _flow_depth(pending.text) <= 0:
            joined.append(pending)
            pending = None
    if pending is not None:
        raise YamlError("unclosed '{' or '['", pending.number)
    return joined


def _strip_comment(raw: str) -> str:
    quote: str | None = None
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote == '"' and char == "\\":
            index += 2
            continue
        if quote is not None:
            if char == quote:
                # '' inside a single-quoted scalar is an escaped quote.
                if quote == "'" and raw[index + 1 : index + 2] == "'":
                    index += 2
                    continue
                quote = None
        elif char in ("'", '"') and (index == 0 or raw[index - 1] in " \t[{,:-"):
            quote = char
        elif char == "#" and (index == 0 or raw[index - 1] in " \t"):
            return raw[:index]
        index += 1
    return raw


def _flow_depth(text: str) -> int:
    """Open ``{``/``[`` count of the flow content in ``text`` (quotes respected)."""
    value = text
    if value.startswith("- "):
        value = value[2:]
    split = _split_key(value)
    if split is not None:
        value = split[1]
    value = value.strip()
    if not value.startswith(("{", "[")):
        return 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote == '"' and char == "\\":
            index += 2
            continue
        if quote is not None:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        index += 1
    return depth


def _split_key(text: str) -> tuple[str, str] | None:
    """``key: rest`` -> ``(key, rest)``; ``None`` when ``text`` is not a mapping entry."""
    if text.startswith(("'", '"')):
        quote = text[0]
        end = _quoted_end(text, 0)
        if end is None:
            return None
        after = text[end + 1 :]
        if after == ":" or after.startswith(": "):
            return _unquote(text[: end + 1], quote), after[1:].strip()
        return None
    if text.startswith(("{", "[", "- ")) or text == "-":
        return None
    match = re.search(r":(?:\s|$)", text)
    if match is None:
        return None
    key = text[: match.start()].strip()
    if not key:
        return None
    return key, text[match.end() :].strip()


def _quoted_end(text: str, start: int) -> int | None:
    quote = text[start]
    index = start + 1
    while index < len(text):
        char = text[index]
        if quote == '"' and char == "\\":
            index += 2
            continue
        if char == quote:
            if quote == "'" and text[index + 1 : index + 2] == "'":
                index += 2
                continue
            return index
        index += 1
    return None


def _unquote(token: str, quote: str) -> str:
    body = token[1:-1]
    if quote == "'":
        return body.replace("''", "'")
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        escape = body[index + 1 : index + 2]
        if escape in _DOUBLE_ESCAPES:
            out.append(_DOUBLE_ESCAPES[escape])
            index += 2
        elif escape == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", body[index + 2 : index + 6]):
            out.append(chr(int(body[index + 2 : index + 6], 16)))
            index += 6
        else:
            raise YamlError(f"unsupported escape '\\{escape}' in a double-quoted scalar")
    return "".join(out)


def _block(lines: list[_Line], index: int, indent: int) -> tuple[Any, int]:
    line = lines[index]
    if line.text == "-" or line.text.startswith("- "):
        return _sequence(lines, index, indent)
    if _split_key(line.text) is not None:
        return _mapping(lines, index, indent)
    value = _scalar_or_flow(line.text, line.number)
    return value, index + 1


def _sequence(lines: list[_Line], index: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while index < len(lines) and lines[index].indent == indent:
        line = lines[index]
        if not (line.text == "-" or line.text.startswith("- ")):
            break
        rest = line.text[1:].strip()
        if not rest:
            index += 1
            if index < len(lines) and lines[index].indent > indent:
                value, index = _block(lines, index, lines[index].indent)
            else:
                value = None
            items.append(value)
            continue
        # `- key: value` opens a mapping whose first entry sits on the dash line:
        # re-read that line as if the entry were written at the column after "- ".
        item_indent = indent + 2 + (len(line.text) - 2 - len(line.text[2:].lstrip()))
        # `lines` is this parse's own list (built by `_lines`), so the rewrite
        # touches nothing a caller holds.
        lines[index] = _Line(item_indent, rest, line.number)
        value, index = _block(lines, index, item_indent)
        items.append(value)
    if index < len(lines) and lines[index].indent > indent:
        raise YamlError("unexpected indentation", lines[index].number)
    return items, index


def _mapping(lines: list[_Line], index: int, indent: int) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}
    while index < len(lines) and lines[index].indent == indent:
        line = lines[index]
        split = _split_key(line.text)
        if split is None:
            raise YamlError(f"expected 'key: value', got {line.text!r}", line.number)
        key, rest = split
        if key in mapping:
            raise YamlError(f"duplicate key {key!r}", line.number)
        index += 1
        if rest:
            mapping[key] = _scalar_or_flow(rest, line.number)
        elif index < len(lines) and lines[index].indent > indent:
            mapping[key], index = _block(lines, index, lines[index].indent)
        elif (
            index < len(lines)
            and lines[index].indent == indent
            and (lines[index].text == "-" or lines[index].text.startswith("- "))
        ):
            # YAML allows a sequence at the SAME indent as its parent key.
            mapping[key], index = _sequence(lines, index, indent)
        else:
            mapping[key] = None
    if index < len(lines) and lines[index].indent > indent:
        raise YamlError("unexpected indentation", lines[index].number)
    return mapping, index


def _scalar_or_flow(text: str, number: int) -> Any:
    try:
        if text.startswith(("{", "[")):
            value, end = _flow(text, 0)
            if text[end:].strip():
                raise YamlError(f"unexpected text after a flow collection: {text[end:]!r}")
            return value
        if text.startswith(("'", '"')):
            quote_end = _quoted_end(text, 0)
            if quote_end is None:
                raise YamlError("unterminated quoted scalar")
            end = quote_end
            if text[end + 1 :].strip():
                raise YamlError(f"unexpected text after a quoted scalar: {text[end + 1 :]!r}")
            return _unquote(text[: end + 1], text[0])
        return _plain(text)
    except YamlError as exc:
        if exc.line:
            raise
        raise YamlError(str(exc), number) from None


def _plain(text: str) -> Any:
    value = text.strip()
    if value.startswith(_UNSUPPORTED_START):
        raise YamlError(
            f"{value[0]!r} (anchors, aliases, tags and block scalars) is not supported; "
            "quote the value"
        )
    if value in ("null", "Null", "NULL", "~", ""):
        return None
    if value in ("true", "True", "TRUE"):
        return True
    if value in ("false", "False", "FALSE"):
        return False
    if _INT.fullmatch(value):
        return int(value.replace("_", ""))
    if _FLOAT.fullmatch(value):
        return float(value.replace("_", ""))
    return value


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _flow(text: str, index: int) -> tuple[Any, int]:
    """One flow value starting at ``index``; returns it and the index after it."""
    index = _skip_space(text, index)
    if index >= len(text):
        raise YamlError("expected a value")
    char = text[index]
    if char == "{":
        return _flow_mapping(text, index + 1)
    if char == "[":
        return _flow_sequence(text, index + 1)
    if char in ("'", '"'):
        end = _quoted_end(text, index)
        if end is None:
            raise YamlError("unterminated quoted scalar")
        return _unquote(text[index : end + 1], char), end + 1
    end = index
    while end < len(text) and text[end] not in ",]}":
        # A ': ' ends a plain KEY inside a flow mapping; the caller handles it.
        if text[end] == ":" and (end + 1 == len(text) or text[end + 1] in " ,]}"):
            break
        end += 1
    return _plain(text[index:end]), end


def _flow_sequence(text: str, index: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while True:
        index = _skip_space(text, index)
        if index >= len(text):
            raise YamlError("unclosed '['")
        if text[index] == "]":
            return items, index + 1
        value, index = _flow(text, index)
        items.append(value)
        index = _skip_space(text, index)
        if index < len(text) and text[index] == ",":
            index += 1
        elif index < len(text) and text[index] == "]":
            continue
        else:
            raise YamlError("expected ',' or ']' in a flow sequence")


def _flow_mapping(text: str, index: int) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}
    while True:
        index = _skip_space(text, index)
        if index >= len(text):
            raise YamlError("unclosed '{'")
        if text[index] == "}":
            return mapping, index + 1
        key, index = _flow(text, index)
        if not isinstance(key, str | int | float | bool) or key is None:
            raise YamlError("a flow mapping key must be a scalar")
        index = _skip_space(text, index)
        if index < len(text) and text[index] == ":":
            value, index = _flow(text, index + 1)
            index = _skip_space(text, index)
        else:
            value = None
        name = key if isinstance(key, str) else str(key).lower()
        if name in mapping:
            raise YamlError(f"duplicate key {name!r}")
        mapping[name] = value
        if index < len(text) and text[index] == ",":
            index += 1
        elif index < len(text) and text[index] == "}":
            continue
        else:
            raise YamlError("expected ',' or '}' in a flow mapping")
