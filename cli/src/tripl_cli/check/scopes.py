"""Named functions out of a token stream, for the hints ``tripl check --list`` gives.

When a value the scan could not read is a plain variable, the useful thing to
say is where it comes from: a parameter of the function the call sits in (the
value is in that function's callers, so the function wants a call spec of its
own), a property of such a parameter (the value is set where the object is
built), or a local. That needs a file's named functions, their parameters and
the token range of their bodies, and nothing about types or statements.

This is its own keyword-led recognizer. It is not built from what
``find_calls`` rejects as a declaration, because ``find_calls`` returns some
declarations as calls (``FunctionScope.signature_end`` lets a caller tell
them), and not on the function walk in ``symbols`` either: changing that walk
would change what the index reads, and so what ``tripl check`` sends. The
shapes, per language:

* Kotlin ``fun [<T>] [Recv.]name(params)[: Ret] { … }`` and the expression
  body ``fun name(params)[: Ret] = expr``;
* Swift ``func name[<T>](params) [async] [throws] [-> Ret] [where …] { … }``,
  and ``init`` / ``init?`` / ``init!`` with any modifiers;
* Java, and Objective-C's C functions, ``[modifiers] Ret name(params)
  [throws A, B] { … }``, a constructor included;
* TypeScript / JavaScript ``function name(params) { … }``, a method
  ``[async|static|get|set] name(params)[: Ret] { … }``, and a named arrow
  ``const name = [async] (params)[: Ret] => …`` or ``const name = x => …``.

Anything else is no scope, and a value inside it stays a ``variable``:
lambdas, closures and anonymous functions, Kotlin ``init { … }`` and
``constructor(…)``, and Objective-C methods (``- (void)log:(NSString *)name
{``), whose parameters sit between selector parts this does not read.

Between a parameter list and its body only a signature tail may sit. It starts
the way the language's signatures continue (``:``, ``->``, ``throws``,
``where`` …), holds at most ``_MAX_TAIL`` tokens (a ``(…)``, ``[…]`` or object
type in it counts as one, however long), and never a ``;``, ``{`` or ``}`` (nor
an ``=`` other than a Kotlin expression body's), an annotation, a statement
keyword or a word that starts a declaration. A TypeScript tail is a type, so
an object type (``: { ok: boolean }``, ``Promise<{ … }>``) and a function
type's ``=>`` belong to it, and a name with a parameter list after it ends it:
the next member of a class written without ``;``. That is where a body-less
abstract or interface function ends, rather than running into the next
function's body; two scopes that claim the same body are both dropped.

Such a declaration with no body (an interface or abstract member, a protocol
requirement, an overload, a C prototype, as ``signatures`` tells them from a
call) is no scope, but where its signature ends is kept
(``ScopeIndex.bodiless``), as ``find_calls`` reads some of them as calls. A
method in a TypeScript interface's body is such a declaration whatever its
parameters look like, since an interface holds no calls: there an untyped
``track(name, props);`` declares too, where ``signatures`` alone cannot tell
it from a call. An object type, in an interface or not, is not read that way. A
Swift ``init`` and a TypeScript ``constructor`` also name the type whose body
they sit in (``FunctionScope.owner``), which is what their callers write.

Where each bracket closes is found once per file (``calls.closers``) and
looked up from there. A parameter list is read at its top level only, and an
expression body steps over its bracketed groups rather than reading them, so
declarations and calls nested in each other's brackets (a parameter list, a
default value, a body in braces, a call inside an expression body) cost about
the file's size, not the square of how deeply they nest. Two shapes still do
cost that square, neither of them valid code: expression bodies nested with no
bracket between them (``fun f() = fun f() = …``), whose ends are each found
by reading on to the end of the nest; and, in Java and Objective-C, a body
nested inside a parameter, since each parameter is read whole up to its
default value. ``ScopeIndex`` keeps a file's
scopes for the lookups made per call, by bisection, so a file with many calls
costs about its own size too.

Mostly a hint for ``--list``. What ``tripl check`` sends depends on it in a few
places only: a declaration a call spec matched is no call (``scan``), and,
through ``places.FilePlaces``, a parameter or a local hides a type's member
named without its type, and a local set once to a constant is read.
``expression_end`` also tells the evaluator where a Kotlin ``when`` entry ends.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace

from tripl_cli.check.calls import closers, matching
from tripl_cli.check.lexer import IDENT, PUNCT, Token
from tripl_cli.check.signatures import Parameter, declares, member_start, parameters

# The most tokens a signature tail (a return type, `throws`, `where` …) may hold,
# a bracketed part it skips (`(…)`, `[…]`, an object type) counting as one.
_MAX_TAIL = 32
# The most tokens between `fun` and its parameter list (type parameters, a
# receiver type), and in a `<…>` type parameter list.
_MAX_HEAD = 40
# The most tokens between a type's keyword and its body (generics, inheritance, `where`).
_MAX_TYPE_HEADER = 64

# Never a function, in the name position or right before it.
_STATEMENTS = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "when",
        "guard",
        "synchronized",
        "with",
        "try",
        "do",
        "else",
        "new",
        "return",
        "throw",
        "case",
    }
)
# Before a method name: a `function` is read as one, and these keywords mean an expression.
_NOT_BEFORE_METHOD = _STATEMENTS | {
    "function",
    "await",
    "typeof",
    "void",
    "yield",
    "delete",
    "in",
    "of",
    "instanceof",
}
# What may follow a parameter list, per language: the body itself, or the first
# token of a signature tail (a return type, `throws`, `async`, `where` …). A
# call followed by anything else on the next line is no declaration.
_TAIL_STARTS: dict[str, frozenset[str]] = {
    "kotlin": frozenset({"{", "=", ":", "where"}),
    "swift": frozenset({"{", "->", "async", "throws", "rethrows", "reasync", "where"}),
    "java": frozenset({"{", "throws"}),
    "objc": frozenset({"{"}),
    "ts": frozenset({"{", ":"}),
}
# A signature tail that reaches one of these has no body: the next declaration starts.
_DECLARATION_STARTS = frozenset(
    {
        "fun",
        "func",
        "function",
        "val",
        "var",
        "let",
        "class",
        "struct",
        "object",
        "interface",
        "enum",
        "init",
        "constructor",
        "companion",
        "override",
        "private",
        "public",
        "protected",
        "internal",
        "abstract",
        "open",
        "suspend",
        "static",
        "final",
    }
)
# TypeScript's: the other languages' words are names there (`: object` is a type).
_TS_DECLARATION_STARTS = frozenset(
    {"function", "var", "let", "const", "class", "interface", "enum", "constructor", "async"}
    | {"override", "private", "public", "protected", "abstract", "static", "declare", "export"}
)
_TAIL_STOPS = (";", "}", ")", "]", "=", "=>", "@")
# In a TypeScript type, a `{` right after one of these opens an object type, not a body.
_OBJECT_TYPE_AFTER = (":", "|", "&", "<", ",", "=>")
# TypeScript type operators, which a parenthesized type (`keyof (A | B)`) or an
# object type (`T extends { id: string } ? …`, `value is { … }`) may follow.
_TYPE_OPERATORS = frozenset({"extends", "import", "is", "keyof", "readonly"})
# The keywords of a type whose body a Swift `init` or a TypeScript `constructor`
# sits in, and the two functions named after their keyword, not their type.
_TYPE_KEYWORDS: dict[str, frozenset[str]] = {
    "swift": frozenset({"class", "struct", "enum", "actor", "extension"}),
    "ts": frozenset({"class"}),
}
_OWNED = frozenset({("swift", "init"), ("ts", "constructor")})
# After `class`, no type's name: Swift's `class func` and `class var` are
# modifiers, and TypeScript's `class extends A {` has no name.
_NOT_TYPE_NAMES = _DECLARATION_STARTS | {"subscript", "extends", "implements"}
_OPERATORS = ("&&", "||", "??", "+", "-", "*", "/", "%", "|", "&", "^")
_COMPARISONS = ("==", "!=", "===", "!==", "<=", ">=")
# A line that ends with one of these goes on: no expression ends in an operator.
_OPEN_ENDS = (*_OPERATORS, *_COMPARISONS, ":", "=", ",", "=>", ".", "?.")
# A line that starts with one of these continues an expression body. A Kotlin
# line that starts with `+` or `-` is a statement of its own; in TypeScript a
# binary operator, or a ternary's `?` or `:`, carries the expression on.
_CONTINUATIONS = (".", "?.", "&&", "||")
_KOTLIN_CONTINUATIONS = ("else", "catch", "finally")
_TS_CONTINUATIONS = (*_CONTINUATIONS, *_OPERATORS, *_COMPARISONS, "?", ":")
# What a TypeScript method's type parameters may hold, so `a < b && c > (d)` is none.
_TYPE_PARAMETER_PUNCT = (",", ".", "<", ">", "[", "]", "|", "&", "=", "?", ":")
_LOCAL_KEYWORDS = ("val", "var", "let", "const")
_OPENERS = ("(", "[", "{")
_CLOSERS = (")", "]", "}")


@dataclass(frozen=True, slots=True)
class FunctionScope:
    """A named function: its name, where it is declared, its parameters and its body."""

    name: str
    # The name token's offset in the file, for the declaration's line.
    offset: int
    parameters: tuple[Parameter, ...]
    # Token indexes: the body's `{` (or the `=` / `=>` before an expression body)
    # and the token that closes it (or the first token past it). A token is in
    # the body when it lies strictly between the two.
    body: tuple[int, int]
    # The file offset just past the parameter list's `)`. A call `find_calls`
    # reports with this `end` is this declaration, read as a call.
    signature_end: int
    # A Swift `init` or a TypeScript `constructor`: the type whose body it sits
    # in, the name its callers write (`ScreenView(screen:)`, `new PageView(…)`;
    # the last part of a dotted `extension A.B`). None for any other function,
    # and for one in no named type's body.
    owner: str | None = None

    def parameter(self, name: str) -> Parameter | None:
        return next((parameter for parameter in self.parameters if parameter.name == name), None)

    def holds(self, index: int) -> bool:
        return self.body[0] < index < self.body[1]


def functions(tokens: list[Token], language: str, source: str) -> list[FunctionScope]:
    """Every named function in ``tokens`` whose body can be bounded, in declaration order.

    ``source`` is the text ``tokens`` came from, read for line breaks only:
    that is where a Kotlin or arrow expression body ends.
    """
    return _declarations(tokens, language, source)[0]


class ScopeIndex:
    """A file's function scopes, built once, and the lookups made per call in it.

    ``functions`` is what ``functions()`` returns. ``bodiless`` holds the
    signature ends of the declarations with no body, by the convention of
    ``FunctionScope.signature_end``: a call ``find_calls`` reports with one of
    these ``end``s is that declaration, read as a call. ``innermost`` and
    ``local_declaration`` bisect, so they cost the same in a file of any size.
    """

    def __init__(self, tokens: list[Token], language: str, source: str) -> None:
        found, bodiless = _declarations(tokens, language, source)
        self.functions: tuple[FunctionScope, ...] = tuple(found)
        self.bodiless: frozenset[int] = frozenset(bodiless)
        self._tokens = tokens
        # By body start, which no two kept scopes share (a shared body drops both).
        self._ordered = sorted(found, key=lambda scope: scope.body[0])
        self._starts = [scope.body[0] for scope in self._ordered]
        self._parents = _parents(self._ordered)
        # Per name: where `val`/`var`/`let`/`const` declares it, built on first use.
        self._locals: dict[str, list[int]] | None = None

    def innermost(self, index: int) -> FunctionScope | None:
        """The scope whose body holds the token at ``index`` most closely."""
        position = bisect_left(self._starts, index) - 1
        while position >= 0:
            scope = self._ordered[position]
            if scope.holds(index):
                return scope
            position = self._parents[position]
        return None

    def local_declaration(self, scope: FunctionScope, before: int, name: str) -> int | None:
        """Where ``scope``'s body declares the local ``name`` last before token ``before``.

        The token index of ``name`` after ``val``/``var``/``let``/``const``, or
        None. A Java typed local (``String name = …``) is not looked for.
        """
        if self._locals is None:
            self._locals = _local_names(self._tokens)
        positions = self._locals.get(name, [])
        last = bisect_left(positions, before) - 1
        if last >= 0 and positions[last] > scope.body[0]:
            return positions[last]
        return None


def _declarations(
    tokens: list[Token], language: str, source: str
) -> tuple[list[FunctionScope], list[int]]:
    """``functions()``'s scopes, and the signature ends of the declarations with no body."""
    found: list[FunctionScope] = []
    bodiless: list[int] = []
    # Per open `{`: the name of the type it is the body of, else None.
    types: list[str | None] = []
    headers: dict[int, str | None] = {}
    # Per open `{`: whether it is a TypeScript interface's body, and the `{`s
    # that will be. Only the innermost `{` counts, so an object type nested in
    # an interface is not one.
    interfaces: list[bool] = []
    interface_bodies: set[int] = set()
    type_keywords = _TYPE_KEYWORDS.get(language, frozenset())
    # Where each bracket closes, found once: the readers below ask it at every
    # level of a nest, where scanning for the closer each time would cost the
    # square of the nest's depth.
    closes = closers(tokens)
    for index, token in enumerate(tokens):
        if token.kind == PUNCT:
            if token.text == "{":
                types.append(headers.pop(index, None))
                interfaces.append(index in interface_bodies)
            elif token.text == "}" and types:
                types.pop()
                interfaces.pop()
            continue
        if token.kind != IDENT:
            continue
        if token.text in type_keywords:
            header = _type_body(tokens, closes, index, language)
            if header is not None:
                headers[header[0]] = header[1]
        elif language == "ts" and token.text == "interface":
            body = _interface_body(tokens, closes, source, index)
            if body is not None:
                interface_bodies.add(body)
        declared = _declared(tokens, closes, source, language, index)
        if isinstance(declared, FunctionScope):
            if (language, declared.name) in _OWNED:
                declared = replace(declared, owner=types[-1] if types else None)
            found.append(declared)
        elif declared is not None:
            bodiless.append(declared)
        elif interfaces and interfaces[-1]:
            member = _interface_member(tokens, closes, source, index)
            if member is not None:
                bodiless.append(member)
    claims = Counter(scope.body for scope in found)
    return [scope for scope in found if claims[scope.body] == 1], bodiless


