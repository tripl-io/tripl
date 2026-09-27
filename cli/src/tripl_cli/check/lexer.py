"""A small, forgiving tokenizer for the languages tracking calls are written in.

Not a parser for any of them. It exists so the call finder never mistakes a
parenthesis inside a string, a comment or an interpolation for code, and so a
string literal arrives as its VALUE with every interpolation marked, whatever
the language spelled it as:

* Swift — ``"…"`` and ``\"\"\"…\"\"\"`` with ``\\(expr)`` interpolation, raw
  strings ``#"…"#`` (interpolated with ``\\#(expr)``), nested ``/* */``.
* Objective-C — ``@"…"`` (``Token.at``), C strings and character literals.
* Kotlin — ``"…"`` and ``\"\"\"…\"\"\"`` with ``$name`` and ``${expr}``
  templates, character literals, nested ``/* */``.
* Java — ``"…"``, text blocks, character literals.
* TypeScript / JavaScript — ``'…'``, ``"…"``, template literals with ``${expr}``
  (nested), and regular-expression literals, told apart from division by the
  token before them.

Anything it does not recognise becomes a one-character punctuation token rather
than an error: a checker that refuses a file over an exotic operator reports
nothing about the ninety-nine tracking calls in it.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

LANGUAGES = ("swift", "objc", "kotlin", "java", "ts")

EXTENSIONS: dict[str, str] = {
    ".swift": "swift",
    ".m": "objc",
    ".mm": "objc",
    ".h": "objc",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".java": "java",
    ".ts": "ts",
    ".tsx": "ts",
    ".mts": "ts",
    ".cts": "ts",
    ".js": "ts",
    ".jsx": "ts",
    ".mjs": "ts",
    ".cjs": "ts",
}

# Language names a config may use, mapped onto the tokenizer that reads them.
LANGUAGE_ALIASES: dict[str, str] = {
    "swift": "swift",
    "objc": "objc",
    "objective-c": "objc",
    "objectivec": "objc",
    "kotlin": "kotlin",
    "kt": "kotlin",
    "java": "java",
    "ts": "ts",
    "typescript": "ts",
    "js": "ts",
    "javascript": "ts",
}

IDENT = "ident"
STRING = "string"
NUMBER = "number"
PUNCT = "punct"

_MULTI_PUNCT = (
    "===",
    "!==",
    "...",
    "..<",
    "?.",
    "::",
    "==",
    "!=",
    "->",
    "=>",
    "<=",
    ">=",
    "&&",
    "||",
    "??",
)

# After one of these a `/` starts a regex literal in JS, not a division.
_REGEX_AFTER_PUNCT = frozenset("(,=:[!&|?{};+-*%<>~^")
_REGEX_AFTER_WORD = frozenset(
    {"return", "typeof", "case", "do", "else", "in", "of", "void", "yield", "await", "throw"}
    | {"delete", "new"}
)


@dataclass(frozen=True, slots=True)
class Interp:
    """One interpolated expression inside a string, as source text."""

    expr: str


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    text: str
    offset: int
    end: int
    # STRING only: the literal value, with every interpolation as an `Interp`.
    parts: tuple[str | Interp, ...] = ()
    # STRING only: written as Objective-C's @"…".
    at: bool = False

    def is_punct(self, *symbols: str) -> bool:
        return self.kind == PUNCT and self.text in symbols

    def is_ident(self, *names: str) -> bool:
        return self.kind == IDENT and (not names or self.text in names)


class LineIndex:
    """Offset -> (1-based line, 1-based column), by binary search over line starts."""

    def __init__(self, source: str) -> None:
        self._starts = [0]
        for index, char in enumerate(source):
            if char == "\n":
                self._starts.append(index + 1)

    def position(self, offset: int) -> tuple[int, int]:
        line = bisect.bisect_right(self._starts, offset) - 1
        return line + 1, offset - self._starts[line] + 1


def tokenize(source: str, language: str) -> list[Token]:
    """Every token of ``source``, comments and whitespace dropped."""
    return _Lexer(source, language).run()


class _Lexer:
    def __init__(self, source: str, language: str) -> None:
        if language not in LANGUAGES:
            raise ValueError(f"unknown language {language!r}")
        self.src = source
        self.lang = language
        self.pos = 0
        self.tokens: list[Token] = []

    # --- driver ------------------------------------------------------------
    def run(self) -> list[Token]:
        src = self.src
        length = len(src)
        while self.pos < length:
            char = src[self.pos]
            if char in " \t\r\n\f\v\ufeff":
                self.pos += 1
            elif src.startswith("//", self.pos):
                end = src.find("\n", self.pos)
                self.pos = length if end < 0 else end
            elif src.startswith("/*", self.pos):
                self._block_comment()
            elif (
                char.isalpha() or char in "_$" or (char == "`" and self.lang in ("swift", "kotlin"))
            ):
                self._identifier()
            elif char.isdigit():
                self._number()
            elif char == "@" and self.lang == "objc" and src.startswith('"', self.pos + 1):
                start = self.pos
                self.pos += 1
                parts, end = self._c_string(self.pos)
                self._emit(STRING, start, end, parts=parts, at=True)
            elif char == "#" and self.lang == "swift" and self._swift_raw_string():
                pass
            elif char in "\"'`":
                self._string(char)
            elif char == "/" and self.lang == "ts" and self._regex_allowed():
                self._regex()
            else:
                self._punct()
        return self.tokens

    def _emit(
        self,
        kind: str,
        start: int,
        end: int,
        *,
        parts: tuple[str | Interp, ...] = (),
        at: bool = False,
        text: str | None = None,
    ) -> None:
        self.tokens.append(
            Token(kind, self.src[start:end] if text is None else text, start, end, parts, at)
        )
        self.pos = end

    # --- simple tokens -------------------------------------------------------
    def _block_comment(self) -> None:
        nests = self.lang in ("swift", "kotlin")
        depth = 0
        index = self.pos
        src = self.src
        while index < len(src):
            if src.startswith("/*", index):
                depth = depth + 1 if nests or depth == 0 else depth
                index += 2
            elif src.startswith("*/", index):
                depth -= 1
                index += 2
                if depth <= 0:
                    break
            else:
                index += 1
        self.pos = index

    def _identifier(self) -> None:
        src = self.src
        start = self.pos
        if src[start] == "`":
            end = src.find("`", start + 1)
            if end < 0 or "\n" in src[start:end]:
                self._emit(PUNCT, start, start + 1)
                return
            self._emit(IDENT, start, end + 1, text=src[start + 1 : end])
            return
        index = start + 1
        while index < len(src) and (src[index].isalnum() or src[index] in "_$"):
            index += 1
        self._emit(IDENT, start, index)

    def _number(self) -> None:
        src = self.src
        index = self.pos
        if src.startswith(("0x", "0X", "0b", "0B", "0o", "0O"), index):
            index += 2
            while index < len(src) and (src[index].isalnum() or src[index] == "_"):
                index += 1
            self._emit(NUMBER, self.pos, index)
            return
        while index < len(src) and (src[index].isdigit() or src[index] == "_"):
            index += 1
        if index + 1 < len(src) and src[index] == "." and src[index + 1].isdigit():
            index += 1
            while index < len(src) and (src[index].isdigit() or src[index] == "_"):
                index += 1
        if index < len(src) and src[index] in "eE":
            probe = index + 1
            if probe < len(src) and src[probe] in "+-":
                probe += 1
            if probe < len(src) and src[probe].isdigit():
                index = probe
                while index < len(src) and src[index].isdigit():
                    index += 1
        while index < len(src) and src[index] in "fFlLuUdDn":
            index += 1
        self._emit(NUMBER, self.pos, index)

    def _punct(self) -> None:
        for symbol in _MULTI_PUNCT:
            if self.src.startswith(symbol, self.pos):
                self._emit(PUNCT, self.pos, self.pos + len(symbol))
                return
        self._emit(PUNCT, self.pos, self.pos + 1)

    # --- strings -------------------------------------------------------------
    def _string(self, quote: str) -> None:
        start = self.pos
        src = self.src
        lang = self.lang
        if quote == "`":
            if lang != "ts":
                self._emit(PUNCT, start, start + 1)
                return
            parts, end = self._template_literal(start)
            self._emit(STRING, start, end, parts=parts)
            return
        if quote == "'":
            if lang == "ts":
                parts, end = self._c_string(start)
                self._emit(STRING, start, end, parts=parts)
            else:
                # A character literal in every other family; a lone quote (a
                # Swift/Kotlin label like `'` never occurs) falls back to punct.
                char_end = self._char_literal(start)
                if char_end is None:
                    self._emit(PUNCT, start, start + 1)
                else:
                    self._emit(NUMBER, start, char_end)
            return
        if src.startswith('"""', start) and lang in ("swift", "kotlin", "java"):
            parts, end = self._multiline_string(start)
            self._emit(STRING, start, end, parts=parts)
            return
        if lang == "swift":
            parts, end = self._swift_string(start, hashes=0)
        elif lang == "kotlin":
            parts, end = self._kotlin_string(start)
        else:
            parts, end = self._c_string(start)
        self._emit(STRING, start, end, parts=parts)

    def _char_literal(self, start: int) -> int | None:
        src = self.src
        index = start + 1
        if index < len(src) and src[index] == "\\":
            index += 2
            while index < len(src) and src[index] != "'" and index - start < 12:
                index += 1
        else:
            index += 1
        if index < len(src) and src[index] == "'":
            return index + 1
        return None

    def _c_string(self, start: int) -> tuple[tuple[str | Interp, ...], int]:
        """``"…"`` / ``'…'`` with C escapes and no interpolation.

        An unterminated literal ends at the newline.
        """
        src = self.src
        quote = src[start]
        out: list[str] = []
        index = start + 1
        while index < len(src):
            char = src[index]
            if char == quote:
                return ("".join(out),), index + 1
            if char == "\n":
                break
            if char == "\\":
                value, index = _escape(src, index)
                out.append(value)
                continue
            out.append(char)
            index += 1
        return ("".join(out),), index

    def _swift_string(self, start: int, *, hashes: int) -> tuple[tuple[str | Interp, ...], int]:
        src = self.src
        closing = '"' + "#" * hashes
        escape = "\\" + "#" * hashes
        parts: list[str | Interp] = []
        buffer: list[str] = []
        index = start + hashes + 1
        while index < len(src):
            if src.startswith(closing, index):
                _flush(parts, buffer)
                return tuple(parts), index + len(closing)
            if src[index] == "\n":
                break
            if src.startswith(escape, index):
                after = index + len(escape)
                if src.startswith("(", after):
                    _flush(parts, buffer)
                    expr, index = self._balanced(after, "(", ")")
                    parts.append(Interp(expr))
                    continue
                if hashes == 0:
                    value, index = _escape(src, index)
                    buffer.append(value)
                    continue
                value, index = _escape(src, after - 1)
                buffer.append(value)
                continue
            buffer.append(src[index])
            index += 1
        _flush(parts, buffer)
        return tuple(parts), index

    def _swift_raw_string(self) -> bool:
        src = self.src
        index = self.pos
        hashes = 0
        while index < len(src) and src[index] == "#":
            hashes += 1
            index += 1
        if not src.startswith('"', index):
            return False
        start = self.pos
        if src.startswith('"""', index):
            parts, end = self._multiline_string(index, hashes=hashes)
        else:
            parts, end = self._swift_string(start, hashes=hashes)
        self._emit(STRING, start, end, parts=parts)
        return True

    def _kotlin_string(self, start: int) -> tuple[tuple[str | Interp, ...], int]:
        src = self.src
        parts: list[str | Interp] = []
        buffer: list[str] = []
        index = start + 1
        while index < len(src):
            char = src[index]
            if char == '"':
                _flush(parts, buffer)
                return tuple(parts), index + 1
            if char == "\n":
                break
            if char == "\\":
                value, index = _escape(src, index)
                buffer.append(value)
                continue
            if char == "$":
                interp, index = self._kotlin_template(index)
                if interp is not None:
                    _flush(parts, buffer)
                    parts.append(interp)
                    continue
                buffer.append("$")
                continue
            buffer.append(char)
            index += 1
        _flush(parts, buffer)
        return tuple(parts), index

    def _kotlin_template(self, index: int) -> tuple[Interp | None, int]:
        src = self.src
        after = index + 1
        if src.startswith("{", after):
            expr, end = self._balanced(after, "{", "}")
            return Interp(expr), end
        if after < len(src) and (src[after].isalpha() or src[after] == "_"):
            end = after + 1
            while end < len(src) and (src[end].isalnum() or src[end] == "_"):
                end += 1
            return Interp(src[after:end]), end
        return None, index + 1

    def _multiline_string(
        self, start: int, *, hashes: int = 0
    ) -> tuple[tuple[str | Interp, ...], int]:
        """``\"\"\"…\"\"\"`` in Swift, Kotlin and Java; escapes/interpolation per language."""
        src = self.src
        closing = '"""' + "#" * hashes
        parts: list[str | Interp] = []
        buffer: list[str] = []
        index = start + 3
        while index < len(src):
            if src.startswith(closing, index):
                _flush(parts, buffer)
                return tuple(parts), index + len(closing)
            char = src[index]
            if self.lang == "kotlin":
                if char == "$":
                    interp, index = self._kotlin_template(index)
                    if interp is not None:
                        _flush(parts, buffer)
                        parts.append(interp)
                    else:
                        buffer.append("$")
                    continue
            elif self.lang == "swift":
                escape = "\\" + "#" * hashes
                if src.startswith(escape, index):
                    after = index + len(escape)
                    if src.startswith("(", after):
                        _flush(parts, buffer)
                        expr, index = self._balanced(after, "(", ")")
                        parts.append(Interp(expr))
                        continue
                    if hashes == 0:
                        value, index = _escape(src, index)
                        buffer.append(value)
                        continue
            elif char == "\\":
                value, index = _escape(src, index)
                buffer.append(value)
                continue
            buffer.append(char)
            index += 1
        _flush(parts, buffer)
        return tuple(parts), index

    def _template_literal(self, start: int) -> tuple[tuple[str | Interp, ...], int]:
        src = self.src
        parts: list[str | Interp] = []
        buffer: list[str] = []
        index = start + 1
        while index < len(src):
            char = src[index]
            if char == "`":
                _flush(parts, buffer)
                return tuple(parts), index + 1
            if char == "\\":
                value, index = _escape(src, index)
                buffer.append(value)
                continue
            if src.startswith("${", index):
                _flush(parts, buffer)
                expr, index = self._balanced(index + 1, "{", "}")
                parts.append(Interp(expr))
                continue
            buffer.append(char)
            index += 1
        _flush(parts, buffer)
        return tuple(parts), index

    def _balanced(self, open_index: int, opener: str, closer: str) -> tuple[str, int]:
        """The source between ``opener`` at ``open_index`` and its match, skipping nested strings.

        Returns the inner text and the index after the closer. Strings inside
        are re-lexed with this language's rules so a ``)`` in ``"\\(a(")")"``
        does not close the interpolation early.
        """
        src = self.src
        depth = 0
        index = open_index
        while index < len(src):
            char = src[index]
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return src[open_index + 1 : index], index + 1
            elif char in "\"'`":
                nested = _Lexer(src, self.lang)
                nested.pos = index
                nested._string(char)
                index = max(nested.pos, index + 1)
                continue
            elif char == "@" and self.lang == "objc" and src.startswith('"', index + 1):
                _, index = self._c_string(index + 1)
                continue
            index += 1
        return src[open_index + 1 :], len(src)

    # --- regex literals (JS/TS) ------------------------------------------------
    def _regex_allowed(self) -> bool:
        if self.src.startswith(("//", "/*"), self.pos):
            return False
        if not self.tokens:
            return True
        last = self.tokens[-1]
        if last.kind == PUNCT:
            return last.text[-1] in _REGEX_AFTER_PUNCT and last.text not in (")", "]", "}")
        if last.kind == IDENT:
            return last.text in _REGEX_AFTER_WORD
        return False

    def _regex(self) -> None:
        src = self.src
        start = self.pos
        index = start + 1
        in_class = False
        while index < len(src) and src[index] != "\n":
            char = src[index]
            if char == "\\":
                index += 2
                continue
            if char == "[":
                in_class = True
            elif char == "]":
                in_class = False
            elif char == "/" and not in_class:
                index += 1
                while index < len(src) and src[index].isalpha():
                    index += 1
                self._emit(PUNCT, start, index, text="/regex/")
                return
            index += 1
        # Not a regex after all (no closing slash on the line): a division.
        self._emit(PUNCT, start, start + 1)


