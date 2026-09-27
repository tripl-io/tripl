"""``.tripl/check.yml``: which calls in the code send which event type, and how.

The user's OWN tracking wrapper is the primary thing configured here; the SDK
presets (``presets.py``) are shorthands for the common vendors. Configuration is
PER EVENT TYPE, because a project tracks different kinds of events differently
— structured events through one wrapper, screen views through another, legacy
flat events through a third::

    project: my-app
    branch: null                 # optional; --branch overrides
    sources: ["Sources/**", "web/src/**"]
    exclude: ["**/Generated/**"]
    enums:
      - file: "Sources/**/Events.swift"
        languages: [swift]
    event_types:
      se:                        # a plan event type name
        calls:
          - function: "Analytics.shared.log"
            args: {category: category, action: action, label: label, properties: properties}
          - objc_selector: "trackWithCategory:action:label:"
      page: {preset: snowplow_screen_view}
      legacy:
        calls: [{function: "Analytics.shared.logEvent", name_arg: 0, properties_arg: parameters}]

A call spec names its call ONE way — ``function`` (a dotted callee, matched on
whole segments from the right, so ``Analytics.shared.log`` also matches
``self.analytics.shared.log``), ``pattern`` (a regex over the whole normalised
callee) or ``objc_selector`` — and then says where each plan field comes from:

``args``
    argument label -> target. Swift ``label:`` and Kotlin ``label =`` labels,
    Objective-C selector parts, and (with ``object_arg``) the keys of an object
    literal. Without ``args`` a label maps to the field of the same name, and a
    selector part to the word after ``With`` (``trackWithCategory:`` -> category).
``positional``
    target per argument position, for unlabelled arguments.
``chain``
    builder method -> target, for calls followed by ``.label("x")``-style chains.
``object_arg``
    the position of an argument that is an object literal of labelled values
    (``trackStructEvent({category, action})``); nested keys read as ``a.b``.
``name_arg`` / ``properties_arg``
    shorthands: an int is a position, a string a label.

A target is a plan field name, or one of the reserved targets ``name`` (the
event's name/identity), ``name:iglu`` (the event name inside an Iglu schema
URI) and ``properties`` (a free-form dictionary whose literal keys are checked
as field names). ``field:<x>`` addresses a plan field literally called ``name``
or ``properties``; ``null`` ignores the argument.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tripl_cli.check import yamlish
from tripl_cli.check.calls import Arg, Call
from tripl_cli.check.lexer import LANGUAGE_ALIASES
from tripl_cli.errors import TriplConfigError

CONFIG_DIR = ".tripl"
CONFIG_NAMES = ("check.yml", "check.yaml", "check.json")

NAME = "name"
NAME_IGLU = "name:iglu"
PROPERTIES = "properties"
FIELD_PREFIX = "field:"
RESERVED_TARGETS = frozenset({NAME, NAME_IGLU, PROPERTIES})

DEFAULT_SOURCES = ("**/*",)
DEFAULT_EXCLUDES = (
    "**/.git/**",
    "**/node_modules/**",
    "**/Pods/**",
    "**/Carthage/**",
    "**/DerivedData/**",
    "**/.build/**",
    "**/build/**",
    "**/dist/**",
    "**/*.min.js",
)

_TOP_KEYS = frozenset({"project", "branch", "root", "sources", "exclude", "enums", "event_types"})
_TYPE_KEYS = frozenset({"calls", "preset", "presets", "field_map"})
_CALL_KEYS = frozenset(
    {
        "function",
        "pattern",
        "objc_selector",
        "receiver",
        "args",
        "positional",
        "chain",
        "object_arg",
        "name_arg",
        "properties_arg",
        "languages",
    }
)
_ENUM_KEYS = frozenset({"file", "files", "languages"})
_DEFAULT_LABELS = {
    "name": NAME,
    "event": NAME,
    "eventName": NAME,
    "event_name": NAME,
    "eventType": NAME,
    "event_type": NAME,
    "properties": PROPERTIES,
    "props": PROPERTIES,
    "parameters": PROPERTIES,
    "params": PROPERTIES,
    "eventProperties": PROPERTIES,
    "event_properties": PROPERTIES,
    "attributes": PROPERTIES,
    "extras": PROPERTIES,
}
_SELECTOR = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*:)+")


@dataclass(frozen=True)
class CallSpec:
    """One way a call site sends an event of the owning type."""

    where: str
    function: str | None = None
    pattern: re.Pattern[str] | None = None
    objc_selector: str | None = None
    receiver: re.Pattern[str] | None = None
    args: Mapping[str, str | None] | None = None
    positional: tuple[str | None, ...] = ()
    chain: Mapping[str, str | None] = field(default_factory=dict)
    object_arg: int | None = None
    languages: frozenset[str] = frozenset()

    def matches(self, call: Call, language: str) -> bool:
        if self.languages and language not in self.languages:
            return False
        if self.objc_selector is not None:
            if call.selector != self.objc_selector:
                return False
            return self.receiver is None or self.receiver.search(call.callee) is not None
        if call.selector is not None:
            return False
        callee = _plain_callee(call.callee)
        if self.pattern is not None:
            return self.pattern.fullmatch(callee) is not None
        wanted = _plain_callee(self.function or "")
        return callee == wanted or callee.endswith("." + wanted)

    def target_for(self, arg: Arg) -> tuple[bool, str | None]:
        """``(mapped, target)`` for one argument; ``mapped`` False means "not configured"."""
        if arg.label is not None and self.args is not None and arg.label in self.args:
            return True, self.args[arg.label]
        if self.objc_selector is not None:
            if arg.index < len(self.positional):
                return True, self.positional[arg.index]
            if self.args is None and arg.label is not None:
                return True, selector_field(arg.label)
            return False, None
        if arg.label is not None:
            if self.args is None:
                return True, default_target(arg.label)
            return False, None
        if arg.index < len(self.positional):
            return True, self.positional[arg.index]
        return False, None


@dataclass(frozen=True)
class EventTypeSpec:
    name: str
    calls: tuple[CallSpec, ...]
    field_map: Mapping[str, str] = field(default_factory=dict)

    def resolve_target(self, target: str) -> str:
        return self.field_map.get(target, target)


@dataclass(frozen=True)
class EnumSource:
    patterns: tuple[str, ...]
    languages: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CheckConfig:
    path: Path | None
    root: Path
    project: str | None = None
    branch: str | None = None
    sources: tuple[str, ...] = DEFAULT_SOURCES
    exclude: tuple[str, ...] = DEFAULT_EXCLUDES
    enums: tuple[EnumSource, ...] = ()
    event_types: tuple[EventTypeSpec, ...] = ()


def default_target(label: str) -> str:
    """Where a label goes when a spec has no ``args``: the field of the same name,
    except the labels SDKs and wrappers use for the name and the property bag."""
    return _DEFAULT_LABELS.get(label, label)


def selector_field(part: str) -> str:
    """``trackWithCategory`` -> ``category``; ``action`` -> ``action``."""
    tail = part.rsplit("With", 1)[-1] if "With" in part else part
    return default_target(tail[:1].lower() + tail[1:] if tail else part)


def _plain_callee(callee: str) -> str:
    return re.sub(r"\(\)|\[\]|\s", "", callee)


# --- locating and loading ------------------------------------------------------
def find_config(start: Path) -> Path | None:
    """The nearest ``.tripl/check.{yml,yaml,json}`` at or above ``start``.

    Stops at the first directory holding ``.git`` (the repository root), so a
    checkout nested in another repository never picks up its parent's config.
    """
    directory = start.resolve()
    while True:
        for name in CONFIG_NAMES:
            candidate = directory / CONFIG_DIR / name
            if candidate.is_file():
                return candidate
        if (directory / ".git").exists() or directory.parent == directory:
            return None
        directory = directory.parent


def load(path: Path) -> CheckConfig:
    """Read and validate one config file. Every problem is a ``TriplConfigError`` (exit 2)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise TriplConfigError(f"check config {path} does not exist.") from None
    except OSError as exc:
        raise TriplConfigError(f"cannot read check config {path}: {exc}") from None
    try:
        document = json.loads(raw) if path.suffix == ".json" else yamlish.loads(raw)
    except (ValueError, yamlish.YamlError) as exc:
        raise TriplConfigError(f"{path} is not valid: {exc}") from None
    directory = path.resolve().parent
    base = directory.parent if directory.name == CONFIG_DIR else directory
    return parse(document, path=path, base=base)


