"""Value types of generated parameters: a plan enum, free text, or a boolean.

``EnumRegistry`` hands out one enum per distinct (name, values) pair within a
file, so two fields backed by the same variable share a type, and two
different value sets that want the same name get ``Name`` and ``Name2``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from tripl_cli.codegen import naming
from tripl_cli.codegen.languages import Dialect, comment
from tripl_cli.codegen.naming import KOTLIN, Namer

KIND_ENUM = "enum"
KIND_STRING = "string"
KIND_BOOL = "bool"
KIND_NUMBER = "number"


@dataclass(frozen=True)
class EnumCase:
    ident: str
    raw: str


@dataclass(frozen=True)
class EnumDef:
    name: str
    cases: tuple[EnumCase, ...]

    def case_for(self, raw: str) -> EnumCase | None:
        return next((case for case in self.cases if case.raw == raw), None)

    def context(self, dialect: Dialect) -> dict[str, Any]:
        cases = []
        names = naming.NameScope(dialect.name)
        for number, case in enumerate(self.cases):
            last = number == len(self.cases) - 1
            cases.append(
                {
                    "ident": case.ident,
                    "raw": comment(case.raw),
                    "literal": dialect.literal(case.raw),
                    # Kotlin enum entries are separated by `,` and closed by `;`.
                    "sep": ";" if last else ",",
                    "last": last,
                    "names": names.take(case.raw),
                }
            )
        return {"name": self.name, "cases": cases}


@dataclass(frozen=True)
class ValueType:
    kind: str
    enum: EnumDef | None = None

    def type_name(self, dialect: Dialect) -> str:
        if self.kind == KIND_ENUM and self.enum is not None:
            return self.enum.name
        if self.kind == KIND_BOOL:
            return dialect.bool_type
        if self.kind == KIND_NUMBER:
            return dialect.number_type
        return dialect.string_type

    def text(self, dialect: Dialect, expression: str, *, optional: bool) -> str:
        """The plan STRING the value stands for (optional in, optional out)."""
        if self.kind == KIND_ENUM:
            return dialect.enum_raw(expression, optional=optional)
        if self.kind in (KIND_BOOL, KIND_NUMBER):
            return dialect.scalar_text(expression, optional=optional)
        return expression

    def value(self, dialect: Dialect, expression: str) -> str:
        """What goes into a properties map: the raw string for an enum, else as is."""
        if self.kind == KIND_ENUM:
            return dialect.enum_raw(expression, optional=False)
        return expression

    def literal(self, dialect: Dialect, raw: str) -> str | None:
        """A plan value as a constant of this type, or ``None`` when it is not one."""
        if self.kind == KIND_ENUM and self.enum is not None:
            case = self.enum.case_for(raw)
            return dialect.case_ref(self.enum.name, case.ident) if case is not None else None
        if self.kind == KIND_BOOL:
            return raw if raw in ("true", "false") else None
        if self.kind == KIND_NUMBER:
            try:
                number = float(raw)
            except ValueError:
                return None
            # Always a floating literal: the generated type is Double / number.
            return repr(number) if number == number and abs(number) != float("inf") else None
        return dialect.literal(raw)


STRING = ValueType(KIND_STRING)
BOOL = ValueType(KIND_BOOL)
NUMBER = ValueType(KIND_NUMBER)


def scalar_type(field_type: str) -> ValueType:
    """A field with no closed list of values, typed by the plan's field type."""
    if field_type == "boolean":
        return BOOL
    if field_type == "number":
        return NUMBER
    return STRING


class EnumRegistry:
    """The enums of one generated file, in the order they were first asked for."""

    def __init__(self, dialect: Dialect, namer: Namer) -> None:
        self.dialect = dialect
        self._namer = namer
        self._by_key: dict[tuple[str, tuple[str, ...]], EnumDef] = {}
        self.enums: list[EnumDef] = []

    def enum(self, base: str, values: Iterable[str]) -> ValueType:
        ordered = tuple(sorted(set(values)))
        key = (base, ordered)
        found = self._by_key.get(key)
        if found is None:
            name = self._namer.take(base)
            cases = Namer()
            snake = self.dialect.name == KOTLIN
            found = EnumDef(
                name=name,
                cases=tuple(
                    EnumCase(cases.take(naming.member(raw, self.dialect.name, snake=snake)), raw)
                    for raw in ordered
                ),
            )
            self._by_key[key] = found
            self.enums.append(found)
        return ValueType(KIND_ENUM, found)

    def context(self) -> list[dict[str, Any]]:
        return [enum.context(self.dialect) for enum in self.enums]
