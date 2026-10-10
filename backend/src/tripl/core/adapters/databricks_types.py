"""Databricks SQL (Spark) type strings, parsed far enough to address nested fields.

``DESCRIBE QUERY`` reports each column's full type as Spark DDL text —
``struct<user:struct<id:bigint>,tags:array<string>>``, ``map<string,string>``,
``variant``, ``decimal(10,2)`` — which is the only place a STRUCT's declared
fields are visible: the driver's cursor description says just ``struct``.

The parser understands exactly what the adapter needs: the container kind
(struct / array / map / scalar), a struct's field names in declaration order
(backquoted names included, with ````` `` ````` as the escaped backtick), and
enough nesting to walk to the leaves. Field modifiers Spark may print after a
field's type (``NOT NULL``, ``COMMENT '...'``) are skipped. Anything it does
not recognize is a scalar, which is the safe reading: a scalar is never
path-expanded.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SparkType:
    """One parsed type: its kind, a struct's fields, an array/map's element."""

    kind: str  # "struct" | "array" | "map" | "scalar"
    name: str
    fields: tuple[tuple[str, SparkType], ...] = field(default=())
    element: SparkType | None = None


class _Parser:
    def __init__(self, text: str) -> None:
        self._text = text
        self._pos = 0

    def _peek(self) -> str:
        return self._text[self._pos] if self._pos < len(self._text) else ""

    def _skip_space(self) -> None:
        while self._peek().isspace():
            self._pos += 1

    def _expect(self, char: str) -> None:
        self._skip_space()
        if self._peek() != char:
            raise ValueError(f"expected {char!r} at {self._pos} in {self._text!r}")
        self._pos += 1

    def _word(self) -> str:
        self._skip_space()
        start = self._pos
        while self._peek() and (self._peek().isalnum() or self._peek() == "_"):
            self._pos += 1
        return self._text[start : self._pos]

    def _field_name(self) -> str:
        self._skip_space()
        if self._peek() != "`":
            name = []
            while self._peek() and self._peek() not in ":<>,":
                name.append(self._peek())
                self._pos += 1
            return "".join(name).strip()
        self._pos += 1
        chars: list[str] = []
        while self._pos < len(self._text):
            char = self._text[self._pos]
            if char == "`":
                if self._text[self._pos + 1 : self._pos + 2] == "`":
                    chars.append("`")
                    self._pos += 2
                    continue
                self._pos += 1
                return "".join(chars)
            chars.append(char)
            self._pos += 1
        raise ValueError(f"unterminated field name in {self._text!r}")

    def _skip_modifiers(self) -> None:
        """Skip ``NOT NULL`` / ``COMMENT '...'`` up to the next ``,`` or ``>``."""
        depth = 0
        quote = ""
        while self._pos < len(self._text):
            char = self._text[self._pos]
            if quote:
                if char == "\\":
                    self._pos += 2
                    continue
                if char == quote:
                    quote = ""
            elif char in "'\"":
                quote = char
            elif char in "<(":
                depth += 1
            elif char in ">)":
                if depth == 0:
                    return
                depth -= 1
            elif char == "," and depth == 0:
                return
            self._pos += 1

    def parse(self) -> SparkType:
        parsed = self._type()
        self._skip_space()
        return parsed

    def _type(self) -> SparkType:
        word = self._word().lower()
        self._skip_space()
        if word == "struct" and self._peek() == "<":
            self._pos += 1
            fields: list[tuple[str, SparkType]] = []
            self._skip_space()
            if self._peek() == ">":
                self._pos += 1
                return SparkType("struct", "struct", ())
            while True:
                name = self._field_name()
                self._expect(":")
                fields.append((name, self._type()))
                self._skip_modifiers()
                self._skip_space()
                if self._peek() == ",":
                    self._pos += 1
                    continue
                self._expect(">")
                return SparkType("struct", "struct", tuple(fields))
        if word == "array" and self._peek() == "<":
            self._pos += 1
            element = self._type()
            self._expect(">")
            return SparkType("array", "array", element=element)
        if word == "map" and self._peek() == "<":
            self._pos += 1
            self._type()  # the key type: only the value type is ever addressed
            self._expect(",")
            value = self._type()
            self._expect(">")
            return SparkType("map", "map", element=value)
        if self._peek() == "(":
            # decimal(10,2), varchar(20): parameters carry no nesting.
            depth = 0
            while self._pos < len(self._text):
                char = self._text[self._pos]
                self._pos += 1
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        break
        return SparkType("scalar", word)


def parse_spark_type(type_name: str) -> SparkType:
    """Parse one ``DESCRIBE QUERY`` type; an unparseable one is an opaque scalar."""
    try:
        return _Parser(type_name).parse()
    except ValueError, IndexError:
        return SparkType("scalar", type_name.strip().lower())


def _walk(spark_type: SparkType, prefix: str, *, blocked: bool, out: dict[str, bool]) -> None:
    for name, sub in spark_type.fields:
        path = f"{prefix}{name}"
        if sub.kind == "struct" and sub.fields:
            _walk(sub, f"{path}.", blocked=blocked, out=out)
            continue
        if sub.kind == "array" and sub.element is not None and sub.element.kind == "struct":
            # ``array<struct<...>>``: its fields exist per element and are only
            # reachable through explode / transform, which this adapter does not
            # generate. Enumerated so they stay visible, flagged unaddressable.
            _walk(sub.element, f"{path}.", blocked=True, out=out)
            continue
        out[path] = not blocked


def declared_struct_paths(type_name: str) -> dict[str, bool]:
    """The dotted leaf paths a STRUCT type declares, path -> addressable.

    The same shape ``bigquery._declared_struct_paths`` returns: a leaf under an
    ARRAY of structs is listed but cannot be addressed with dotted field access.
    """
    parsed = parse_spark_type(type_name)
    if parsed.kind != "struct":
        return {}
    paths: dict[str, bool] = {}
    _walk(parsed, "", blocked=False, out=paths)
    return dict(sorted(paths.items()))


def is_ntz_type(type_name: str) -> bool:
    """``timestamp_ntz``: a zone-less wall clock, compared against a zone-less literal."""
    return type_name.strip().lower().startswith("timestamp_ntz")
