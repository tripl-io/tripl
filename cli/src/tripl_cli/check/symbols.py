"""What an identifier in a tracking call stands for.

Enum raw values, string constants, and the strings a function can return.

A wrapper call rarely passes its category as ``"home"``. It passes ``.home``,
``Events.Category.home.rawValue`` or ``Category.HOME.value``, and the string the
plan knows lives in an enum declared in another file. This module builds that
lookup from the enum files named in the config (and from every scanned source),
per language:

* Swift — ``enum Category: String { case home, map = "map_screen" }``. A case
  with no ``=`` in a ``String`` enum has its own name as its raw value. Also
  ``static let x = "y"`` inside any type and ``let x = "y"`` at file scope.
* Kotlin / Java — ``enum class Category(val value: String) { HOME("home") }``
  (the first string argument), ``const val X = "y"``, ``static final String X = "y"``.
* TypeScript — ``enum Category { Home = "home" }``, ``const Category = { Home: "home" }``
  and ``const X = "y"``.
* Objective-C — ``NSString *const X = @"y";`` and ``#define X @"y"``.

Every entry is keyed by its qualified path (``Events.Category.home``). A dotted
reference resolves on its longest matching suffix of at least two segments;
Swift's ``.home`` shorthand resolves on the member name alone; a bare identifier
resolves only against file-scope constants. When two enums share a case name
with DIFFERENT raw values, the plan field the value is going to (``category``)
picks the enum whose name matches it; failing that the value stays dynamic
rather than guessed.

Accessors are language-aware. ``rawValue`` / ``value`` (and the other
``RAW_ACCESSORS``) read the raw value. The accessors that read the CASE NAME
instead — Kotlin/Java ``.name`` / ``name()`` and Swift ``.description`` — resolve
to the case identifier (``Category.HOME.name`` -> ``HOME``), and only for a path
that names a known enum case; anything else stays dynamic.

Functions: ``func name(…) -> String { … }`` (and the Kotlin, Java and TS
equivalents, plus Swift's computed ``var name: String { … }``) whose every
``return`` is a string literal resolve to that set of strings. One non-literal
return makes the whole function dynamic.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from tripl_cli.check.calls import matching, split_top_level
from tripl_cli.check.lexer import IDENT, NUMBER, PUNCT, STRING, Interp, Token

# Accessors that read the raw value off an enum case, in the languages covered.
RAW_ACCESSORS = frozenset(
    {"rawValue", "value", "key", "raw", "string", "description", "name", "title", "id"}
)
# Accessors that read the enum case's own IDENTIFIER, per language: Kotlin's
# `.name`, Java's `name()`, Swift's `.description` (a case prints as its name).
CASE_NAME_ACCESSORS: dict[str, frozenset[str]] = {
    "kotlin": frozenset({"name"}),
    "java": frozenset({"name"}),
    "swift": frozenset({"description"}),
}
# The accessors an interpolation hint drops (`type.rawValue` -> `type`); any other
# trailing member is the variable itself (`sheet.id` -> `id`).
_HINT_ACCESSORS = frozenset({"rawValue", "value"})
# How many alternative strings one function may return before it counts as dynamic.
MAX_CHOICES = 50

_SWIFT_TYPES = frozenset({"enum", "struct", "class", "extension", "protocol", "actor"})
_JVM_TYPES = frozenset({"enum", "class", "object", "interface"})
_FUNCTION_KEYWORDS = frozenset({"func", "fun", "function"})
# A type header ends at its `{`; one of these first means it had no body here.
_HEADER_STOPS = frozenset(
    {
        "func",
        "fun",
        "function",
        "val",
        "var",
        "let",
        "class",
        "struct",
        "enum",
        "case",
        "import",
        "return",
        "typealias",
        "package",
    }
)
_HEADER_LIMIT = 200
_STRING_TYPES = frozenset({"String", "string", "NSString"})


@dataclass(frozen=True, slots=True)
class Entry:
    path: tuple[str, ...]
    value: str
    # A member of a type (enum case, static constant) rather than a file-scope constant.
    member: bool


def normalise_name(text: str) -> str:
    """``EventCategory`` / ``event_category`` / ``Categories`` -> one comparable form."""
    lowered = re.sub(r"[^a-z0-9]", "", text.lower())
    return lowered[:-1] if lowered.endswith("s") and len(lowered) > 3 else lowered


def variable_name(expr: str) -> str:
    """The plan-variable name an interpolated expression stands for.

    The last identifier that is not a raw-value accessor (``rawValue`` /
    ``value``): ``id`` -> ``id``; ``sheet.id`` -> ``id``; ``type.rawValue`` ->
    ``type``; ``name(for: x)`` -> ``name``. ``self`` / ``this`` / ``it`` /
    ``super`` never count. Anything without an identifier is ``value``.
    """
    flattened = expr
    previous: str | None = None
    while previous != flattened:
        previous = flattened
        flattened = re.sub(r"\([^()]*\)|\[[^\[\]]*\]", "", flattened)
    names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", flattened)
    names = [name for name in names if name not in ("self", "this", "it", "super")]
    while len(names) > 1 and names[-1] in _HINT_ACCESSORS:
        names.pop()
    return names[-1] if names else "value"


def template_text(parts: Sequence[str | Interp]) -> str:
    """A string's value with every interpolation written as a ``${name}`` plan token."""
    return "".join(
        f"${{{variable_name(part.expr)}}}" if isinstance(part, Interp) else part for part in parts
    )


