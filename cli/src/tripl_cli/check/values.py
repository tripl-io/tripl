"""An argument's tokens -> what the checker can say about its value.

Four outcomes, and the distinction between them is the whole point:

``Value.text``
    A string known at scan time. ``interpolated`` is set when it carries
    ``${name}`` plan-variable tokens (``"promo_sheet_\\(id)_shown"`` becomes
    ``promo_sheet_${id}_shown``), which the validator matches against the plan's
    naming rules rather than as a literal.
``Value.choices``
    One of several known strings (a function whose every ``return`` is a
    literal). The caller checks each.
``Value.entries``
    A dictionary literal — properties, or an object argument whose keys a
    preset reads as labels.
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
    could not resolve: its file is not scanned, two constants share the name,
    or the case has no string raw value. ``path`` keeps the segments:
    ``inventory`` refines a value into a parameter or a local from them,
    without re-reading the text. ``.home`` has none: it names a case of the
    type the call expects, never a name in scope.
``format``
    A printf-style format: ``x.format(…)`` / ``x.formatted(…)`` whatever ``x``
    is, Swift ``String(format: …)`` and Objective-C ``stringWithFormat:`` (a
    message only there: in Swift and TypeScript ``[…]`` is a literal). Told
    from the shape before anything else, since a format is a call too. A member
    call binds tighter than an operator, so ``"a" + y.format(z)`` is a
    concatenation and ``("a" + y).format(z)`` a format. When the format string
    reads as text, ``expression`` is ``format of <template>``.
``function``
    A call of a function the index cannot read: it reads one whose every
    ``return`` is a string literal and that is declared to return a string
    (``String``, ``string``, ``NSString``), or a Kotlin ``fun f() = "…"``. A
    map builder the dictionary reader rejected (Kotlin ``mapOf(a to b, )``) is
    an ``expression`` instead.
``conditional``
    A choice between values: ``if`` / ``when`` / ``switch``, a ternary,
    Kotlin's ``?:``, Swift's ``??``.
``concatenation``
    A top-level ``+`` with no string literal among its operands, or one whose
    operand is several values or a dictionary.
``expression``
    Anything else: a chained call, an operator, a subscript, a lambda, an
    unparsable number.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from tripl_cli.check.calls import matching, split_top_level
from tripl_cli.check.lexer import IDENT, NUMBER, PUNCT, STRING, Interp, Token, tokenize
from tripl_cli.check.model import (
    REASON_CONCATENATION,
    REASON_CONDITIONAL,
    REASON_EXPRESSION,
    REASON_FORMAT,
    REASON_FUNCTION,
    REASON_UNKNOWN_CONSTANT,
    REASON_VARIABLE,
)
from tripl_cli.check.symbols import (
    CASE_NAME_ACCESSORS,
    RAW_ACCESSORS,
    SymbolIndex,
    number_text,
    template_text,
    variable_name,
)

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
_OBJC_FORMATS = frozenset({"stringWithFormat", "localizedStringWithFormat"})
_CHOICE_KEYWORDS = frozenset({"if", "when", "switch"})
# Binary operators, assignment and a lambda's arrow: each binds looser than a
# member call.
_OPERATORS = frozenset(
    {"+", "-", "*", "/", "%", "&", "|", "^", "==", "!=", "===", "!==", "<=", ">="}
    | {"&&", "||", "??", "=", "->", "=>"}
)
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

    @property
    def is_known(self) -> bool:
        return self.kind in ("text", "choices")


DYNAMIC = Value("dynamic")
ABSENT = Value("absent")


def text(value: str, *, interpolated: bool = False) -> Value:
    return Value("text", text=value, interpolated=interpolated)


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
    """

    def __init__(self, symbols: SymbolIndex, language: str, *, explain: bool = False) -> None:
        self.symbols = symbols
        self.language = language
        self.explain = explain

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
        entries = self._dictionary(span)
        if entries is not None:
            return Value("entries", entries=entries)
        concatenated = self._concatenation(span, field)
        if concatenated is not None:
            return concatenated
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
            resolved = self.symbols.resolve([token.text], hint=field, language=self.language)
            if resolved is not None:
                return text(resolved)
            if not self.explain:
                return dynamic(token.text)
            return dynamic(
                token.text,
                reason=_path_reason(token.text, shorthand=False),
                expression=expression_text([token]),
                path=(token.text,),
            )
        return None

    def _string(self, token: Token, field: str | None) -> Value:
        parts: list[str | Interp] = []
        for part in token.parts:
            if isinstance(part, Interp):
                inlined = self._inline(part.expr, field)
                parts.append(inlined if inlined is not None else part)
            else:
                parts.append(part)
        interpolated = any(isinstance(part, Interp) for part in parts)
        return text(template_text(parts), interpolated=interpolated)

    def _inline(self, expr: str, field: str | None) -> str | None:
        """An interpolation that is itself a constant (an enum raw value, a literal) is inlined."""
        try:
            tokens = tokenize(expr, self.language)
        except ValueError:
            return None
        value = self.evaluate(tokens, field=field)
        return value.text if value.kind == "text" and not value.interpolated else None

    # --- compound forms ------------------------------------------------------
    def _concatenation(self, span: list[Token], field: str | None) -> Value | None:
        """``"a" + x + "b"`` and C's adjacent literals ``@"a" @"b"``; needs at least one literal."""
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
        if not any(len(op) == 1 and op[0].kind == STRING for op in operands):
            return None
        pieces: list[str] = []
        interpolated = False
        for operand in operands:
            value = self.evaluate(operand, field=field)
            if value.kind == "text" and value.text is not None:
                pieces.append(value.text)
                interpolated = interpolated or value.interpolated
            elif value.kind == "dynamic":
                pieces.append(f"${{{value.hint}}}")
                interpolated = True
            else:
                # An operand that is several values, a dictionary or nothing.
                if not self.explain:
                    return dynamic(_hint_of(span))
                return self._unread(span, field, _hint_of(span), otherwise=REASON_CONCATENATION)
        return text("".join(pieces), interpolated=interpolated)

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
        while position < len(body):
            token = body[position]
            if token.kind == IDENT:
                path.append(token.text)
                position += 1
            elif token.is_punct(".", "?.", "::", "?", "!"):
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
            values = self.symbols.function_values(called)
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
        resolved = self.symbols.resolve(
            path, hint=field, shorthand=shorthand, language=self.language
        )
        if resolved is not None:
            return text(resolved)
        hint = variable_name(".".join(path))
        if not self.explain:
            return dynamic(hint)
        segments = _segments(reference)
        if segments is None:
            # Not a plain path after all: `!flag`, `::name`, an infix `a to b`.
            return self._unread(span, field, hint)
        return dynamic(
            hint,
            reason=_path_reason(segments[0], shorthand=shorthand),
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

        Only ``expression`` shows it. The reader explains nothing, so a format
        inside the format string (``x.format(a).format(b)``,
        ``String.format(…, String.format(…))``) is not read in turn: its own
        template would change its own ``expression``, never what the outer one
        reads as. So reading never nests, however long the chain, and a format
        string nested past the stack is not read at all.
        """
        if not format_string:
            return None
        reader = Evaluator(self.symbols, self.language)
        try:
            template = reader.evaluate(format_string, field=field)
        except RecursionError:
            return None
        return template.text if template.kind == "text" else None

    def _dictionary(self, span: list[Token]) -> tuple[tuple[str, Value], ...] | None:
        """A dictionary literal's ``(key, value)`` pairs, or None when ``span`` is not one."""
        first = span[0]
        if first.is_punct("@") and len(span) > 1 and span[1].is_punct("{"):
            return self._pairs(span[1:], ":")  # ObjC @{ @"k": v }
        if first.is_punct("[") and self.language == "swift":
            if len(span) == 3 and span[1].is_punct(":"):
                return ()  # [:]
            return self._pairs(span, ":")
        if first.is_punct("{") and self.language == "ts":
            return self._pairs(span, ":", bare_keys=True)
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

    def _pairs(
        self, span: list[Token], separator: str, *, bare_keys: bool = False
    ) -> tuple[tuple[str, Value], ...] | None:
        if matching(span, 0) != len(span) - 1:
            return None
        pairs: list[tuple[str, Value]] = []
        parts = split_top_level(span[1:-1])
        if not parts:
            return ()
        for part in parts:
            if not part:
                continue
            split = next(
                (index for index, token in enumerate(part) if token.is_punct(separator)), None
            )
            if split is None:
                if bare_keys and len(part) == 1 and part[0].kind == IDENT:
                    # `{ category }` shorthand: key and variable share a name.
                    pairs.append((part[0].text, self.evaluate(part, field=part[0].text)))
                    continue
                if part[0].is_punct("..."):
                    continue  # a spread adds keys nobody can see statically
                return None
            key_tokens = part[:split]
            key = self._key(key_tokens, bare_keys)
            value = self.evaluate(part[split + 1 :], field=key)
            if key is not None:
                pairs.append((key, value))
        return tuple(pairs)

    def _key(self, tokens: list[Token], bare_keys: bool) -> str | None:
        stripped = [token for token in tokens if not token.is_punct("@")]
        if len(stripped) == 1:
            token = stripped[0]
            if token.kind == STRING:
                value = self._string(token, None)
                return value.text if not value.interpolated else None
            if bare_keys and token.kind == IDENT:
                return token.text
        value = self.evaluate(stripped)
        return value.text if value.kind == "text" and not value.interpolated else None

    def _kotlin_pairs(self, inner: list[Token]) -> tuple[tuple[str, Value], ...] | None:
        pairs: list[tuple[str, Value]] = []
        parts = split_top_level(inner)
        if parts and not parts[-1]:
            parts = parts[:-1]  # a trailing comma: `mapOf("a" to x,)`
        for part in parts:
            split = next((i for i, token in enumerate(part) if token.is_ident("to")), None)
            if split is None:
                return None
            key = self._key(part[:split], False)
            if key is not None:
                pairs.append((key, self.evaluate(part[split + 1 :], field=key)))
        return tuple(pairs)

    def _java_pairs(self, inner: list[Token]) -> tuple[tuple[str, Value], ...] | None:
        parts = split_top_level(inner)
        if len(parts) % 2:
            return None
        pairs: list[tuple[str, Value]] = []
        for index in range(0, len(parts), 2):
            key = self._key(parts[index], False)
            if key is not None:
                pairs.append((key, self.evaluate(parts[index + 1], field=key)))
        return tuple(pairs)


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
            if _has_operator(receiver):
                return None
            if not receiver or not receiver[-1].is_ident("String"):
                return receiver
            # `String.format(Locale.US, "%s", x)`: the first argument that is not a locale.
            for part in split_top_level(span[opening + 1 : -1]):
                if part and not part[0].is_ident("Locale"):
                    return part
            return []
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
        return parts[0][2:] if parts else []
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
                return parts[0] if parts else []
    return None


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


def _has_operator(span: list[Token]) -> bool:
    """A choice, or an operator at ``span``'s top level: a binary one, ``=``, a lambda's arrow.

    The first token is none: a leading ``-`` or ``*`` is unary.
    """
    if not span:
        return False
    return _is_choice(span) or any(
        token.kind == PUNCT and token.text in _OPERATORS
        for index, token in _top_level(span)
        if index > 0
    )


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
