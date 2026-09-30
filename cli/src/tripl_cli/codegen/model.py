"""The ``codegen_model`` export, read into typed values.

Shape (``GET /projects/{slug}/plan/export?format=codegen_model``)::

    {revision, branch, plan_hash,
     event_types: [{name, display_name, name_rule,
                    fields: [{name, required, type, values: [..] | null, variable}],
                    events: [{identity, name, status, field_values: {field: value},
                              deprecated, overrides: {token: [allowed values]}}]}],
     variables: [{name, allowed_values, tokens, variable_type}]}

The token grammars match the backend's exactly (``tripl.core.name_template``):
a plan token is ``${…}`` with anything but ``}`` inside (``${}`` included, so a
stray one is seen rather than silently kept as text); a name-rule key is
``{…}`` with at least one character, never the ``{…}`` of a ``${…}`` token.

Read defensively, like every response in this package: a member of the wrong
type reads as absent rather than raising, so an older or newer server degrades
to less typed output instead of a traceback. Archived events are excluded by
the server; any that arrive anyway are dropped here too, because generating a
typed call for an event the plan has retired is the opposite of the point.

Everything is SORTED here — event types by name, fields in name-rule order
then by name, events by identity, values by value — so the generated output is
a function of the plan's content alone, never of the order a query returned it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tripl_cli.model import as_dict, as_list, text_of

ARCHIVED = "archived"
TOKEN = re.compile(r"\$\{([^}]*)\}")
# A token is matched FIRST, so its braces never read as a rule key; group 1 is
# set only for a rule key.
RULE_KEY_OR_TOKEN = re.compile(r"\$\{[^}]*\}|\{([^}]+)\}")


@dataclass(frozen=True)
class FieldModel:
    name: str
    required: bool = False
    type: str = "string"
    values: tuple[str, ...] | None = None
    variable: str | None = None


@dataclass(frozen=True)
class EventModel:
    identity: str
    name: str
    status: str | None = None
    field_values: Mapping[str, str] | None = None
    deprecated: bool = False
    # Per-event allowed values of a token, overriding the variable's own; an
    # empty tuple means the event leaves the token free.
    overrides: Mapping[str, tuple[str, ...]] | None = None

    def value_of(self, field: str) -> str | None:
        return (self.field_values or {}).get(field)


@dataclass(frozen=True)
class EventTypeModel:
    name: str
    display_name: str | None = None
    name_rule: str | None = None
    fields: tuple[FieldModel, ...] = ()
    events: tuple[EventModel, ...] = ()

    @property
    def rule_keys(self) -> tuple[str, ...]:
        """The fields the name rule is built from, in rule order: ``{a}:{b}`` -> ``(a, b)``."""
        if not self.name_rule:
            return ()
        keys: list[str] = []
        for key in rule_keys(self.name_rule):
            if key not in keys:
                keys.append(key)
        return tuple(keys)

    def field(self, name: str) -> FieldModel | None:
        return next((item for item in self.fields if item.name == name), None)


@dataclass(frozen=True)
class CodegenModel:
    revision: str | None = None
    branch: str | None = None
    plan_hash: str | None = None
    event_types: tuple[EventTypeModel, ...] = ()
    variables: Mapping[str, tuple[str, ...]] | None = None
    # Token -> the variable's ``variable_type`` (F23); absent from an older
    # server, where every token reads as text.
    variable_types: Mapping[str, str] | None = None

    def event_type(self, name: str) -> EventTypeModel | None:
        return next((item for item in self.event_types if item.name == name), None)

    def allowed(self, variable: str) -> tuple[str, ...] | None:
        """A variable's allowed values, or ``None`` when it has none (free text)."""
        values = (self.variables or {}).get(variable)
        return values or None

    def allowed_for(self, event: EventModel, token: str) -> tuple[str, ...] | None:
        """A token's allowed values for ``event``: its own override first, then the variable's."""
        overrides = event.overrides or {}
        if token in overrides:
            return overrides[token] or None
        return self.allowed(token)

    def type_of(self, token: str) -> str:
        """The ``variable_type`` behind a token; ``string`` when unknown."""
        return (self.variable_types or {}).get(token) or "string"


def rule_keys(rule: str) -> list[str]:
    """``'{a}:${t}:{b}'`` -> ``['a', 'b']``, in order, repeats kept."""
    return [match.group(1) for match in RULE_KEY_OR_TOKEN.finditer(rule) if match.group(1)]


def tokens(text: str) -> list[str]:
    """``'promo_${id}_shown'`` -> ``['id']``, first occurrence order, no repeats."""
    found: list[str] = []
    for token in TOKEN.findall(text):
        if token not in found:
            found.append(token)
    return found


