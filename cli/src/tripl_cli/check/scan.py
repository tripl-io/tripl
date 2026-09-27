"""Static mode: source files -> tracking call sites -> validation items.

Two passes over the tree. The first tokenizes every configured enum file and
every source file and builds one ``SymbolIndex``, because the enum a call's
``.home`` refers to is usually declared in a different file — often one outside
``sources``. The second finds the call sites, matches each against the event
types IN CONFIG ORDER (the first matching spec wins, so a call is never counted
as two event types), and maps its arguments onto plan fields.

Nothing here touches the network.
"""

from __future__ import annotations

import itertools
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from tripl_cli.check.calls import Arg, Call, find_calls
from tripl_cli.check.config import (
    FIELD_PREFIX,
    NAME,
    NAME_IGLU,
    PROPERTIES,
    CallSpec,
    CheckConfig,
    EventTypeSpec,
)
from tripl_cli.check.lexer import EXTENSIONS, LineIndex, Token, tokenize
from tripl_cli.check.model import CheckItem, Origin
from tripl_cli.check.symbols import SymbolIndex
from tripl_cli.check.values import Evaluator, Value
from tripl_cli.errors import TriplConfigError

# Past this a file is generated or bundled, not written by hand.
MAX_FILE_BYTES = 2_000_000
# One call site may expand into this many items (a name function returning
# several strings, times a field doing the same); past it the rest is dynamic.
MAX_EXPANSION = 50

_IGLU = re.compile(r"iglu:[^/]+/([^/]+)/[^/]+/[^/]+")


@dataclass(frozen=True)
class SourceFile:
    path: Path
    relative: str
    language: str


@dataclass(frozen=True)
class ScanOutcome:
    items: tuple[CheckItem, ...]
    files: int
    calls: int


# --- globbing -------------------------------------------------------------------
@lru_cache(maxsize=256)
def glob_regex(pattern: str) -> re.Pattern[str]:
    """A ``**``-aware glob over POSIX relative paths. ``{a,b}`` alternation is supported."""
    out: list[str] = []
    index = 0
    text = pattern.strip()
    if text.startswith("./"):
        text = text[2:]
    while index < len(text):
        char = text[index]
        if text.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif text.startswith("**", index):
            out.append(".*")
            index += 2
        elif char == "*":
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char == "{":
            end = text.find("}", index)
            if end < 0:
                out.append(re.escape(char))
                index += 1
            else:
                options = text[index + 1 : end].split(",")
                out.append("(?:" + "|".join(re.escape(option) for option in options) + ")")
                index = end + 1
        else:
            out.append(re.escape(char))
            index += 1
    return re.compile("".join(out))


def _matches(relative: str, patterns: Iterable[str]) -> bool:
    return any(glob_regex(pattern).fullmatch(relative) for pattern in patterns)


def _is_dir_pattern_match(relative_dir: str, patterns: Iterable[str]) -> bool:
    """Whether everything under ``relative_dir`` is excluded, so the walk can prune it."""
    probe = f"{relative_dir}/\0probe"
    return any(glob_regex(pattern).fullmatch(probe) for pattern in patterns)


def walk(root: Path, include: Iterable[str], exclude: Iterable[str]) -> Iterator[tuple[Path, str]]:
    """Every file under ``root`` matching ``include`` and not ``exclude``, sorted.

    Yields ``(path, posix-relative path)``; excluded directories are pruned.
    """
    include = tuple(include)
    exclude = tuple(exclude)
    for directory, subdirs, files in os.walk(root):
        relative_dir = Path(directory).relative_to(root).as_posix()
        relative_dir = "" if relative_dir == "." else relative_dir
        subdirs[:] = sorted(
            name
            for name in subdirs
            if not _is_dir_pattern_match(f"{relative_dir}/{name}".lstrip("/"), exclude)
        )
        for name in sorted(files):
            relative = f"{relative_dir}/{name}".lstrip("/")
            if _matches(relative, include) and not _matches(relative, exclude):
                yield Path(directory) / name, relative


def discover(config: CheckConfig) -> list[SourceFile]:
    if not config.root.is_dir():
        raise TriplConfigError(f"the check root {config.root} is not a directory.")
    found: list[SourceFile] = []
    for path, relative in walk(config.root, config.sources, config.exclude):
        language = EXTENSIONS.get(path.suffix.lower())
        if language is not None:
            found.append(SourceFile(path, relative, language))
    return found


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# --- the two passes -------------------------------------------------------------
def scan(config: CheckConfig) -> ScanOutcome:
    """Find and map every configured tracking call under ``config.root``."""
    if not config.event_types:
        raise TriplConfigError(
            "the check config names no event types; add an `event_types:` section "
            "saying how each plan event type is tracked."
        )
    files = discover(config)
    symbols = SymbolIndex()
    parsed: list[tuple[SourceFile, str, list[Token]]] = []
    seen: set[Path] = set()
    for enum_source in config.enums:
        for path, _relative in walk(config.root, enum_source.patterns, config.exclude):
            languages = enum_source.languages or frozenset(
                filter(None, [EXTENSIONS.get(path.suffix.lower())])
            )
            text = _read(path)
            if text is None:
                continue
            for language in sorted(languages):
                symbols.index_tokens(tokenize(text, language), language)
            seen.add(path)
    for source in files:
        text = _read(source.path)
        if text is None:
            continue
        tokens = tokenize(text, source.language)
        if source.path not in seen:
            symbols.index_tokens(tokens, source.language)
        parsed.append((source, text, tokens))
    items: list[CheckItem] = []
    matched = 0
    for source, text, tokens in parsed:
        lines = LineIndex(text)
        evaluator = Evaluator(symbols, source.language)
        for call in find_calls(tokens, source.language, text):
            hit = _first_match(config.event_types, call, source.language)
            if hit is None:
                continue
            matched += 1
            event_type, spec = hit
            origin = _origin(source, call, lines)
            items.extend(map_call(call, spec, event_type, evaluator, origin))
    return ScanOutcome(tuple(items), len(parsed), matched)


