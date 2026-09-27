"""Call sites out of a token stream: ``callee(args…)`` and Objective-C ``[recv sel:arg …]``.

Language-aware only where the languages disagree:

* a labelled argument is ``label: value`` in Swift and ``label = value`` in
  Kotlin; TypeScript and Java have none (an object literal is a value, and
  ``object_arg`` in the config reads its keys as labels instead);
* a message send exists only in Objective-C;
* ``->`` ends a signature only in Swift (``func f() -> T``); in Kotlin it opens a
  ``when`` branch or a lambda body and in Java a lambda body, so the call after
  it is scanned.

A call may span any number of lines — the tokens carry no newlines, and the
argument list is cut at its balanced closing parenthesis. Builder chains that
follow the call (``Structured(…).label("x").value(3)``) are collected as
``Call.chain`` so a preset can read them.
"""

from __future__ import annotations

from dataclasses import dataclass

from tripl_cli.check.lexer import IDENT, PUNCT, Token

# An identifier directly before a callee means a declaration (`func log(`,
# `void log(`, `fun log(`), except for these, which precede EXPRESSIONS.
_EXPRESSION_KEYWORDS = frozenset(
    {
        "return",
        "await",
        "try",
        "new",
        "throw",
        "in",
        "else",
        "case",
        "yield",
        "typeof",
        "void",
        "do",
        "then",
        "is",
        "as",
        "not",
        "and",
        "or",
        "of",
        "when",
        "if",
        "while",
        "guard",
        "defer",
        "go",
        "echo",
        "default",
        "export",
        "async",
    }
)

# Kotlin labelled jumps: `return@forEach log(…)` is a call, not a declaration.
_LABELLED_JUMPS = frozenset({"return", "break", "continue"})

# Tokens that may sit between a ternary's `?` and the call it evaluates:
# `cond ? await track(…) : …`, `cond ? !check(…) : …`, `cond ? (track(…)) : …`.
_TERNARY_SKIP_IDENTS = frozenset({"await", "new", "typeof", "void"})
_TERNARY_SKIP_PUNCT = ("!", "(", "-", "+", "~")

_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {")": "(", "]": "[", "}": "{"}

# Longest builder chain read after a call; past this it is a fluent API, not a
# tracking call, and nothing more is learned from it.
_MAX_CHAIN = 12


@dataclass(frozen=True, slots=True)
class Arg:
    """One argument: its label (if any), its position, and its value tokens."""

    label: str | None
    index: int
    tokens: tuple[Token, ...]


@dataclass(frozen=True, slots=True)
class ChainCall:
    method: str
    args: tuple[Arg, ...]


@dataclass(frozen=True, slots=True)
class Call:
    """A function call, or an Objective-C message send.

    For a message ``selector`` is set and ``callee`` is the receiver; for a
    function call ``selector`` is None.
    """

    callee: str
    args: tuple[Arg, ...]
    offset: int
    end: int
    selector: str | None = None
    chain: tuple[ChainCall, ...] = ()


def matching(tokens: list[Token], open_index: int) -> int:
    """Index of the token closing the group opened at ``open_index``, or ``len(tokens)``."""
    depth = 0
    for index in range(open_index, len(tokens)):
        token = tokens[index]
        if token.kind != PUNCT:
            continue
        if token.text in _OPENERS:
            depth += 1
        elif token.text in _CLOSERS:
            depth -= 1
            if depth == 0:
                return index
    return len(tokens)


def _matching_back(tokens: list[Token], close_index: int) -> int:
    depth = 0
    for index in range(close_index, -1, -1):
        token = tokens[index]
        if token.kind != PUNCT:
            continue
        if token.text in _CLOSERS:
            depth += 1
        elif token.text in _OPENERS:
            depth -= 1
            if depth == 0:
                return index
    return -1


