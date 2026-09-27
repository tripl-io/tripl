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

from tripl_cli.check.config import Transport
from tripl_cli.codegen.languages import Dialect, ts_string
from tripl_cli.codegen.naming import KOTLIN, SWIFT
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
