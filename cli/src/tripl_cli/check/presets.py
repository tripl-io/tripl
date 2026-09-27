"""Built-in call specs for the SDKs tripl knows out of the box: Segment, Amplitude, Snowplow.

A preset is written in exactly the vocabulary a user's own ``calls:`` entry
uses and goes through the same validator, so a preset can never do something a
hand-written spec could not — and ``tripl check`` can print any of them as the
starting point for a custom one.

Callee patterns match the NORMALISED callee: whitespace, ``?``/``!`` and the
contents of intermediate calls are dropped, so ``Amplitude.instance().logEvent``
arrives as ``Amplitude.instance.logEvent``.

Targets are canonical (``category``, ``action``, ``label``, ``property``,
``value``, ``id``, ``type``); an event type whose plan fields are named
otherwise renames them with ``field_map``.
"""

from __future__ import annotations

from typing import Any

from tripl_cli.check.config import CallSpec, parse_call
from tripl_cli.errors import TriplConfigError

_ANY_RECEIVER = r"(?:[\w$.]*\.)?"
# `.shared`, `.instance`, `.getInstance` … between an SDK's type and its method.
_SINGLETON = r"(?:\.(?:shared|sharedAnalytics|instance|getInstance|main|default)\w*)?"

PRESETS: dict[str, tuple[dict[str, Any], ...]] = {
    # analytics.track("Name", {…}) — JS/TS, Kotlin (analytics-kotlin), Swift
    # (analytics-swift: track(name:properties:)), and ObjC [SEGAnalytics … track:properties:].
    # Receivers are matched on their name: `analytics`, `Analytics.shared`, `segment`.
    "segment_track": (
        {
            "pattern": _ANY_RECEIVER
            + r"\w*(?:[Aa]nalytics|[Ss]egment)\w*"
            + _SINGLETON
            + r"\.track",
            "args": {"name": "name", "event": "name", "properties": "properties"},
            "positional": ["name", "properties"],
        },
        {
            "objc_selector": "track:properties:",
            "receiver": r"(?i)analytics|segment",
            "positional": ["name", "properties"],
        },
        {
            "objc_selector": "track:",
            "receiver": r"(?i)analytics|segment",
            "positional": ["name"],
        },
    ),
    # amplitude.track("Name", {…}) / Amplitude.instance().logEvent("Name", withEventProperties: …)
    # — JS/TS, Kotlin, Swift, and ObjC [[Amplitude instance] logEvent:withEventProperties:].
    "amplitude_log_event": (
        {
            "pattern": (
                _ANY_RECEIVER + r"\w*[Aa]mplitude\w*" + _SINGLETON + r"\.(?:logEvent|track)"
            ),
            "args": {
                "eventType": "name",
                "event_type": "name",
                "eventProperties": "properties",
                "event_properties": "properties",
                "withEventProperties": "properties",
            },
            "positional": ["name", "properties"],
        },
        {
            "objc_selector": "logEvent:withEventProperties:",
            "receiver": r"(?i)amplitude",
            "positional": ["name", "properties"],
        },
        {
            "objc_selector": "logEvent:",
            "receiver": r"(?i)amplitude",
            "positional": ["name"],
        },
    ),
    # Structured(category: "c", action: "a").label("l") — Swift, Kotlin, Java
    # (`new Structured(…)`, `Structured.builder().category(…)…`), JS
    # trackStructEvent({category, action, label, property, value}) and
    # ObjC [[SPStructured alloc] initWithCategory:action:].
    "snowplow_structured": (
        {
            "pattern": _ANY_RECEIVER + r"(?:SP)?Structured(?:Event)?(?:\.builder)?",
            "args": {
                "category": "category",
                "action": "action",
                "label": "label",
                "property": "property",
                "value": "value",
            },
            "positional": ["category", "action"],
            "chain": {
                "category": "category",
                "action": "action",
                "label": "label",
                "property": "property",
                "value": "value",
            },
        },
        {
            "pattern": _ANY_RECEIVER + r"trackStructEvent",
            "object_arg": 0,
            "args": {
                "category": "category",
                "action": "action",
                "label": "label",
                "property": "property",
                "value": "value",
            },
        },
        {
            "objc_selector": "initWithCategory:action:",
            "receiver": r"SPStructured|Structured",
            "positional": ["category", "action"],
        },
    ),
    # ScreenView(name: "Home", screenId: …) — Swift, Kotlin, Java; JS
    # trackScreenView({name, id}); ObjC [[SPScreenView alloc] initWithName:screenId:].
    "snowplow_screen_view": (
        {
            "pattern": _ANY_RECEIVER + r"(?:SP)?ScreenView(?:\.builder)?",
            "args": {"name": "name", "screenId": "id", "id": "id", "type": "type"},
            "positional": ["name", "id"],
            "chain": {"name": "name", "id": "id", "type": "type"},
        },
        {
            "pattern": _ANY_RECEIVER + r"trackScreenView",
            "object_arg": 0,
            "args": {"name": "name", "id": "id", "type": "type"},
        },
        {
            "objc_selector": "initWithName:screenId:",
            "receiver": r"SPScreenView|ScreenView",
            "positional": ["name", "id"],
        },
    ),
    # SelfDescribing(schema: "iglu:com.acme/button_click/jsonschema/1-0-0", payload: […])
    # — Swift, Kotlin, Java; JS trackSelfDescribingEvent({event: {schema, data}});
    # ObjC [[SPSelfDescribing alloc] initWithSchema:payload:]. The event NAME is
    # the Iglu schema's name segment.
    "snowplow_self_describing": (
        {
            "pattern": _ANY_RECEIVER + r"(?:SP)?SelfDescribing(?:Json)?",
            "args": {
                "schema": "name:iglu",
                "payload": "properties",
                "eventData": "properties",
                "data": "properties",
            },
            "positional": ["name:iglu", "properties"],
        },
        {
            "pattern": _ANY_RECEIVER + r"trackSelfDescribingEvent",
            "object_arg": 0,
            "args": {"event.schema": "name:iglu", "event.data": "properties"},
        },
        {
            "objc_selector": "initWithSchema:payload:",
            "receiver": r"SPSelfDescribing|SelfDescribing",
            "positional": ["name:iglu", "properties"],
        },
    ),
}


def preset_specs(name: str, where: str) -> tuple[CallSpec, ...]:
    """The call specs of preset ``name``.

    An unknown name is a config error that lists the known ones.
    """
    raw = PRESETS.get(name)
    if raw is None:
        raise TriplConfigError(
            f"{where}: unknown preset {name!r}; the presets are {', '.join(sorted(PRESETS))}."
        )
    return tuple(parse_call(spec, f"preset {name}[{number}]") for number, spec in enumerate(raw))