def _declared(
    tokens: list[Token], closes: list[int], source: str, language: str, index: int
) -> FunctionScope | int | None:
    """The declaration at ``tokens[index]``: its scope, or its signature end when it has no body.

    None when no declaration starts there. ``closes`` is ``calls.closers(tokens)``,
    which every reader below takes for where a bracket closes.
    """
    text = tokens[index].text
    if language == "kotlin" and text == "fun":
        return _kotlin(tokens, closes, source, index)
    if language == "swift" and text in ("func", "init"):
        return _swift(tokens, closes, source, index)
    if language in ("java", "objc"):
        return _c_family(tokens, closes, source, language, index)
    if language == "ts":
        return _typescript(tokens, closes, source, index)
    return None


def _parents(ordered: Sequence[FunctionScope]) -> list[int]:
    """Per scope in ``ordered`` (by body start): the last one before it still open there, or -1.

    So a lookup walks from the last scope that starts before a token out to
    the ones around it, and the first whose body holds the token is the
    innermost one.
    """
    parents: list[int] = []
    around: list[int] = []
    for position, scope in enumerate(ordered):
        while around and ordered[around[-1]].body[1] <= scope.body[0]:
            around.pop()
        parents.append(around[-1] if around else -1)
        around.append(position)
    return parents


def _local_names(tokens: list[Token]) -> dict[str, list[int]]:
    """Per name: the token indexes where ``val``/``var``/``let``/``const`` declares it, in order."""
    found: dict[str, list[int]] = {}
    for index in range(1, len(tokens)):
        token = tokens[index]
        if token.kind == IDENT and tokens[index - 1].is_ident(*_LOCAL_KEYWORDS):
            found.setdefault(token.text, []).append(index)
    return found