def literal_text(token: Token) -> str | None:
    """A STRING token's value when it has no interpolation."""
    if token.kind != STRING or any(isinstance(part, Interp) for part in token.parts):
        return None
    return "".join(part for part in token.parts if isinstance(part, str))


def number_text(token: Token) -> str | None:
    """A NUMBER token as the text a plan value would hold (type suffixes dropped)."""
    if token.kind != NUMBER or token.text.startswith("'"):
        return None
    text = token.text.replace("_", "")
    if text.lower().startswith(("0x", "0b", "0o")):
        return text
    return text.rstrip("fFlLuUdDn")


@dataclass
class SymbolIndex:
    """Built once per run from every configured enum file and scanned source."""

    entries: list[Entry] = field(default_factory=list)
    # Qualified paths of every enum case seen, with or without a raw value.
    cases: set[tuple[str, ...]] = field(default_factory=set)
    functions: dict[str, list[tuple[str, ...] | None]] = field(default_factory=dict)

    # --- building ------------------------------------------------------------
    def add(self, path: tuple[str, ...], value: str, *, member: bool) -> None:
        self.entries.append(Entry(path, value, member))

    def add_case(self, path: tuple[str, ...]) -> None:
        self.cases.add(path)

    def add_function(self, name: str, values: tuple[str, ...] | None) -> None:
        self.functions.setdefault(name, []).append(values)

    def index_tokens(self, tokens: list[Token], language: str) -> None:
        if language == "swift":
            _index_scoped(self, tokens, language, _SWIFT_TYPES)
        elif language in ("kotlin", "java"):
            _index_scoped(self, tokens, language, _JVM_TYPES)
        elif language == "ts":
            _index_ts(self, tokens)
        elif language == "objc":
            _index_objc(self, tokens)
        _index_functions(self, tokens, language)

    # --- lookup --------------------------------------------------------------
    def resolve(
        self,
        path: Sequence[str],
        *,
        hint: str | None = None,
        shorthand: bool = False,
        language: str = "",
    ) -> str | None:
        """The raw value ``path`` refers to, or ``None`` when unknown or ambiguous.

        In ``language`` a trailing case-name accessor (Kotlin/Java ``name``,
        Swift ``description``) yields the enum case's identifier instead.
        """
        segments = [segment for segment in path if segment]
        name_accessors = CASE_NAME_ACCESSORS.get(language, frozenset())
        if len(segments) > 1 and segments[-1] in name_accessors and not self._has(segments):
            return self.case_name(segments[:-1], shorthand=shorthand)
        raw_accessors = RAW_ACCESSORS - name_accessors
        while len(segments) > 1 and segments[-1] in raw_accessors and not self._has(segments):
            segments.pop()
        if not segments:
            return None
        if shorthand:
            return _pick([e for e in self.entries if e.path[-1] == segments[-1]], hint)
        if len(segments) == 1:
            return _pick(
                [e for e in self.entries if not e.member and e.path == (segments[0],)], hint
            )
        for size in range(len(segments), 1, -1):
            suffix = tuple(segments[-size:])
            candidates = [e for e in self.entries if e.path[-size:] == suffix]
            if candidates:
                return _pick(candidates, hint)
        return None

    def case_name(self, segments: Sequence[str], *, shorthand: bool = False) -> str | None:
        """``segments[-1]`` when the path names a known enum case, else ``None``."""
        if not segments:
            return None
        if shorthand:
            return segments[-1] if any(c[-1] == segments[-1] for c in self.cases) else None
        if len(segments) < 2:
            return None
        suffix = tuple(segments[-2:])
        return segments[-1] if any(c[-2:] == suffix for c in self.cases) else None

    def _has(self, segments: list[str]) -> bool:
        suffix = tuple(segments[-2:])
        return any(e.path[-2:] == suffix for e in self.entries)

    def function_values(self, name: str) -> tuple[str, ...] | None:
        """The strings function ``name`` can return, or None (unknown, dynamic or ambiguous)."""
        definitions = self.functions.get(name)
        if not definitions:
            return None
        distinct = set(definitions)
        return distinct.pop() if len(distinct) == 1 else None


