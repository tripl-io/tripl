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
``DYNAMIC``
    Not knowable without running the code. Sent to the validator as ``null``,
    which is never an error; ``--strict`` reports it.

``ABSENT`` is ``nil``/``null``: the argument was passed as "nothing", so the
field is left out rather than sent as dynamic.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tripl_cli.check.calls import matching, split_top_level
from tripl_cli.check.lexer import IDENT, NUMBER, PUNCT, STRING, Interp, Token, tokenize
from tripl_cli.check.symbols import (
    CASE_NAME_ACCESSORS,
    RAW_ACCESSORS,
    SymbolIndex,
    number_text,
    template_text,
    variable_name,
)

_NULLS = frozenset({"nil", "null", "undefined", "NULL", "Nil", "None"})
_TRUE = frozenset({"true", "YES", "True"})
_FALSE = frozenset({"false", "NO", "False"})
_MAP_BUILDERS = frozenset(
    {"mapOf", "hashMapOf", "mutableMapOf", "linkedMapOf", "bundleOf", "of", "ofEntries"}
)
# Prefixes that do not change a value: `try`, `await`, casts, `new`.
_TRANSPARENT_PREFIXES = frozenset({"try", "await", "new"})


@dataclass(frozen=True, slots=True)
class Value:
    kind: str  # "text" | "choices" | "entries" | "dynamic" | "absent"
    text: str | None = None
    interpolated: bool = False
    choices: tuple[str, ...] = ()
    entries: tuple[tuple[str, Value], ...] = ()
    # For a dynamic value: the name a template would use for it (`${name}`).
    hint: str = "value"

    @property
    def is_known(self) -> bool:
        return self.kind in ("text", "choices")


DYNAMIC = Value("dynamic")
ABSENT = Value("absent")


def text(value: str, *, interpolated: bool = False) -> Value:
    return Value("text", text=value, interpolated=interpolated)


def dynamic(hint: str = "value") -> Value:
    return Value("dynamic", hint=hint or "value")


class Evaluator:
    """Evaluates argument token spans against one run's ``SymbolIndex``."""

    def __init__(self, symbols: SymbolIndex, language: str) -> None:
        self.symbols = symbols
        self.language = language

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
            return text(f"-{number}") if number is not None else DYNAMIC
        entries = self._dictionary(span)
        if entries is not None:
            return Value("entries", entries=entries)
        concatenated = self._concatenation(span, field)
        if concatenated is not None:
            return concatenated
        reference = self._reference(span, field)
        if reference is not None:
            return reference
        return dynamic(_hint_of(span))

    # --- single tokens -------------------------------------------------------
    def _single(self, token: Token, field: str | None) -> Value | None:
        if token.kind == STRING:
            return self._string(token, field)
        if token.kind == NUMBER:
            number = number_text(token)
            return text(number) if number is not None else DYNAMIC
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
            return dynamic(token.text)
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
                return dynamic(_hint_of(span))
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
        if called is not None and len(path) > 1 and _accessor_call(body, self.language):
            called = None
        if called is not None:
            values = self.symbols.function_values(called)
            if values is None:
                return dynamic(called)
            if len(values) == 1:
                only = values[0]
                return text(only, interpolated="${" in only)
            return Value("choices", choices=values)
        resolved = self.symbols.resolve(
            path, hint=field, shorthand=shorthand, language=self.language
        )
        if resolved is not None:
            return text(resolved)
        return dynamic(variable_name(".".join(path)))

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
        for part in split_top_level(inner):
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