# --- per language -----------------------------------------------------------------
def _kotlin(
    tokens: list[Token], closes: list[int], source: str, keyword: int
) -> FunctionScope | int | None:
    """``fun [<T>] [Recv.]name(``: the name is the identifier before the first ``(`` past ``<>``."""
    angles = 0
    for index in range(keyword + 1, min(len(tokens), keyword + 1 + _MAX_HEAD)):
        token = tokens[index]
        if token.is_punct("<"):
            angles += 1
        elif token.is_punct(">"):
            angles -= 1
        elif token.is_punct("{", "}", "=", ";"):
            return None  # `fun interface X {`
        elif token.is_punct("(") and angles == 0:
            name = index - 1
            if name > keyword and _is_name(tokens[name]):
                return _scope(tokens, closes, source, "kotlin", name, index, declaring=True)
            return None  # an anonymous `fun(…)`
    return None


def _swift(
    tokens: list[Token], closes: list[int], source: str, keyword: int
) -> FunctionScope | int | None:
    if tokens[keyword].text == "func":
        name = keyword + 1
        if name >= len(tokens) or not _is_name(tokens[name]):
            return None  # an operator function (`func ==`)
        opening = _after_angles(tokens, name + 1)
    else:
        if keyword and tokens[keyword - 1].is_punct("."):
            return None  # `self.init(…)`, `super.init(…)`: a call
        name = keyword
        opening = keyword + 1
        if opening < len(tokens) and tokens[opening].is_punct("?", "!"):
            opening += 1  # `init?(` is no call candidate at all
        opening = _after_angles(tokens, opening)
    if opening >= len(tokens) or not tokens[opening].is_punct("("):
        return None
    return _scope(tokens, closes, source, "swift", name, opening, declaring=True)