def _pick(candidates: list[Entry], hint: str | None) -> str | None:
    values = {e.value for e in candidates}
    if len(values) == 1:
        return values.pop()
    if not values or not hint:
        return None
    wanted = normalise_name(hint)
    if not wanted:
        return None
    preferred = {
        e.value
        for e in candidates
        if len(e.path) > 1
        and (wanted in normalise_name(e.path[-2]) or normalise_name(e.path[-2]) in wanted)
    }
    return preferred.pop() if len(preferred) == 1 else None


# --- scoped languages: Swift, Kotlin, Java ---------------------------------------
@dataclass(frozen=True, slots=True)
class _Scope:
    names: tuple[str, ...]
    depth: int
    is_enum: bool
    string_raw: bool


def _body_start(tokens: list[Token], position: int) -> int | None:
    """Index of the ``{`` opening the type whose header continues at ``position``."""
    limit = min(len(tokens), position + _HEADER_LIMIT)
    while position < limit:
        token = tokens[position]
        if token.is_punct("{"):
            return position
        if token.is_punct("(", "["):
            position = matching(tokens, position) + 1
            continue
        if token.is_punct(";", "}", "=") or (token.kind == IDENT and token.text in _HEADER_STOPS):
            return None
        position += 1
    return None


def _index_scoped(
    index: SymbolIndex, tokens: list[Token], language: str, type_keywords: frozenset[str]
) -> None:
    scopes: list[_Scope] = []
    opening: dict[int, tuple[tuple[str, ...], bool, bool]] = {}
    depth = 0
    position = 0
    while position < len(tokens):
        token = tokens[position]
        if token.kind == IDENT and token.text in type_keywords:
            header = _type_header(tokens, position, language)
            if header is not None:
                brace, names, is_enum, string_raw = header
                opening[brace] = (names, is_enum, string_raw)
                position = brace
                continue
        if token.is_punct("{"):
            depth += 1
            if position in opening:
                names, is_enum, string_raw = opening.pop(position)
                scopes.append(_Scope(names, depth, is_enum, string_raw))
                if is_enum and language in ("kotlin", "java"):
                    position = _jvm_entries(index, tokens, position + 1, scopes)
                    continue
        elif token.is_punct("}"):
            if scopes and scopes[-1].depth == depth:
                scopes.pop()
            depth -= 1
        elif (scopes and scopes[-1].depth == depth) or (not scopes and depth == 0):
            member = bool(scopes)
            if language == "swift" and scopes and scopes[-1].is_enum and token.is_ident("case"):
                position = _swift_cases(index, tokens, position + 1, scopes)
                continue
            if token.is_ident("let", "var", "val") or (
                language == "java" and token.is_ident("String")
            ):
                _constant(index, tokens, position + 1, scopes, member=member)
        position += 1


def _type_header(
    tokens: list[Token], position: int, language: str
) -> tuple[int, tuple[str, ...], bool, bool] | None:
    keyword = tokens[position].text
    probe = position + 1
    is_enum = keyword == "enum"
    if keyword == "enum" and probe < len(tokens) and tokens[probe].is_ident("class"):
        probe += 1  # Kotlin `enum class`
    elif keyword == "class" and position > 0 and tokens[position - 1].is_ident("enum"):
        return None  # already handled at `enum`
    names: list[str] = []
    if keyword == "object" and probe < len(tokens) and tokens[probe].is_punct("{"):
        pass  # anonymous `companion object {`: members belong to the outer type
    else:
        if probe >= len(tokens) or tokens[probe].kind != IDENT:
            return None
        names.append(tokens[probe].text)
        probe += 1
        while (
            probe + 1 < len(tokens)
            and tokens[probe].is_punct(".")
            and tokens[probe + 1].kind == IDENT
        ):
            names.append(tokens[probe + 1].text)
            probe += 2
    if probe < len(tokens) and tokens[probe].is_punct("<"):
        while probe < len(tokens) and not tokens[probe].is_punct(">", "{"):
            probe += 1
        probe += 1
    string_raw = (
        language == "swift"
        and probe + 1 < len(tokens)
        and tokens[probe].is_punct(":")
        and tokens[probe + 1].is_ident("String")
    )
    brace = _body_start(tokens, probe)
    if brace is None:
        return None
    return brace, tuple(names), is_enum, string_raw


