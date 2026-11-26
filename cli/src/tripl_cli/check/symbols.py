"""What an identifier in a tracking call stands for.

Enum raw values, string constants, and the strings a function can return.

A wrapper call rarely passes its category as ``"home"``. It passes ``.home``,
``Events.Category.home.rawValue`` or ``Category.HOME.value``, and the string the
plan knows lives in an enum declared in another file. This module builds that
lookup from the enum files named in the config (and from every scanned source),
per language:

* Swift — ``enum Category: String { case home, map = "map_screen" }``. A case
  with no ``=`` in a ``String`` enum has its own name as its raw value. Also
  ``static let x = "y"`` inside any type and ``let x = "y"`` at file scope;
  never a ``var``, which can change before the call.
* Kotlin / Java — ``enum class Category(val value: String) { HOME("home") }``
  (the first string argument), ``const val X = "y"``, ``val X = "y"``,
  ``static final String X = "y"``; never a Kotlin ``var`` or a Java field
  that is not ``final``. A named ``companion object Keys`` keeps its members
  under ``Outer.Keys.X`` and, as Kotlin lets callers write them, ``Outer.X``.
* TypeScript — ``enum Category { Home = "home" }``, ``const Category = { Home: "home" }``
  (with objects in it too: ``{ Screen: { Home: "home" } }``), ``const X = "y"``
  and a class's ``static readonly X = "y"``; never ``let``, ``var`` or a
  ``static`` that is not ``readonly``.
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

Swift's computed property ``var name: String { "y" }`` (or ``{ return "y" }``,
or a ``get`` holding either) inside a type is a constant of that type, read
like a ``static let``. One at file scope is not read: a bare name is looked up
across every scanned file, so ``logEvent(name)`` anywhere would read it.

Functions: ``func name(…) -> String { … }`` (and the Kotlin, Java and TS
equivalents) whose every ``return`` is a string literal resolve, at a call
(``name()``), to that set of strings. One non-literal return makes the whole
function dynamic. Swift's ``async``, ``throws`` (typed too), ``rethrows`` and a
``where`` clause, and a Java ``throws`` list, may sit between the parameters
and the body.

``type_bodies`` gives a file's type bodies, each with the path its members are
keyed under, for a reference to a member of the type it sits in that does not
name the type (Kotlin and Java ``X``, Swift ``Self.x``).
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
# How deep a TypeScript `const` object's objects are read: `Events.Screen.Home` is two.
_TS_OBJECT_DEPTH = 4
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
    # Keyed by (language, name): a Kotlin `genericName()` must not read a Swift
    # `func genericName()` in another file of a mixed repository.
    functions: dict[tuple[str, str], list[tuple[str, ...] | None]] = field(default_factory=dict)
    # `entries` under each suffix of their path (`(home,)`, `(Category, home)`,
    # …) and file-scope constants under their name, in the order added, and
    # the last one and two segments of each case: a lookup reads only what can
    # match it, not every entry in the run.
    _suffixes: dict[tuple[str, ...], list[Entry]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _constants: dict[str, list[Entry]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _case_suffixes: set[tuple[str, ...]] = field(
        default_factory=set, init=False, repr=False, compare=False
    )

    # --- building ------------------------------------------------------------
    def add(self, path: tuple[str, ...], value: str, *, member: bool) -> None:
        entry = Entry(path, value, member)
        self.entries.append(entry)
        for start in range(len(path)):
            self._suffixes.setdefault(path[start:], []).append(entry)
        if not member and len(path) == 1:
            self._constants.setdefault(path[0], []).append(entry)

    def add_case(self, path: tuple[str, ...]) -> None:
        self.cases.add(path)
        self._case_suffixes.update((path[-1:], path[-2:]))

    def add_function(self, name: str, values: tuple[str, ...] | None, *, language: str) -> None:
        self.functions.setdefault((language, name), []).append(values)

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
        return _pick(self._candidates(segments, shorthand, name_accessors), hint)

    def ambiguous(
        self,
        path: Sequence[str],
        *,
        hint: str | None = None,
        shorthand: bool = False,
        language: str = "",
    ) -> bool:
        """Whether ``resolve`` gives up on ``path`` because it names constants of different values.

        The constants ``resolve`` would choose among, which the field ``hint``
        does not tell apart. For ``tripl check --list`` only.
        """
        segments = [segment for segment in path if segment]
        name_accessors = CASE_NAME_ACCESSORS.get(language, frozenset())
        if len(segments) > 1 and segments[-1] in name_accessors and not self._has(segments):
            return False
        return _ambiguous(self._candidates(segments, shorthand, name_accessors), hint)

    def _candidates(
        self, segments: list[str], shorthand: bool, name_accessors: frozenset[str]
    ) -> list[Entry]:
        """The entries ``resolve`` picks a value among: those of the longest matching suffix."""
        raw_accessors = RAW_ACCESSORS - name_accessors
        while len(segments) > 1 and segments[-1] in raw_accessors and not self._has(segments):
            segments.pop()
        if not segments:
            return []
        if shorthand:
            return self._suffixes.get((segments[-1],), [])
        if len(segments) == 1:
            return self._constants.get(segments[0], [])
        for size in range(len(segments), 1, -1):
            candidates = self._suffixes.get(tuple(segments[-size:]))
            if candidates:
                return candidates
        return []

    def member(self, path: Sequence[str], *, hint: str | None = None) -> str | None:
        """The value of the member whose qualified path is exactly ``path``, or None.

        No suffix of it: ``(Screen, HOME)`` is ``Screen``'s ``HOME``, never
        ``Other.Screen.HOME``. None when unknown or ambiguous, as ``resolve``.
        """
        wanted = tuple(path)
        return _pick([e for e in self._suffixes.get(wanted, []) if e.path == wanted], hint)

    def ambiguous_member(self, path: Sequence[str], *, hint: str | None = None) -> bool:
        """Whether ``member`` gives up on ``path`` because it is declared with different values."""
        wanted = tuple(path)
        return _ambiguous([e for e in self._suffixes.get(wanted, []) if e.path == wanted], hint)

    def names_member(self, name: str) -> bool:
        """Whether some type has a member called ``name`` (a cheap test before ``member``)."""
        return any(entry.member for entry in self._suffixes.get((name,), []))

    def declares(self, path: Sequence[str]) -> bool:
        """Whether a constant has exactly ``path`` as its qualified path, whatever its value."""
        wanted = tuple(path)
        return any(entry.path == wanted for entry in self._suffixes.get(wanted, []))

    def case_name(self, segments: Sequence[str], *, shorthand: bool = False) -> str | None:
        """``segments[-1]`` when the path names a known enum case, else ``None``."""
        if not segments:
            return None
        if shorthand:
            return segments[-1] if (segments[-1],) in self._case_suffixes else None
        if len(segments) < 2:
            return None
        return segments[-1] if tuple(segments[-2:]) in self._case_suffixes else None

    def _has(self, segments: list[str]) -> bool:
        suffix = tuple(segments[-2:])
        return any(e.path[-2:] == suffix for e in self._suffixes.get(suffix, []))

    def function_values(self, name: str, *, language: str) -> tuple[str, ...] | None:
        """The strings ``language``'s function ``name`` can return, or None.

        None when it is unknown, dynamic or ambiguous. Only functions declared
        in the same language are looked at.
        """
        definitions = self.functions.get((language, name))
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


def _ambiguous(candidates: list[Entry], hint: str | None) -> bool:
    """Whether ``candidates`` hold several values and ``_pick`` cannot choose one."""
    return len({e.value for e in candidates}) > 1 and _pick(candidates, hint) is None


# --- scoped languages: Swift, Kotlin, Java ---------------------------------------
@dataclass(frozen=True, slots=True)
class _Scope:
    names: tuple[str, ...]
    depth: int
    is_enum: bool
    string_raw: bool
    # A named Kotlin `companion object Keys`: its members are the outer type's too.
    companion: bool = False


@dataclass(frozen=True, slots=True)
class _Header:
    """A type declaration's ``{`` and what its body's scope is."""

    brace: int
    names: tuple[str, ...]
    is_enum: bool
    string_raw: bool
    companion: bool


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
    opening: dict[int, _Header] = {}
    depth = 0
    position = 0
    while position < len(tokens):
        token = tokens[position]
        if token.kind == IDENT and token.text in type_keywords:
            header = _type_header(tokens, position, language)
            if header is not None:
                opening[header.brace] = header
                position = header.brace
                continue
        if token.is_punct("{"):
            depth += 1
            if position in opening:
                found = opening.pop(position)
                scopes.append(
                    _Scope(found.names, depth, found.is_enum, found.string_raw, found.companion)
                )
                if found.is_enum and language in ("kotlin", "java"):
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
            # Only what cannot be reassigned: Swift `let`, Kotlin `val` / `const val`,
            # a Java `final` String. A `var` or a non-final field holds whatever was
            # last assigned at runtime; reading its initial value checked
            # `var currentScreen = ""` as an empty event name.
            if token.is_ident("let", "val") or (
                language == "java" and token.is_ident("String") and _java_final(tokens, position)
            ):
                _constant(index, tokens, position + 1, scopes, member=member)
            elif language == "swift" and scopes and token.is_ident("var"):
                # A computed property cannot be assigned either: `var x: String { "y" }`.
                _computed(index, tokens, position + 1, scopes)
        position += 1


def _java_final(tokens: list[Token], position: int) -> bool:
    """Whether the Java ``String`` at ``position`` follows a ``final`` among its modifiers.

    Walks back over the identifiers before the type (``public static final``),
    stopping at the first token that is no identifier: the end of the previous
    statement, a brace, an annotation.
    """
    index = position - 1
    while index >= 0 and tokens[index].kind == IDENT:
        if tokens[index].text == "final":
            return True
        index -= 1
    return False


def _type_header(tokens: list[Token], position: int, language: str) -> _Header | None:
    keyword = tokens[position].text
    probe = position + 1
    is_enum = keyword == "enum"
    if keyword == "enum" and probe < len(tokens) and tokens[probe].is_ident("class"):
        probe += 1  # Kotlin `enum class`
    elif keyword == "class" and position > 0 and tokens[position - 1].is_ident("enum"):
        return None  # already handled at `enum`
    companion = keyword == "object" and position > 0 and tokens[position - 1].is_ident("companion")
    names: list[str] = []
    if keyword == "object" and probe < len(tokens) and tokens[probe].is_punct("{"):
        pass  # anonymous `companion object {`: members belong to the outer type
    else:
        if (
            probe >= len(tokens)
            or tokens[probe].kind != IDENT
            or tokens[probe].text in _HEADER_STOPS
        ):
            # No name, or a modifier: Swift's `class var x`, `class func f`.
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
    return _Header(brace, tuple(names), is_enum, string_raw, companion and bool(names))


def _prefix(scopes: list[_Scope]) -> tuple[str, ...]:
    return tuple(name for scope in scopes for name in scope.names)


def _add_member(
    index: SymbolIndex, scopes: list[_Scope], name: str, raw: str, *, member: bool
) -> None:
    """``raw`` under ``name``'s path, and under the outer type's when a named companion holds it.

    Kotlin lets a caller write ``Outer.X`` for a member of ``companion object
    Keys`` inside ``Outer``, as well as ``Outer.Keys.X``.
    """
    index.add((*_prefix(scopes), name), raw, member=member)
    if any(scope.companion for scope in scopes):
        outer = tuple(part for scope in scopes if not scope.companion for part in scope.names)
        index.add((*outer, name), raw, member=member)


def type_bodies(tokens: list[Token], language: str) -> list[tuple[int, int, tuple[str, ...]]]:
    """Each type body in ``tokens``: its ``{`` and ``}`` token indexes, and its members' path.

    The path is the one ``index_tokens`` keys the members under: the names of
    the types around the body and its own, outermost first (``Outer``,
    ``Keys`` for a named companion inside ``Outer``). Swift, Kotlin and Java
    only, in the order the bodies open.
    """
    if language == "swift":
        keywords = _SWIFT_TYPES
    elif language in ("kotlin", "java"):
        keywords = _JVM_TYPES
    else:
        return []
    bodies: list[tuple[int, int, tuple[str, ...]]] = []
    # The bodies open at the current position, outermost first: (closing index, path).
    around: list[tuple[int, tuple[str, ...]]] = []
    for position, token in enumerate(tokens):
        if token.kind != IDENT or token.text not in keywords:
            continue
        header = _type_header(tokens, position, language)
        if header is None:
            continue
        while around and around[-1][0] < header.brace:
            around.pop()
        close = matching(tokens, header.brace)
        path = (*(around[-1][1] if around else ()), *header.names)
        bodies.append((header.brace, close, path))
        around.append((close, path))
    return bodies


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
            _add_member(index, scopes, name, raw, member=member)


def _computed(index: SymbolIndex, tokens: list[Token], position: int, scopes: list[_Scope]) -> None:
    """Swift ``NAME: String { "y" }``, starting at ``position`` inside a type.

    The body is one string literal with no interpolation, alone or after
    ``return``, or such a body inside ``get { … }``. Anything else (a stored
    ``var``, a ``didSet``, a protocol's ``{ get }``) is not read.
    """
    if (
        position + 3 >= len(tokens)
        or tokens[position].kind != IDENT
        or not tokens[position + 1].is_punct(":")
        or not tokens[position + 2].is_ident("String")
        or not tokens[position + 3].is_punct("{")
    ):
        return
    body = tokens[position + 4 : matching(tokens, position + 3)]
    if len(body) >= 3 and body[0].is_ident("get") and body[1].is_punct("{"):
        if matching(body, 1) != len(body) - 1:
            return
        body = body[2:-1]
    if body and body[0].is_ident("return"):
        body = body[1:]
    raw = literal_text(body[0]) if len(body) == 1 else None
    if raw is not None:
        _add_member(index, scopes, tokens[position].text, raw, member=True)


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


def _ts_object(
    index: SymbolIndex, tokens: list[Token], open_index: int, path: tuple[str, ...]
) -> int:
    """The keys of the object literal opened at ``open_index``, under ``path``; returns its ``}``.

    An object in it is read in turn (``{ Screen: { Home: "home" } }`` gives
    ``Events.Screen.Home``), at most ``_TS_OBJECT_DEPTH`` objects deep.
    """
    close = matching(tokens, open_index)
    for part in split_top_level(tokens[open_index + 1 : close]):
        pair = _keyed_literals(part, ":")
        if pair is not None:
            index.add((*path, pair[0]), pair[1], member=True)
        elif (
            len(path) < _TS_OBJECT_DEPTH
            and len(part) >= 4
            and part[0].kind in (IDENT, STRING)
            and part[1].is_punct(":")
            and part[2].is_punct("{")
            and matching(part, 2) == len(part) - 1
        ):
            key = part[0].text if part[0].kind == IDENT else literal_text(part[0])
            if key is not None:
                _ts_object(index, part, 2, (*path, key))
    return close


def _ts_class(index: SymbolIndex, tokens: list[Token], keyword: int) -> int | None:
    """A class's ``static readonly X = "y"`` members; returns its body's ``}``, or None.

    The header runs from ``class Name`` to the first ``{`` outside ``<…>``.
    """
    if (
        keyword + 1 >= len(tokens)
        or tokens[keyword + 1].kind != IDENT
        or tokens[keyword + 1].text in ("extends", "implements")
    ):
        return None  # no name: nothing could name its members
    if keyword and tokens[keyword - 1].is_punct(".", "?."):
        return None  # `x.class`: a property
    name = tokens[keyword + 1].text
    position = keyword + 2
    angles = 0
    limit = min(len(tokens), position + _HEADER_LIMIT)
    while position < limit:
        token = tokens[position]
        if token.is_punct("<"):
            angles += 1
        elif token.is_punct(">"):
            angles = max(angles - 1, 0)
        elif token.is_punct("{") and not angles:
            break
        elif token.is_punct("(", "[", "{"):
            position = matching(tokens, position)
        elif token.is_punct(";", "}", "=") and not angles:
            return None
        position += 1
    else:
        return None
    close = matching(tokens, position)
    depth = 0
    for member in range(position + 1, close):
        token = tokens[member]
        if token.is_punct("{", "(", "["):
            depth += 1
        elif token.is_punct("}", ")", "]"):
            depth -= 1
        elif (
            depth == 0
            and token.is_ident("readonly")
            and tokens[member - 1].is_ident("static")
            and member + 1 < close
            and tokens[member + 1].kind == IDENT
        ):
            probe = member + 2
            if probe < close and tokens[probe].is_punct(":"):
                while probe < close and not tokens[probe].is_punct("=", ";"):
                    probe += 1
            if probe + 1 < close and tokens[probe].is_punct("="):
                raw = literal_text(tokens[probe + 1])
                after = tokens[probe + 2] if probe + 2 < close else None
                if raw is not None and (after is None or not after.is_punct("+", ".", "(", "?")):
                    index.add((name, tokens[member + 1].text), raw, member=True)
    return close


def _index_ts(index: SymbolIndex, tokens: list[Token]) -> None:
    depth = 0
    position = 0
    while position < len(tokens):
        token = tokens[position]
        if token.is_punct("{"):
            depth += 1
        elif token.is_punct("}"):
            depth -= 1
        elif depth == 0 and token.is_ident("class"):
            close = _ts_class(index, tokens, position)
            if close is not None:
                position = close + 1
                continue
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
        # `const` only: an exported `let` or `var` can be reassigned before the call.
        elif depth == 0 and token.is_ident("const") and position + 3 < len(tokens):
            name_token = tokens[position + 1]
            probe = position + 2
            if name_token.kind == IDENT and tokens[probe].is_punct(":"):
                while probe < len(tokens) and not tokens[probe].is_punct("=", ";"):
                    probe += 1
            if name_token.kind == IDENT and probe + 1 < len(tokens) and tokens[probe].is_punct("="):
                value = tokens[probe + 1]
                if value.is_punct("{"):
                    position = _ts_object(index, tokens, probe + 1, (name_token.text,)) + 1
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
    after = _effects(tokens, matching(tokens, probe) + 1, language)
    declared: str | None = "String" if language == "java" else None
    if after + 1 < len(tokens) and tokens[after].is_punct("->", ":"):
        declared = tokens[after + 1].text
        after += 2
        while after < len(tokens) and tokens[after].is_punct("?", "!"):
            after += 1
    after = _effects(tokens, after, language)
    if language == "swift" and after < len(tokens) and tokens[after].is_ident("where"):
        after = _body_start(tokens, after + 1) or len(tokens)
    if after >= len(tokens):
        return
    if language == "kotlin" and tokens[after].is_punct("="):
        # Expression body: `fun name() = "x"` / `fun name(): String = "x"`.
        value = _single_string(tokens[after + 1 : after + 3])
        index.add_function(name, (value,) if value is not None else None, language=language)
        return
    if declared in _STRING_TYPES and tokens[after].is_punct("{"):
        index.add_function(name, _body_values(tokens, after), language=language)


def _effects(tokens: list[Token], position: int, language: str) -> int:
    """The index past what may follow a parameter list before or after the return type.

    Swift's ``async``, ``throws`` (``throws(E)`` too) and ``rethrows``, and a
    Java ``throws A, b.B`` list.
    """
    while position < len(tokens) and tokens[position].is_ident("throws", "async", "rethrows"):
        throws = tokens[position].text == "throws"
        position += 1
        if language == "swift" and throws and position < len(tokens):
            if tokens[position].is_punct("("):
                position = matching(tokens, position) + 1  # typed throws: `throws(E)`
        elif language == "java" and throws:
            while position < len(tokens) and (
                tokens[position].kind == IDENT or tokens[position].is_punct(".", ",")
            ):
                position += 1
    return position


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