def _c_family(
    tokens: list[Token], closes: list[int], source: str, language: str, name: int
) -> FunctionScope | int | None:
    """``Ret name(``: a type, a generic's ``>`` or an array's ``]`` before the name.

    A constructor has no return type: a member boundary (``{``, ``}``, ``;``)
    stands before it, where a call followed by ``{`` would not compile. In
    Objective-C a pointer's ``*`` may sit there too (``NSString *name(``).
    """
    if name == 0 or name + 1 >= len(tokens) or not tokens[name + 1].is_punct("("):
        return None
    if not _is_name(tokens[name]):
        return None
    before = tokens[name - 1]
    if before.kind == IDENT:
        typed = before.text not in _STATEMENTS
    else:
        typed = before.is_punct(">", "]", "{", "}", ";") or (
            language == "objc" and before.is_punct("*")
        )
    return _scope(tokens, closes, source, language, name, name + 1) if typed else None


def _typescript(
    tokens: list[Token], closes: list[int], source: str, index: int
) -> FunctionScope | int | None:
    token = tokens[index]
    if token.text == "function":
        name = index + 1
        if name < len(tokens) and tokens[name].is_punct("*"):
            name += 1  # a generator
        if name >= len(tokens) or not _is_name(tokens[name]):
            return None  # an anonymous function expression
        opening = _after_angles(tokens, name + 1)
        if opening >= len(tokens) or not tokens[opening].is_punct("("):
            return None
        return _scope(tokens, closes, source, "ts", name, opening, declaring=True)
    if token.text in _LOCAL_KEYWORDS:
        return _arrow(tokens, closes, source, index)
    return _method(tokens, closes, source, index)