def _prefix(scopes: list[_Scope]) -> tuple[str, ...]:
    return tuple(name for scope in scopes for name in scope.names)


def _swift_cases(
    index: SymbolIndex, tokens: list[Token], position: int, scopes: list[_Scope]
) -> int:
    scope = scopes[-1]
    prefix = _prefix(scopes)
    while position < len(tokens) and tokens[position].kind == IDENT:
        name = tokens[position].text
        index.add_case((*prefix, name))
        position += 1
        if position < len(tokens) and tokens[position].is_punct("("):
            # Associated values carry no raw value.
            position = matching(tokens, position) + 1
        elif position + 1 < len(tokens) and tokens[position].is_punct("="):
            raw = literal_text(tokens[position + 1])
            if raw is not None:
                index.add((*prefix, name), raw, member=True)
            position += 2
        elif scope.string_raw:
            index.add((*prefix, name), name, member=True)
        if position < len(tokens) and tokens[position].is_punct(","):
            position += 1
            continue
        break
    return position


def _constant(
    index: SymbolIndex,
    tokens: list[Token],
    position: int,
    scopes: list[_Scope],
    *,
    member: bool,
) -> None:
    """``NAME (: Type)? = "literal"`` starting at ``position``."""
    if position >= len(tokens) or tokens[position].kind != IDENT:
        return
    name = tokens[position].text
    probe = position + 1
    if probe < len(tokens) and tokens[probe].is_punct(":"):
        probe += 1
        while probe < len(tokens) and (
            tokens[probe].kind == IDENT or tokens[probe].is_punct(".", "?")
        ):
            probe += 1
    if probe + 1 < len(tokens) and tokens[probe].is_punct("="):
        raw = literal_text(tokens[probe + 1])
        follows = tokens[probe + 2] if probe + 2 < len(tokens) else None
        if raw is not None and (follows is None or not follows.is_punct("+", ".", "(", "?")):
            index.add((*_prefix(scopes), name), raw, member=member)


def _jvm_entries(
    index: SymbolIndex, tokens: list[Token], position: int, scopes: list[_Scope]
) -> int:
    """Enum entries up to the first ``;`` or the closing brace; returns where to resume."""
    prefix = _prefix(scopes)
    while position < len(tokens):
        token = tokens[position]
        if token.is_punct(";", "}"):
            return position
        if token.kind != IDENT:
            position += 1
            continue
        name = token.text
        index.add_case((*prefix, name))
        position += 1
        if position < len(tokens) and tokens[position].is_punct("("):
            close = matching(tokens, position)
            for part in split_top_level(tokens[position + 1 : close]):
                raw = literal_text(part[-1]) if part else None
                if raw is not None:
                    index.add((*prefix, name), raw, member=True)
                    break
            position = close + 1
        if position < len(tokens) and tokens[position].is_punct("{"):
            position = matching(tokens, position) + 1
        if position < len(tokens) and tokens[position].is_punct(","):
            position += 1
    return position


# --- TypeScript and Objective-C ---------------------------------------------------
def _keyed_literals(part: list[Token], separator: str) -> tuple[str, str] | None:
    if len(part) == 3 and part[0].kind in (IDENT, STRING) and part[1].is_punct(separator):
        key = part[0].text if part[0].kind == IDENT else literal_text(part[0])
        raw = literal_text(part[2])
        if key is not None and raw is not None:
            return key, raw
    return None


def _index_ts(index: SymbolIndex, tokens: list[Token]) -> None:
    depth = 0
    position = 0
    while position < len(tokens):
        token = tokens[position]
        if token.is_punct("{"):
            depth += 1
        elif token.is_punct("}"):
            depth -= 1
        elif depth == 0 and token.is_ident("enum") and position + 2 < len(tokens):
            name = tokens[position + 1].text
            if tokens[position + 2].is_punct("{"):
                close = matching(tokens, position + 2)
                for part in split_top_level(tokens[position + 3 : close]):
                    pair = _keyed_literals(part, "=")
                    if pair is not None:
                        index.add((name, pair[0]), pair[1], member=True)
                position = close + 1
                continue
        elif depth == 0 and token.is_ident("const", "let", "var") and position + 3 < len(tokens):
            name_token = tokens[position + 1]
            probe = position + 2
            if name_token.kind == IDENT and tokens[probe].is_punct(":"):
                while probe < len(tokens) and not tokens[probe].is_punct("=", ";"):
                    probe += 1
            if name_token.kind == IDENT and probe + 1 < len(tokens) and tokens[probe].is_punct("="):
                value = tokens[probe + 1]
                if value.is_punct("{"):
                    close = matching(tokens, probe + 1)
                    for part in split_top_level(tokens[probe + 2 : close]):
                        pair = _keyed_literals(part, ":")
                        if pair is not None:
                            index.add((name_token.text, pair[0]), pair[1], member=True)
                    position = close + 1
                    continue
                raw = literal_text(value)
                if raw is not None:
                    index.add((name_token.text,), raw, member=False)
        position += 1