def whole_token(text: str) -> str | None:
    """The variable name when ``text`` is exactly one ``${token}`` and nothing else."""
    match = TOKEN.fullmatch(text)
    return match.group(1) if match is not None else None


def _scalar(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return value if isinstance(value, str) else None


def _values(raw: Any) -> tuple[str, ...] | None:
    if not isinstance(raw, list):
        return None
    found = {text for item in raw if (text := _scalar(item)) is not None}
    return tuple(sorted(found))


def _overrides(raw: Any) -> dict[str, tuple[str, ...]]:
    """``{token: [values]}`` or ``[{token|name, allowed_values|values}]``; else nothing."""
    found: dict[str, tuple[str, ...]] = {}
    items: list[tuple[Any, Any]]
    if isinstance(raw, Mapping):
        items = list(raw.items())
    else:
        items = [
            (
                text_of(body, "token") or text_of(body, "name"),
                body.get("allowed_values", body.get("values")),
            )
            for body in as_list(raw)
        ]
    for token, values in items:
        if not isinstance(token, str):
            continue
        parsed = _values(values)
        if parsed is not None:
            found[token] = parsed
    return dict(sorted(found.items()))


def _plan_hash(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def _revision(raw: Any) -> str | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int | float | str):
        return str(raw) or None
    body = as_dict(raw)
    return text_of(body, "id") or _scalar(body.get("number"))


def parse_field(raw: Mapping[str, Any]) -> FieldModel | None:
    name = text_of(raw, "name")
    if name is None:
        return None
    return FieldModel(
        name=name,
        required=raw.get("required") is True,
        type=text_of(raw, "type") or "string",
        values=_values(raw.get("values")),
        variable=text_of(raw, "variable"),
    )


def parse_event(raw: Mapping[str, Any]) -> EventModel | None:
    identity = text_of(raw, "identity") or text_of(raw, "name")
    if identity is None or text_of(raw, "status") == ARCHIVED:
        return None
    values = {
        str(key): text
        for key, value in as_dict(raw.get("field_values")).items()
        if (text := _scalar(value)) is not None
    }
    return EventModel(
        identity=identity,
        name=text_of(raw, "name") or identity,
        status=text_of(raw, "status"),
        field_values=dict(sorted(values.items())),
        deprecated=raw.get("deprecated") is True,
        overrides=_overrides(raw.get("overrides")),
    )


def parse_event_type(raw: Mapping[str, Any]) -> EventTypeModel | None:
    name = text_of(raw, "name")
    if name is None:
        return None
    fields = [field for body in as_list(raw.get("fields")) if (field := parse_field(body))]
    events = [event for body in as_list(raw.get("events")) if (event := parse_event(body))]
    shell = EventTypeModel(name=name, name_rule=text_of(raw, "name_rule"))
    order = {key: index for index, key in enumerate(shell.rule_keys)}
    fields.sort(key=lambda item: (order.get(item.name, len(order)), item.name))
    unique_events: dict[str, EventModel] = {}
    for event in sorted(events, key=lambda item: item.identity):
        unique_events.setdefault(event.identity, event)
    return EventTypeModel(
        name=name,
        display_name=text_of(raw, "display_name"),
        name_rule=shell.name_rule,
        fields=tuple(dict((item.name, item) for item in fields).values()),
        events=tuple(unique_events.values()),
    )


def parse_model(payload: Any) -> CodegenModel:
    body = as_dict(payload)
    types = [item for raw in as_list(body.get("event_types")) if (item := parse_event_type(raw))]
    variables: dict[str, tuple[str, ...]] = {}
    aliases: dict[str, tuple[str, ...]] = {}
    variable_types: dict[str, str] = {}
    for raw in as_list(body.get("variables")):
        name = text_of(raw, "name")
        if name is None:
            continue
        allowed = _values(raw.get("allowed_values")) or ()
        variables[name] = allowed
        variable_type = text_of(raw, "variable_type")
        if variable_type:
            variable_types[name] = variable_type
        # Every `${token}` spelling that resolves to the variable (its source
        # name, bindings) reads the same allowed values; its own name wins.
        for token in raw.get("tokens") or ():
            if isinstance(token, str) and token:
                aliases.setdefault(token, allowed)
                if variable_type:
                    variable_types.setdefault(token, variable_type)
    for token, allowed in aliases.items():
        variables.setdefault(token, allowed)
    branch = body.get("branch")
    return CodegenModel(
        revision=_revision(body.get("revision")),
        plan_hash=_plan_hash(body.get("plan_hash")),
        branch=(branch if isinstance(branch, str) and branch else text_of(as_dict(branch), "name")),
        event_types=tuple(sorted(types, key=lambda item: item.name)),
        variables=dict(sorted(variables.items())),
        variable_types=dict(sorted(variable_types.items())),
    )