def _method(
    tokens: list[Token], closes: list[int], source: str, name: int
) -> FunctionScope | int | None:
    """``name(params)[: Ret] {``: a class or object-literal method.

    Outside a class body that shape does not compile except as a control
    statement, which ``_STATEMENTS`` leaves out. A ``void`` before the name is
    the operator, unless it is the return type that ends the line before; an
    ``async`` with an ``=>`` after its list is an anonymous arrow.
    """
    if not _is_name(tokens[name]) or tokens[name].text == "function":
        return None
    if name:
        before = tokens[name - 1]
        if before.is_punct(".", "?.", "@", "!", "?") or (
            before.kind == IDENT
            and before.text in _NOT_BEFORE_METHOD
            and not _void_return(tokens, source, name)
        ):
            # A call on something, a decorator, a ternary's branch, a `function` (read above).
            return None
        if before.is_punct("*") and name >= 2 and tokens[name - 2].is_ident("function"):
            return None
    opening = name + 1
    if opening < len(tokens) and tokens[opening].is_punct("<"):
        opening = _after_angles(tokens, opening, allowed=_TYPE_PARAMETER_PUNCT)
    if opening >= len(tokens) or not tokens[opening].is_punct("("):
        return None
    if tokens[name].text == "async":
        close = closes[opening]
        if close < len(tokens) and _arrow_after(tokens, closes, close + 1) is not None:
            return None
    return _scope(tokens, closes, source, "ts", name, opening)


def _interface_member(tokens: list[Token], closes: list[int], source: str, name: int) -> int | None:
    """The signature end of the method ``tokens[name]`` names in a TypeScript interface body.

    An interface holds no statements, so ``name[?][<T>](params)`` at the start
    of a member declares a method, whatever its parameters look like: the
    untyped ``track(name, props);`` included, which ``signatures.declares``
    cannot tell from a call. A construct signature (``new(config);``) is one
    too, though ``new`` is no name elsewhere. None when no method starts at
    ``name``.
    """
    token = tokens[name]
    if not (_is_name(token) or token.is_ident("new")) or not member_start(tokens, source, name):
        return None
    opening = name + 1
    if opening < len(tokens) and tokens[opening].is_punct("?"):
        opening += 1  # an optional method: `track?(name);`
    if opening < len(tokens) and tokens[opening].is_punct("<"):
        opening = _after_angles(tokens, opening, allowed=_TYPE_PARAMETER_PUNCT)
    if opening >= len(tokens) or not tokens[opening].is_punct("("):
        return None
    close = closes[opening]
    return tokens[close].end if close < len(tokens) else None


def _interface_body(
    tokens: list[Token], closes: list[int], source: str, keyword: int
) -> int | None:
    """The ``{`` of the body of the TypeScript interface ``tokens[keyword]`` declares.

    Only a declaration counts: ``interface`` starts a statement (after
    ``export``, ``declare`` or ``default`` at most), and its header is exactly
    ``Name[<…>] [extends A[<…>], B.C[<…>] …] {``. Prose in JSX text
    (``<p>Our new interface is ready, {track("seen")}</p>``) is none, so the
    call in its ``{…}`` stays a call. The header may be of any length, since
    every token in it is one of these parts. None when no such body follows.
    """
    start = keyword
    while start and tokens[start - 1].is_ident("export", "declare", "default"):
        start -= 1
    if start:
        before = tokens[start - 1]
        if (
            not before.is_punct("{", "}", ";")
            and "\n" not in source[before.end : tokens[start].offset]
        ):
            return None
    position = keyword + 1
    if position >= len(tokens) or not _is_type_name(tokens[position]):
        return None
    position = _past_type_arguments(tokens, closes, position + 1)
    if position < len(tokens) and tokens[position].is_ident("extends"):
        while True:
            position = _past_heritage(tokens, closes, position + 1)
            if position >= len(tokens) or not tokens[position].is_punct(","):
                break
    if position < len(tokens) and tokens[position].is_punct("{"):
        return position
    return None