def split_top_level(
    tokens: list[Token] | tuple[Token, ...], separator: str = ","
) -> list[list[Token]]:
    """``tokens`` split at ``separator`` wherever it is not inside (), [] or {}."""
    parts: list[list[Token]] = [[]]
    depth = 0
    for token in tokens:
        if token.kind == PUNCT:
            if token.text in _OPENERS:
                depth += 1
            elif token.text in _CLOSERS:
                depth -= 1
            elif token.text == separator and depth == 0:
                parts.append([])
                continue
        parts[-1].append(token)
    if parts == [[]]:
        return []
    return parts


def _args(tokens: list[Token], open_index: int, close_index: int, language: str) -> tuple[Arg, ...]:
    args: list[Arg] = []
    for index, span in enumerate(split_top_level(tokens[open_index + 1 : close_index])):
        label: str | None = None
        value = span
        if (
            len(span) >= 2
            and span[0].kind == IDENT
            and (
                (language == "swift" and span[1].is_punct(":"))
                or (language == "kotlin" and span[1].is_punct("="))
            )
        ):
            label, value = span[0].text, span[2:]
        args.append(Arg(label, index, tuple(value)))
    return tuple(args)


def _is_labelled_jump(tokens: list[Token], label_index: int) -> bool:
    """``tokens[label_index]`` is the label of ``return@label`` / ``break@label``."""
    return (
        label_index >= 2
        and tokens[label_index - 1].is_punct("@")
        and tokens[label_index - 2].kind == IDENT
        and tokens[label_index - 2].text in _LABELLED_JUMPS
    )


def _in_ternary(tokens: list[Token], first: int) -> bool:
    """The call starting at ``tokens[first]`` is a ternary branch (``? call(…) :``)."""
    index = first - 1
    while index >= 0:
        token = tokens[index]
        if token.kind == IDENT and token.text in _TERNARY_SKIP_IDENTS:
            index -= 1
            continue
        if token.is_punct(*_TERNARY_SKIP_PUNCT):
            index -= 1
            continue
        return token.is_punct("?")
    return False


def _callee(
    tokens: list[Token], name_index: int, source: str, language: str = ""
) -> tuple[str, int] | None:
    """The dotted chain ending at ``tokens[name_index]``, normalised, and its first index.

    ``None`` when the identifier is being DECLARED rather than called. Optional
    chaining (``?.``, ``?``, ``!``) is dropped, and a call or subscript inside
    the chain reads as ``()`` / ``[]`` whatever it held, so
    ``Analytics.shared()?.log`` normalises to ``Analytics.shared().log``.
    """
    pieces = [tokens[name_index].text]
    index = name_index
    while index >= 2:
        dot = tokens[index - 1]
        if not dot.is_punct(".", "?.", "::"):
            break
        before = index - 2
        while before >= 0 and tokens[before].is_punct("?", "!"):
            before -= 1
        if before < 0:
            break
        token = tokens[before]
        if token.kind == IDENT:
            pieces.append(".")
            pieces.append(token.text)
            index = before
            continue
        if token.is_punct(")", "]"):
            open_index = _matching_back(tokens, before)
            if open_index <= 0 or tokens[open_index - 1].kind != IDENT:
                break
            pieces.append(".")
            pieces.append("()" if token.text == ")" else "[]")
            pieces.append(tokens[open_index - 1].text)
            index = open_index - 1
            continue
        break
    if index >= 1:
        previous = tokens[index - 1]
        # Only on the SAME line: Swift and Kotlin end statements at a newline,
        # so `x = y` followed by `log(…)` on the next line is a call.
        same_line = "\n" not in source[previous.end : tokens[index].offset]
        if (
            same_line
            and previous.kind == IDENT
            and previous.text not in _EXPRESSION_KEYWORDS
            and not _is_labelled_jump(tokens, index - 1)
        ):
            return None
        # `->` marks a declaration only in Swift (a return type or a closure
        # signature). In Kotlin it opens a `when` branch or a lambda body and in
        # Java a lambda body, and the call after it is a real call.
        if language == "swift" and previous.is_punct("->"):
            return None
    return "".join(reversed(pieces)), index