def parse(document: Any, *, path: Path | None, base: Path) -> CheckConfig:
    """Validate an already-parsed document. ``base`` is where relative globs start."""
    from tripl_cli.check.presets import preset_specs

    where = str(path) if path is not None else "check config"
    if document is None:
        document = {}
    top = _mapping(document, where)
    _known(top, _TOP_KEYS, where)
    root = base
    if top.get("root") is not None:
        root = (base / _text(top["root"], f"{where}: root")).resolve()
    event_types: list[EventTypeSpec] = []
    raw_types = _mapping(top.get("event_types") or {}, f"{where}: event_types")
    for type_name, body in raw_types.items():
        type_where = f"{where}: event_types.{type_name}"
        spec_body = _mapping(body if body is not None else {}, type_where)
        _known(spec_body, _TYPE_KEYS, type_where)
        calls: list[CallSpec] = []
        for number, call in enumerate(_list(spec_body.get("calls"), f"{type_where}.calls")):
            calls.append(parse_call(call, f"{type_where}.calls[{number}]"))
        presets = _names(spec_body.get("preset"), f"{type_where}.preset") + _names(
            spec_body.get("presets"), f"{type_where}.presets"
        )
        for preset in presets:
            calls.extend(preset_specs(preset, f"{type_where}.preset"))
        if not calls:
            raise TriplConfigError(
                f"{type_where}: say how this event type is tracked, with `calls:` or `preset:`."
            )
        field_map = {
            str(key): _text(value, f"{type_where}.field_map.{key}")
            for key, value in _mapping(
                spec_body.get("field_map") or {}, f"{type_where}.field_map"
            ).items()
        }
        event_types.append(EventTypeSpec(str(type_name), tuple(calls), field_map))
    enums: list[EnumSource] = []
    for number, item in enumerate(_list(top.get("enums"), f"{where}: enums")):
        enum_where = f"{where}: enums[{number}]"
        if isinstance(item, str):
            enums.append(EnumSource((item,)))
            continue
        body = _mapping(item, enum_where)
        _known(body, _ENUM_KEYS, enum_where)
        patterns = _names(body.get("file"), f"{enum_where}.file") + _names(
            body.get("files"), f"{enum_where}.files"
        )
        if not patterns:
            raise TriplConfigError(f"{enum_where}: needs `file:` (a glob).")
        enums.append(EnumSource(tuple(patterns), _languages(body.get("languages"), enum_where)))
    sources = tuple(_names(top.get("sources"), f"{where}: sources")) or DEFAULT_SOURCES
    exclude = DEFAULT_EXCLUDES + tuple(_names(top.get("exclude"), f"{where}: exclude"))
    project = top.get("project")
    branch = top.get("branch")
    return CheckConfig(
        path=path,
        root=root,
        project=_text(project, f"{where}: project") if project is not None else None,
        branch=_text(branch, f"{where}: branch") if branch is not None else None,
        sources=sources,
        exclude=exclude,
        enums=tuple(enums),
        event_types=tuple(event_types),
    )