def _past_heritage(tokens: list[Token], closes: list[int], position: int) -> int:
    """The index past one type an interface extends (``A.B<T>``, ``import('x').C``).

    ``len(tokens)`` when no such type starts at ``position``.
    """
    if position >= len(tokens) or not _is_type_name(tokens[position]):
        return len(tokens)
    if tokens[position].text == "import":
        if position + 1 >= len(tokens) or not tokens[position + 1].is_punct("("):
            return len(tokens)
        position = closes[position + 1]
    position += 1
    while (
        position + 1 < len(tokens)
        and tokens[position].is_punct(".")
        and tokens[position + 1].kind == IDENT
    ):
        position += 2
    return _past_type_arguments(tokens, closes, position)


# In a `<…>` type argument list none of these appear: they start a declaration
# or a statement, so the `<` was no type argument list.
_NOT_IN_TYPE_ARGUMENTS = (_STATEMENTS - {"new"}) | {
    "interface",
    "class",
    "function",
    "enum",
    "var",
    "let",
    "export",
    "declare",
}


def _past_type_arguments(tokens: list[Token], closes: list[int], position: int) -> int:
    """The index past a balanced ``<…>`` at ``position``; ``position`` when none is there.

    Brackets inside are passed over whole, an object type included. ``len(tokens)``
    when the list does not close before a ``;``, a stray closing bracket or a
    word no type holds, which also keeps the scan from running past the next
    declaration.
    """
    if position >= len(tokens) or not tokens[position].is_punct("<"):
        return position
    depth = 0
    while position < len(tokens):
        token = tokens[position]
        if token.is_punct("<"):
            depth += 1
        elif token.is_punct(">"):
            depth -= 1
            if not depth:
                return position + 1
        elif token.is_punct(*_OPENERS):
            position = closes[position]
        elif token.is_punct(";", *_CLOSERS) or (
            token.kind == IDENT and token.text in _NOT_IN_TYPE_ARGUMENTS
        ):
            break
        position += 1
    return len(tokens)


def _is_type_name(token: Token) -> bool:
    return token.kind == IDENT and token.text not in _NOT_TYPE_NAMES and _is_name(token)


def _arrow(
    tokens: list[Token], closes: list[int], source: str, keyword: int
) -> FunctionScope | None:
    """``const name = [async] (params)[: Ret] => …`` or ``const name = x => …``."""
    name = keyword + 1
    if name + 2 >= len(tokens) or not _is_name(tokens[name]):
        return None
    if not tokens[name + 1].is_punct("="):
        return None
    start = name + 2
    if tokens[start].is_ident("async") and start + 1 < len(tokens):
        start += 1
    first = tokens[start]
    if first.kind == IDENT and start + 1 < len(tokens) and tokens[start + 1].is_punct("=>"):
        found: tuple[Parameter, ...] = (Parameter(first.text, 0),)
        arrow = start + 1
        signature_end = first.end
    elif first.is_punct("("):
        close = closes[start]
        after = _arrow_after(tokens, closes, close + 1) if close < len(tokens) else None
        if after is None:
            return None
        found = parameters(tokens, start, close, "ts", closes)
        arrow = after
        signature_end = tokens[close].end
    else:
        return None
    if arrow + 1 < len(tokens) and tokens[arrow + 1].is_punct("{"):
        body = (arrow + 1, closes[arrow + 1])
    else:
        body = (arrow, expression_end(tokens, source, arrow, "ts", closes))
    return FunctionScope(tokens[name].text, tokens[name].offset, found, body, signature_end)


def _type_body(
    tokens: list[Token], closes: list[int], keyword: int, language: str
) -> tuple[int, str | None] | None:
    """The ``{`` of the body of the type whose header starts at ``keyword``, and its name.

    The name is the last part of a dotted one (``extension A.B``), None for an
    anonymous class. Generics, inheritance and ``where`` are passed over, an
    object type in the generics (``<T extends { id: string }>``) included. A
    word that starts a declaration or a statement ends the header: in
    TypeScript one of its own (``object`` is a type there), and only outside
    ``<…>``, which may hold ``const T`` or ``new () => T``.
    """
    if keyword and tokens[keyword - 1].is_punct(".", "?."):
        return None  # `type.class`: a property
    position = keyword + 1
    if position >= len(tokens):
        return None
    name: str | None = None
    following = tokens[position]
    anonymous = following.is_punct("{") or following.is_ident("extends", "implements")
    if following.kind == IDENT and following.text not in _NOT_TYPE_NAMES:
        name = following.text
        position += 1
        while (
            position + 1 < len(tokens)
            and tokens[position].is_punct(".")
            and tokens[position + 1].kind == IDENT
        ):
            name = tokens[position + 1].text
            position += 2
    elif language != "ts" or not anonymous:
        return None  # `class func`, `class var`: a modifier
    typescript = language == "ts"
    starts = _TS_DECLARATION_STARTS if typescript else _DECLARATION_STARTS
    angles = 0
    limit = min(len(tokens), position + _MAX_TYPE_HEADER)
    while position < limit:
        token = tokens[position]
        if token.is_punct("{"):
            if not angles:
                return position, name
            position = closes[position] + 1
            continue
        if token.is_punct("(", "["):
            position = closes[position] + 1
            continue
        if token.is_punct("<"):
            angles += 1
        elif token.is_punct(">"):
            angles = max(angles - 1, 0)
        elif (token.is_punct(";", "}", "=") and not angles) or (
            token.kind == IDENT
            and not (typescript and angles)
            and (token.text in starts or token.text in _STATEMENTS)
        ):
            return None
        position += 1
    return None


