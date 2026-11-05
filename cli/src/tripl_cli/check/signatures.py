"""A declaration's signature: its parameters, and when one with no body still declares.

``scopes`` finds where a named function's parameter list is. This reads the
list into parameters, and tells a declaration with no body after its list (an
interface or abstract member, an overload, a C prototype) from a call
statement, which can look the same. A declaring keyword (Kotlin's ``fun``,
Swift's ``func`` and ``init``, TypeScript's ``function``) settles that on its
own. Otherwise a Java or C declaration needs a return type before its name (the
end of a preprocessor line, as in ``#endif``, is none) and a ``;`` after its
list, and a TypeScript method a typed parameter, a modifier or a return type: a
call statement has none of these.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from tripl_cli.check.calls import matching, split_top_level
from tripl_cli.check.lexer import IDENT, PUNCT, Token

# The most tokens between a parameter list and the `;` of a declaration with no body.
_MAX_AFTER = 32
# The most tokens a return type's `<…>` arguments may hold.
_MAX_TYPE_ARGUMENTS = 40

# Dropped from a parameter before its name is read.
_PARAMETER_MODIFIERS = frozenset(
    {
        "vararg",
        "noinline",
        "crossinline",
        "val",
        "var",
        "inout",
        "final",
        "readonly",
        "public",
        "private",
        "protected",
    }
)
# A TypeScript member's modifiers, on its name's line: no call is written after one.
_TS_MODIFIERS = frozenset(
    {"abstract", "public", "private", "protected", "static", "override", "declare", "async"}
    | {"get", "set"}
)
# Never a Java or C return type: these start a statement or an expression.
_NOT_TYPES = frozenset(
    {"return", "throw", "new", "else", "do", "case", "assert", "yield", "await", "in"}
    | {"instanceof", "sizeof", "typeof", "__typeof__", "alignof", "goto", "default"}
    | {"break", "continue"}
)
# What a Java or C type's `<…>` arguments may hold besides names (`Map<K, ? extends V>`).
_TYPE_ARGUMENT_PUNCT = (",", ".", "?", "[", "]", "*", "&")
_OPENERS = ("(", "[", "{")
_CLOSERS = (")", "]", "}")


@dataclass(frozen=True, slots=True)
class Parameter:
    """One declared parameter: its name, its position, and the label a caller writes.

    ``label`` is Swift's external label (None for ``_``) and the name itself in
    Kotlin, whose named arguments use it; the other languages have none.
    ``index`` counts the parameter list's parts, a part with no readable name
    included, so it is the position a caller passes the argument at.
    """

    name: str
    index: int
    label: str | None = None


def parameters(
    tokens: list[Token], opening: int, close: int, language: str
) -> tuple[Parameter, ...]:
    """The parameters of the list from ``tokens[opening]`` (its ``(``) to ``tokens[close]``."""
    found: list[Parameter] = []
    index = 0
    for part in _parts(tokens[opening + 1 : close]):
        if not part:
            continue  # a trailing comma: no parameter, and no position either
        name, label = _name_of(_undecorated(part), language)
        if name is not None:
            found.append(Parameter(name, index, label))
        index += 1
    return tuple(found)


def declares(
    tokens: list[Token], source: str, language: str, name: int, opening: int, close: int
) -> bool:
    """``name(…)``, with no body after its list, still declares a function.

    Java and C: a return type before the name and a ``;`` after the list
    (``throws …`` or attribute macros between). A TypeScript method: a typed
    parameter, a modifier on the name's line, or a return type on a member of
    its own. ``source`` is read for line breaks only.
    """
    if language in ("java", "objc"):
        return _returns(tokens, source, name, language) and _prototype_end(
            tokens, close + 1, language
        )
    if language != "ts":
        return False
    if _typed(tokens, opening, close) or _modified(tokens, source, name):
        return True
    return (
        close + 1 < len(tokens)
        and tokens[close + 1].is_punct(":")
        and _member_start(tokens, source, name)
    )


# --- parameters ---------------------------------------------------------------------
def _parts(tokens: Sequence[Token]) -> list[list[Token]]:
    """A parameter list split at its top-level commas.

    Type arguments count as brackets (``Map<String, Any>`` is one part), but
    only before a part's default value, where ``<`` and ``>`` compare.
    """
    parts: list[list[Token]] = [[]]
    depth = 0
    angles = 0
    defaulted = False
    for token in tokens:
        if token.kind == PUNCT and token.text in _OPENERS:
            depth += 1
        elif token.kind == PUNCT and token.text in _CLOSERS:
            depth -= 1
        elif depth == 0 and token.kind == PUNCT:
            if token.text == "," and (defaulted or not angles):
                parts.append([])
                angles = 0
                defaulted = False
                continue
            if not defaulted:
                if token.text == "<":
                    angles += 1
                elif token.text == ">" and angles:
                    angles -= 1
                elif token.text == "=" and not angles:
                    defaulted = True
        parts[-1].append(token)
    return [] if parts == [[]] else parts


def _undecorated(part: list[Token]) -> list[Token]:
    """``part`` without annotations (``@X``, ``@a.X(…)``, ``@field:X``) and modifiers."""
    kept: list[Token] = []
    index = 0
    while index < len(part):
        token = part[index]
        if token.is_punct("@"):
            index += 1
            while index < len(part) and part[index].kind == IDENT:
                index += 1
                if (
                    index + 1 < len(part)
                    and part[index].is_punct(".", ":")
                    and part[index + 1].kind == IDENT
                ):
                    index += 1
                    continue
                break
            if index < len(part) and part[index].is_punct("("):
                index = matching(part, index) + 1
            continue
        if not (token.kind == IDENT and token.text in _PARAMETER_MODIFIERS):
            kept.append(token)
        index += 1
    return kept


def _name_of(part: list[Token], language: str) -> tuple[str | None, str | None]:
    """``(name, label)`` of one parameter, ``(None, None)`` when it has no plain name."""
    head = (split_top_level(part, "=") or [[]])[0]
    if language in ("java", "objc"):
        # The last identifier: `final String event`, `String... events`, `NSString *name`.
        names = [token.text for token in head if token.kind == IDENT]
        return (names[-1], None) if names and names != ["void"] else (None, None)
    typed = split_top_level(head, ":")
    if len(typed) < 2:
        if language == "ts":
            bare = [token for token in head if not token.is_punct("...", "?")]
            if len(bare) == 1 and bare[0].kind == IDENT:
                return bare[0].text, None  # `name`, `name = 1`, `...rest`
        return None, None
    before = typed[0]
    if language == "ts" and before and before[-1].is_punct("?"):
        before = before[:-1]  # `name?: T`
    if not before or before[-1].kind != IDENT:
        return None, None  # destructured: `{ a, b }: Props`
    name = before[-1].text
    if language == "swift":
        if len(before) >= 2 and before[-2].kind == IDENT:
            label = before[-2].text
            return name, None if label == "_" else label
        return name, name
    return name, name if language == "kotlin" else None


# --- Java and C: a return type and a `;` ---------------------------------------------
def _returns(tokens: list[Token], source: str, name: int, language: str) -> bool:
    """A return type ends right before ``tokens[name]``: ``void``, ``List<T>``, ``int[]``, ``T *``.

    A pointer's type or a generic one must start the declaration: ``a * f(x)``
    and ``a < b > f(x)`` are expressions. Nor does a C preprocessor line end in
    one: after ``#endif`` or ``#ifdef FLAG`` the next line's ``f(x);`` is a call.
    """
    position = name - 1
    pointer = False
    if language == "objc":
        while position > 0 and tokens[position].is_punct("*"):
            position -= 1
            pointer = True
    if _directive(source, tokens[position], tokens[name]):
        return False
    token = tokens[position]
    if token.kind == IDENT:
        return _is_type(token) and (not pointer or _declaration_start(tokens, source, position))
    if token.is_punct(">"):
        opening = _angles_back(tokens, position)
        return (
            opening > 0
            and _is_type(tokens[opening - 1])
            and _declaration_start(tokens, source, opening - 1)
        )
    if token.is_punct("]"):
        # An array type's empty `[]`: `String[] names();`.
        return (
            position >= 2
            and tokens[position - 1].is_punct("[")
            and (tokens[position - 2].kind == IDENT or tokens[position - 2].is_punct(">", "]"))
        )
    return False


def _is_type(token: Token) -> bool:
    return token.kind == IDENT and token.text not in _NOT_TYPES


def _directive(source: str, token: Token, later: Token) -> bool:
    """``token`` sits on a preprocessor line (``#endif``, ``#if DEBUG``) that ends before ``later``.

    A line that ends in a ``\\`` goes on with the next one, as a ``#define``'s
    body does, so ``token`` may sit lines below its ``#``. Java has no such
    line: none of its lines starts with ``#``.
    """
    if "\n" not in source[token.end : later.offset]:
        return False
    signs, ends = _directives(source)
    line = bisect_left(signs, token.offset) - 1
    return line >= 0 and token.offset < ends[line]


@lru_cache(maxsize=1)
def _directives(source: str) -> tuple[list[int], list[int]]:
    """Where each preprocessor line of ``source`` has its ``#``, and where the line ends.

    Found once for the source whose declarations are being read, then
    bisected: walking back from each token over the lines a long ``#define``
    continues would cost the square of its length. A directive's ``#`` is the
    first character of its line past the indentation, and a line that goes on
    from a ``\\`` before it starts no directive, whatever its first character.
    Each line is looked at once, from its first ``#``, so a line full of them
    (a minified one) costs no more than its length.
    """
    signs: list[int] = []
    ends: list[int] = []
    sign = source.find("#")
    while sign >= 0:
        start = source.rfind("\n", 0, sign) + 1
        end = source.find("\n", sign)
        if (start == sign or source[start:sign].isspace()) and not source.endswith(
            "\\\n", 0, start
        ):
            while end > 0 and source[end - 1] == "\\":
                end = source.find("\n", end + 1)
            signs.append(sign)
            ends.append(len(source) if end < 0 else end)
        if end < 0:
            break
        sign = source.find("#", end + 1)
    return signs, ends


def _declaration_start(tokens: list[Token], source: str, position: int) -> bool:
    """The type ending at ``tokens[position]`` (a dotted one read back) starts a declaration.

    It starts a line, follows ``;``, ``{`` or ``}``, or follows a modifier or
    a type parameter list (``static``, ``FOUNDATION_EXPORT``, ``<T>``), read
    back over as many lists as stand there: in a loop, as a line may hold more
    of them than the stack is deep. The word or ``>`` that ends a preprocessor
    line (``#else``, ``#import <UIKit/UIKit.h>``) is neither: the type starts
    the line after it.
    """
    while True:
        while (
            position >= 2
            and tokens[position - 1].is_punct(".")
            and tokens[position - 2].kind == IDENT
        ):
            position -= 2
        if position == 0:
            return True
        before = tokens[position - 1]
        if _directive(source, before, tokens[position]):
            return True
        if before.kind == IDENT:
            return _is_type(before)
        if not before.is_punct(">"):
            return (
                before.is_punct(";", "{", "}")
                or "\n" in source[before.end : tokens[position].offset]
            )
        position = _angles_back(tokens, position - 1)
        if position < 0:
            return False


def _angles_back(tokens: list[Token], close: int) -> int:
    """The index of the ``<`` the ``>`` at ``close`` closes, over a type's arguments; -1 if none."""
    depth = 0
    for position in range(close, max(-1, close - _MAX_TYPE_ARGUMENTS), -1):
        token = tokens[position]
        if token.is_punct(">"):
            depth += 1
        elif token.is_punct("<"):
            depth -= 1
            if depth == 0:
                return position
        elif token.kind != IDENT and not token.is_punct(*_TYPE_ARGUMENT_PUNCT):
            return -1
    return -1


def _prototype_end(tokens: list[Token], position: int, language: str) -> bool:
    """A Java or C declaration with no body ends at ``position``: a ``;``.

    Java's ``throws A, B`` and C's attribute macros (``NS_SWIFT_NAME(…)``,
    ``__attribute__((…))``) may stand before it.
    """
    limit = min(len(tokens), position + _MAX_AFTER + 1)
    if language == "java" and position < limit and tokens[position].is_ident("throws"):
        position += 1
        while position < limit and (
            tokens[position].kind == IDENT or tokens[position].is_punct(".", ",")
        ):
            position += 1
    elif language == "objc":
        while position < limit and tokens[position].kind == IDENT:
            position += 1
            if position < len(tokens) and tokens[position].is_punct("("):
                position = matching(tokens, position) + 1
    return position < len(tokens) and tokens[position].is_punct(";")


# --- TypeScript: a type, a modifier, a member of its own ------------------------------
def _typed(tokens: list[Token], opening: int, close: int) -> bool:
    """A parameter in the list has a type or a ``?``, which no argument has."""
    for part in _parts(tokens[opening + 1 : close]):
        kept = _undecorated(part)
        if kept and kept[0].is_punct("..."):
            kept = kept[1:]
        if not kept:
            continue
        if kept[0].is_punct("{", "["):
            # A destructured parameter: `{ a, b }: Props`.
            end = matching(kept, 0)
            if end + 1 < len(kept) and kept[end + 1].is_punct(":"):
                return True
        elif kept[0].kind == IDENT and len(kept) > 1:
            if kept[1].is_punct(":") or (
                kept[1].is_punct("?") and (len(kept) == 2 or kept[2].is_punct(":"))
            ):
                return True
    return False


def _modified(tokens: list[Token], source: str, name: int) -> bool:
    """A modifier stands before ``tokens[name]`` on its line (``abstract track(``)."""
    if not name:
        return False
    before = tokens[name - 1]
    return (
        before.kind == IDENT
        and before.text in _TS_MODIFIERS
        and "\n" not in source[before.end : tokens[name].offset]
    )


def _member_start(tokens: list[Token], source: str, name: int) -> bool:
    """``tokens[name]`` starts a member: it is first on its line, or follows ``{};,``."""
    if not name:
        return True
    before = tokens[name - 1]
    return before.is_punct("{", "}", ";", ",") or "\n" in source[before.end : tokens[name].offset]
