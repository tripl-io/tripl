"""Spelling the forwarding call: the generated adapter calling the team's own wrapper.

The wrapper's argument mapping comes from ``.tripl/check.yml`` — the same
``args``/``positional``/``object_arg`` vocabulary ``tripl check`` reads calls
with — so one description of the wrapper serves both directions: checking
calls that exist and generating calls that will.

Per language, an argument is spelled the way the wrapper would be called:

* Swift — positional arguments unlabelled, the rest ``label: value``;
* Kotlin — positional, then named ``label = value``;
* TypeScript — positional only; with ``object_arg`` the labelled arguments
  become one object literal at that position (``{category, action}``), dotted
  labels nesting (``event.schema`` -> ``{event: {schema: …}}``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from tripl_cli.check.config import NAME, NAME_IGLU, PROPERTIES, VALUE_NUMBER, Transport
from tripl_cli.codegen.languages import Dialect, comment, ts_string
from tripl_cli.codegen.naming import KOTLIN, SWIFT, KeyedNames
from tripl_cli.errors import TriplConfigError

_CALLEE = re.compile(r"[A-Za-z_$][\w$]*(?:\(\))?(?:\.[A-Za-z_$][\w$]*(?:\(\))?)*")
_TS_KEY = re.compile(r"[A-Za-z_$][\w$]*")


@dataclass(frozen=True)
class Arg:
    """One argument: its label (``None`` when positional) and plan target."""

    label: str | None
    target: str | None


def args_of(transport: Transport | None, default: list[Arg]) -> tuple[list[Arg], int | None]:
    """The wrapper's arguments in call order, and its ``object_arg`` position."""
    if transport is None or not transport.mapped:
        return default, None
    args = [Arg(None, target) for target in transport.positional]
    args.extend(Arg(label, target) for label, target in transport.labelled)
    return args, transport.object_arg


def callee(transport: Transport, where: str) -> str:
    function = transport.function.strip()
    if not _CALLEE.fullmatch(function):
        raise TriplConfigError(
            f"{where}: {function!r} is not a function to call, e.g. Analytics.shared.log."
        )
    return function


def function_name(transport: Transport | None, fallback: str) -> str:
    """The generated function mirrors the wrapper's own method name: ``log`` for ``….log``."""
    if transport is None:
        return fallback
    last = re.sub(r"\(\)$", "", transport.function.strip().rsplit(".", 1)[-1])
    return last or fallback


def render_call(
    dialect: Dialect,
    function: str,
    arguments: list[tuple[str | None, str]],
    object_arg: int | None,
) -> str:
    """``function(arg, label: value, …)`` in the dialect's calling convention."""
    positional = [expression for label, expression in arguments if label is None]
    labelled = [(label, expression) for label, expression in arguments if label is not None]
    if dialect.name == SWIFT:
        parts = positional + [f"{label}: {expression}" for label, expression in labelled]
    elif dialect.name == KOTLIN:
        parts = positional + [f"{label} = {expression}" for label, expression in labelled]
    elif object_arg is not None:
        parts = list(positional)
        literal = _ts_object(labelled)
        index = min(object_arg, len(parts))
        parts.insert(index, literal)
    else:
        parts = positional + [expression for _, expression in labelled]
    return f"{function}({', '.join(parts)})"


def _ts_object(labelled: list[tuple[str, str]]) -> str:
    tree: dict[str, Any] = {}
    for label, expression in labelled:
        node = tree
        *parents, leaf = label.split(".")
        for part in parents:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[leaf] = expression
    return _ts_render(tree)


def _ts_render(tree: dict[str, Any]) -> str:
    if not tree:
        return "{}"
    items = []
    for key, value in tree.items():
        spelled = key if _TS_KEY.fullmatch(key) else ts_string(key)
        rendered = _ts_render(value) if isinstance(value, dict) else value
        items.append(f"{spelled}: {rendered}")
    return "{ " + ", ".join(items) + " }"


# --- the ``transport`` object of a template context ------------------------------------
ROLES = {NAME: "name", NAME_IGLU: "name_iglu", PROPERTIES: "properties", VALUE_NUMBER: "value"}


def role_of(plan_target: str | None) -> str:
    """What an argument carries: ``field``, ``name``, ``name_iglu``, ``properties``,
    ``value`` (the numeric ``value:number``) or ``none`` (a ``null`` target)."""
    if plan_target is None:
        return "none"
    return ROLES.get(plan_target, "field")


@dataclass(frozen=True)
class ArgRow:
    """One forwarded argument, resolved: what a template needs to build its own call."""

    label: str | None
    plan_target: str | None
    param: str | None
    expression: str


def transport_context(
    transport: Transport | None, rows: list[ArgRow], object_arg: int | None, names: KeyedNames
) -> dict[str, Any] | None:
    """``transport``: the configured wrapper call and its arguments in call order,
    so a custom template can spell the call (or a constructor) itself.

    A key with no value for an argument (``label`` of a positional one, ``param``
    of one no parameter feeds, ``target``/``names`` of one that is not a field) is
    LEFT OUT rather than ``None``: ``{{param}}`` on it is then an "unknown value"
    error instead of an empty hole in the code, and ``{{#param}}``/``{{^label}}``
    still work as guards. A field's ``names`` are the ones ``plan.fields`` gives it."""
    if transport is None:
        return None
    args = []
    for number, row in enumerate(rows):
        role = role_of(row.plan_target)
        last = number == len(rows) - 1
        arg: dict[str, Any] = {
            "positional": row.label is None,
            "role": role,
            "is_field": role == "field",
            "is_name": role in ("name", "name_iglu"),
            "is_properties": role == "properties",
            "is_value": role == "value",
            "is_none": role == "none",
            "expression": row.expression,
            "last": last,
            "comma": "" if last else ",",
        }
        if row.label is not None:
            arg["label"] = row.label
        if role == "field" and row.plan_target:
            arg["target"] = comment(row.plan_target)
            arg["names"] = names.of(row.plan_target)
        if row.param is not None:
            arg["param"] = row.param
        args.append(arg)
    context: dict[str, Any] = {
        "function": transport.function.strip(),
        "args": args,
        "has_args": bool(args),
    }
    if transport.import_line:
        context["import"] = transport.import_line
    if transport.object_arg is not None:
        context["object_arg"] = transport.object_arg
    return context
