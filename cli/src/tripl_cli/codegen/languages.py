"""What differs between Swift, Kotlin and TypeScript, as small pure functions.

Every plan string that reaches generated source goes through ``literal`` (a
string literal of the target language) or ``comment`` (text safe inside a line
or block comment) — never straight into the output. That is the injection
boundary: a plan value is typed by any project member, and a value such as
``"); exec(…`` or ``*/ …`` must come out as inert text in every language.
"""

from __future__ import annotations

from dataclasses import dataclass

from tripl_cli.codegen.naming import KOTLIN, SWIFT, TS


def _escape(text: str, *, quote: str, dollar: bool, unicode: str) -> str:
    out: list[str] = []
    for char in text:
        code = ord(char)
        if char == "\\":
            out.append("\\\\")
        elif char == quote:
            out.append("\\" + quote)
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif char == "$" and dollar:
            out.append("\\$")
        elif code < 0x20 or code == 0x7F or code in (0x2028, 0x2029):
            out.append(unicode.format(code))
        else:
            out.append(char)
    return "".join(out)


def swift_string(text: str) -> str:
    # A backslash is escaped, so `\(` can never start an interpolation.
    return '"' + _escape(text, quote='"', dollar=False, unicode="\\u{{{0:x}}}") + '"'


def kotlin_string(text: str) -> str:
    # `$` is escaped: Kotlin interpolates `$name` and `${…}` in every string.
    return '"' + _escape(text, quote='"', dollar=True, unicode="\\u{0:04x}") + '"'


def ts_string(text: str) -> str:
    # Single quotes: `${…}` only interpolates inside backticks.
    return "'" + _escape(text, quote="'", dollar=False, unicode="\\u{0:04x}") + "'"


def ts_template_part(text: str) -> str:
    """Text inside a TS template literal: backticks, backslashes and ``${`` escaped."""
    escaped = _escape(text, quote="`", dollar=False, unicode="\\u{0:04x}")
    return escaped.replace("${", "\\${")


def swift_interpolation_part(text: str) -> str:
    return _escape(text, quote='"', dollar=False, unicode="\\u{{{0:x}}}")


def kotlin_template_part(text: str) -> str:
    return _escape(text, quote='"', dollar=True, unicode="\\u{0:04x}")


def comment(text: str) -> str:
    """One line of comment text: no line breaks, no way to close a block comment."""
    flat = " ".join(text.split())
    return flat.replace("*/", "* /").replace("/*", "/ *")


@dataclass(frozen=True)
class Dialect:
    """The per-language spellings the context builder needs."""

    name: str
    string_type: str
    bool_type: str
    number_type: str
    properties_type: str
    null: str
    extension: str
    empty_map: str

    def literal(self, text: str) -> str:
        if self.name == SWIFT:
            return swift_string(text)
        if self.name == KOTLIN:
            return kotlin_string(text)
        return ts_string(text)

    def optional(self, type_name: str) -> str:
        return f"{type_name} | undefined" if self.name == TS else f"{type_name}?"

    def enum_raw(self, expression: str, *, optional: bool) -> str:
        """The plan string an enum value stands for."""
        if self.name == SWIFT:
            return f"{expression}?.rawValue" if optional else f"{expression}.rawValue"
        if self.name == KOTLIN:
            return f"{expression}?.value" if optional else f"{expression}.value"
        return expression

    def scalar_text(self, expression: str, *, optional: bool) -> str:
        """A Bool/Double as the plan string it stands for."""
        if self.name == SWIFT:
            return f"{expression}.map {{ String($0) }}" if optional else f"String({expression})"
        if self.name == KOTLIN:
            return f"{expression}?.toString()" if optional else f"{expression}.toString()"
        if optional:
            return f"({expression} === undefined ? undefined : String({expression}))"
        return f"String({expression})"

    def case_ref(self, enum: str, case: str) -> str:
        return f"{enum}.{case}"

    def interpolate(self, parts: list[tuple[bool, str]]) -> str:
        """A string built from literal text and expressions: ``[(is_expr, text), …]``.

        Adjacent literal parts are merged BEFORE escaping: escaped one at a time,
        ``'$'`` then ``'{app}'`` would each be inert and together spell a live
        ``${app}`` in a TypeScript template literal.
        """
        merged: list[tuple[bool, str]] = []
        for is_expr, text in parts:
            if not is_expr and merged and not merged[-1][0]:
                merged[-1] = (False, merged[-1][1] + text)
            else:
                merged.append((is_expr, text))
        parts = merged
        if all(not is_expr for is_expr, _ in parts):
            return self.literal("".join(text for _, text in parts))
        if len(parts) == 1:
            return parts[0][1]  # already a string expression
        if self.name == SWIFT:
            body = "".join(
                f"\\({text})" if is_expr else swift_interpolation_part(text)
                for is_expr, text in parts
            )
            return f'"{body}"'
        if self.name == KOTLIN:
            body = "".join(
                f"${{{text}}}" if is_expr else kotlin_template_part(text) for is_expr, text in parts
            )
            return f'"{body}"'
        body = "".join(
            f"${{{text}}}" if is_expr else ts_template_part(text) for is_expr, text in parts
        )
        return f"`{body}`"


DIALECTS: dict[str, Dialect] = {
    SWIFT: Dialect(
        name=SWIFT,
        string_type="String",
        bool_type="Bool",
        number_type="Double",
        properties_type="[String: Any]",
        null="nil",
        extension="swift",
        empty_map="[:]",
    ),
    KOTLIN: Dialect(
        name=KOTLIN,
        string_type="String",
        bool_type="Boolean",
        number_type="Double",
        properties_type="Map<String, Any?>",
        null="null",
        extension="kt",
        empty_map="emptyMap()",
    ),
    TS: Dialect(
        name=TS,
        string_type="string",
        bool_type="boolean",
        number_type="number",
        properties_type="Readonly<Record<string, unknown>>",
        null="undefined",
        extension="ts",
        empty_map="{}",
    ),
}
