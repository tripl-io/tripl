"""Where a token of one scanned file sits: the types around it, the function it is in.

The evaluator reads most values from the argument's tokens and the run's
symbol index alone. A few need the place the argument is written: a bare
``HOME`` inside ``class Screen`` is ``Screen.HOME`` in Kotlin and Java,
Swift's ``Self.home`` is the ``home`` of the type around it, and a local set
once to a constant (``val screen = "home"``) is that constant further down
its function. ``FilePlaces`` answers that for one file, and the scan builds
one per file it reads calls in.

It works nothing out until asked: the type bodies on the first lookup, the
function scopes (``scopes.ScopeIndex``) on the first question about a
parameter or a local, so a file whose arguments never ask pays nothing. The
same ``ScopeIndex`` serves ``scan``, which tells a declaration from a call
with it, and ``tripl check --list``, which refines a variable into a
parameter or a local.

A local is read when all of this holds, and stays dynamic otherwise:

* it is declared so that it cannot be reassigned: Kotlin ``val``, Swift
  ``let`` (not one in an ``if``/``while``/``guard``/``case`` condition),
  TypeScript ``const``, or a Java typed local (``String name = …``, after a
  statement or ``final``) that nothing in the function assigns again;
* the innermost named function around the use declares it exactly once,
  earlier, in a block that is still open at the use, and binds the name
  nowhere else: no parameter of that function, no other local, no lambda,
  closure or loop variable, no destructured name;
* its initializer, which ends where the statement ends, reads as a constant
  (the evaluator decides that, see ``Evaluator._local``).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right

from tripl_cli.check.calls import matching
from tripl_cli.check.lexer import IDENT, PUNCT, Token
from tripl_cli.check.scopes import ScopeIndex, expression_end
from tripl_cli.check.symbols import type_bodies

# The keyword that declares a local nothing can reassign, per language. Java's
# typed locals are found by their shape instead.
_CONSTANT_LOCALS = {"kotlin": "val", "swift": "let", "ts": "const"}
# Every keyword that binds a local name, constant or not.
_LOCAL_KEYWORDS = frozenset({"val", "var", "let", "const"})
# A Swift `let` after one of these binds a name for a condition's block only.
_SWIFT_CONDITIONS = frozenset({"if", "while", "guard", "case"})
# Words a Java statement starts with that are no type: `return name;` declares nothing.
_JAVA_NOT_TYPES = frozenset(
    {
        "return",
        "new",
        "throw",
        "case",
        "else",
        "yield",
        "assert",
        "instanceof",
        "default",
        "break",
        "continue",
        "do",
        "this",
        "super",
        "package",
        "import",
    }
)
# What a Java typed local's name is followed by: an initializer, the statement's
# end, a for-each's `:`, the next name, a catch or lambda parameter's `)`.
_JAVA_AFTER_NAME = ("=", ";", ":", ",", ")")
# What ends a lambda's, a closure's or a loop's parameters: the names before it are bound.
_BINDING_ARROWS = ("->", "=>")
_BINDING_WORDS = frozenset({"in", "of"})
# What may sit among those parameters, walking back from the arrow, and how far.
_PARAMETER_PUNCT = (",", "(", ")", ":", ".", "?", "<", ">", "[", "]")
_MAX_PARAMETERS = 16
# The most tokens a local's declared type (`val x: Map<String, Int> =`) may hold.
_MAX_TYPE = 32
# A Swift line that starts with one of these may go on with the line before
# (`let x = "a"` then `+ b`), so the initializer's end is not known.
_SWIFT_STATEMENT_STARTS = ("}", ";", "@", "#")


class FilePlaces:
    """One file's tokens, and the lookups made by a token's offset in it."""

    def __init__(self, tokens: list[Token], language: str, source: str) -> None:
        self.tokens = tokens
        self.language = language
        self.source = source
        self._scopes: ScopeIndex | None = None
        self._offsets: list[int] | None = None
        # The type bodies by their `{` offset, and each one's `}` offset and members' path.
        self._starts: list[int] | None = None
        self._bodies: list[tuple[int, tuple[str, ...]]] = []
        # Per name: where a local that cannot be reassigned is declared, and
        # where anything at all binds the name. Built on first use.
        self._constant_locals: dict[str, list[int]] | None = None
        self._bindings: dict[str, list[int]] | None = None

    @property
    def scopes(self) -> ScopeIndex:
        """The file's function scopes, built on first use."""
        if self._scopes is None:
            self._scopes = ScopeIndex(self.tokens, self.language, self.source)
        return self._scopes

    def index_at(self, offset: int) -> int:
        """The index of the token that starts at ``offset`` (or the first one after it)."""
        if self._offsets is None:
            self._offsets = [token.offset for token in self.tokens]
        return bisect_left(self._offsets, offset)

    def types_around(self, offset: int) -> list[tuple[str, ...]]:
        """The members' path of each type body holding ``offset``, innermost first."""
        if self._starts is None:
            found = type_bodies(self.tokens, self.language)
            last = len(self.tokens) - 1
            self._starts = [self.tokens[brace].offset for brace, _, _ in found]
            self._bodies = [
                (self.tokens[min(close, last)].offset if last >= 0 else 0, path)
                for _, close, path in found
            ]
        around: list[tuple[str, ...]] = []
        # Bodies open in order and nest, so those holding `offset` are among the
        # ones that opened before it, the innermost last. An unnamed companion
        # object's body has its outer type's path: it is listed once.
        for position in range(bisect_right(self._starts, offset) - 1, -1, -1):
            close, path = self._bodies[position]
            if offset < close and (not around or path != around[-1]):
                around.append(path)
        return around

    def shadowed(self, offset: int, name: str) -> bool:
        """Whether ``name`` at ``offset`` may be a parameter or a local, not a type's member.

        A parameter of any named function around it, or a local declared with
        ``val``/``var``/``let``/``const`` in the innermost one before it. Blocks
        and lambdas are not told apart: a local anywhere earlier in the
        function counts, and a lambda's parameter does not.
        """
        index = self.index_at(offset)
        scopes = self.scopes
        if any(scope.holds(index) and scope.parameter(name) for scope in scopes.functions):
            return True
        innermost = scopes.innermost(index)
        return innermost is not None and (
            scopes.local_declaration(innermost, index, name) is not None
        )

    def may_be_local(self, name: str) -> bool:
        """Whether the file declares a local ``name`` that cannot be reassigned, anywhere.

        The cheap test before ``local_initializer``: a name no such local has
        never builds the function scopes.
        """
        if self._constant_locals is None:
            self._constant_locals = _constant_locals(self.tokens, self.language)
        return name in self._constant_locals

    def local_initializer(self, offset: int, name: str) -> list[Token] | None:
        """The initializer of the local ``name`` that the token at ``offset`` reads, or None.

        None unless the rule in this module's docstring holds; the initializer
        is not read here.
        """
        if not self.may_be_local(name):
            return None
        index = self.index_at(offset)
        scope = self.scopes.innermost(index)
        if scope is None or scope.parameter(name) is not None:
            return None
        if self._bindings is None:
            self._bindings = _bindings(self.tokens, self.language)
        start, end = scope.body
        positions = self._bindings.get(name, [])
        inside = positions[bisect_right(positions, start) : bisect_left(positions, end)]
        if len(inside) != 1:
            return None
        declared = inside[0]
        constant = self._constant_locals or {}
        if declared >= index or declared not in constant.get(name, ()):
            return None
        span = _initializer(self.tokens, self.source, self.language, declared)
        if span is None or index < span[1] or not _still_open(self.tokens, declared, index):
            return None
        if self.language == "java" and _assigned(self.tokens, name, start, end, declared):
            return None
        return self.tokens[span[0] : span[1]]


