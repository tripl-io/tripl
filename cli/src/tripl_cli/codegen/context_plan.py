"""The style-independent ``plan`` object every template context carries.

The style contexts (``context``/``context_named``) are shaped around the
built-in templates. A team whose tracking code has another shape — constant
holders instead of enums, its own event value object, screen views as planned
(type, id) pairs, name and key constants — builds it from ``plan`` instead:
the event type's fields with their values, its events with the value each sets,
and every planned combination, each item carrying its plan string three ways
(``raw`` for comments, ``literal`` quoted for the language, ``names`` as
identifiers in four casings, escaped and unique within their list).

Shape (every list item also has ``first``, ``last`` and ``comma`` — ``","`` except on the
last item — for separators)::

    plan:
      event_type: {raw, literal, names}
      fields: [{raw, literal, names, type, required, closed, variable?,
                values: [{raw, literal, names}], has_values}]
      field: {<key>: <the same field item>}
      events: [{raw, literal, names, deprecated, dynamic, name_expr,
                values: [{field, raw, literal, names, dynamic}],
                value: {<field key>: <the same value item>},
                tokens: [{raw, literal, names, closed, values}], has_tokens,
                properties: [{raw, literal, names, required, fixed, value?}]}]
      combinations: [{raw, literal, names, event, deprecated,
                      values: [...], value: {...}}]
      has_combinations

A value item's ``names`` are the names its field's ``values`` gave the same
string, so ``AcmeCategory.{{value.category.names.pascal}}`` in one template
refers to the constant another template declared. A field's ``names`` come from
the same scope as ``function.params`` and ``transport.args`` use (``KeyedNames``).

``<key>`` is ``naming.plain_key`` of the field name — its lower_snake words with
no keyword escaping and no numbering, so it is the same in every language
(``plan.field.default``, ``value.promo_code``); of two fields with one key the
first in plan order keeps it. A key marked ``?`` is LEFT OUT when it has no
value (a field with no ``variable``, a property the event does not fix), so a
bare ``{{variable}}`` there is an error, not an empty hole.
"""

from __future__ import annotations

import itertools
from typing import Any

from tripl_cli.codegen.context import Target, field_names, field_values, interpolation
from tripl_cli.codegen.languages import Dialect, comment
from tripl_cli.codegen.model import TOKEN, EventModel, tokens
from tripl_cli.codegen.naming import KeyedNames, NameScope, plain_key

# Past this many combinations one event is not a closed set a template should list.
MAX_COMBINATIONS_PER_EVENT = 1000


def _text_item(dialect: Dialect, raw: str, names: dict[str, str]) -> dict[str, Any]:
    return {"raw": comment(raw), "literal": dialect.literal(raw), "names": names}