# --- shared parts -------------------------------------------------------------------
def _is_name(token: Token) -> bool:
    return token.kind == IDENT and token.text not in _STATEMENTS


def _after_angles(tokens: list[Token], index: int, *, allowed: Sequence[str] = ()) -> int:
    """The index past a balanced ``<…>`` at ``index``; ``index`` itself when none is there.

    ``len(tokens)`` when the list does not close within ``_MAX_HEAD`` tokens, or
    (with ``allowed``) holds punctuation a type parameter list never does.
    """
    if index >= len(tokens) or not tokens[index].is_punct("<"):
        return index
    depth = 0
    for position in range(index, min(len(tokens), index + _MAX_HEAD)):
        token = tokens[position]
        if token.is_punct("<"):
            depth += 1
        elif token.is_punct(">"):
            depth -= 1
            if depth == 0:
                return position + 1
        elif token.is_punct("{", "}", ";", "(") or (
            allowed and token.kind == PUNCT and token.text not in allowed
        ):
            break
    return len(tokens)


def _scope(
    tokens: list[Token],
    closes: list[int],
    source: str,
    language: str,
    name: int,
    opening: int,
    *,
    declaring: bool = False,
) -> FunctionScope | int | None:
    """The scope whose parameter list opens at ``opening``, when a body follows it.

    With no body, the signature end when this still declares a function:
    always after a keyword that only declares one (``declaring``: ``fun``,
    ``func``, ``init``, ``function``), else as ``signatures.declares`` says.
    None otherwise.
    """
    close = closes[opening]
    if close >= len(tokens):
        return None
    body = _body_after(tokens, closes, source, language, close + 1)
    if body is None:
        if declaring or declares(tokens, source, language, name, opening, close, closes):
            return tokens[close].end
        return None
    return FunctionScope(
        tokens[name].text,
        tokens[name].offset,
        parameters(tokens, opening, close, language, closes),
        body,
        tokens[close].end,
    )


def _body_after(
    tokens: list[Token], closes: list[int], source: str, language: str, position: int
) -> tuple[int, int] | None:
    """The body after a signature tail starting at ``position``, or None (no body here).

    A TypeScript tail is a type: a ``{`` inside ``<…>`` or right after ``:``,
    ``|``, ``&``, ``,``, ``=>`` or a type operator (``extends``, ``is`` …)
    opens an object type, ``=>`` is a function type's (``(): () => void {``),
    and a name with a parameter list after it is the next member, so the tail
    ends there with no body.
    """
    if position >= len(tokens):
        return None
    first = tokens[position]
    if first.kind not in (IDENT, PUNCT) or first.text not in _TAIL_STARTS[language]:
        return None
    typescript = language == "ts"
    starts = _TS_DECLARATION_STARTS if typescript else _DECLARATION_STARTS
    angles = 0
    held = 0
    while position < len(tokens) and held <= _MAX_TAIL:
        token = tokens[position]
        held += 1
        if token.is_punct("{"):
            if not (typescript and _object_type(tokens, position, angles)):
                return position, closes[position]
            position = closes[position] + 1
            continue
        if token.is_punct("=") and language == "kotlin":
            return position, expression_end(tokens, source, position, language, closes)
        if token.is_punct("(", "["):
            # A function or tuple type in the return type: `(Int) -> Unit`, `-> (A, B)`.
            position = closes[position] + 1
            continue
        if typescript:
            if token.is_punct("<"):
                angles += 1
            elif token.is_punct(">") and angles:
                angles -= 1
            elif _next_member(tokens, position):
                return None
            if token.is_punct("<", ">", "=>"):
                position += 1
                continue
        if token.is_punct(*_TAIL_STOPS) or (
            token.kind == IDENT and (token.text in starts or token.text in _STATEMENTS)
        ):
            return None
        position += 1
    return None


def _object_type(tokens: list[Token], position: int, angles: int) -> bool:
    """The ``{`` at ``position``, in a TypeScript type, opens an object type."""
    before = tokens[position - 1]
    return angles > 0 or before.is_punct(*_OBJECT_TYPE_AFTER) or before.is_ident(*_TYPE_OPERATORS)