def parse_call(raw: Any, where: str) -> CallSpec:
    body = _mapping(raw, where)
    _known(body, _CALL_KEYS, where)
    kinds = [key for key in ("function", "pattern", "objc_selector") if body.get(key) is not None]
    if len(kinds) != 1:
        raise TriplConfigError(
            f"{where}: give exactly one of `function`, `pattern` or `objc_selector`"
            + (f" (got {', '.join(kinds)})" if kinds else "")
            + "."
        )
    function: str | None = None
    pattern: re.Pattern[str] | None = None
    selector: str | None = None
    receiver: re.Pattern[str] | None = None
    if body.get("function") is not None:
        function = _text(body["function"], f"{where}.function")
    if body.get("pattern") is not None:
        pattern = _regex(body["pattern"], f"{where}.pattern")
    if body.get("objc_selector") is not None:
        selector = _text(body["objc_selector"], f"{where}.objc_selector")
        if not _SELECTOR.fullmatch(selector):
            raise TriplConfigError(
                f"{where}.objc_selector: {selector!r} is not a selector with arguments, "
                "e.g. 'trackWithCategory:action:label:'."
            )
    if body.get("receiver") is not None:
        if selector is None:
            raise TriplConfigError(f"{where}.receiver: only applies to an objc_selector.")
        receiver = _regex(body["receiver"], f"{where}.receiver")
    args: dict[str, str | None] | None = None
    if body.get("args") is not None:
        args = {
            str(label): _target(value, f"{where}.args.{label}")
            for label, value in _mapping(body["args"], f"{where}.args").items()
        }
    positional = [
        _target(value, f"{where}.positional[{number}]")
        for number, value in enumerate(_list(body.get("positional"), f"{where}.positional"))
    ]
    for shorthand, target in (("name_arg", NAME), ("properties_arg", PROPERTIES)):
        value = body.get(shorthand)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int | str):
            raise TriplConfigError(
                f"{where}.{shorthand}: an argument position (0, 1, …) or label, got {value!r}."
            )
        if isinstance(value, int):
            if value < 0:
                raise TriplConfigError(f"{where}.{shorthand}: a position is 0 or greater.")
            positional.extend([None] * (value + 1 - len(positional)))
            positional[value] = target
        else:
            args = dict(args or {})
            args[value] = target
    chain = {
        str(method): _target(value, f"{where}.chain.{method}")
        for method, value in _mapping(body.get("chain") or {}, f"{where}.chain").items()
    }
    object_arg = body.get("object_arg")
    if object_arg is not None and (
        isinstance(object_arg, bool) or not isinstance(object_arg, int) or object_arg < 0
    ):
        raise TriplConfigError(f"{where}.object_arg: an argument position (0, 1, …).")
    return CallSpec(
        where=where,
        function=function,
        pattern=pattern,
        objc_selector=selector,
        receiver=receiver,
        args=args,
        positional=tuple(positional),
        chain=chain,
        object_arg=object_arg,
        languages=_languages(body.get("languages"), where),
    )


