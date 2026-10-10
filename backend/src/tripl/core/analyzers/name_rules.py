"""Naming rules as slot parsers (GH #265, F12).

A scan's ``event_name_format`` (``{category}:{action}:{label}``,
``{screen}_{element}_tap``, ``{a} - {b}``) spells event names from warehouse
values. To compare two names under one rule VALUE BY VALUE, a name has to be
split back into those values. The rule itself says how: every non-empty
literal between two placeholders is a separator, whatever it is — ``:``,
``_``, ``-``, a space or a longer string — and matching the whole format as a
regular expression with one non-greedy group per placeholder recovers the
slots. A value that contains the separator itself lands in the LAST slot
(non-greedy groups before it stop at the first separator), which is at least
the same for every name of the rule.

Pure: no database, no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from tripl.core.name_template import NAME_FORMAT_PATTERN


@dataclass(frozen=True)
class NameRule:
    """One compiled ``event_name_format``.

    ``parts`` is the format as ``(is_slot, text)`` pieces with adjacent
    placeholders merged into one slot (``{a}{b}`` cannot be split, so it is one
    value); ``slots`` is the placeholder text of each slot, for labels.
    """

    name_format: str
    parts: tuple[tuple[bool, str], ...]
    pattern: re.Pattern[str]

    @property
    def slots(self) -> tuple[str, ...]:
        return tuple(text for is_slot, text in self.parts if is_slot)

    def split(self, name: str) -> tuple[str, ...] | None:
        """The slot values of ``name``, or None when it does not follow the rule."""
        match = self.pattern.fullmatch(name or "")
        return match.groups() if match else None

    def render(self, values: tuple[str, ...]) -> str:
        """The format with ``values`` in its slots (``*`` works as a mask)."""
        pieces: list[str] = []
        slot = 0
        for is_slot, text in self.parts:
            if is_slot:
                pieces.append(values[slot])
                slot += 1
            else:
                pieces.append(text)
        return "".join(pieces)


@lru_cache(maxsize=512)
def compile_rule(name_format: str | None) -> NameRule | None:
    """``name_format`` as a :class:`NameRule`, or None when it has no placeholder."""
    if not name_format or not name_format.strip():
        return None
    parts: list[tuple[bool, str]] = []
    position = 0
    for match in NAME_FORMAT_PATTERN.finditer(name_format):
        literal = name_format[position : match.start()]
        if literal:
            parts.append((False, literal))
        if parts and parts[-1][0]:
            parts[-1] = (True, parts[-1][1] + match.group(0))
        else:
            parts.append((True, match.group(0)))
        position = match.end()
    if name_format[position:]:
        parts.append((False, name_format[position:]))
    if not any(is_slot for is_slot, _ in parts):
        return None
    regex = "".join("(.+?)" if is_slot else re.escape(text) for is_slot, text in parts)
    return NameRule(
        name_format=name_format,
        parts=tuple(parts),
        pattern=re.compile(regex, re.IGNORECASE | re.DOTALL),
    )