def _next_member(tokens: list[Token], position: int) -> bool:
    """``tokens[position]`` names a member: a parameter list follows it, past type parameters."""
    token = tokens[position]
    if token.kind != IDENT or token.text in _TYPE_OPERATORS:
        return False
    opening = _after_angles(tokens, position + 1, allowed=_TYPE_PARAMETER_PUNCT)
    return opening < len(tokens) and tokens[opening].is_punct("(")


def _void_return(tokens: list[Token], source: str, name: int) -> bool:
    """The ``void`` before ``tokens[name]`` is a return type ending the line before."""
    void = name - 1
    return (
        void > 0
        and tokens[void].is_ident("void")
        and tokens[void - 1].is_punct(":", "=>", "|", "&")
        and "\n" in source[tokens[void].end : tokens[name].offset]
    )


def _arrow_after(tokens: list[Token], closes: list[int], position: int) -> int | None:
    """The index of the ``=>`` after an arrow's parameter list (past a return type)."""
    if position >= len(tokens) or not tokens[position].is_punct("=>", ":"):
        return None
    angles = 0
    held = 0
    while position < len(tokens) and held <= _MAX_TAIL:
        token = tokens[position]
        held += 1
        if token.is_punct("=>") and not angles:
            return position
        if token.is_punct("(", "[") or (
            token.is_punct("{") and _object_type(tokens, position, angles)
        ):
            position = closes[position] + 1
            continue
        if token.is_punct("<"):
            angles += 1
        elif token.is_punct(">") and angles:
            angles -= 1
        elif token.is_punct("{", "}", ";", "=", ")", "]"):
            return None
        position += 1
    return None


def expression_end(
    tokens: list[Token],
    source: str,
    marker: int,
    language: str,
    closes: Sequence[int] | None = None,
) -> int:
    """The first token past the expression body after ``tokens[marker]`` (``=`` or ``=>``).

    The body's first token belongs to it wherever it is, the next line too. It
    ends at the first later token outside brackets that starts a line, unless
    the line before cannot end there (an operator, a bracket left open, Kotlin's
    ``else`` or its brace-less ``if (…)``) or this line goes on with it
    (``.``, ``?.``, ``?:``, ``&&`` or ``||``; Kotlin's ``else``; in TypeScript
    also a ternary's ``?`` or ``:`` and any binary operator). It also ends at a
    bracket closing the enclosing block or call, at ``;`` (and, for an arrow,
    at a ``,`` separating it from what follows), or at the end.

    Nothing inside brackets can end the body, so each bracketed group is
    stepped over rather than read. Through ``closes`` (``calls.closers(tokens)``)
    when given, as this module's readers give it: a body that holds further
    declarations in its brackets is then not read again for each of them.
    Without it, a caller that asks about one expression (``places``, the
    evaluator's Kotlin ``when``) pays for that expression's own brackets only.
    """
    first = marker + 1
    # The `)` of a Kotlin `if (…)` outside brackets: its branch may start on the next line.
    condition = -1
    position = first
    while position < len(tokens):
        token = tokens[position]
        if token.kind == PUNCT and token.text in _CLOSERS:
            return position
        if position > first:
            if token.is_punct(";") or (language == "ts" and token.is_punct(",")):
                return position
            previous = position - 1
            if (
                "\n" in source[tokens[previous].end : token.offset]
                and previous != condition
                and not _open_ended(tokens, previous, language)
                and not _continues(tokens, position, language)
            ):
                return position
        if token.kind == PUNCT and token.text in _OPENERS:
            close = closes[position] if closes is not None else matching(tokens, position)
            if close >= len(tokens):
                return len(tokens)  # a bracket left open runs to the end
            if language == "kotlin" and tokens[position - 1].is_ident("if"):
                condition = close
            position = close + 1
            continue
        position += 1
    return len(tokens)


def _open_ended(tokens: list[Token], index: int, language: str) -> bool:
    """``tokens[index]``, the last token of a line, cannot end the expression there."""
    token = tokens[index]
    if token.kind == IDENT:
        return language == "kotlin" and token.text == "else"
    if token.kind != PUNCT:
        return False
    if (
        token.text in ("+", "-")
        and tokens[index - 1].is_punct(token.text)
        and tokens[index - 1].end == token.offset
    ):
        return False  # a postfix `++` or `--`
    if token.text == "?":
        return language == "ts"  # in Kotlin it ends a nullable type: `as String?`
    return token.text in _OPEN_ENDS


def _continues(tokens: list[Token], position: int, language: str) -> bool:
    """A line that starts with ``tokens[position]`` goes on with the expression before it."""
    token = tokens[position]
    if language == "ts":
        return token.is_punct(*_TS_CONTINUATIONS)
    if token.is_punct(*_CONTINUATIONS) or token.is_ident(*_KOTLIN_CONTINUATIONS):
        return True
    # Kotlin's elvis `?:` lexes as `?` then `:`.
    return token.is_punct("?") and position + 1 < len(tokens) and tokens[position + 1].is_punct(":")