# --- small validators ----------------------------------------------------------
def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TriplConfigError(f"{where}: expected a mapping, got {type(value).__name__}.")
    return value


def _list(value: Any, where: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TriplConfigError(f"{where}: expected a list, got {type(value).__name__}.")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TriplConfigError(f"{where}: expected a non-empty string, got {value!r}.")
    return value.strip()


def _names(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [_text(value, where)]
    return [_text(item, f"{where}[{n}]") for n, item in enumerate(_list(value, where))]


def _target(value: Any, where: str) -> str | None:
    if value is None:
        return None
    target = _text(value, where)
    if target.startswith(FIELD_PREFIX):
        name = target[len(FIELD_PREFIX) :].strip()
        if not name:
            raise TriplConfigError(f"{where}: 'field:' needs a field name after it.")
        return FIELD_PREFIX + name
    if ":" in target and target not in RESERVED_TARGETS:
        raise TriplConfigError(
            f"{where}: {target!r} is not a target. Use a plan field name, "
            f"{', '.join(sorted(RESERVED_TARGETS))}, or field:<name>."
        )
    return target


def _regex(value: Any, where: str) -> re.Pattern[str]:
    text = _text(value, where)
    try:
        return re.compile(text)
    except re.error as exc:
        raise TriplConfigError(f"{where}: {text!r} is not a valid regex: {exc}.") from None


def _languages(value: Any, where: str) -> frozenset[str]:
    found: set[str] = set()
    for name in _names(value, f"{where}.languages"):
        language = LANGUAGE_ALIASES.get(name.lower())
        if language is None:
            raise TriplConfigError(
                f"{where}.languages: {name!r} is not one of {', '.join(sorted(LANGUAGE_ALIASES))}."
            )
        found.add(language)
    return frozenset(found)


def _known(body: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(str(key) for key in body if key not in allowed)
    if unknown:
        raise TriplConfigError(
            f"{where}: unknown key(s) {', '.join(unknown)}; "
            f"expected any of {', '.join(sorted(allowed))}."
        )