def _flush(parts: list[str | Interp], buffer: list[str]) -> None:
    if buffer:
        parts.append("".join(buffer))
        buffer.clear()


_SIMPLE_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "0": "\0",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "\\": "\\",
    '"': '"',
    "'": "'",
    "`": "`",
    "$": "$",
}


def _escape(src: str, index: int) -> tuple[str, int]:
    """Decode the escape at ``src[index] == '\\'``; returns the text and the index after it."""
    code = src[index + 1 : index + 2]
    if code in _SIMPLE_ESCAPES:
        return _SIMPLE_ESCAPES[code], index + 2
    if code == "u":
        if src.startswith("{", index + 2):
            end = src.find("}", index + 3)
            char = _hex_char(src[index + 3 : end]) if end > 0 else None
            if char is not None:
                return char, end + 1
        char = _hex_char(src[index + 2 : index + 6], width=4)
        if char is not None:
            return char, index + 6
    if code == "x":
        char = _hex_char(src[index + 2 : index + 4], width=2)
        if char is not None:
            return char, index + 4
    if code == "\n":
        return "", index + 2
    return code, index + 2


def _hex_char(digits: str, *, width: int | None = None) -> str | None:
    """``digits`` as a code point, or None when they are not hex (or not ``width`` long)."""
    if not digits or (width is not None and len(digits) != width):
        return None
    if any(char not in "0123456789abcdefABCDEF" for char in digits):
        return None
    value = int(digits, 16)
    return chr(value) if value <= 0x10FFFF else None