def _index_objc(index: SymbolIndex, tokens: list[Token]) -> None:
    depth = 0
    for position, token in enumerate(tokens):
        if token.is_punct("{"):
            depth += 1
        elif token.is_punct("}"):
            depth -= 1
        elif depth == 0 and token.is_punct("=") and 1 <= position < len(tokens) - 1:
            name = tokens[position - 1]
            raw = literal_text(tokens[position + 1])
            if name.kind == IDENT and raw is not None:
                index.add((name.text,), raw, member=False)
        elif (
            token.is_punct("#")
            and position + 3 < len(tokens)
            and tokens[position + 1].is_ident("define")
        ):
            name = tokens[position + 2]
            raw = literal_text(tokens[position + 3])
            if name.kind == IDENT and raw is not None:
                index.add((name.text,), raw, member=False)


# --- string-returning functions ---------------------------------------------------
def _index_functions(index: SymbolIndex, tokens: list[Token], language: str) -> None:
    for position, token in enumerate(tokens):
        if token.kind != IDENT:
            continue
        if language == "swift" and token.text == "var" and position + 4 < len(tokens):
            # Computed property: `var name: String {`
            if (
                tokens[position + 1].kind == IDENT
                and tokens[position + 2].is_punct(":")
                and tokens[position + 3].is_ident("String")
                and tokens[position + 4].is_punct("{")
            ):
                index.add_function(tokens[position + 1].text, _body_values(tokens, position + 4))
            continue
        name_index: int | None = None
        if (
            token.text in _FUNCTION_KEYWORDS
            and position + 1 < len(tokens)
            and tokens[position + 1].kind == IDENT
        ) or (
            language == "java"
            and token.text == "String"
            and position + 2 < len(tokens)
            and tokens[position + 1].kind == IDENT
            and tokens[position + 2].is_punct("(")
        ):
            name_index = position + 1
        if name_index is not None:
            _function(index, tokens, name_index, language)


def _function(index: SymbolIndex, tokens: list[Token], name_index: int, language: str) -> None:
    name = tokens[name_index].text
    probe = name_index + 1
    if probe < len(tokens) and tokens[probe].is_punct("<"):
        while probe < len(tokens) and not tokens[probe].is_punct(">"):
            probe += 1
        probe += 1
    if probe >= len(tokens) or not tokens[probe].is_punct("("):
        return
    after = matching(tokens, probe) + 1
    declared: str | None = "String" if language == "java" else None
    if after + 1 < len(tokens) and tokens[after].is_punct("->", ":"):
        declared = tokens[after + 1].text
        after += 2
        while after < len(tokens) and tokens[after].is_punct("?", "!"):
            after += 1
    while after < len(tokens) and tokens[after].is_ident("throws", "async", "rethrows"):
        after += 1
    if after >= len(tokens):
        return
    if language == "kotlin" and tokens[after].is_punct("="):
        # Expression body: `fun name() = "x"` / `fun name(): String = "x"`.
        value = _single_string(tokens[after + 1 : after + 3])
        index.add_function(name, (value,) if value is not None else None)
        return
    if declared in _STRING_TYPES and tokens[after].is_punct("{"):
        index.add_function(name, _body_values(tokens, after))


def _single_string(span: Sequence[Token]) -> str | None:
    """The first token's string value, unless the expression continues past it."""
    if not span or span[0].kind != STRING:
        return None
    if len(span) > 1 and span[1].kind == PUNCT and span[1].text in ("+", ".", "(", "?", "??"):
        return None
    return template_text(span[0].parts)


def _body_values(tokens: list[Token], open_index: int) -> tuple[str, ...] | None:
    close = matching(tokens, open_index)
    body = tokens[open_index + 1 : close]
    returns = [position for position, token in enumerate(body) if token.is_ident("return")]
    if not returns:
        # Swift single-expression body: `{ "literal" }`.
        value = _single_string(body) if len(body) == 1 else None
        return (value,) if value is not None else None
    values: list[str] = []
    for position in returns:
        value = _single_string(body[position + 1 : position + 3])
        if value is None:
            return None
        if value not in values:
            values.append(value)
    if len(values) > MAX_CHOICES:
        return None
    return tuple(values)
