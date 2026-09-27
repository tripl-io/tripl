"""Payload mode: captured events (NDJSON or JSON) -> validation items.

Each event is an object ``{event_type?, name?, fields?, properties?}``. The file
is either one JSON array of them, one such object, or NDJSON — one object per
line, blank lines ignored — which is what a test run or a staging proxy writes.
``-`` reads standard input.

A captured payload is what the app ACTUALLY sent, so every item is
``complete``: the validator may report a required field as missing, which it
never does for a call site in the code.

The validator's per-item size limits (``api.plan_validation``) are enforced
here, before anything is sent: an oversize value would otherwise make the route
reject the whole batch. It is sent as ``null`` instead — "unknown", never an
error — and its line carries an ``oversize_value`` warning.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from tripl_cli.api import plan_validation as limits
from tripl_cli.check.model import (
    CODE_OVERSIZE_VALUE,
    SEVERITY_WARNING,
    STDIN_LABEL,
    CheckFinding,
    CheckItem,
    Origin,
)
from tripl_cli.errors import TriplConfigError

STDIN = "-"
_KEYS = frozenset({"event_type", "name", "fields", "properties", "ref"})


def load(path: str) -> list[CheckItem]:
    """Read ``path`` (or stdin for ``-``) into items; a malformed file is a usage error."""
    label = STDIN_LABEL if path == STDIN else path
    try:
        text = sys.stdin.read() if path == STDIN else Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise TriplConfigError(f"--payloads: {path} does not exist.") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise TriplConfigError(f"--payloads: cannot read {path}: {exc}") from None
    return parse(text, label)


def parse(text: str, label: str) -> list[CheckItem]:
    stripped = text.strip()
    if not stripped:
        raise TriplConfigError(f"--payloads: {label} holds no events.")
    records: list[tuple[int, Any]] = []
    if stripped.startswith("["):
        try:
            document = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise TriplConfigError(f"--payloads: {label} is not valid JSON: {exc}") from None
        # A JSON array has no per-event line; report each at its 1-based position.
        records = [(number, record) for number, record in enumerate(document, start=1)]
        positional = True
    else:
        positional = False
        try:
            records = [(1, json.loads(stripped))]
        except json.JSONDecodeError:
            for number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    records.append((number, json.loads(line)))
                except json.JSONDecodeError as exc:
                    raise TriplConfigError(
                        f"--payloads: {label} line {number} is not valid JSON: {exc.msg}."
                    ) from None
    items = [_item(record, label, number, positional) for number, record in records]
    if not items:
        raise TriplConfigError(f"--payloads: {label} holds no events.")
    return items


def _item(record: Any, label: str, number: int, positional: bool) -> CheckItem:
    where = f"{label} {'event' if positional else 'line'} {number}"
    if not isinstance(record, dict):
        raise TriplConfigError(f"--payloads: {where} is not a JSON object.")
    unknown = sorted(key for key in record if key not in _KEYS)
    if unknown:
        raise TriplConfigError(
            f"--payloads: {where} has unknown key(s) {', '.join(unknown)}; "
            "an event is {event_type?, name?, fields?, properties?}."
        )
    event_type = _optional_text(record.get("event_type"), f"{where}: event_type")
    name = _optional_text(record.get("name"), f"{where}: name")
    fields_raw = record.get("fields") or {}
    if not isinstance(fields_raw, dict):
        raise TriplConfigError(f"--payloads: {where}: fields must be an object.")
    properties = record.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise TriplConfigError(f"--payloads: {where}: properties must be an object.")
    if event_type is None and name is None:
        raise TriplConfigError(f"--payloads: {where} names neither an event_type nor a name.")
    item = CheckItem(
        origin=Origin(
            label,
            1 if positional else number,
            snippet=f"event {number}" if positional else "",
        ),
        event_type=event_type,
        name=name,
        fields={str(key): _field_value(value) for key, value in fields_raw.items()},
        properties=properties,
        complete=True,
    )
    return enforce_limits(item)


def enforce_limits(item: CheckItem) -> CheckItem:
    """``item`` within the validator's size limits, each cut noted as a warning.

    A name, event type or field value over its limit becomes ``null``; a field
    or property key over its limit is dropped; fields past the count limit are
    dropped (the first ``MAX_FIELDS`` are kept); a ``properties`` object over the
    count limit is sent as ``null``. An item already within bounds comes back
    unchanged (the same object).
    """
    notes: list[CheckFinding] = []

    def note(message: str, field: str | None = None) -> None:
        notes.append(
            CheckFinding(
                code=CODE_OVERSIZE_VALUE,
                severity=SEVERITY_WARNING,
                message=message,
                field=field,
            )
        )

    name = item.name
    if name is not None and len(name) > limits.NAME_MAX_LENGTH:
        note(
            f"the name is {len(name)} characters, over the validator's "
            f"{limits.NAME_MAX_LENGTH}; it was sent as null"
        )
        name = None
    event_type = item.event_type
    if event_type is not None and len(event_type) > limits.EVENT_TYPE_MAX_LENGTH:
        note(
            f"the event type is {len(event_type)} characters, over the validator's "
            f"{limits.EVENT_TYPE_MAX_LENGTH}; it was sent as null"
        )
        event_type = None
    fields: dict[str, str | None] = {}
    for key, value in item.fields.items():
        if not _key_fits(key):
            note(_key_message(key, "field"))
            continue
        if len(fields) >= limits.MAX_FIELDS:
            note(
                f"the event has {len(item.fields)} fields, over the validator's "
                f"{limits.MAX_FIELDS}; the fields past the first {limits.MAX_FIELDS} "
                "were not sent"
            )
            break
        if value is not None and len(value) > limits.FIELD_VALUE_MAX_LENGTH:
            note(
                f"the value is {len(value)} characters, over the validator's "
                f"{limits.FIELD_VALUE_MAX_LENGTH}; it was sent as null",
                field=key,
            )
            value = None
        fields[key] = value
    properties = item.properties
    if properties is not None:
        if len(properties) > limits.MAX_FIELDS:
            note(
                f"properties holds {len(properties)} keys, over the validator's "
                f"{limits.MAX_FIELDS}; it was sent as null"
            )
            properties = None
        else:
            kept = {key: value for key, value in properties.items() if _key_fits(key)}
            for key in properties:
                if not _key_fits(key):
                    note(_key_message(key, "property"))
            properties = kept
    if not notes:
        return item
    return replace(
        item,
        name=name,
        event_type=event_type,
        fields=fields,
        properties=properties,
        notes=(*item.notes, *notes),
    )


def _key_fits(key: str) -> bool:
    return 1 <= len(key) <= limits.FIELD_KEY_MAX_LENGTH


def _key_message(key: str, what: str) -> str:
    if not key:
        return f"an empty {what} name is not accepted by the validator; it was not sent"
    return (
        f"a {what} name is {len(key)} characters, over the validator's "
        f"{limits.FIELD_KEY_MAX_LENGTH}; it was not sent"
    )


def _optional_text(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TriplConfigError(f"--payloads: {where} must be a non-empty string.")
    return value


def _field_value(value: Any) -> str | None:
    """Fields travel as strings: ``3`` -> ``"3"``, ``true`` -> ``"true"``, ``null`` stays null."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return json.dumps(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