def _constant_locals(tokens: list[Token], language: str) -> dict[str, list[int]]:
    """Per name: the token indexes where a local that cannot be reassigned is declared.

    Kept to plain comparisons, no calls: plain ``tripl check`` asks this at
    the innermost value of a nested argument, where a frame more is a level
    of nesting less.
    """
    found: dict[str, list[int]] = {}
    keyword = _CONSTANT_LOCALS.get(language)
    java = language == "java"
    if keyword is None and not java:
        return found
    for index in range(1, len(tokens) - 1):
        token = tokens[index]
        if token.kind != IDENT:
            continue
        before = tokens[index - 1]
        if keyword is not None:
            if before.kind != IDENT or before.text != keyword:
                continue
            if language == "swift" and index >= 2:
                condition = tokens[index - 2]
                if (condition.kind == IDENT and condition.text in _SWIFT_CONDITIONS) or (
                    condition.kind == PUNCT and condition.text in (",", "(")
                ):
                    continue  # `if let x = …`, `case let x`, `if a, let x = …`
        else:
            after = tokens[index + 1]
            if before.kind != IDENT or before.text in _JAVA_NOT_TYPES or before.text == "final":
                continue
            if after.kind != PUNCT or after.text != "=" or index < 2:
                continue
            start = tokens[index - 2]
            if not (
                (start.kind == PUNCT and start.text in (";", "{", "}"))
                or (start.kind == IDENT and start.text == "final")
            ):
                continue
        found.setdefault(token.text, []).append(index)
    return found