def _chain(tokens: list[Token], after: int, language: str) -> tuple[tuple[ChainCall, ...], int]:
    chain: list[ChainCall] = []
    index = after
    while len(chain) < _MAX_CHAIN:
        probe = index
        while probe < len(tokens) and tokens[probe].is_punct("?", "!"):
            probe += 1
        if not (
            probe + 2 < len(tokens)
            and tokens[probe].is_punct(".", "?.")
            and tokens[probe + 1].kind == IDENT
            and tokens[probe + 2].is_punct("(")
        ):
            break
        close = matching(tokens, probe + 2)
        chain.append(ChainCall(tokens[probe + 1].text, _args(tokens, probe + 2, close, language)))
        index = close + 1
    return tuple(chain), index


def find_calls(tokens: list[Token], language: str, source: str) -> list[Call]:
    """Every function call in ``tokens``, plus every message send when ``language`` is objc.

    ``source`` is the text ``tokens`` came from; it is consulted for line breaks
    only, which is how a declaration (``void log(…)``, ``func log(…)``) is told
    from a call on the next line after a statement with no semicolon.
    """
    calls: list[Call] = []
    for index in range(len(tokens) - 1):
        token = tokens[index]
        if token.kind != IDENT or not tokens[index + 1].is_punct("("):
            continue
        found = _callee(tokens, index, source, language)
        if found is None:
            continue
        callee, first = found
        close = matching(tokens, index + 1)
        if language in ("ts", "java", "objc") and close + 1 < len(tokens):
            # `name(params) {` / `name(params): Type {` is a method DEFINITION in
            # these languages; Swift and Kotlin use that shape for a trailing closure.
            # A `:` after a call that is the middle of a ternary is not a return type.
            following = tokens[close + 1]
            in_ternary = _in_ternary(tokens, first)
            if following.is_punct("{") or (
                language == "ts" and following.is_punct(":") and not in_ternary
            ):
                continue
        args = _args(tokens, index + 1, close, language)
        chain, _ = _chain(tokens, close + 1, language)
        end = tokens[min(close, len(tokens) - 1)].end
        calls.append(Call(callee, args, tokens[first].offset, end, chain=chain))
    if language == "objc":
        calls.extend(_messages(tokens))
    calls.sort(key=lambda call: call.offset)
    return calls


def _messages(tokens: list[Token]) -> list[Call]:
    """``[receiver part1:arg1 part2:arg2]`` sends; unary sends and subscripts are skipped."""
    found: list[Call] = []
    for index, token in enumerate(tokens):
        if not token.is_punct("["):
            continue
        if index > 0 and tokens[index - 1].is_punct("@"):
            continue  # @[ … ] is an array literal
        close = matching(tokens, index)
        inner = tokens[index + 1 : close]
        parts: list[tuple[str, int]] = []
        depth = 0
        for position, item in enumerate(inner):
            if item.kind == PUNCT:
                if item.text in _OPENERS:
                    depth += 1
                elif item.text in _CLOSERS:
                    depth -= 1
            if (
                depth == 0
                and position > 0
                and item.kind == IDENT
                and position + 1 < len(inner)
                and inner[position + 1].is_punct(":")
                and not inner[position - 1].is_punct(".", "->")
            ):
                parts.append((item.text, position))
        if not parts:
            continue
        receiver = (
            " ".join(t.text for t in inner[: parts[0][1]]).replace("[ ", "[").replace(" ]", "]")
        )
        args: list[Arg] = []
        for number, (name, position) in enumerate(parts):
            stop = parts[number + 1][1] if number + 1 < len(parts) else len(inner)
            value = inner[position + 2 : stop]
            # Variadic tails (`arrayWithObjects:a, b, nil`) keep only the first value.
            head = split_top_level(value)
            args.append(Arg(name, number, tuple(head[0] if head else ())))
        selector = "".join(f"{name}:" for name, _ in parts)
        end = tokens[min(close, len(tokens) - 1)].end
        found.append(Call(receiver, tuple(args), token.offset, end, selector=selector))
    return found
