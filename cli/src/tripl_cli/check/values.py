"""An argument's tokens -> what the checker can say about its value.

Four outcomes, and the distinction between them is the whole point:

``Value.text``
    A string known at scan time. ``interpolated`` is set when it carries
    ``${name}`` plan-variable tokens (``"promo_sheet_\\(id)_shown"`` becomes
    ``promo_sheet_${id}_shown``), which the validator matches against the plan's
    naming rules rather than as a literal. Besides literals and constants, a
    concatenation (``"a_" + id``, or constants alone: ``Events.A + Params.B``)
    and a printf-style format (``"promo_%s_shown".format(id)``) read as text,
    each part the scan cannot read becoming a ``${name}`` token named after it.
``Value.choices``
    One of several known strings (a function whose every ``return`` is a
    literal, an ``if``/``when``/ternary whose every branch is a constant, or
    a local set to one). The caller checks each.
``Value.entries``
    A dictionary literal — properties, or an object argument whose keys a
    preset reads as labels; Android's ``Bundle().apply { putString(k, v) }``
    written inline is one too.
``dynamic``
    Not knowable without running the code. Sent to the validator as ``null``,
    which is never an error; ``--strict`` reports it.

``ABSENT`` is ``nil``/``null``: the argument was passed as "nothing", so the
field is left out rather than sent as dynamic.

For ``tripl check --list`` a dynamic value also says why the scan could not
read it: ``reason`` is one of ``model.REASON_CODES`` and ``expression`` the
argument as written (at most ``EXPRESSION_MAX`` characters). Only an evaluator
built with ``explain`` works them out, which plain ``tripl check`` never does,
and neither changes what is sent or decided. The evaluator's reasons, decided
where it gives up:

``variable`` / ``unknown_constant``
    A plain path (``event``, ``action.event``, ``Events.Name.Foo``) the index
    has no constant for. A lower-case first letter (after any leading ``_`` or
    ``$``) reads as a variable, a parameter or a property, which the scan does
    not follow; an upper-case one, a ``k`` before an upper-case one (Apple's
    and Google's ``kFIREventLogin``), or Swift's ``.home``, as a constant it
    could not resolve: its file is not scanned, or the case has no string raw
    value. ``path`` keeps the segments:
    ``inventory`` refines a value into a parameter or a local from them,
    without re-reading the text. ``.home`` has none: it names a case of the
    type the call expects, never a name in scope. A local set once to a
    constant is read before any of this (``Evaluator._local``).
``ambiguous_constant``
    Such a constant that resolves in several objects to different values,
    which the field the value goes to does not tell apart
    (``SymbolIndex.ambiguous``).
``format``
    A printf-style format the scan does not fill in: ``x.format(…)`` /
    ``x.formatted(…)`` whatever ``x`` is, Swift ``String(format: …)`` and
    Objective-C ``stringWithFormat:`` (a message only there: in Swift and
    TypeScript ``[…]`` is a literal). Told from the shape before anything else,
    since a format is a call too. A member call binds tighter than an operator,
    so ``"a" + y.format(z)`` is a concatenation and ``("a" + y).format(z)`` a
    format. A comparison, a range or a word operator does the same:
    ``a < y.format(z)``, ``a..y.format(z)`` and ``a to y.format(z)`` are an
    ``expression``, while type arguments (``pick<String>(a).format(z)``) are no
    comparison. A word operator is any name between two operands in Kotlin (an
    infix call), and ``instanceof``, TypeScript's ``in`` and Swift's ``is``
    elsewhere. When the format string reads as text, ``expression`` is ``format
    of <template>``. ``Evaluator._format`` says which formats are filled in.
``function``
    A call of a function the index cannot read: it reads one whose every
    ``return`` is a string literal and that is declared to return a string
    (``String``, ``string``, ``NSString``), or a Kotlin ``fun f() = "…"``. A
    map builder the dictionary reader rejected (Kotlin ``mapOf(a to b, )``) is
    an ``expression`` instead.
``conditional``
    A choice between values the scan does not read: an ``if`` / ``when`` /
    ternary with a branch that is no constant (``Evaluator._conditional``
    reads the others), a ``switch``, Kotlin's ``?:``, Swift's ``??``.
``concatenation``
    A top-level ``+`` with no string literal among its operands and one the
    scan cannot read as text, or one whose operand is several values or a
    dictionary.
``expression``
    Anything else: a chained call, an operator, a subscript, a lambda, an
    unparsable number.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tripl_cli.check.calls import matching, split_top_level
from tripl_cli.check.lexer import IDENT, NUMBER, PUNCT, STRING, Interp, Token, tokenize
from tripl_cli.check.model import (
    REASON_AMBIGUOUS_CONSTANT,
    REASON_CONCATENATION,
    REASON_CONDITIONAL,
    REASON_EXPRESSION,
    REASON_FORMAT,
    REASON_FUNCTION,
    REASON_UNKNOWN_CONSTANT,
    REASON_VARIABLE,
)
from tripl_cli.check.scopes import expression_end
from tripl_cli.check.symbols import (
    CASE_NAME_ACCESSORS,
    MAX_CHOICES,
    RAW_ACCESSORS,
    Entry,
    SymbolIndex,
    number_text,
    template_text,
    variable_name,
)

if TYPE_CHECKING:
    from tripl_cli.check.places import FilePlaces

# The longest ``Value.expression``; a longer one is cut and ends in "...".
EXPRESSION_MAX = 60

_NULLS = frozenset({"nil", "null", "undefined", "NULL", "Nil", "None"})
_TRUE = frozenset({"true", "YES", "True"})
_FALSE = frozenset({"false", "NO", "False"})
_MAP_BUILDERS = frozenset(
    {"mapOf", "hashMapOf", "mutableMapOf", "linkedMapOf", "bundleOf", "of", "ofEntries"}
)
# Prefixes that do not change a value: `try`, `await`, casts, `new`.
_TRANSPARENT_PREFIXES = frozenset({"try", "await", "new"})
# What joins the segments of a plain path: `a.b`, `a?.b`, `A::b`, `a!!.b`.
_PATH_JOINS = (".", "?.", "::", "?", "!")
_FORMAT_METHODS = frozenset({"format", "formatted"})
# The format each language's printf is written as, which the evaluator fills in.
_FORMAT_FORMS = {"kotlin": "method", "java": "method", "swift": "swift", "objc": "objc"}
_OBJC_FORMATS = frozenset({"stringWithFormat", "localizedStringWithFormat"})
_CHOICE_KEYWORDS = frozenset({"if", "when", "switch"})
# The languages whose `if` is an expression, and the one whose `when` is.
_IF_EXPRESSIONS = frozenset({"kotlin", "swift"})
# How many locals one value may be read through: `val a = b` with `val b = "x"` is two.
_MAX_LOCALS = 8
# The Bundle methods an inline `Bundle().apply { … }` may call: `putString`, `putInt` …
_BUNDLE_PUT = re.compile(r"put[A-Z][A-Za-z]*")
# One printf conversion: `%s`, `%1$s`, `%ld`, `%-5.2f`, `%%`. Its parts are told
# apart so that `_conversions` can refuse what it does not fill in.
_CONVERSION = re.compile(
    r"%(?:(?P<index>[1-9][0-9]*)\$)?(?P<flags>[-#+ 0,(<']*)(?P<width>[0-9]+|\*)?"
    r"(?:\.(?P<precision>[0-9]+|\*))?(?P<length>hh|h|ll|l|q|z|j|t|L)?(?P<conversion>.?)",
    re.DOTALL,
)
# The conversions a format is filled in for, per language, and the ones of them
# that print an integer. Any other (`%f`, `%x`, `%S`, Java's `%n` and dates) is
# printed in a way the scan does not reproduce, so the format stays unread.
_FILLED_CONVERSIONS = {
    "kotlin": frozenset("sd"),
    "java": frozenset("sd"),
    "swift": frozenset("s@diu"),
    "objc": frozenset("s@diu"),
}
_INTEGER_CONVERSIONS = frozenset("diu")
# Length modifiers (`%ld`, `%lld`, `%lu`) exist in C's printf only.
_LENGTH_LANGUAGES = frozenset({"swift", "objc"})
_INTEGER = re.compile(r"-?[0-9]+")
# A number as a number literal is written (hexadecimal, a fraction, an exponent),
# and an integer as printf prints it: in decimal, with no leading zero.
_NUMBER = re.compile(
    r"-?(?:0[xXbBoO][0-9a-fA-F]+|[0-9]+(?:\.[0-9]*)?(?:[eE][-+]?[0-9]+)?|\.[0-9]+(?:[eE][-+]?[0-9]+)?)"
)
_PRINTED_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)")
# Binary operators, assignment and a lambda's arrow: each binds looser than a
# member call. A comparison `<` / `>` does too, but the same tokens also open
# and close a type-argument list (`listOf<String>()`), so `_has_operator`
# tells the two apart before counting one.
_OPERATORS = frozenset(
    {"+", "-", "*", "/", "%", "&", "|", "^", "==", "!=", "===", "!==", "<=", ">="}
    | {"&&", "||", "??", "=", "->", "=>"}
    # Ranges: Kotlin's `..<`, Swift's `...` and `..<`. Kotlin's `..` is two
    # `.` tokens, which `_has_operator` checks for apart.
    | {"...", "..<"}
)
# Operators written as words, per language. In Kotlin any name that stands
# right after an operand is an infix call (`a to b`, `a zip b`, `a !in b`),
# so there the words are the keywords that also follow an operand but join
# no operands: the rest of an `if`, a `try`. Elsewhere only these words are
# operators: `instanceof` and TypeScript's `in`, Swift's `is`. Like the
# operators above, each binds looser than a member call.
_INFIX_WORDS: dict[str, frozenset[str]] = {
    "ts": frozenset({"instanceof", "in"}),
    "java": frozenset({"instanceof"}),
    "swift": frozenset({"is"}),
}
_KOTLIN_NOT_INFIX = frozenset({"else", "catch", "finally"})
# What a type-argument list may hold between its angle brackets besides
# names, literals (TypeScript's `pick<"a">`) and bracketed groups (a function
# type's parameters, a TypeScript object type or tuple): qualified names,
# nested lists, `,`, Kotlin's `?` and `*`, Java's `?` wildcard, TypeScript's
# `&` / `|`, and a function type's arrow.
_TYPE_ARGUMENT_PUNCT = (",", ".", "?", "*", "&", "|", "->", "=>")
# What may follow a type-argument list: a call, a member, a reference,
# Kotlin's trailing lambda (`run<String> { … }`), TypeScript's indexed
# access (`Foo<T>["k"]`).
_AFTER_TYPE_ARGUMENTS = ("(", ".", "?.", "::", "{", "[")
_OPENING = ("(", "[", "{")
_CLOSING = (")", "]", "}")


@dataclass(frozen=True, slots=True)
class Value:
    kind: str  # "text" | "choices" | "entries" | "dynamic" | "absent"
    text: str | None = None
    interpolated: bool = False
    choices: tuple[str, ...] = ()
    entries: tuple[tuple[str, Value], ...] = ()
    # For a dynamic value: the name a template would use for it (`${name}`).
    hint: str = "value"
    # For a dynamic value: why it was not read (a `model.REASON_*` code), the
    # argument as written, and the path it named when it was a plain reference.
    # Outside equality: they describe a value, they do not change it.
    reason: str = field(default="", compare=False)
    expression: str = field(default="", compare=False)
    path: tuple[str, ...] = field(default=(), compare=False)
    # The named constants a text value was read from, whole or in part (an
    # operand of a concatenation, an inlined interpolation); for a dictionary,
    # the constants each key was read from. Outside equality too: the lint
    # (``lint.py``) reads them to find two constants holding one value.
    constants: tuple[Entry, ...] = field(default=(), compare=False)
    key_constants: tuple[tuple[str, tuple[Entry, ...]], ...] = field(default=(), compare=False)

    @property
    def is_known(self) -> bool:
        return self.kind in ("text", "choices")


DYNAMIC = Value("dynamic")
ABSENT = Value("absent")

# One entry of a dictionary literal: its key, its value, and the constants the
# key was read from.
_Pair = tuple[str, Value, tuple[Entry, ...]]


def text(value: str, *, interpolated: bool = False, constants: tuple[Entry, ...] = ()) -> Value:
    return Value("text", text=value, interpolated=interpolated, constants=constants)


def dynamic(
    hint: str = "value", *, reason: str = "", expression: str = "", path: tuple[str, ...] = ()
) -> Value:
    return Value("dynamic", hint=hint or "value", reason=reason, expression=expression, path=path)


def clip(source: str, limit: int) -> str:
    """``source`` on one line, whitespace runs collapsed, at most ``limit`` characters.

    A longer one is cut to exactly ``limit``, the last three being ``...``.
    """
    flat = " ".join(source.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 3] + "..."


def expression_text(span: Sequence[Token]) -> str:
    """``span`` as written: its token texts, with one space wherever the source had a gap.

    Rebuilt from the tokens rather than sliced out of the file: an
    interpolation is evaluated from its own re-tokenized text, whose offsets
    are not the file's. Every token of one span comes from one ``tokenize``
    call, so comparing their offsets is sound.
    """
    pieces: list[str] = []
    for position, token in enumerate(span):
        if position and span[position - 1].end < token.offset:
            pieces.append(" ")
        pieces.append(token.text)
    return clip("".join(pieces), EXPRESSION_MAX)


def pure_path(tokens: Sequence[Token]) -> tuple[str, ...] | None:
    """The segments of an argument that is a plain path (``a.b``, ``A::b``, ``a?.b!!``)."""
    return _segments(_strip(list(tokens)))


class Evaluator:
    """Evaluates argument token spans against one run's ``SymbolIndex``.

    ``explain`` makes each dynamic value say why it was not read, which only
    ``tripl check --list`` shows. Plain ``tripl check`` leaves it off, and a
    value it gives up on is then its hint alone: no time spent on a reason
    nobody reads, and no stack a deeply nested argument would run out of.

    ``places`` is the file the spans come from, for the values that depend on
    where they are written (a type's own member named without its type). An
    evaluator without one reads every such value as dynamic.
    """

    def __init__(
        self,
        symbols: SymbolIndex,
        language: str,
        *,
        explain: bool = False,
        places: FilePlaces | None = None,
    ) -> None:
        self.symbols = symbols
        self.language = language
        self.explain = explain
        self.places = places
        # Set while a format is being filled in: a format inside it is not.
        self._formats = 0
        # Set while an interpolation is read: its tokens' offsets are not the
        # file's, so what is looked up by where it is written is looked up
        # where its string is (this file offset) instead.
        self._anchor: int | None = None
        # How many locals the value being read goes through (`val a = b`).
        self._locals = 0

    def evaluate(self, tokens: Sequence[Token], *, field: str | None = None) -> Value:
        span = _strip(list(tokens))
        if not span:
            return ABSENT
        if len(span) == 1:
            single = self._single(span[0], field)
            if single is not None:
                return single
        if span[0].is_punct("-") and len(span) == 2 and span[1].kind == NUMBER:
            number = number_text(span[1])
            if number is not None:
                return text(f"-{number}")
            if not self.explain:
                return DYNAMIC
            return self._unread(span, field, "value")
        pairs = self._dictionary(span)
        if pairs is not None:
            # Wrapped here, not in a helper `_dictionary` calls: no frame is
            # added to the recursion a nested dictionary goes through.
            return _entries(pairs)
        chosen = self._conditional(span, field)
        if chosen is not None:
            return chosen
        concatenated = self._concatenation(span, field)
        if concatenated is not None:
            return concatenated
        formatted = self._format(span, field)
        if formatted is not None:
            return formatted
        reference = self._reference(span, field)
        if reference is not None:
            return reference
        if not self.explain:
            return dynamic(_hint_of(span))
        return self._unread(span, field, _hint_of(span))

    # --- single tokens -------------------------------------------------------
    def _single(self, token: Token, field: str | None) -> Value | None:
        if token.kind == STRING:
            return self._string(token, field)
        if token.kind == NUMBER:
            number = number_text(token)
            if number is not None:
                return text(number)
            if not self.explain:
                return DYNAMIC
            return self._unread([token], field, "value")
        if token.kind == IDENT:
            if token.text in _NULLS:
                return ABSENT
            if token.text in _TRUE:
                return text("true")
            if token.text in _FALSE:
                return text("false")
            if self.places is not None and self.places.may_be_local(token.text):
                local = self._local(token, field)
                if local is not None:
                    return local
            # A member of the type around the name hides a file-scope constant
            # of that name, as it does in Kotlin and Java.
            owner = self._member_path(token)
            if owner is not None:
                resolved = self.symbols.member(owner, hint=field)
            else:
                resolved = self.symbols.lookup([token.text], hint=field, language=self.language)
            if resolved is not None:
                return text(resolved[0], constants=resolved[1])
            if not self.explain:
                return dynamic(token.text)
            reason = _path_reason(token.text, shorthand=False)
            if reason == REASON_UNKNOWN_CONSTANT and self._ambiguous_name(token, field):
                reason = REASON_AMBIGUOUS_CONSTANT
            return dynamic(
                token.text,
                reason=reason,
                expression=expression_text([token]),
                path=(token.text,),
            )
        return None

    def _string(self, token: Token, field: str | None) -> Value:
        parts: list[str | Interp] = []
        constants: list[Entry] = []
        for part in token.parts:
            if isinstance(part, Interp):
                inlined = self._inline(part.expr, token.offset, field)
                if inlined is None or inlined.text is None:
                    parts.append(part)
                else:
                    parts.append(inlined.text)
                    constants.extend(inlined.constants)
            else:
                parts.append(part)
        interpolated = any(isinstance(part, Interp) for part in parts)
        return text(template_text(parts), interpolated=interpolated, constants=tuple(constants))

    def _inline(self, expr: str, offset: int, field: str | None) -> Value | None:
        """An interpolation that is itself a constant (an enum raw value, a literal) is inlined.

        ``offset`` is its string's: what depends on where the interpolation is
        written (a local, a type's member) is looked up there, its own tokens'
        offsets being the expression's.
        """
        try:
            tokens = tokenize(expr, self.language)
        except ValueError:
            return None
        anchor = self._anchor
        if anchor is None:
            self._anchor = offset
        try:
            value = self.evaluate(tokens, field=field)
        finally:
            self._anchor = anchor
        return value if value.kind == "text" and not value.interpolated else None

    def _offset(self, token: Token) -> int:
        """Where ``token`` is written in the file: its string's offset inside an interpolation."""
        return token.offset if self._anchor is None else self._anchor

    def _local(self, token: Token, field: str | None) -> Value | None:
        """A local set once to a constant, named further down its function.

        ``places.FilePlaces.local_initializer`` says which locals qualify; the
        initializer must then read as one or more constants: text with no
        ``${name}`` hole, or choices with none (a conditional over constants).
        Read without explaining, at most ``_MAX_LOCALS`` locals deep (``val a
        = b`` with ``val b = "x"``). A local takes precedence over a constant
        of the same name, as it does in the code.
        """
        places = self.places
        if places is None or self._locals >= _MAX_LOCALS:
            return None
        reader = self._reader()
        anchor = reader._anchor
        try:
            initializer = places.local_initializer(self._offset(token), token.text)
            if initializer is None:
                return None
            # The initializer's tokens are the file's own, wherever the name was.
            reader._anchor = None
            reader._locals += 1
            try:
                value = reader.evaluate(initializer, field=field)
            finally:
                reader._locals -= 1
                reader._anchor = anchor
        except RecursionError:
            return None
        return value if _constant_options(value) is not None else None

    def _member_path(self, token: Token) -> tuple[str, ...] | None:
        """The path of the Kotlin or Java constant ``token`` names without its type, if any.

        ``HOME`` inside ``class Screen`` (or a type nested in it) is
        ``Screen.HOME``, a companion's or a static one included, and it wins
        over a file-scope constant of the same name, as the member does in the
        code. Looked for only for a name that reads as a constant (``HOME``, ``kHome``),
        and from the innermost type out: the first type that declares it
        decides, an ambiguous one included. A parameter of a function around
        the call, or a local declared before it in the innermost one, hides
        the constant.
        """
        places = self.places
        name = token.text
        if (
            places is None
            or self.language not in ("kotlin", "java")
            or _path_reason(name, shorthand=False) != REASON_UNKNOWN_CONSTANT
            or not self.symbols.names_member(name)
        ):
            return None
        offset = self._offset(token)
        for owner in places.types_around(offset):
            path = (*owner, name)
            if not self.symbols.declares(path):
                continue
            if places.shadowed(offset, name):
                return None
            return path
        return None

    def _ambiguous_name(self, token: Token, field: str | None) -> bool:
        """Whether the bare name ``token`` names constants of different values (``--list`` only).

        The members of the type around it that ``_member_path`` finds, else
        the file-scope constants of that name.
        """
        owner = self._member_path(token)
        if owner is not None:
            return self.symbols.ambiguous_member(owner, hint=field)
        return self.symbols.ambiguous([token.text], hint=field, language=self.language)

    def _own_type(
        self, path: list[str], offset: int, field: str | None
    ) -> tuple[str, tuple[Entry, ...]] | None:
        """Swift's ``Self.x``: ``x`` of the type whose body holds ``offset``, as ``lookup``."""
        places = self.places
        if places is None or self.language != "swift":
            return None
        owners = places.types_around(offset if self._anchor is None else self._anchor)
        if not owners:
            return None
        return self.symbols.lookup([*owners[0], *path[1:]], hint=field, language=self.language)

    # --- compound forms ------------------------------------------------------
    def _conditional(self, span: list[Token], field: str | None) -> Value | None:
        """``if`` / ``when`` / a ternary whose every branch is a constant: those constants.

        Read: Kotlin ``if (…) a else b`` (``else if`` chains, ``{ … }``
        branches) and ``when``, Swift's ``if … { a } else { b }``, and the
        ternary ``c ? a : b`` in Swift, Java, TypeScript and Objective-C. Each
        branch must read as text with no ``${name}`` hole, or as such choices;
        the result is their distinct values in order, one text when they
        agree, else choices each checked as a function's returns are. Any
        other branch, an ``if`` with no ``else``, a ``switch``, or more than
        ``symbols.MAX_CHOICES`` values make the whole value dynamic, with the
        ``conditional`` reason. None for what is no such form, Kotlin's ``?:``
        and Swift's ``??`` included: they read on as before.
        """
        source = self.places.source if self.places is not None and self._anchor is None else None
        branches = _branches(span, self.language, source)
        if branches is None:
            return None
        options: list[str] = []
        reader = self._reader()
        try:
            for branch in branches:
                if _statements(branch, self.language):
                    found = None
                else:
                    found = _constant_options(reader.evaluate(branch, field=field))
                if found is None:
                    options = []
                    break
                options.extend(option for option in found if option not in options)
        except RecursionError:
            options = []
        if options and len(options) <= MAX_CHOICES:
            if len(options) == 1:
                return text(options[0])
            return Value("choices", choices=tuple(options))
        if not self.explain:
            return dynamic(_hint_of(span))
        return dynamic(_hint_of(span), reason=REASON_CONDITIONAL, expression=expression_text(span))

    def _concatenation(self, span: list[Token], field: str | None) -> Value | None:
        """``"a" + x + "b"``, C's adjacent literals ``@"a" @"b"``, and constants: ``A + B``.

        With a string literal among the operands, an operand the scan cannot
        read is a ``${name}`` token. Without one, ``+`` is a concatenation only
        when every operand reads as text (a constant, a format, a function's
        one string): ``a + b`` over values not read may be a sum, and so may
        an operand that is a number or a boolean. Objective-C has no ``+``
        for strings at all.

        An operand that reads as a number (a number literal, or a constant or
        a local set to one) may be added rather than joined, so it is never
        joined where that is possible: anywhere when no string literal is an
        operand (``a + b`` with ``val a = 1``), and in front of the first
        literal when it is not alone there (``a + b + "x"`` is ``3x`` in Java
        and TypeScript). Either leaves the value dynamic.
        """
        operands: list[list[Token]] = [[]]
        depth = 0
        for token in span:
            if token.kind == PUNCT:
                if token.text in "([{":
                    depth += 1
                elif token.text in ")]}":
                    depth -= 1
                elif token.text == "+" and depth == 0:
                    operands.append([])
                    continue
            operands[-1].append(token)
        if len(operands) == 1:
            if len(span) > 1 and all(token.kind == STRING for token in span):
                operands = [[token] for token in span]
            else:
                return None
        literal = any(len(op) == 1 and op[0].kind == STRING for op in operands)
        if not literal and (
            self.language == "objc" or not all(_may_be_text(operand) for operand in operands)
        ):
            return None
        pieces: list[str] = []
        constants: list[Entry] = []
        interpolated = False
        numbers = 0  # operands read as a number, before the first string literal
        seen_literal = False
        for operand in operands:
            seen_literal = seen_literal or (len(operand) == 1 and operand[0].kind == STRING)
            value = self.evaluate(operand, field=field)
            if value.kind == "text" and value.text is not None:
                if not seen_literal and not value.interpolated and _NUMBER.fullmatch(value.text):
                    numbers += 1
                    if not literal or numbers > 1:
                        return None  # a sum, or may be one
                pieces.append(value.text)
                constants.extend(value.constants)
                interpolated = interpolated or value.interpolated
            elif not literal:
                return None
            elif value.kind == "dynamic":
                pieces.append(f"${{{value.hint}}}")
                interpolated = True
            else:
                # An operand that is several values, a dictionary or nothing.
                if not self.explain:
                    return dynamic(_hint_of(span))
                return self._unread(span, field, _hint_of(span), otherwise=REASON_CONCATENATION)
        return text("".join(pieces), interpolated=interpolated, constants=tuple(constants))

    def _reference(self, span: list[Token], field: str | None) -> Value | None:
        """``.home``, ``A.B.home.rawValue``, ``Category.HOME.value``, ``name(for: x)``.

        A zero-argument accessor call on a path (Java ``Category.HOME.name()`` /
        ``Category.HOME.value()``) is read like the property it wraps.
        """
        shorthand = span[0].is_punct(".")
        body = span[1:] if shorthand else span
        path: list[str] = []
        called: str | None = None
        position = 0
        # Two names with no `.` / `?.` / `::` between them (`a to b`, Kotlin's
        # `a !in b`), or two touching dots (Kotlin's range `a..b`), are an
        # operator, and an operator binds looser than a path or a call: what
        # it makes is no reference, so `a to f()` calls no `f`.
        joined = True
        while position < len(body):
            token = body[position]
            if token.kind == IDENT:
                if not joined:
                    return None
                path.append(token.text)
                joined = False
                position += 1
            elif token.is_punct(".", "?.", "::"):
                if position > 0 and _range_dots(body, position - 1):
                    return None
                joined = True
                position += 1
            elif token.is_punct("?", "!"):
                position += 1
            elif token.is_punct("(") and path:
                close = matching(list(body), position)
                called = path[-1]
                position = close + 1
                if position < len(body):
                    # A call in the MIDDLE of a chain (`x.name().lowercased()`) is not a lookup.
                    return None
            else:
                return None
        if not path:
            return None
        reference = body
        if called is not None and len(path) > 1 and _accessor_call(body, self.language):
            called = None
            reference = body[:-2]  # `Category.HOME.name()` names the path `Category.HOME.name`
        if called is not None:
            values = self.symbols.function_values(called, language=self.language)
            if values is None:
                if not self.explain:
                    return dynamic(called)
                # A map builder here is a literal `_dictionary` could not read
                # (a trailing comma, a spread), not a function to look up.
                otherwise = REASON_EXPRESSION if called in _MAP_BUILDERS else REASON_FUNCTION
                return self._unread(span, field, called, otherwise=otherwise)
            if len(values) == 1:
                only = values[0]
                return text(only, interpolated="${" in only)
            return Value("choices", choices=values)
        resolved = self.symbols.lookup(
            path, hint=field, shorthand=shorthand, language=self.language
        )
        if resolved is None and not shorthand and len(path) > 1 and path[0] == "Self":
            resolved = self._own_type(path, body[0].offset, field)
        if resolved is not None:
            return text(resolved[0], constants=resolved[1])
        hint = variable_name(".".join(path))
        if not self.explain:
            return dynamic(hint)
        segments = _segments(reference)
        if segments is None:
            # Not a plain path after all: `!flag`, `::name`, an infix `a to b`.
            return self._unread(span, field, hint)
        reason = _path_reason(segments[0], shorthand=shorthand)
        if reason == REASON_UNKNOWN_CONSTANT and self.symbols.ambiguous(
            path, hint=field, shorthand=shorthand, language=self.language
        ):
            reason = REASON_AMBIGUOUS_CONSTANT
        return dynamic(
            hint,
            reason=reason,
            expression=expression_text(span),
            path=() if shorthand else segments,
        )

    def _unread(
        self,
        span: list[Token],
        field: str | None,
        hint: str,
        *,
        otherwise: str = REASON_EXPRESSION,
    ) -> Value:
        """A dynamic value for ``span``, with the reason its shape gives (``explain`` only).

        The checks run in a fixed order: a printf-style format first (it is a
        call too, and would otherwise read as one), then a choice, then a
        concatenation; ``otherwise`` is what the caller found ``span`` to be.
        """
        expression = expression_text(span)
        format_string = _format_string(span, self.language)
        if format_string is not None:
            template = self._template(format_string, field)
            if template is not None:
                expression = clip(f"format of {template}", EXPRESSION_MAX)
            return dynamic(hint, reason=REASON_FORMAT, expression=expression)
        if _is_choice(span):
            reason = REASON_CONDITIONAL
        elif any(token.is_punct("+") for index, token in _top_level(span) if index > 0):
            reason = REASON_CONCATENATION
        else:
            reason = otherwise
        return dynamic(hint, reason=reason, expression=expression)

    def _template(self, format_string: list[Token], field: str | None) -> str | None:
        """What a format's format string reads as, when that is text.

        Only ``expression`` shows it. The reader explains nothing and fills in
        no format, so a format inside the format string
        (``x.format(a).format(b)``, ``String.format(…, String.format(…))``) is
        not read in turn: its own template would change its own
        ``expression``, never what the outer one reads as. So reading never
        nests, however long the chain, and a format string nested past the
        stack is not read at all.
        """
        if not format_string:
            return None
        reader = self._reader()
        reader._formats = 1
        try:
            template = reader.evaluate(format_string, field=field)
        except RecursionError:
            return None
        return template.text if template.kind == "text" else None

    def _reader(self) -> Evaluator:
        """An evaluator that explains nothing, for the same file: ``self`` when it does not."""
        if not self.explain:
            return self
        reader = Evaluator(self.symbols, self.language, places=self.places)
        reader._anchor = self._anchor
        reader._locals = self._locals
        return reader

    def _format(self, span: list[Token], field: str | None) -> Value | None:
        """A printf-style format filled in, or None when ``span`` is none or is not filled in.

        Filled in: Kotlin and Java ``template.format(…)``, ``.formatted(…)``
        and ``String.format([Locale, ]template, …)``, Swift ``String(format:
        template, …)`` and Objective-C ``stringWithFormat:``, whose template
        reads as text. Each ``%s`` (``%@``, ``%d``; ``%1$s`` by position) takes
        its argument: its text when that reads as text, else a ``${name}``
        token named after it, as in a concatenation; ``%%`` is ``%``. Not
        filled in: any other conversion, a flag, a width or a precision, an
        argument count the conversions do not use up exactly, a spread
        (``*args``, Swift's ``arguments:``), an argument that is several
        values or a dictionary, and a format inside another one (in its
        template or an argument), which keeps reading from nesting. The
        template and the arguments are read without explaining: a value
        inside a format never shows its own reason.
        """
        found = _format_call(span, self.language)
        if found is None:
            return None
        reader = self._reader()
        if reader._formats:
            return None
        template_tokens, arguments = found
        # Read here, not in a helper: each frame below `evaluate` is a level
        # of nesting less before the stack runs out.
        values: list[Value] = []
        reader._formats += 1
        try:
            template = reader.evaluate(template_tokens, field=field)
            conversions = _conversions(template, self.language, len(arguments))
            if conversions is None:
                return None
            for argument in arguments:
                values.append(reader.evaluate(argument, field=field))
        except RecursionError:
            return None
        finally:
            reader._formats -= 1
        return _filled(template, conversions, values)

    def _dictionary(self, span: list[Token]) -> list[_Pair] | None:
        """A dictionary literal's ``(key, value, key constants)``; None when ``span`` is not one."""
        first = span[0]
        if first.is_punct("@") and len(span) > 1 and span[1].is_punct("{"):
            return self._pairs(span[1:], ":")  # ObjC @{ @"k": v }
        if first.is_punct("[") and self.language == "swift":
            if len(span) == 3 and span[1].is_punct(":"):
                return []  # [:]
            return self._pairs(span, ":")
        if first.is_punct("{") and self.language == "ts":
            return self._pairs(span, ":", bare_keys=True)
        if first.kind == IDENT and self.language == "kotlin" and span[-1].is_punct("}"):
            return self._bundle(span)
        if first.kind == IDENT and self.language in ("kotlin", "java") and span[-1].is_punct(")"):
            builder = next(
                (
                    index
                    for index, token in enumerate(span)
                    if token.kind == IDENT
                    and token.text in _MAP_BUILDERS
                    and index + 1 < len(span)
                    and span[index + 1].is_punct("(")
                ),
                None,
            )
            if builder is None or matching(span, builder + 1) != len(span) - 1:
                return None
            inner = span[builder + 2 : -1]
            if self.language == "kotlin":
                return self._kotlin_pairs(inner)
            return self._java_pairs(inner)
        return None

    def _bundle(self, span: list[Token]) -> list[_Pair] | None:
        """Android's ``Bundle().apply { putString(K, v) … }``, written inline, as key/value pairs.

        The receiver is ``Bundle()`` (``android.os.Bundle()``, or with a
        capacity), and the block holds nothing but ``putX(key, value)`` calls
        (``this.putX`` too), one per statement: anything else in it, a
        ``Bundle(other)`` or a ``putAll`` may add keys nobody can see, and
        then it is no dictionary. A key the scan cannot read is left out, as
        in ``mapOf``; a key put twice keeps its last value, as at runtime.
        """
        opening = _opening(span, len(span) - 1)
        if opening < 3 or not span[opening - 1].is_ident("apply"):
            return None
        if not span[opening - 2].is_punct(".") or not span[opening - 3].is_punct(")"):
            return None
        receiver = span[: opening - 2]
        arguments = _opening(receiver, len(receiver) - 1)
        inside = receiver[arguments + 1 : -1]
        if (
            arguments < 1
            or not receiver[arguments - 1].is_ident("Bundle")
            or _segments(receiver[:arguments]) is None
            or not (not inside or (len(inside) == 1 and inside[0].kind == NUMBER))
        ):
            return None
        body = span[opening + 1 : -1]
        pairs: list[_Pair] = []
        position = 0
        while position < len(body):
            if body[position].is_punct(";"):
                position += 1
                continue
            if body[position].is_ident("this") and position + 1 < len(body):
                if not body[position + 1].is_punct("."):
                    return None
                position += 2
            method = body[position] if position < len(body) else None
            if (
                method is None
                or method.kind != IDENT
                or not _BUNDLE_PUT.fullmatch(method.text)
                or position + 1 >= len(body)
                or not body[position + 1].is_punct("(")
            ):
                return None
            close = matching(body, position + 1)
            if close >= len(body):
                return None
            parts = split_top_level(body[position + 2 : close])
            if len(parts) != 2 or not parts[0] or not parts[1]:
                return None
            key = self._key(parts[0], False)
            if key is not None and key.text is not None:
                value = self.evaluate(parts[1], field=key.text)
                pairs.append((key.text, value, key.constants))
            position = close + 1
        return pairs

    def _pairs(
        self, span: list[Token], separator: str, *, bare_keys: bool = False
    ) -> list[_Pair] | None:
        if matching(span, 0) != len(span) - 1:
            return None
        pairs: list[_Pair] = []
        parts = split_top_level(span[1:-1])
        if not parts:
            return []
        for part in parts:
            if not part:
                continue
            split = next(
                (index for index, token in enumerate(part) if token.is_punct(separator)), None
            )
            if split is None:
                if bare_keys and len(part) == 1 and part[0].kind == IDENT:
                    # `{ category }` shorthand: key and variable share a name.
                    pairs.append((part[0].text, self.evaluate(part, field=part[0].text), ()))
                    continue
                if part[0].is_punct("..."):
                    continue  # a spread adds keys nobody can see statically
                return None
            key_tokens = part[:split]
            key = self._key(key_tokens, bare_keys)
            value = self.evaluate(part[split + 1 :], field=None if key is None else key.text)
            if key is not None and key.text is not None:
                pairs.append((key.text, value, key.constants))
        return pairs

    def _key(self, tokens: list[Token], bare_keys: bool) -> Value | None:
        """A dictionary key as a text value (with the constants it was read from), or None."""
        stripped = [token for token in tokens if not token.is_punct("@")]
        if len(stripped) == 1:
            token = stripped[0]
            if token.kind == STRING:
                value = self._string(token, None)
                return value if not value.interpolated else None
            if bare_keys and token.kind == IDENT:
                return text(token.text)
        value = self.evaluate(stripped)
        return value if value.kind == "text" and not value.interpolated else None

    def _kotlin_pairs(self, inner: list[Token]) -> list[_Pair] | None:
        pairs: list[_Pair] = []
        parts = split_top_level(inner)
        if parts and not parts[-1]:
            parts = parts[:-1]  # a trailing comma: `mapOf("a" to x,)`
        for part in parts:
            split = next((i for i, token in enumerate(part) if token.is_ident("to")), None)
            if split is None:
                return None
            key = self._key(part[:split], False)
            if key is not None and key.text is not None:
                value = self.evaluate(part[split + 1 :], field=key.text)
                pairs.append((key.text, value, key.constants))
        return pairs

    def _java_pairs(self, inner: list[Token]) -> list[_Pair] | None:
        parts = split_top_level(inner)
        if len(parts) % 2:
            return None
        pairs: list[_Pair] = []
        for index in range(0, len(parts), 2):
            key = self._key(parts[index], False)
            if key is not None and key.text is not None:
                value = self.evaluate(parts[index + 1], field=key.text)
                pairs.append((key.text, value, key.constants))
        return pairs


def _entries(pairs: list[_Pair]) -> Value:
    """A dictionary literal's pairs as an ``entries`` value, each key's constants kept aside."""
    return Value(
        "entries",
        entries=tuple((key, value) for key, value, _ in pairs),
        key_constants=tuple((key, constants) for key, _, constants in pairs if constants),
    )


def _strip(span: list[Token]) -> list[Token]:
    """Drop what does not change a value: prefixes, casts, wrapping parens, `!`."""
    changed = True
    while changed and span:
        changed = False
        if span[0].kind == IDENT and span[0].text in _TRANSPARENT_PREFIXES:
            span = span[1:]
            changed = True
            continue
        if span and span[0].is_punct("?", "!") and len(span) > 1:
            span = span[1:]  # `try?` / `try!`
            changed = True
            continue
        cast = _top_level_cast(span)
        if cast is not None:
            span = span[:cast]
            changed = True
            continue
        if span and span[-1].is_punct("!"):
            span = span[:-1]
            changed = True
            continue
        if len(span) >= 2 and span[0].is_punct("(") and matching(span, 0) == len(span) - 1:
            span = span[1:-1]
            changed = True
            continue
        if (
            len(span) >= 2
            and span[0].is_punct("@")
            and span[1].is_punct("(")
            and matching(span, 1) == len(span) - 1
        ):
            span = span[2:-1]  # ObjC boxed expression @(…)
            changed = True
            continue
        if len(span) == 2 and span[0].is_punct("@") and span[1].kind == NUMBER:
            span = span[1:]  # ObjC @42
            changed = True
    return span


def _top_level_cast(span: list[Token]) -> int | None:
    """Index of a top-level ``as``/``satisfies`` (a cast), ignoring any inside brackets."""
    depth = 0
    for index, token in enumerate(span):
        if token.kind == PUNCT:
            if token.text in "([{":
                depth += 1
            elif token.text in ")]}":
                depth -= 1
        elif depth == 0 and index > 0 and token.is_ident("as", "satisfies"):
            return index
    return None


def _accessor_call(body: Sequence[Token], language: str) -> bool:
    """``body`` ends in an empty-argument call of an enum accessor (``.name()``, ``.value()``)."""
    if len(body) < 4 or not (body[-2].is_punct("(") and body[-1].is_punct(")")):
        return False
    method = body[-3]
    if method.kind != IDENT or not body[-4].is_punct(".", "?.", "::"):
        return False
    return method.text in RAW_ACCESSORS or method.text in CASE_NAME_ACCESSORS.get(
        language, frozenset()
    )


def _may_be_text(operand: list[Token]) -> bool:
    """Whether a ``+`` operand can be a string: not empty, a number, a boolean or a null."""
    span = _strip(list(operand))
    if not span:
        return False
    if len(span) == 1:
        token = span[0]
        return token.kind != NUMBER and token.text not in _TRUE | _FALSE | _NULLS
    return not (len(span) == 2 and span[0].is_punct("-") and span[1].kind == NUMBER)


def _conversions(
    template: Value, language: str, arguments: int
) -> list[tuple[int, int, int, str]] | None:
    """Each printf conversion in ``template``: start, end, the argument it takes, its kind.

    The kind is the conversion letter, ``%`` for ``%%`` (which takes argument
    -1). None when ``template`` is not text, when one conversion is not
    filled in (see ``Evaluator._format``) or a ``%`` ends it, when both
    ``%1$s`` and ``%s`` are in it (an order the scan does not guess), and
    when the conversions do not use up exactly ``arguments`` arguments.
    """
    if template.kind != "text" or template.text is None:
        return None
    found: list[tuple[int, int, int, str]] = []
    filled = _FILLED_CONVERSIONS.get(language, frozenset())
    explicit = implicit = 0
    for match in _CONVERSION.finditer(template.text):
        kind = match["conversion"]
        decorated = bool(match["flags"] or match["width"] or match["precision"] is not None)
        if kind == "%":
            if decorated or match["index"] or match["length"]:
                return None
            found.append((match.start(), match.end(), -1, kind))
            continue
        if kind not in filled or decorated or not _length_fits(match["length"], kind, language):
            return None
        if match["index"]:
            position = int(match["index"]) - 1
            explicit = max(explicit, position + 1)
        else:
            position = implicit
            implicit += 1
        found.append((match.start(), match.end(), position, kind))
    if explicit and implicit:
        return None
    return found if max(explicit, implicit) == arguments else None


def _filled(
    template: Value, conversions: list[tuple[int, int, int, str]], values: list[Value]
) -> Value | None:
    """``template`` with each conversion replaced by its argument's text or a ``${name}`` token."""
    source = template.text or ""
    pieces: list[str] = []
    interpolated = template.interpolated
    last = 0
    for start, end, position, kind in conversions:
        pieces.append(source[last:start])
        last = end
        if kind == "%":
            pieces.append("%")
            continue
        value = values[position]
        if value.kind == "text" and value.text is not None:
            if kind in _INTEGER_CONVERSIONS and (
                value.interpolated or not _INTEGER.fullmatch(value.text)
            ):
                return None  # printed as a number: text would fail at runtime
            if (
                not value.interpolated
                and _NUMBER.fullmatch(value.text)
                and not _PRINTED_INTEGER.fullmatch(value.text)
            ):
                # `0x10`, `010`, `1.50`: printed otherwise than written (16, 8, 1.5).
                return None
            pieces.append(value.text)
            interpolated = interpolated or value.interpolated
        elif value.kind == "dynamic":
            pieces.append(f"${{{value.hint}}}")
            interpolated = True
        else:
            return None  # several values, a dictionary, or nothing
    pieces.append(source[last:])
    return text("".join(pieces), interpolated=interpolated)


def _constant_options(value: Value) -> tuple[str, ...] | None:
    """The constants ``value`` is: text with no ``${name}`` hole, or such choices; else None."""
    if value.kind == "text" and value.text is not None and not value.interpolated:
        return (value.text,)
    if value.kind == "choices" and not any("${" in choice for choice in value.choices):
        return value.choices
    return None


def _statements(branch: list[Token], language: str) -> bool:
    """A Kotlin or Swift branch block of string literals alone: statements, the last its value.

    ``{ "a" "b" }`` there is two statements, never the adjacent literals C
    joins, which is how the evaluator would read it.
    """
    return language != "objc" and len(branch) > 1 and all(token.kind == STRING for token in branch)


def _branches(span: list[Token], language: str, source: str | None) -> list[list[Token]] | None:
    """The branches of the conditional ``span``, each to be read as a value.

    None when ``span`` is no ``if``, ``when``, ``switch`` or ternary (Kotlin's
    ``?:`` and Swift's ``??`` are none); empty when it is one this does not
    read. ``source`` is the file's text, for where a ``when`` entry ends;
    without it (an interpolation's tokens) a ``when`` is not read.
    """
    first = span[0]
    if first.is_ident("if") and language in _IF_EXPRESSIONS:
        return _if_branches(span, language) or []
    if first.is_ident("when") and language == "kotlin":
        return (_when_branches(span, source) if source is not None else None) or []
    if first.is_ident(*_CHOICE_KEYWORDS):
        return []
    if language == "kotlin":
        return None  # no ternary: `?` `:` is the elvis operator
    return _ternary(span, language)


def _if_branches(span: list[Token], language: str) -> list[list[Token]] | None:
    """Kotlin ``if (c) a else if (d) { b } else c``, Swift ``if c { a } else { b }``.

    None for an ``if`` with no ``else``, one this does not bound, or more after it.
    """
    branches: list[list[Token]] = []
    position = 0
    while True:
        # `span[position]` is an `if`; its branch starts past the condition.
        start = _condition_end(span, position + 1, language)
        if start >= len(span):
            return None
        if span[start].is_punct("{"):
            close = matching(span, start)
            if close >= len(span):
                return None
            branches.append(span[start + 1 : close])
            after = close + 1
        elif language == "kotlin":
            found = next(
                (
                    index
                    for index, token in _top_level(span)
                    if index > start and token.is_ident("else")
                ),
                None,
            )
            if found is None:
                return None
            branches.append(span[start:found])
            after = found
        else:
            return None
        if after >= len(span) or not span[after].is_ident("else"):
            return None
        position = after + 1
        if position < len(span) and span[position].is_ident("if"):
            continue
        rest = span[position:]
        if rest and rest[0].is_punct("{"):
            if matching(rest, 0) != len(rest) - 1:
                return None
            branches.append(rest[1:-1])
        elif rest and language == "kotlin":
            branches.append(rest)
        else:
            return None
        return branches


def _condition_end(span: list[Token], position: int, language: str) -> int:
    """Past an ``if``'s condition at ``position``: Kotlin's ``(…)``, Swift's up to ``{``."""
    if language == "kotlin":
        if position >= len(span) or not span[position].is_punct("("):
            return len(span)
        return matching(span, position) + 1
    depth = 0
    while position < len(span):
        token = span[position]
        if token.is_punct("(", "["):
            depth += 1
        elif token.is_punct(")", "]"):
            depth -= 1
        elif token.is_punct("{") and depth == 0:
            return position
        position += 1
    return position


def _when_branches(span: list[Token], source: str) -> list[list[Token]] | None:
    """The results of Kotlin's ``when (x) { a -> "x"; b, c -> { "y" } else -> "z" }``.

    An entry's result is a block or an expression, which ends where
    ``scopes.expression_end`` ends an expression body (a line that does not
    go on with it, a ``;``). None when the ``when`` is not one ``{ … }`` to
    the end of ``span``, an entry has no condition, or a result runs into
    the next entry's ``->``.
    """
    position = 1
    if position < len(span) and span[position].is_punct("("):
        position = matching(span, position) + 1
    if position >= len(span) or not span[position].is_punct("{"):
        return None
    if matching(span, position) != len(span) - 1:
        return None
    body = span[position + 1 : -1]
    branches: list[list[Token]] = []
    condition = 0  # where the current entry's condition starts
    end = 0  # past the last result
    for index, token in _top_level(body):
        if not token.is_punct("->"):
            continue
        if index < end or index == condition:
            return None
        if index + 1 < len(body) and body[index + 1].is_punct("{"):
            close = matching(body, index + 1)
            branches.append(body[index + 2 : close])
            end = close + 1
        else:
            end = expression_end(body, source, index, "kotlin")
            # A line that starts with `else` goes on with an `if` in the
            # result, unless it is the next entry: `else ->`.
            end = next(
                (
                    index + 1 + offset
                    for offset, word in _top_level(body[index + 1 : end])
                    if word.is_ident("else")
                    and index + 2 + offset < len(body)
                    and body[index + 2 + offset].is_punct("->")
                ),
                end,
            )
            branches.append(body[index + 1 : end])
        if end <= index + 1:
            return None
        while end < len(body) and body[end].is_punct(";"):
            end += 1
        condition = end
    if not branches or end < len(body):
        return None
    return branches


def _ternary(span: list[Token], language: str) -> list[list[Token]] | None:
    """The two branches of ``c ? a : b``; None when ``span`` has no top-level ``?``.

    The first top-level ``?`` ends the condition (in Swift only one with a
    space before it: ``x?`` is optional chaining). The ``:`` that matches it
    skips the ``?``/``:`` pairs of ternaries nested in the first branch.
    Empty when a branch is empty (Objective-C's ``a ?: b``); None when no
    ``:`` matches, which is no ternary either. None too for a lambda (a
    top-level ``->`` or ``=>``): ``x => x ? "a" : "b"`` is a function, not
    a string, whatever its body returns.
    """

    def opens(index: int) -> bool:
        token = span[index]
        return token.is_punct("?") and (
            language != "swift" or (index > 0 and span[index - 1].end < token.offset)
        )

    question: int | None = None
    for index, token in _top_level(span):
        if token.is_punct("->", "=>"):
            return None
        if question is None and index > 0 and opens(index):
            question = index
    if question is None:
        return None
    nested = 0
    for index, token in _top_level(span):
        if index <= question:
            continue
        if opens(index):
            nested += 1
        elif token.is_punct(":"):
            if nested == 0:
                first, second = span[question + 1 : index], span[index + 1 :]
                return [first, second] if first and second else []
            nested -= 1
    return None


def _length_fits(length: str | None, kind: str, language: str) -> bool:
    """No length modifier, or C's on an integer (``%ld``, ``%lu``) where printf is C's."""
    return not length or (language in _LENGTH_LANGUAGES and kind in _INTEGER_CONVERSIONS)


def _hint_of(span: list[Token]) -> str:
    names = [token.text for token in span if token.kind == IDENT]
    return variable_name(".".join(names)) if names else "value"


# --- why a value was not read -------------------------------------------------------
def _segments(span: Sequence[Token]) -> tuple[str, ...] | None:
    """The identifiers of ``span`` when it is ``IDENT (join+ IDENT)*``, else None."""
    segments: list[str] = []
    joined = True
    for token in span:
        if token.kind == IDENT and joined:
            segments.append(token.text)
            joined = False
        elif token.is_punct(*_PATH_JOINS) and segments:
            joined = True
        else:
            return None
    return tuple(segments) if segments and not joined else None


def _path_reason(first: str, *, shorthand: bool) -> str:
    """``unknown_constant`` for what reads as a constant (upper case, ``kName``, ``.home``)."""
    name = first.lstrip("_$")
    constant = name[:1].isupper() or (name[:1] == "k" and name[1:2].isupper())
    return REASON_UNKNOWN_CONSTANT if shorthand or constant else REASON_VARIABLE


def _format_string(span: list[Token], language: str) -> list[Token] | None:
    """The format string of a printf-style format ``span``, or None when it is not one.

    Empty when ``span`` is a format whose format string cannot be singled out
    (``String.format`` given only locales).
    """
    found = _format_parts(span, language)
    return None if found is None else found[1]


def _format_parts(
    span: list[Token], language: str
) -> tuple[str, list[Token], list[list[Token]]] | None:
    """A printf-style format ``span``'s form, format string and arguments; None for no format.

    The form is ``method`` (``x.format(…)``, ``String.format(…)``), ``swift``
    (``String(format: …)``) or ``objc`` (``stringWithFormat:``). The arguments
    are the parts after the format string, as written.
    """
    if len(span) >= 4 and span[-1].is_punct(")"):
        # `x.format(…)` / `x.formatted(…)`, whatever `x` is (a literal included),
        # unless an operator joins `x` to more: `"a" + y.format(z)` formats `y`.
        opening = _opening(span, len(span) - 1)
        if (
            opening >= 2
            and span[opening - 1].is_ident(*_FORMAT_METHODS)
            and span[opening - 2].is_punct(".", "?.")
        ):
            receiver = span[: opening - 2]
            if _has_operator(receiver, language):
                return None
            parts = split_top_level(span[opening + 1 : -1])
            if not receiver or not receiver[-1].is_ident("String"):
                if parts and parts[0] and parts[0][0].is_ident("Locale"):
                    parts = parts[1:]  # Kotlin `"%s".format(Locale.US, x)`
                return "method", receiver, parts
            # `String.format(Locale.US, "%s", x)`: the first argument that is not a locale.
            for position, part in enumerate(parts):
                if part and not part[0].is_ident("Locale"):
                    return "method", part, parts[position + 1 :]
            return "method", [], []
    if (
        len(span) >= 5
        and span[0].is_ident("String")
        and span[1].is_punct("(")
        and span[2].is_ident("format")
        and span[3].is_punct(":")
        and matching(span, 1) == len(span) - 1
    ):
        # Swift `String(format: "%@_x", id)`.
        parts = split_top_level(span[2:-1])
        return ("swift", parts[0][2:], parts[1:]) if parts else ("swift", [], [])
    if language == "objc" and span[0].is_punct("[") and matching(span, 0) == len(span) - 1:
        # Objective-C `[NSString stringWithFormat:@"%@_x", y]`. Only there is
        # `[…]` a message: in Swift and TypeScript it is an array or a dictionary.
        inner = span[1:-1]
        for index, token in _top_level(inner):
            if (
                token.is_ident(*_OBJC_FORMATS)
                and index + 1 < len(inner)
                and inner[index + 1].is_punct(":")
            ):
                parts = split_top_level(inner[index + 2 :])
                return ("objc", parts[0], parts[1:]) if parts else ("objc", [], [])
    return None


def _format_call(span: list[Token], language: str) -> tuple[list[Token], list[list[Token]]] | None:
    """The format string and arguments of a format ``Evaluator._format`` may fill in.

    Its form must be the language's printf (``method`` in Kotlin and Java,
    ``String(format:)`` in Swift, the message in Objective-C), and its
    arguments plain values: no spread, no label other than Swift's
    ``locale:``, which is passed over.
    """
    found = _format_parts(span, language)
    if found is None:
        return None
    form, template, parts = found
    if not template or form != _FORMAT_FORMS.get(language):
        return None
    if parts and not parts[-1] and language == "kotlin":
        parts = parts[:-1]  # a trailing comma
    arguments: list[list[Token]] = []
    for part in parts:
        if not part or part[0].is_punct("*", "..."):
            return None
        if language == "swift" and len(part) > 1 and part[1].is_punct(":"):
            if part[0].is_ident("locale") and not arguments:
                continue
            return None  # `arguments: list`, or a label it does not know
        arguments.append(part)
    return template, arguments


def _opening(span: list[Token], close: int) -> int:
    """Index of the bracket that ``span[close]`` closes, or -1."""
    depth = 0
    for index in range(close, -1, -1):
        token = span[index]
        if token.kind != PUNCT:
            continue
        if token.text in _CLOSING:
            depth += 1
        elif token.text in _OPENING:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _has_operator(span: list[Token], language: str) -> bool:
    """A choice, or an operator at ``span``'s top level.

    An operator is a binary one, ``=``, a lambda's arrow, a range, a
    comparison ``<`` / ``>`` that does not bracket type arguments, or a word
    operator (``_has_infix_word``). The first token is none: a leading ``-``
    or ``*`` is unary. What stands inside type arguments is no operator
    either: ``Class<*>``, ``Foo<A | B>``.
    """
    if not span:
        return False
    if _is_choice(span):
        return True
    types = _type_arguments(span)
    return any(
        token.kind == PUNCT
        and (token.text in _OPERATORS or token.text in ("<", ">") or _range_dots(span, index))
        for index, token in _top_level(span)
        if index > 0 and index not in types
    ) or _has_infix_word(span, language, skip=types)


def _range_dots(span: list[Token], index: int) -> bool:
    """``span[index]`` is the first of the two touching dots of Kotlin's range ``a..b``."""
    following = index + 1
    return (
        span[index].is_punct(".")
        and following < len(span)
        and span[following].is_punct(".")
        and span[index].end == span[following].offset
    )


def _has_infix_word(span: list[Token], language: str, skip: frozenset[int]) -> bool:
    """A word operator at ``span``'s top level, outside the indexes in ``skip``.

    A word counts only right after an operand, so a variable that shares the
    name stays a variable: ``to.format(z)`` and ``x.step.format(z)`` hold none,
    ``a to b``, ``a zip b`` and Kotlin's ``a !in b`` and ``a!! to b`` hold one.
    """
    words = _INFIX_WORDS.get(language)
    if words is None and language != "kotlin":
        return False
    # A loop, not `any(…)` over a generator: a format's receiver is read here
    # one level below `Evaluator._format`, and a generator's frame would be one
    # more at the innermost value (`test_plain_check_stacks_no_more_frames_than_before_list`).
    for index, token in _top_level(span):
        if (
            index > 0
            and token.kind == IDENT
            and (token.text in words if words is not None else token.text not in _KOTLIN_NOT_INFIX)
            and index not in skip
            and _follows_operand(span, index)
        ):
            return True
    return False


def _follows_operand(span: list[Token], index: int) -> bool:
    """``span[index]`` comes right after an operand.

    An operand ends in a name, a literal or a closing bracket, then any
    number of ``!``: Kotlin's not-null ``a!!``, and the ``!`` that Kotlin
    writes before ``in`` / ``is`` to negate them. A Kotlin annotation
    (``@Keep``, ``@Suppress("x")``) ends the same way but is no operand.
    """
    before = index - 1
    while before >= 0 and span[before].is_punct("!"):
        before -= 1
    if before < 0:
        return False
    token = span[before]
    if not (token.kind in (IDENT, STRING, NUMBER) or token.is_punct(*_CLOSING)):
        return False
    return not _is_annotation(span, before)


def _is_annotation(span: list[Token], end: int) -> bool:
    """``span[end]`` ends an annotation: ``@Name``, ``@a.b.Name`` or ``@Name(…)``.

    A label (``this@Outer``) has a name before its ``@``; an annotation has
    none.
    """
    name = _opening(span, end) - 1 if span[end].is_punct(")") else end
    while name >= 0 and span[name].kind == IDENT:
        if name >= 2 and span[name - 1].is_punct("."):
            name -= 2
            continue
        at = name - 1
        return at >= 0 and span[at].is_punct("@") and (at == 0 or span[at - 1].kind != IDENT)
    return False


def _type_arguments(span: list[Token]) -> frozenset[int]:
    """The indexes of ``span`` inside a top-level type-argument list, brackets included.

    A ``<`` opens one when its ``>`` closes it over nothing but a type (names,
    literals, bracketed groups and ``_TYPE_ARGUMENT_PUNCT``) and the list is
    placed where type arguments go: before a call, a member, a reference, a
    trailing lambda or an index (``listOf<String>(a)``, ``Foo<Bar>.NAME``,
    ``run<String> { s }``, ``Foo<T>["k"]``), at the end, after a ``.``
    (Java's ``Util.<String>pick(a)``), or opening the span (TypeScript's
    ``<string>x``). Any other ``<`` or ``>`` is a comparison or a shift.

    One pass pairs the brackets: a token no type holds closes every ``<``
    still open, as none of them can be type arguments any more.
    """
    pairs: list[tuple[int, int]] = []
    open_angles: list[int] = []
    depth = 0
    for index, token in enumerate(span):
        if token.kind == PUNCT and token.text in _OPENING:
            depth += 1
        elif token.kind == PUNCT and token.text in _CLOSING:
            depth -= 1
        elif depth != 0:
            continue
        elif token.is_punct("<"):
            open_angles.append(index)
        elif token.is_punct(">"):
            if open_angles:
                pairs.append((open_angles.pop(), index))
        elif token.kind == PUNCT and not token.is_punct(*_TYPE_ARGUMENT_PUNCT):
            open_angles.clear()
    inside: set[int] = set()
    for opening, close in sorted(pairs):
        if opening in inside:
            continue
        placed = (
            opening == 0
            or span[opening - 1].is_punct(".")
            or close == len(span) - 1
            or span[close + 1].is_punct(*_AFTER_TYPE_ARGUMENTS)
        )
        if placed:
            inside.update(range(opening, close + 1))
    return frozenset(inside)


def _is_choice(span: list[Token]) -> bool:
    """``if`` / ``when`` / ``switch``, a top-level ternary (Kotlin's ``?:`` too), or ``??``."""
    if span[0].is_ident(*_CHOICE_KEYWORDS):
        return True
    question = False
    for _, token in _top_level(span):
        if token.is_punct("??"):
            return True
        if token.is_punct("?"):
            question = True
        elif token.is_punct(":") and question:
            return True
    return False


def _top_level(span: Sequence[Token]) -> Iterator[tuple[int, Token]]:
    """``(index, token)`` for each token of ``span`` outside every bracket pair."""
    depth = 0
    for index, token in enumerate(span):
        if token.kind == PUNCT and token.text in _OPENING:
            depth += 1
        elif token.kind == PUNCT and token.text in _CLOSING:
            depth -= 1
        elif depth == 0:
            yield index, token