def _first_match(
    event_types: Iterable[EventTypeSpec], call: Call, language: str
) -> tuple[EventTypeSpec, CallSpec] | None:
    for event_type in event_types:
        for spec in event_type.calls:
            if spec.matches(call, language):
                return event_type, spec
    return None


def _origin(source: SourceFile, call: Call, lines: LineIndex) -> Origin:
    line, column = lines.position(call.offset)
    end_line, end_column = lines.position(max(call.end - 1, call.offset))
    snippet = call.selector if call.selector is not None else call.callee
    return Origin(source.relative, line, column, end_line, end_column + 1, snippet)


# --- one call -> items ------------------------------------------------------------
@dataclass
class _Collected:
    name: Value | None = None
    fields: dict[str, Value] | None = None
    properties: Value | None = None


def map_call(
    call: Call,
    spec: CallSpec,
    event_type: EventTypeSpec,
    evaluator: Evaluator,
    origin: Origin,
) -> list[CheckItem]:
    collected = _Collected(fields={})
    for arg in call.args:
        mapped, target = spec.target_for(arg)
        if spec.object_arg is not None and arg.index == spec.object_arg and arg.label is None:
            _object_arg(arg, spec, event_type, evaluator, collected)
            continue
        if not mapped or target is None:
            continue
        _assign(collected, event_type.resolve_target(target), _evaluate(evaluator, arg, target))
    for link in call.chain:
        target = spec.chain.get(link.method)
        if target is None or not link.args:
            continue
        _assign(
            collected,
            event_type.resolve_target(target),
            _evaluate(evaluator, link.args[0], target),
        )
    return _items(collected, event_type.name, origin)


def _evaluate(evaluator: Evaluator, arg: Arg, target: str) -> Value:
    hint = target.removeprefix(FIELD_PREFIX) if target not in (NAME, NAME_IGLU) else None
    return evaluator.evaluate(arg.tokens, field=hint or arg.label)


def _object_arg(
    arg: Arg,
    spec: CallSpec,
    event_type: EventTypeSpec,
    evaluator: Evaluator,
    collected: _Collected,
) -> None:
    value = evaluator.evaluate(arg.tokens)
    if value.kind != "entries":
        return
    labels = spec.args or {}
    for key, entry in _flatten(value.entries):
        target = labels.get(key)
        if target is not None:
            _assign(collected, event_type.resolve_target(target), entry)


def _flatten(entries: Iterable[tuple[str, Value]], prefix: str = "") -> Iterator[tuple[str, Value]]:
    for key, value in entries:
        path = f"{prefix}{key}"
        yield path, value
        if value.kind == "entries":
            yield from _flatten(value.entries, f"{path}.")


def _assign(collected: _Collected, target: str, value: Value) -> None:
    if value.kind == "absent":
        return
    if target == NAME:
        collected.name = value
    elif target == NAME_IGLU:
        collected.name = _iglu_name(value)
    elif target == PROPERTIES:
        collected.properties = value
    else:
        assert collected.fields is not None
        collected.fields[target.removeprefix(FIELD_PREFIX)] = value


def _iglu_name(value: Value) -> Value:
    if value.kind != "text" or value.text is None:
        return value
    match = _IGLU.fullmatch(value.text)
    return Value("text", text=match.group(1)) if match else value


def _options(value: Value | None) -> list[str | None]:
    """Every concrete value ``value`` may take; ``[None]`` means dynamic/unknown."""
    if value is None:
        return [None]
    if value.kind == "text":
        return [value.text]
    if value.kind == "choices":
        return list(value.choices)
    return [None]


def _properties(value: Value | None) -> dict[str, Any] | None:
    if value is None or value.kind != "entries":
        return None
    return {
        key: entry.text if entry.kind == "text" and not entry.interpolated else None
        for key, entry in value.entries
    }


def _items(collected: _Collected, event_type: str, origin: Origin) -> list[CheckItem]:
    fields = collected.fields or {}
    dynamic_targets = [
        target
        for target, value in (("name", collected.name), *fields.items())
        if value is not None and not value.is_known
    ]
    if collected.properties is not None and collected.properties.kind not in ("entries",):
        dynamic_targets.append("properties")
    names = _options(collected.name)
    field_names = list(fields)
    field_options = [_options(fields[name]) for name in field_names]
    known = any(option is not None for option in names) or any(
        option is not None for options in field_options for option in options
    )
    properties = _properties(collected.properties)
    if not known:
        return [
            CheckItem(
                origin=origin,
                event_type=event_type,
                name=None,
                fields={name: None for name in field_names},
                properties=properties,
                complete=False,
                dynamic=tuple(dynamic_targets) or ("name",),
                sendable=False,
            )
        ]
    items: list[CheckItem] = []
    for combination in itertools.islice(itertools.product(names, *field_options), MAX_EXPANSION):
        name, *values = combination
        items.append(
            CheckItem(
                origin=origin,
                event_type=event_type,
                name=name,
                fields=dict(zip(field_names, values, strict=True)),
                properties=properties,
                complete=False,
                dynamic=tuple(dynamic_targets),
            )
        )
    return items
