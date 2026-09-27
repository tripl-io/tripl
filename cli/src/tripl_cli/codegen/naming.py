"""Plan strings -> identifiers, per target language.

A plan value is whatever a person typed: ``weather_alert``, ``Home Screen
View``, ``checkout:start``, ``1st_open``. An identifier is built from it by
splitting on everything that is not an ASCII letter or digit, splitting again on
camelCase boundaries, and joining the words in the language's convention:

* ``lower_camel`` — Swift/TS enum cases, parameters (``homeScreenView``);
* ``upper_camel`` — type names (``HomeScreenView``);
* ``upper_snake`` — Kotlin enum entries (``HOME_SCREEN_VIEW``).

Then the three things that make a string of words not yet an identifier:

* **empty** — a value of only punctuation or non-ASCII letters becomes ``value``;
* **digit first** — gets a leading ``_`` (``_1stOpen``), valid in all three;
* **reserved** — a keyword gets a trailing ``_`` (``default_``), a reserved
  TYPE name (``String``, ``Type``) gets ``Value`` (``TypeValue``). A suffix
  rather than Swift/Kotlin backticks: a suffixed name is valid in EVERY
  position — declaration, member access, type annotation — where backticks are
  not (Swift refuses ``self`` and ``Type`` even in backticks as member names).

Two values that land on the same identifier are told apart by ``Namer``: the
first keeps the name, the next gets ``2``, then ``3``, in the order the caller
takes them — callers sort first, so the numbering is deterministic. The raw plan
string is never derived from the identifier; it is always carried alongside it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

SWIFT = "swift"
KOTLIN = "kotlin"
TS = "ts"
LANGUAGES: tuple[str, ...] = (SWIFT, KOTLIN, TS)

_CHUNK = re.compile(r"[A-Za-z0-9]+")
_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z0-9]+")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

EMPTY_WORD = "value"
TYPE_SUFFIX = "Value"

_SWIFT_KEYWORDS = frozenset(
    [
        "associatedtype",
        "class",
        "deinit",
        "enum",
        "extension",
        "fileprivate",
        "func",
        "import",
        "init",
        "inout",
        "internal",
        "let",
        "operator",
        "precedencegroup",
        "private",
        "protocol",
        "public",
        "rethrows",
        "static",
        "struct",
        "subscript",
        "typealias",
        "var",
        "break",
        "case",
        "catch",
        "continue",
        "default",
        "defer",
        "do",
        "else",
        "fallthrough",
        "for",
        "guard",
        "if",
        "in",
        "repeat",
        "return",
        "throw",
        "switch",
        "where",
        "while",
        "Any",
        "as",
        "await",
        "false",
        "is",
        "nil",
        "self",
        "Self",
        "super",
        "throws",
        "true",
        "try",
        "any",
        "Type",
        "Protocol",
    ]
)
_KOTLIN_KEYWORDS = frozenset(
    [
        "as",
        "break",
        "class",
        "continue",
        "do",
        "else",
        "false",
        "for",
        "fun",
        "if",
        "in",
        "interface",
        "is",
        "null",
        "object",
        "package",
        "return",
        "super",
        "this",
        "throw",
        "true",
        "try",
        "typealias",
        "typeof",
        "val",
        "var",
        "when",
        "while",
    ]
)
_TS_KEYWORDS = frozenset(
    [
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "default",
        "delete",
        "do",
        "else",
        "enum",
        "export",
        "extends",
        "false",
        "finally",
        "for",
        "function",
        "if",
        "import",
        "in",
        "instanceof",
        "new",
        "null",
        "return",
        "super",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "typeof",
        "var",
        "void",
        "while",
        "with",
        "implements",
        "interface",
        "let",
        "package",
        "private",
        "protected",
        "public",
        "static",
        "yield",
        "await",
        "undefined",
        "arguments",
        "eval",
    ]
)

KEYWORDS: dict[str, frozenset[str]] = {
    SWIFT: _SWIFT_KEYWORDS,
    KOTLIN: _KOTLIN_KEYWORDS,
    TS: _TS_KEYWORDS,
}

# Reserved words only: Swift's contextual keywords (`open`, `some`, `actor` …) and
# TypeScript's (`type`, `string`, `from` …) are valid identifiers and stay unescaped,
# so `open` stays `open` rather than becoming `open_` everywhere it is a plan value.

# Names a generated TYPE must not take: the language's own everyday types, which
# a nested declaration would shadow for every other line of the generated file.
RESERVED_TYPES: dict[str, frozenset[str]] = {
    SWIFT: frozenset(
        [
            "Any",
            "AnyObject",
            "Array",
            "Bool",
            "Character",
            "Data",
            "Date",
            "Dictionary",
            "Double",
            "Error",
            "Float",
            "Int",
            "Never",
            "Optional",
            "Protocol",
            "Self",
            "Set",
            "String",
            "Type",
            "URL",
            "Void",
            "Sendable",
            "CaseIterable",
            "Hashable",
        ]
    ),
    KOTLIN: frozenset(
        [
            "Any",
            "Array",
            "Boolean",
            "Double",
            "Enum",
            "Float",
            "Int",
            "List",
            "Long",
            "Map",
            "Nothing",
            "Set",
            "String",
            "Unit",
            "Volatile",
            "Deprecated",
        ]
    ),
    TS: frozenset(
        [
            "Array",
            "Boolean",
            "Date",
            "Error",
            "Function",
            "Map",
            "Number",
            "Object",
            "Partial",
            "Promise",
            "Readonly",
            "Record",
            "Set",
            "String",
            "Symbol",
        ]
    ),
}

# Members an enum case must not take, because the generated enum already has
# them: Swift's `rawValue`/`allCases`, and the `name`/`properties` every named
# event enum exposes. Kotlin entries are UPPER_SNAKE and cannot collide.
RESERVED_MEMBERS: dict[str, frozenset[str]] = {
    SWIFT: frozenset({"rawValue", "allCases", "hashValue", "name", "properties", "init"}),
    KOTLIN: frozenset({"name", "ordinal", "value", "entries", "values", "valueOf"}),
    TS: frozenset(),
}


def words(text: str) -> list[str]:
    """``'Home Screen View'`` -> ``[Home, Screen, View]``; ``'URLOpened'`` -> ``[URL, Opened]``."""
    found: list[str] = []
    for chunk in _CHUNK.findall(text):
        found.extend(_WORD.findall(chunk))
    return found


def _capitalised(word: str) -> str:
    lowered = word.lower()
    return lowered[:1].upper() + lowered[1:]


def lower_camel(text: str) -> str:
    parts = words(text) or [EMPTY_WORD]
    return parts[0].lower() + "".join(_capitalised(word) for word in parts[1:])


def upper_camel(text: str) -> str:
    parts = words(text) or [EMPTY_WORD]
    return "".join(_capitalised(word) for word in parts)


def upper_snake(text: str) -> str:
    parts = words(text) or [EMPTY_WORD]
    return "_".join(word.upper() for word in parts)


def _leading_digit(name: str) -> str:
    return "_" + name if name[:1].isdigit() else name


def member(text: str, language: str, *, snake: bool = False) -> str:
    """An enum case, parameter or property name: lowerCamel (or UPPER_SNAKE), escaped."""
    name = _leading_digit(upper_snake(text) if snake else lower_camel(text))
    if name in KEYWORDS[language] or name in RESERVED_MEMBERS[language]:
        name += "_"
    return name


def type_name(text: str, language: str) -> str:
    """A type name: UpperCamel, escaped."""
    name = _leading_digit(upper_camel(text))
    if name in RESERVED_TYPES[language] or name in KEYWORDS[language]:
        name += TYPE_SUFFIX
    return name


def is_identifier(name: str, language: str) -> bool:
    """Is ``name`` usable as written — for ``type_names`` overrides, which are taken verbatim."""
    return _IDENTIFIER.fullmatch(name) is not None and name not in KEYWORDS[language]


class Namer:
    """Hands out unique names within one scope. The first taker keeps the plain name."""

    def __init__(self, taken: Iterable[str] = ()) -> None:
        self._taken: set[str] = set(taken)

    def reserve(self, name: str) -> None:
        self._taken.add(name)

    def take(self, name: str) -> str:
        if name not in self._taken:
            self._taken.add(name)
            return name
        number = 2
        while f"{name}{number}" in self._taken:
            number += 1
        unique = f"{name}{number}"
        self._taken.add(unique)
        return unique
