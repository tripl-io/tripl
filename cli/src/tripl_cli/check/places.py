"""Where a token of one scanned file sits: the types around it, the function it is in.

The evaluator reads most values from the argument's tokens and the run's
symbol index alone. A few need the place the argument is written: a bare
``HOME`` inside ``class Screen`` is ``Screen.HOME`` in Kotlin and Java, and
Swift's ``Self.home`` is the ``home`` of the type around it. ``FilePlaces``
answers that for one file, and the scan builds one per file it reads calls in.

It works nothing out until asked: the type bodies on the first lookup, the
function scopes (``scopes.ScopeIndex``) on the first question about a
parameter or a local, so a file whose arguments never ask pays nothing. The
same ``ScopeIndex`` serves ``scan``, which tells a declaration from a call
with it, and ``tripl check --list``, which refines a variable into a
parameter or a local.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right

from tripl_cli.check.lexer import Token
from tripl_cli.check.scopes import ScopeIndex
from tripl_cli.check.symbols import type_bodies


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
