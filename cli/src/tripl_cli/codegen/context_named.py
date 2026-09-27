"""The template context for the event-value styles: ``named`` and ``self_describing``.

``named`` (Segment/Amplitude style: a flat name plus properties)
    ONE generic ``track(event)`` taking a typed value, never a function per
    event. Swift: ``enum LegacyEvent { case homeScreenView(HomeScreenView) … }``
    with ``name`` and ``properties``; Kotlin: a sealed interface with a data
    class (or object) per event; TypeScript: a name-keyed props map and
    ``track<K extends LegacyEventName>(name: K, props: LegacyEventProps[K])``.
``self_describing`` (schema-based, Snowplow style)
    One data class per schema with its fields, sharing one ``track``.

An event's parameters are the type's fields it does NOT fix, plus one
parameter per ``${token}`` in its identity or in a fixed field value
(``promo_sheet_${id}_shown`` takes ``id``). A token backed by a variable with
allowed values is typed by them — the event's own override of the token's
values first, then the variable's. Fixed values are filled in by the generated
code, so a call site can only pass what the plan leaves open; a fixed value of
a boolean or number field is emitted as a typed literal (``true``, ``2.0``),
not as the plan's string for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from tripl_cli.check.config import NAME, NAME_IGLU, PROPERTIES, STYLE_SELF_DESCRIBING
from tripl_cli.codegen import naming
from tripl_cli.codegen.calls import Arg, args_of, callee, render_call
from tripl_cli.codegen.context import (
    DESTINATION_FUNCTION,
    SHARED_TYPES,
    Target,
    base_context,
    field_target,
    interpolation,
)
from tripl_cli.codegen.languages import Dialect, comment
from tripl_cli.codegen.model import TOKEN, EventModel, FieldModel, tokens
from tripl_cli.codegen.naming import KOTLIN, SWIFT, TS, Namer
from tripl_cli.codegen.value_types import STRING, EnumRegistry, ValueType, scalar_type

_IGLU = re.compile(r"iglu:([^/]+)/([^/]+)/([^/]+)/([0-9]+-[0-9]+-[0-9]+)")
_RESERVED_PROPS = frozenset({"name", "properties", "schema", "data", "self", "this"})


@dataclass(frozen=True)
class _Prop:
    ident: str
    key: str | None  # the plan field it fills; None for a token-only parameter
    token: str | None
    value_type: ValueType
    optional: bool


def _field_type(
    target: Target, field: FieldModel, dialect: Dialect, registry: EnumRegistry
) -> ValueType:
    if field.values:
        base = target.type_override(field.name) or naming.type_name(field.name, dialect.name)
        return registry.enum(base, field.values)
    return scalar_type(field.type)


def _token_type(
    target: Target, event: EventModel, token: str, dialect: Dialect, registry: EnumRegistry
) -> ValueType:
    allowed = target.model.allowed_for(event, token)
    if not allowed:
        return STRING
    base = target.type_override(token) or naming.type_name(token, dialect.name)
    return registry.enum(base, allowed)


def _schema_template(target: Target, event: EventModel) -> str:
    template = target.codegen.schema
    if template is None:
        return event.identity
    return template.replace("{name}", event.identity)


def _class_base(target: Target, event: EventModel, lang: str, versions: dict[str, int]) -> str:
    if target.style == STYLE_SELF_DESCRIBING:
        match = _IGLU.fullmatch(_schema_template(target, event))
        if match is not None:
            name, version = match.group(2), match.group(4)
            base = naming.type_name(name, lang)
            if versions.get(name, 0) > 1:
                # `CheckoutStartedV1_0_0`: the version stays readable as a version.
                return f"{base}V{version.replace('-', '_')}"
            return base
    return naming.type_name(event.identity, lang)


def _versions(target: Target) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in target.event_type.events:
        match = _IGLU.fullmatch(_schema_template(target, event))
        if match is not None:
            counts[match.group(2)] = counts.get(match.group(2), 0) + 1
    return counts


def _text(dialect: Dialect, prop: _Prop, receiver: str = "") -> str:
    """A non-optional string expression for ``prop``."""
    text = prop.value_type.text(dialect, receiver + prop.ident, optional=prop.optional)
    if not prop.optional:
        return text
    if dialect.name == SWIFT:
        return f'({text} ?? "")'
    if dialect.name == KOTLIN:
        return f'({text} ?: "")'
    return f"({text} ?? '')"


def _put(dialect: Dialect, key: str, value: str) -> str:
    literal = dialect.literal(key)
    if dialect.name == SWIFT:
        return f"properties[{literal}] = {value}"
    return f"put({literal}, {value})"


def _prop_put(dialect: Dialect, prop: _Prop) -> str:
    assert prop.key is not None
    lang = dialect.name
    if not prop.optional:
        return _put(dialect, prop.key, prop.value_type.value(dialect, prop.ident))
    if lang == SWIFT:
        stmt = _put(dialect, prop.key, prop.value_type.value(dialect, prop.ident))
        return f"if let {prop.ident} {{ {stmt} }}"
    stmt = _put(dialect, prop.key, prop.value_type.value(dialect, "it"))
    return f"{prop.ident}?.let {{ {stmt} }}"


def _event_context(
    target: Target,
    event: EventModel,
    dialect: Dialect,
    registry: EnumRegistry,
    field_types: dict[str, ValueType],
    case_name: str,
    class_name: str,
) -> dict[str, Any]:
    lang = dialect.name
    fixed = dict(event.field_values or {})
    idents = Namer({*naming.KEYWORDS[lang], *_RESERVED_PROPS})
    props: list[_Prop] = []
    by_key: dict[str, _Prop] = {}
    for field in target.event_type.fields:
        if field.name in fixed:
            continue
        prop = _Prop(
            ident=idents.take(naming.member(field.name, lang)),
            key=field.name,
            token=None,
            value_type=field_types[field.name],
            optional=not field.required,
        )
        props.append(prop)
        by_key[field.name] = prop
    token_names: list[str] = []
    for template in (event.identity, *fixed.values()):
        for token in tokens(template):
            if token not in token_names:
                token_names.append(token)
    resolve: dict[str, str] = {}
    token_only: list[str] = []
    for token in token_names:
        known = by_key.get(token)
        if known is not None:
            prop = known
        else:
            prop = _Prop(
                ident=idents.take(naming.member(token, lang)),
                key=None,
                token=token,
                value_type=_token_type(target, event, token, dialect, registry),
                optional=False,
            )
            props.append(prop)
            token_only.append(token)
        resolve[token] = _text(dialect, prop)
    props.sort(key=lambda prop: prop.optional)  # required first, stable otherwise
    name_expression = interpolation(dialect, event.identity, resolve, braces=False)
    schema_expression = interpolation(
        dialect, _schema_template(target, event), resolve, braces=False
    )
    fixed_literals = {
        key: _fixed_literal(target, dialect, key, value) for key, value in fixed.items()
    }
    puts = [
        _put(
            dialect,
            key,
            fixed_literals[key] or interpolation(dialect, value, resolve, braces=False),
        )
        for key, value in fixed.items()
    ] + [_prop_put(dialect, prop) for prop in props if prop.key is not None]
    fixed_pairs = [
        (dialect.literal(key), fixed_literals[key] or dialect.literal(value))
        for key, value in fixed.items()
    ]
    return {
        "case": case_name,
        "class_name": class_name,
        "identity": comment(event.identity),
        "identity_literal": dialect.literal(event.identity),
        "schema_literal": dialect.literal(_schema_template(target, event)),
        "deprecated": event.deprecated,
        "has_props": bool(props),
        "props": [
            _prop_context(dialect, prop, number, len(props)) for number, prop in enumerate(props)
        ],
        "init_signature": ", ".join(_init_param(dialect, prop) for prop in props),
        "name_expr": name_expression,
        "schema_expr": schema_expression,
        "puts": [{"stmt": stmt} for stmt in puts],
        "has_puts": bool(puts),
        "properties_literal": _map_literal(dialect, fixed_pairs),
        "fixed": [{"key": key, "value": value} for key, value in fixed_pairs],
        "tokens": [{"literal": dialect.literal(token)} for token in token_only],
        "tokens_literal": "[" + ", ".join(dialect.literal(token) for token in token_only) + "]",
    }


def _fixed_literal(target: Target, dialect: Dialect, key: str, value: str) -> str | None:
    """A fixed value of a boolean/number field as a typed literal; ``None`` otherwise
    (a string, an enum value, a ``${token}`` template, or text that is not one)."""
    field = target.event_type.field(key)
    if field is None or field.values or TOKEN.search(value):
        return None
    value_type = scalar_type(field.type)
    if value_type is STRING:
        return None
    return value_type.literal(dialect, value)


def _prop_context(dialect: Dialect, prop: _Prop, number: int, count: int) -> dict[str, Any]:
    type_name = prop.value_type.type_name(dialect)
    return {
        "ident": prop.ident,
        "type": dialect.optional(type_name) if prop.optional else type_name,
        "base_type": type_name,
        "optional": prop.optional,
        "key": dialect.literal(prop.key if prop.key is not None else prop.token or prop.ident),
        "sep": "," if number < count - 1 else "",
        "init": _init_param(dialect, prop),
    }


def _init_param(dialect: Dialect, prop: _Prop) -> str:
    type_name = prop.value_type.type_name(dialect)
    if dialect.name == SWIFT:
        suffix = "? = nil" if prop.optional else ""
        return f"{prop.ident}: {type_name}{suffix}"
    if dialect.name == KOTLIN:
        prefix = f"val {prop.ident}: "
        return prefix + (f"{type_name}? = null" if prop.optional else type_name)
    return f"{prop.ident}{'?' if prop.optional else ''}: {type_name}"


def _map_literal(dialect: Dialect, pairs: list[tuple[str, str]]) -> str:
    if dialect.name == SWIFT:
        return "[" + ", ".join(f"{k}: {v}" for k, v in pairs) + "]" if pairs else "[:]"
    if dialect.name == KOTLIN:
        if not pairs:
            return "emptyMap()"
        return "mapOf(" + ", ".join(f"{k} to {v}" for k, v in pairs) + ")"
    return "{ " + ", ".join(f"{k}: {v}" for k, v in pairs) + " }" if pairs else "{}"


def named_context(target: Target, dialect: Dialect) -> dict[str, Any]:
    event_type = target.event_type
    lang = dialect.name
    self_describing = target.style == STYLE_SELF_DESCRIBING
    namespace = target.type_override("namespace") or naming.type_name(
        event_type.name + " tracking", lang
    )
    event_name = target.type_override("event") or naming.type_name(event_type.name + " event", lang)
    props_type = f"{event_name}Props"
    names_type = f"{event_name}Name"
    types = Namer({namespace, event_name, props_type, names_type, *SHARED_TYPES})
    registry = EnumRegistry(dialect, types)
    field_types = {
        field.name: _field_type(target, field, dialect, registry) for field in event_type.fields
    }
    cases = Namer({*naming.KEYWORDS[lang], *naming.RESERVED_MEMBERS[lang]})
    versions = _versions(target)
    events = [
        _event_context(
            target,
            event,
            dialect,
            registry,
            field_types,
            cases.take(naming.member(event.identity, lang)),
            types.take(_class_base(target, event, lang, versions)),
        )
        for event in event_type.events
    ]
    # Swift: the switches over the enum's cases live behind a private protocol when a
    # case is deprecated (see named.swift.mustache), under a name no event type took.
    resolver = types.take(f"{event_name}Resolving")
    transport = target.codegen.transport.get(lang)
    default = [Arg(None, NAME_IGLU if self_describing else NAME), Arg(None, PROPERTIES)]
    args, object_arg = args_of(transport, default)
    data = "data" if self_describing else "properties"
    receiver = "" if lang == TS else "event."
    used: set[str] = set()

    def expression_for(raw: str | None) -> str:
        plan_target = field_target(target, raw)
        if plan_target is None:
            return dialect.null
        if plan_target == NAME_IGLU and self_describing:
            used.add("schema")
            return receiver + "schema"
        if plan_target in (NAME, NAME_IGLU):
            used.add("name")
            return receiver + ("resolvedName" if lang == TS else "name")
        if plan_target == PROPERTIES:
            return receiver + data
        key = dialect.literal(plan_target)
        if lang == TS:
            return f"{data}[{key}] as string | undefined"
        return f"{receiver}{data}[{key}] as? String"

    if transport is not None:
        where = f"event_types.{event_type.name}.codegen.transport.{lang}"
        keep_labels = lang != TS or object_arg is not None
        arguments = [
            (arg.label if keep_labels else None, expression_for(arg.target)) for arg in args
        ]
        forward = render_call(dialect, callee(transport, where), arguments, object_arg)
    else:
        name = expression_for(NAME_IGLU if self_describing else NAME)
        properties = expression_for(PROPERTIES)
        literal = dialect.literal(event_type.name)
        if lang == SWIFT:
            forward = (
                f"{DESTINATION_FUNCTION[lang]}(eventType: {literal}, name: {name}, "
                f"fields: [:], properties: {properties})"
            )
        elif lang == KOTLIN:
            forward = (
                f"{DESTINATION_FUNCTION[lang]}(eventType = {literal}, name = {name}, "
                f"fields = emptyMap(), properties = {properties})"
            )
        else:
            forward = f"{DESTINATION_FUNCTION[lang]}({literal}, {name}, {{}}, {properties})"
    context = base_context(target, dialect)
    context.update(
        {
            "namespace": namespace,
            "event_name": event_name,
            "props_type": props_type,
            "names_type": names_type,
            "enums": registry.context(),
            "events": events,
            "has_deprecated": any(event.deprecated for event in event_type.events),
            "resolver": resolver,
            "function": {
                "name": target.type_override("function") or "track",
                "forward": forward,
                "uses_name": "name" in used,
                "uses_schema": "schema" in used,
            },
        }
    )
    return context