def _with_separators(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for number, item in enumerate(items):
        last = number == len(items) - 1
        item["last"] = last
        item["first"] = number == 0
        item["comma"] = "" if last else ","
    return items


def _keyed(pairs: list[tuple[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """``(field name, item)`` pairs by the field's plain key; of two fields with one
    key the first keeps it."""
    found: dict[str, dict[str, Any]] = {}
    for name, item in pairs:
        found.setdefault(plain_key(name), item)
    return found


def plan_context(
    target: Target, dialect: Dialect, scope: KeyedNames | None = None
) -> dict[str, Any]:
    event_type = target.event_type
    lang = dialect.name
    field_scope = scope or field_names(target, lang)
    fields: list[dict[str, Any]] = []
    value_names: dict[str, dict[str, dict[str, str]]] = {}
    for field in event_type.fields:
        planned, free = field_values(target, field)
        value_scope = NameScope(lang)
        value_items = []
        for raw in sorted(planned):
            names = value_scope.take(raw)
            value_names.setdefault(field.name, {})[raw] = names
            value_items.append(_text_item(dialect, raw, names))
        item = _text_item(dialect, field.name, field_scope.of(field.name))
        item.update(
            {
                "type": comment(field.type),
                "required": field.required,
                "closed": not free,
                "values": _with_separators(value_items),
                "has_values": bool(value_items),
            }
        )
        if field.variable:
            item["variable"] = comment(field.variable)
        fields.append(item)
    _with_separators(fields)

    def ref(name: str) -> dict[str, Any]:
        # A key an event sets that the type has no field for is named on first use,
        # in the same scope as the fields.
        return _text_item(dialect, name, field_scope.of(name))

    def value_item(field: str, raw: str) -> dict[str, Any]:
        dynamic = TOKEN.search(raw) is not None
        names = value_names.get(field, {}).get(raw) or NameScope(lang).take(raw)
        return {**_text_item(dialect, raw, names), "field": ref(field), "dynamic": dynamic}

    def values_of(field_values_: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        keys = [field.name for field in event_type.fields if field.name in field_values_]
        keys += sorted(key for key in field_values_ if event_type.field(key) is None)
        pairs = [(key, value_item(key, field_values_[key])) for key in keys]
        items = [item for _, item in pairs]
        return _with_separators(items), _keyed(pairs)

    event_scope = NameScope(lang)
    event_names: dict[str, dict[str, str]] = {}
    events = []
    for event in event_type.events:
        fixed = dict(event.field_values or {})
        values, value = values_of(fixed)
        event_tokens = _event_tokens(event)
        token_scope = NameScope(lang)
        token_items = []
        resolve: dict[str, str] = {}
        for token in event_tokens:
            names = token_scope.take(token)
            resolve[token] = names["camel"]
            allowed = target.model.allowed_for(event, token) or ()
            allowed_scope = NameScope(lang)
            item = _text_item(dialect, token, names)
            item["closed"] = bool(allowed)
            item["values"] = _with_separators(
                [_text_item(dialect, raw, allowed_scope.take(raw)) for raw in allowed]
            )
            token_items.append(item)
        properties = []
        for field in event_type.fields:
            fixed_value = fixed.get(field.name)
            prop = ref(field.name)
            prop.update({"required": field.required, "fixed": fixed_value is not None})
            if fixed_value is not None:
                prop["value"] = value_item(field.name, fixed_value)
            properties.append(prop)
        event_names[event.identity] = event_scope.take(event.identity)
        item = _text_item(dialect, event.identity, event_names[event.identity])
        item.update(
            {
                "deprecated": event.deprecated,
                "dynamic": TOKEN.search(event.identity) is not None,
                "name_expr": interpolation(dialect, event.identity, resolve, braces=False),
                "values": values,
                "value": value,
                "tokens": _with_separators(token_items),
                "has_tokens": bool(token_items),
                "properties": _with_separators(properties),
                "has_properties": bool(properties),
            }
        )
        events.append(item)
    _with_separators(events)

    combination_scope = NameScope(lang)
    combinations = []
    for event, expanded, fixed in _combinations(target, event_type.events):
        values, value = values_of(fixed)
        item = _text_item(dialect, expanded, combination_scope.take(expanded))
        item.update(
            {
                "event": _text_item(dialect, event.identity, event_names[event.identity]),
                "deprecated": event.deprecated,
                "values": values,
                "value": value,
            }
        )
        combinations.append(item)
    _with_separators(combinations)
    return {
        "event_type": _text_item(dialect, event_type.name, NameScope(lang).take(event_type.name)),
        "fields": fields,
        "has_fields": bool(fields),
        "field": _keyed(
            [(field.name, item) for field, item in zip(event_type.fields, fields, strict=True)]
        ),
        "events": events,
        "has_events": bool(events),
        "combinations": combinations,
        "has_combinations": bool(combinations),
    }


def _event_tokens(event: EventModel) -> list[str]:
    found: list[str] = []
    for text in (event.identity, *(event.field_values or {}).values()):
        for token in tokens(text):
            if token not in found:
                found.append(token)
    return found


def _combinations(
    target: Target, events: tuple[EventModel, ...]
) -> list[tuple[EventModel, str, dict[str, str]]]:
    """Every planned (identity, field values) an event stands for: a ``${token}``
    with allowed values is expanded to each; an event with a free token is left
    out, because it is not one of a closed set."""
    found: list[tuple[EventModel, str, dict[str, str]]] = []
    for event in events:
        names = _event_tokens(event)
        choices = [target.model.allowed_for(event, token) for token in names]
        if any(not allowed for allowed in choices):
            continue
        total = 1
        for allowed in choices:
            total *= len(allowed or ())
        if total > MAX_COMBINATIONS_PER_EVENT:
            continue
        fixed = dict(event.field_values or {})
        for picked in itertools.product(*(allowed or () for allowed in choices)):
            chosen = dict(zip(names, picked, strict=True))

            def fill(text: str, chosen: dict[str, str] = chosen) -> str:
                return TOKEN.sub(lambda match: chosen.get(match.group(1), match.group(0)), text)

            found.append(
                (event, fill(event.identity), {key: fill(raw) for key, raw in fixed.items()})
            )
    return found