def _bindings(tokens: list[Token], language: str) -> dict[str, list[int]]:
    """Per name: every token index where something binds it, in order.

    A local of any kind (and each name a destructuring declares), a Java
    typed local or parameter, and the names before a lambda's, a closure's or
    a loop's ``->``, ``=>``, ``in`` or ``of``. It errs toward too many: a
    type's name or a ``when`` entry's condition counts too, which only ever
    leaves a local unread.
    """
    found: dict[str, list[int]] = {}

    def bind(index: int) -> None:
        found.setdefault(tokens[index].text, []).append(index)

    for index, token in enumerate(tokens):
        if token.kind == IDENT and token.text in _LOCAL_KEYWORDS and index + 1 < len(tokens):
            following = tokens[index + 1]
            if following.kind == IDENT:
                bind(index + 1)
            elif following.is_punct("(", "[", "{"):
                close = matching(tokens, index + 1)
                for inner in range(index + 2, min(close, len(tokens))):
                    if tokens[inner].kind == IDENT:
                        bind(inner)
        elif (
            language == "java"
            and token.kind == IDENT
            and 0 < index < len(tokens) - 1
            and tokens[index + 1].is_punct(*_JAVA_AFTER_NAME)
            and (
                (tokens[index - 1].kind == IDENT and tokens[index - 1].text not in _JAVA_NOT_TYPES)
                or tokens[index - 1].is_punct(">", "]")
            )
        ):
            bind(index)
        if token.is_punct(*_BINDING_ARROWS) or (
            token.kind == IDENT and token.text in _BINDING_WORDS
        ):
            position = index - 1
            while position >= 0 and index - position <= _MAX_PARAMETERS:
                before = tokens[position]
                if before.kind == IDENT:
                    bind(position)
                elif not before.is_punct(*_PARAMETER_PUNCT):
                    break
                position -= 1
    for positions in found.values():
        positions.sort()
    return found


def _initializer(
    tokens: list[Token], source: str, language: str, declared: int
) -> tuple[int, int] | None:
    """The token range ``[first, last)`` of the initializer of the local named at ``declared``.

    Past a declared type (``val x: String =``) to the ``=``; it ends where the
    statement does: at Java's ``;``, else where ``scopes.expression_end``
    ends an expression body. A Swift line after it that may go on with it
    (it starts with an operator) leaves the end unknown.
    """
    position = declared + 1
    if language != "java" and position < len(tokens) and tokens[position].is_punct(":"):
        position += 1
        while position < len(tokens) and position - declared <= _MAX_TYPE:
            token = tokens[position]
            if token.is_punct("="):
                break
            if token.is_punct("("):
                position = matching(tokens, position) + 1  # a function type
            elif token.kind == IDENT or token.is_punct(".", "?", "<", ">", ",", "[", "]", "->"):
                position += 1
            else:
                return None
    if position >= len(tokens) or not tokens[position].is_punct("="):
        return None
    if language == "java":
        last = _statement_end(tokens, position + 1)
    else:
        last = expression_end(tokens, source, position, language)
        if language == "swift" and last < len(tokens):
            following = tokens[last]
            if following.kind == PUNCT and following.text not in _SWIFT_STATEMENT_STARTS:
                return None
    if last <= position + 1:
        return None
    return position + 1, last


def _statement_end(tokens: list[Token], position: int) -> int:
    """The index of the ``;`` that ends the Java statement at ``position`` (or what closes it)."""
    depth = 0
    while position < len(tokens):
        token = tokens[position]
        if token.kind == PUNCT:
            if token.text in ("(", "[", "{"):
                depth += 1
            elif token.text in (")", "]", "}"):
                if depth == 0:
                    return position
                depth -= 1
            elif token.text == ";" and depth == 0:
                return position
        position += 1
    return position


def _still_open(tokens: list[Token], declared: int, index: int) -> bool:
    """Whether the block that declares the local at ``declared`` is still open at ``index``."""
    depth = 0
    for position in range(declared + 1, index):
        token = tokens[position]
        if token.is_punct("{"):
            depth += 1
        elif token.is_punct("}"):
            depth -= 1
            if depth < 0:
                return False
    return True


def _assigned(tokens: list[Token], name: str, start: int, end: int, declared: int) -> bool:
    """Whether the body between ``start`` and ``end`` assigns ``name`` other than at ``declared``.

    ``name = …`` and a compound ``name += …``; ``this.name = …`` is a field's.
    """
    for position in range(start + 1, min(end, len(tokens) - 2)):
        token = tokens[position]
        if position == declared or token.kind != IDENT or token.text != name:
            continue
        if tokens[position - 1].is_punct(".", "?.", "::"):
            continue
        following = tokens[position + 1]
        if following.is_punct("="):
            return True
        if (
            following.kind == PUNCT
            and following.text in ("+", "-", "*", "/", "%", "&", "|", "^")
            and tokens[position + 2].is_punct("=")
            and tokens[position + 2].offset == following.end
        ):
            return True
    return False
