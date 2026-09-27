"""A tiny Mustache subset, enough for the built-in and user-supplied codegen templates.

Hand-rolled for the same reason ``check/yamlish.py`` is: this distribution
depends on httpx alone. Supported:

``{{name}}`` / ``{{a.b}}`` / ``{{.}}``
    Insert a value VERBATIM. There is no HTML escaping — this renders source
    code, and every value that needs quoting arrives pre-quoted for its target
    language (``literal``, ``ident``, … in the context; see ``codegen/context.py``).
    ``{{&name}}`` is accepted as a synonym. A name that resolves to nothing is an
    ERROR with a line number, not an empty string: a typo in a template must not
    silently generate code with a hole in it.
``{{#name}}…{{/name}}``
    A list renders its body once per item, with the item pushed on the context
    stack; a mapping renders once with it pushed; any other truthy value once.
    A missing name, ``None``, ``False``, ``""`` and ``[]`` render nothing.
``{{^name}}…{{/name}}``
    The inverse: renders once when the name is missing or falsy.
``{{! comment }}``
    Dropped.

A section, inverted-section, closing or comment tag alone on its line
("standalone", in Mustache's terms) takes the whole line with it — indentation
and newline — so templates can be laid out legibly without blank lines leaking
into the output. Partials, lambdas and delimiter changes are not supported.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_TAG = re.compile(r"\{\{\s*([#^/!&]?)\s*(.*?)\s*\}\}", re.DOTALL)
_STANDALONE = re.compile(r"[ \t]*(\{\{\s*[#^/!][^{}]*\}\})[ \t]*\r?\n?")
_NAME = re.compile(r"\.|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


class TemplateError(ValueError):
    """The template is malformed, or names a value the context does not have."""

    def __init__(self, message: str, line: int = 0, source: str | None = None) -> None:
        where = f"{source}:" if source else ""
        where += f"{line}: " if line else (" " if source else "")
        super().__init__(f"{where}{message}")
        self.line = line


@dataclass
class _Section:
    name: str
    inverted: bool
    line: int
    children: list[_Node] = field(default_factory=list)


@dataclass(frozen=True)
class _Var:
    name: str
    line: int


_Node = str | _Var | _Section


def _strip_standalone(text: str) -> str:
    """Drop the indentation and newline around a tag that is alone on its line."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        match = _STANDALONE.fullmatch(line)
        out.append(match.group(1) if match is not None else line)
    return "".join(out)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


class Template:
    """A parsed template. Parse once, render many times."""

    def __init__(self, text: str, *, source: str | None = None) -> None:
        self.source = source
        self._nodes = self._parse(text)

    def _parse(self, text: str) -> list[_Node]:
        # Line numbers are taken from the ORIGINAL text, so a standalone tag's
        # line is still where the author wrote it. Stripping keeps line breaks
        # of every non-standalone line, and a standalone line holds exactly one
        # tag, so counting tags per original line is enough to map back.
        stripped = _strip_standalone(text)
        original_lines = [_line_of(text, m.start()) for m in _TAG.finditer(text)]
        root: list[_Node] = []
        stack: list[_Section] = []
        position = 0
        for number, match in enumerate(_TAG.finditer(stripped)):
            line = original_lines[number] if number < len(original_lines) else 0
            target = stack[-1].children if stack else root
            if match.start() > position:
                target.append(stripped[position : match.start()])
            position = match.end()
            sigil, name = match.group(1), match.group(2)
            if sigil == "!":
                continue
            if not _NAME.fullmatch(name):
                raise TemplateError(f"{{{{{sigil}{name}}}}} is not a valid tag", line, self.source)
            if sigil in ("#", "^"):
                section = _Section(name=name, inverted=sigil == "^", line=line)
                target.append(section)
                stack.append(section)
            elif sigil == "/":
                if not stack:
                    raise TemplateError(f"{{{{/{name}}}}} closes nothing", line, self.source)
                opened = stack.pop()
                if opened.name != name:
                    raise TemplateError(
                        f"{{{{/{name}}}}} closes {{{{#{opened.name}}}}} "
                        f"opened on line {opened.line}",
                        line,
                        self.source,
                    )
            else:
                target.append(_Var(name=name, line=line))
        if stack:
            raise TemplateError(
                f"{{{{#{stack[-1].name}}}}} is never closed", stack[-1].line, self.source
            )
        if position < len(stripped):
            (stack[-1].children if stack else root).append(stripped[position:])
        return root

    def render(self, context: Mapping[str, Any], *frames: Any) -> str:
        """Render with ``context`` at the bottom of the stack and ``frames`` pushed
        over it, innermost last, as if the template sat inside sections of them."""
        out: list[str] = []
        self._render(self._nodes, [context, *frames], out)
        return "".join(out)

    def _render(self, nodes: Sequence[_Node], stack: list[Any], out: list[str]) -> None:
        for node in nodes:
            if isinstance(node, str):
                out.append(node)
            elif isinstance(node, _Var):
                found, value = _lookup(stack, node.name)
                if not found:
                    raise TemplateError(
                        f"unknown value {{{{{node.name}}}}}", node.line, self.source
                    )
                out.append(_text(value))
            else:
                found, value = _lookup(stack, node.name)
                truthy = found and _truthy(value)
                if node.inverted:
                    if not truthy:
                        self._render(node.children, stack, out)
                    continue
                if not truthy:
                    continue
                items = value if isinstance(value, list | tuple) else [value]
                for item in items:
                    stack.append(item)
                    try:
                        self._render(node.children, stack, out)
                    finally:
                        stack.pop()


def _truthy(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str | list | tuple | Mapping):
        return len(value) > 0
    return True


def _text(value: Any) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _lookup(stack: Sequence[Any], name: str) -> tuple[bool, Any]:
    if name == ".":
        return True, stack[-1]
    head, *rest = name.split(".")
    for frame in reversed(stack):
        if isinstance(frame, Mapping) and head in frame:
            value = frame[head]
            for part in rest:
                if not (isinstance(value, Mapping) and part in value):
                    return False, None
                value = value[part]
            return True, value
    return False, None


def render(text: str, context: Mapping[str, Any], *, source: str | None = None) -> str:
    """Parse and render in one step."""
    return Template(text, source=source).render(context)
