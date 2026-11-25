"""Plan coverage: the planned events that no scanned call site sends.

``tripl check`` answers "does the code match the plan"; ``--coverage`` answers
the opposite question for the same run: which events the plan says are (or are
about to be) in the code that the scan found no call for.

An event is **found** when a result of this run carries its ``event_id``. Every
other planned event falls in one of three groups, and the report never merges
them, because they call for different actions:

* ``not_found`` - no call sends it, and every call of its event type that
  matched no event was fully read. The event is not sent from the scanned files.
* ``maybe_dynamic`` - a call of its event type matched no event AND has a value
  the scan could not read, so that call may be this event. A call with a
  templated name (``promo_${id}_shown``) is such a call only for the events
  whose name it could spell; a call with an unread target (a variable name, an
  unread field) is one for every event of its type, since an event's identity
  may be built from any field.
* ``not_scanned`` - its event type has no call spec in the check config, so the
  scan did not look. Counted per type, not listed.

Only the statuses in ``COVERED_STATUSES`` are planned-to-be-in-code. A draft or
an event in review is not expected yet, and a deprecated or archived one is
expected to disappear.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from tripl_cli.check.model import CheckResult
from tripl_cli.model import JsonDict, JsonList

# EventStatus values (``api.events.STATUSES``) of an event the code should send.
COVERED_STATUSES: tuple[str, ...] = ("ready_for_dev", "implemented", "live")

_HOLE = re.compile(r"\$\{[^}]*\}")


@dataclass(frozen=True)
class PlannedEvent:
    event_id: str
    name: str
    event_type: str
    status: str

    def document(self) -> JsonDict:
        return {
            "event_id": self.event_id,
            "name": self.name,
            "event_type": self.event_type,
            "status": self.status,
        }


@dataclass(frozen=True)
class Coverage:
    statuses: tuple[str, ...]
    # Planned events of the scanned event types that a call of this run matched.
    found: int
    not_found: tuple[PlannedEvent, ...] = ()
    maybe_dynamic: tuple[PlannedEvent, ...] = ()
    # Event type -> its planned events, for the types the config has no calls for.
    not_scanned: Mapping[str, int] = field(default_factory=dict)
    # Calls that matched no event and have a value the scan did not read.
    unread_calls: int = 0

    @property
    def planned(self) -> int:
        """The planned events of the scanned event types."""
        return self.found + len(self.not_found) + len(self.maybe_dynamic)


def _spells(template: str) -> re.Pattern[str]:
    """What a templated name could be at runtime: each ``${...}`` hole is any text."""
    literal = _HOLE.split(template)
    return re.compile(".+".join(re.escape(part) for part in literal), re.DOTALL)


def _unread(result: CheckResult) -> tuple[bool, re.Pattern[str] | None]:
    """How a call that matched no event could still be one: (any of its type, by name).

    A target the scan did not read, or a hole in a field, can stand for any
    event of the type. A hole in the name alone narrows it to the names the
    template could spell.
    """
    item = result.item
    holed_field = any(value is not None and _HOLE.search(value) for value in item.fields.values())
    if item.dynamic or holed_field:
        return True, None
    if item.name is not None and _HOLE.search(item.name):
        return False, _spells(item.name)
    return False, None


def build(
    events: JsonList,
    type_names: Mapping[str, str],
    scanned_types: Iterable[str],
    results: Sequence[CheckResult],
) -> Coverage:
    """``events`` are ``GET /events`` rows already filtered to ``COVERED_STATUSES``."""
    scanned = set(scanned_types)
    matched = {result.event_id for result in results if result.event_id is not None}
    any_of_type: set[str] = set()
    by_name: dict[str, list[re.Pattern[str]]] = {}
    unread_calls = 0
    for result in results:
        if result.event_id is not None or result.item.event_type is None:
            continue
        whole_type, pattern = _unread(result)
        if not whole_type and pattern is None:
            continue
        unread_calls += 1
        if whole_type:
            any_of_type.add(result.item.event_type)
        elif pattern is not None:
            by_name.setdefault(result.item.event_type, []).append(pattern)

    found = 0
    not_found: list[PlannedEvent] = []
    maybe_dynamic: list[PlannedEvent] = []
    not_scanned: dict[str, int] = {}
    for row in events:
        event_id = str(row.get("id") or "")
        type_name = type_names.get(str(row.get("event_type_id") or ""), "")
        if type_name not in scanned:
            not_scanned[type_name] = not_scanned.get(type_name, 0) + 1
            continue
        if event_id in matched:
            found += 1
            continue
        name = str(row.get("name") or "")
        event = PlannedEvent(event_id, name, type_name, str(row.get("status") or ""))
        # The server matches a call by `source_name` where the event has one.
        spellings = [text for text in (row.get("source_name"), name) if isinstance(text, str)]
        dynamic = type_name in any_of_type or any(
            pattern.fullmatch(text) for pattern in by_name.get(type_name, ()) for text in spellings
        )
        (maybe_dynamic if dynamic else not_found).append(event)
    return Coverage(
        statuses=COVERED_STATUSES,
        found=found,
        not_found=tuple(not_found),
        maybe_dynamic=tuple(maybe_dynamic),
        not_scanned=dict(sorted(not_scanned.items())),
        unread_calls=unread_calls,
    )


def coverage_document(coverage: Coverage) -> JsonDict:
    return {
        "statuses": list(coverage.statuses),
        "planned": coverage.planned,
        "found": coverage.found,
        "not_found": [event.document() for event in coverage.not_found],
        "maybe_dynamic": [event.document() for event in coverage.maybe_dynamic],
        "not_scanned": dict(coverage.not_scanned),
        "unread_calls": coverage.unread_calls,
    }


def _listing(events: Sequence[PlannedEvent]) -> list[str]:
    type_width = max(len(event.event_type) for event in events)
    name_width = max(len(event.name) for event in events)
    return [
        f"  {event.event_type:<{type_width}}  {event.name:<{name_width}}  {event.status}"
        for event in events
    ]


def coverage_lines(coverage: Coverage) -> list[str]:
    """The text section, after the check's own tally."""
    statuses = ", ".join(coverage.statuses)
    planned = coverage.planned
    lines = [
        "",
        f"Plan coverage ({statuses}): {planned} planned "
        f"{'event' if planned == 1 else 'events'} of the scanned event types, "
        f"{coverage.found} found in code, {len(coverage.not_found)} not found, "
        f"{len(coverage.maybe_dynamic)} possibly sent by a call the scan did not read.",
    ]
    if coverage.not_found:
        lines += ["", "Not found in the scanned files:", *_listing(coverage.not_found)]
    if coverage.maybe_dynamic:
        calls = coverage.unread_calls
        lines += [
            "",
            f"Possibly sent: {calls} {'call matches' if calls == 1 else 'calls match'} no event "
            "and could not be fully read (`tripl check --list` shows why):",
            *_listing(coverage.maybe_dynamic),
        ]
    if coverage.not_scanned:
        total = sum(coverage.not_scanned.values())
        per_type = ", ".join(f"{name} ({count})" for name, count in coverage.not_scanned.items())
        lines += [
            "",
            f"Not scanned: {total} planned {'event' if total == 1 else 'events'} of event types "
            f"the check config has no calls for: {per_type}.",
        ]
    return lines
