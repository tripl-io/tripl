"""The baseline: the findings a repository already has, in a file it commits (``--baseline``).

The gate (``gate.py``) needs a diff base, which a default-branch pipeline, a
nightly schedule (the plan changes in tripl, not in git) and ``--payloads``
(captured events, no source) do not have. A baseline file stands in for it:
``--write-baseline FILE`` records every finding that fails the run today, and
``--baseline FILE`` fails a later run only on what the file does not hold.

An entry is a finding without its place: the item it is on, by what the item
sends (``baseline_key``: ``gate.item_key``, so never its line or its file),
and the finding's code and field, with how many such findings there were and
the most severe of them. A finding is held by the baseline when an entry for
its item, code and field exists at its severity or a higher one, and the
entry's count is not used up: three identical calls where the file holds two
leave one new (a copy pasted anywhere), and a warning that became an error is
new (an escalation). Which copy is the new one is decided as the gate decides
it: under a gate, the findings the change did not bring in take up the count
first, so a pasted copy is the one that fails. A finding a marker suppresses
takes up nothing (the marker wins), and ``no_verdict`` is never held: nothing
was decided about the call.

The file is JSON, written so that it diffs cleanly in review: no timestamp,
entries sorted, one key per line, each entry with a short ``example`` (the
item and one place it was at when written) for a reader. It names the
project, the mode, the key recipe and ``FINGERPRINT_KEY``, and a file written
for anything else is refused (exit 2) before any request, rather than read as
a baseline holding nothing.

``--prune-baseline`` rewrites the file without what this run no longer has
(an entry's count lowered, or the entry dropped, its severity lowered to the
worst still seen); it never adds anything.

Captured events (``--payloads``) are matched by presence, not by count: how
many times a test run fired an event is runtime frequency, not copies of a
call in the code. An entry holds every finding of its key, code and field;
its count is what was seen when it was written, for a reader, and a prune
only drops an entry this run did not see at all.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from tripl_cli import __version__
from tripl_cli.check.fingerprint import FINGERPRINT_KEY
from tripl_cli.check.gate import item_key
from tripl_cli.check.model import (
    CODE_NO_VERDICT,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    CheckFinding,
    CheckItem,
    CheckReport,
    CheckResult,
    severe,
)
from tripl_cli.errors import TriplConfigError
from tripl_cli.model import JsonDict

# The file's format and its key recipe (``baseline_key``): a change to either
# must change this, or an old file would be read as one holding nothing.
BASELINE_VERSION = 1

_RANK = {SEVERITY_ERROR: 2, SEVERITY_WARNING: 1, SEVERITY_INFO: 0}
_SEVERITIES = tuple(_RANK)


@dataclass(frozen=True)
class Entry:
    """One line of the baseline: ``count`` findings of ``code`` on ``field`` at items like ``key``.

    ``key`` is ``baseline_key``'s canonical JSON; ``severity`` the most
    severe of the findings; ``example`` what a reader is shown, never compared.
    """

    key: str
    code: str
    field: str | None
    severity: str
    count: int
    example: str = ""

    @property
    def group(self) -> tuple[str, str, str | None]:
        return (self.key, self.code, self.field)


@dataclass(frozen=True)
class Baseline:
    """A baseline file as read: its path as given, and its entries."""

    path: str
    entries: tuple[Entry, ...]

    @property
    def findings(self) -> int:
        return sum(entry.count for entry in self.entries)


@dataclass(frozen=True)
class Matching:
    """``match``'s answer: the results with ``baselined`` set, and what each entry took.

    ``seen`` counts per entry the findings it holds in this run, ``worst``
    the most severe of them (None for an entry nothing took). ``presence``
    is the payload rule: an entry's count was not enforced.
    """

    results: tuple[CheckResult, ...]
    seen: tuple[int, ...]
    worst: tuple[str | None, ...]
    presence: bool = False

    def stale(self, entries: Sequence[Entry]) -> int:
        """How many findings the file holds that this run did not have.

        By presence, an entry this run saw holds nothing stale, and one it
        did not see is stale as a whole.
        """
        pairs = zip(entries, self.seen, strict=True)
        if self.presence:
            return sum(entry.count for entry, seen in pairs if not seen)
        return sum(entry.count - seen for entry, seen in pairs)


def baseline_key(item: CheckItem) -> str:
    """What an item is for the baseline: ``gate.item_key`` without its markers.

    Never its line or its file. A captured event has no callee, and its
    snippet is its position in a JSON array, so it is keyed on what it
    sends alone, and its properties by their names only: their values are
    runtime data (a timestamp, a session), and the same event captured again
    must stay the same entry. Its fields are kept as they are: they name the
    event. The markers on a call are left out: one added for a finding must
    not make the call's other, baselined findings new.
    """
    if item.origin.column is None:
        names = None if item.properties is None else dict.fromkeys(sorted(item.properties))
        item = replace(item, origin=replace(item.origin, snippet=""), properties=names)
    return item_key(item, markers=False)


def _rank(severity: str) -> int:
    return _RANK.get(severity, 0)


def _counted(finding: CheckFinding) -> bool:
    """Whether a baseline can hold ``finding``: not suppressed, and not ``no_verdict``."""
    return finding.suppression is None and finding.code != CODE_NO_VERDICT


# --- reading -------------------------------------------------------------------------
def load(path: str, *, project: str, mode: str) -> Baseline:
    """Read and check the baseline file at ``path``; any problem is a usage error (exit 2).

    Refused: a file that cannot be read or is not a baseline, one of another
    format version or key recipe, or one written for another project or mode.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise TriplConfigError(
            f"--baseline: {path} does not exist; write it with tripl check --write-baseline {path}."
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise TriplConfigError(f"--baseline: cannot read {path}: {exc}") from None
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TriplConfigError(f"--baseline: {path} is not valid JSON: {exc}") from None
    if not isinstance(document, dict) or "entries" not in document:
        raise TriplConfigError(f"--baseline: {path} is not a tripl check baseline.")
    rewrite = f"rewrite it with tripl check --write-baseline {path}"
    version = document.get("version")
    if version != BASELINE_VERSION:
        raise TriplConfigError(
            f"--baseline: {path} is baseline version {version!r}, and this tripl reads "
            f"version {BASELINE_VERSION}; upgrade tripl, or {rewrite}."
        )
    key = document.get("fingerprint_key")
    if key != FINGERPRINT_KEY:
        raise TriplConfigError(
            f"--baseline: {path} was written for fingerprints {key!r}, and this tripl "
            f"computes {FINGERPRINT_KEY}; {rewrite}."
        )
    if document.get("project") != project:
        raise TriplConfigError(
            f"--baseline: {path} is project {document.get('project')!r}'s, and this run "
            f"checks {project!r}; pass the baseline of {project}, or {rewrite}."
        )
    if document.get("mode") != mode:
        raise TriplConfigError(
            f"--baseline: {path} holds {document.get('mode')!r} findings, and this run is "
            f"{mode!r}; keep one baseline per mode."
        )
    raw = document["entries"]
    if not isinstance(raw, list):
        raise TriplConfigError(f"--baseline: {path}: entries must be a list.")
    entries = [_entry(value, path, number) for number, value in enumerate(raw, start=1)]
    seen: set[tuple[str, str, str | None]] = set()
    for number, entry in enumerate(entries, start=1):
        if entry.group in seen:
            raise TriplConfigError(
                f"--baseline: {path} entry {number} repeats an earlier entry's key, code and "
                f"field; merge the two counts, or {rewrite}."
            )
        seen.add(entry.group)
    return Baseline(path, tuple(entries))


def _entry(value: Any, path: str, number: int) -> Entry:
    where = f"--baseline: {path} entry {number}"
    if not isinstance(value, dict):
        raise TriplConfigError(f"{where} is not a JSON object.")
    key, code, field = value.get("key"), value.get("code"), value.get("field")
    severity, count, example = value.get("severity"), value.get("count"), value.get("example", "")
    if not _well_formed(key):
        raise TriplConfigError(f"{where}: key must be the list tripl wrote.")
    if not isinstance(code, str) or not code:
        raise TriplConfigError(f"{where}: code must be a finding code.")
    if field is not None and not isinstance(field, str):
        raise TriplConfigError(f"{where}: field must be a string or null.")
    if severity not in _SEVERITIES:
        raise TriplConfigError(f"{where}: severity must be one of {', '.join(_SEVERITIES)}.")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise TriplConfigError(f"{where}: count must be a whole number from 1.")
    if not isinstance(example, str):
        raise TriplConfigError(f"{where}: example must be a string.")
    # The key as item_key prints it, so the two compare as strings.
    canonical = json.dumps(key, sort_keys=True, ensure_ascii=False, default=str)
    return Entry(canonical, code, field, str(severity), count, example)


_WIRE = frozenset({"event_type", "name", "fields", "properties", "complete"})


def _well_formed(key: Any) -> bool:
    """Whether ``key`` has the shape ``baseline_key`` writes (``gate.item_key``, no markers).

    ``[callee, wire, sendable, dynamic, notes, []]``. Anything else could
    never match a finding, so a prune would drop it without a word: a hand
    edit that broke the file is refused instead.
    """
    if not isinstance(key, list) or len(key) != 6:
        return False
    callee, wire, sendable, dynamic, notes, waived = key
    return (
        isinstance(callee, str)
        and isinstance(wire, dict)
        and set(wire) == _WIRE
        and all(isinstance(wire[name], str | None) for name in ("event_type", "name"))
        and isinstance(wire["fields"], dict)
        and isinstance(wire["properties"], dict | None)
        and isinstance(wire["complete"], bool)
        and isinstance(sendable, bool)
        and isinstance(dynamic, list)
        and all(isinstance(target, str) for target in dynamic)
        and isinstance(notes, list)
        and all(
            isinstance(note, list)
            and len(note) == 2
            and all(isinstance(part, str) for part in note)
            for note in notes
        )
        and waived == []
    )


# --- matching ------------------------------------------------------------------------
def match(
    results: Sequence[CheckResult], entries: Sequence[Entry], *, presence: bool = False
) -> Matching:
    """``results`` with ``baselined`` on every finding, as copies, and what each entry held.

    Each entry holds up to ``count`` findings of its key, code and field at
    its severity or below. When there are more, the ones the change did not
    bring in (under a gate) go first, then the more severe, then in run
    order; the rest are new. With ``presence`` (captured events) the count is
    not enforced: an entry holds every such finding.
    """
    index = {entry.group: position for position, entry in enumerate(entries)}
    found: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for at, result in enumerate(results):
        key: str | None = None
        for position, finding in enumerate(result.findings):
            if not _counted(finding):
                continue
            key = key if key is not None else baseline_key(result.item)
            entry = index.get((key, finding.code, finding.field))
            if entry is not None and _rank(finding.severity) <= _rank(entries[entry].severity):
                found[entry].append((at, position))
    held: set[tuple[int, int]] = set()
    seen = [0] * len(entries)
    worst: list[str | None] = [None] * len(entries)

    def order(place: tuple[int, int]) -> tuple[bool, int, tuple[int, int]]:
        finding = results[place[0]].findings[place[1]]
        return (finding.in_change is True, -_rank(finding.severity), place)

    for entry, places in found.items():
        limit = None if presence else entries[entry].count
        for place in sorted(places, key=order)[:limit]:
            held.add(place)
            seen[entry] += 1
            severity = results[place[0]].findings[place[1]].severity
            if worst[entry] is None or _rank(severity) > _rank(worst[entry] or ""):
                worst[entry] = severity
    marked = tuple(
        replace(
            result,
            findings=tuple(
                replace(finding, baselined=(at, position) in held)
                for position, finding in enumerate(result.findings)
            ),
        )
        for at, result in enumerate(results)
    )
    return Matching(marked, tuple(seen), tuple(worst), presence)


def collect(report: CheckReport) -> tuple[Entry, ...]:
    """The baseline of ``report``: every finding that fails it on its own, gate and baseline aside.

    That is a severe finding (an error, or anything under ``--strict``) that
    no marker suppresses, ``no_verdict`` excepted. Each entry's example is its
    first place in the repository, in path and line order.
    """
    counts: dict[tuple[str, str, str | None], int] = defaultdict(int)
    worst: dict[tuple[str, str, str | None], str] = {}
    first: dict[tuple[str, str, str | None], tuple[tuple[str, int, int], str]] = {}
    for result in report.results:
        key: str | None = None
        for finding in result.findings:
            if not (_counted(finding) and severe(finding, report.strict)):
                continue
            key = key if key is not None else baseline_key(result.item)
            group = (key, finding.code, finding.field)
            counts[group] += 1
            if group not in worst or _rank(finding.severity) > _rank(worst[group]):
                worst[group] = finding.severity
            origin = result.item.origin
            place = (report.repo_path(origin), origin.line, origin.column or 0)
            if group not in first or place < first[group][0]:
                first[group] = (place, _example(result.item, place[0]))
    entries = (Entry(*group, worst[group], counts[group], first[group][1]) for group in counts)
    return tuple(sorted(entries, key=entry_order))


def _example(item: CheckItem, path: str) -> str:
    """The item as a reader finds it: its type and name (or field values), and where it was."""
    label = item.name or (
        " ".join(f"{key}={'?' if value is None else value}" for key, value in item.fields.items())
        or "(nothing known)"
    )
    origin = item.origin
    # A JSON array's event has no line of its own, only its position.
    where = f"{path} ({origin.snippet})" if origin.column is None and origin.snippet else None
    return f"{item.event_type or '-'} {label} at {where or f'{path}:{origin.line}'}"


def entry_order(entry: Entry) -> tuple[str, str, str, str]:
    return (entry.key, entry.code, entry.field or "", entry.severity)


def pruned(entries: Sequence[Entry], matching: Matching) -> tuple[Entry, ...]:
    """``entries`` without what this run no longer has; nothing is ever added.

    An entry keeps the count of findings it held in this run and their worst
    severity; one that held none is dropped. Its example stays as written.
    By presence (captured events) the count is never adjusted.
    """
    kept = (
        replace(
            entry,
            count=entry.count if matching.presence else seen,
            severity=worst or entry.severity,
        )
        for entry, seen, worst in zip(entries, matching.seen, matching.worst, strict=True)
        if seen
    )
    return tuple(sorted(kept, key=entry_order))


# --- writing -------------------------------------------------------------------------
def render_file(entries: Sequence[Entry], *, project: str, mode: str) -> str:
    """The baseline file: the same entries always give the same bytes.

    No timestamp, entries sorted, and each key on one line, so a rerun
    rewrites nothing and a review diff shows one line per finding that came
    or went.
    """
    header = [
        ("version", json.dumps(BASELINE_VERSION)),
        ("fingerprint_key", json.dumps(FINGERPRINT_KEY)),
        ("project", _text(project)),
        ("mode", _text(mode)),
        ("cli_version", _text(__version__)),
    ]
    lines = ["{", *(f'  "{name}": {value},' for name, value in header)]
    if not entries:
        lines.append('  "entries": []')
    else:
        lines.append('  "entries": [')
        for number, entry in enumerate(sorted(entries, key=entry_order), start=1):
            values = [
                ("code", _text(entry.code)),
                ("field", _text(entry.field)),
                ("severity", _text(entry.severity)),
                ("count", json.dumps(entry.count)),
                ("example", _text(entry.example)),
                ("key", entry.key),
            ]
            body = ",\n".join(f'      "{name}": {value}' for name, value in values)
            lines.append("    {\n" + body + "\n    }" + ("," if number < len(entries) else ""))
        lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _text(value: str | None) -> str:
    return json.dumps(value, ensure_ascii=False)


# --- the report ----------------------------------------------------------------------
def baseline_document(report: CheckReport) -> JsonDict | None:
    """The baseline as ``--json`` and SARIF carry it; None for a run with none.

    ``entries`` and ``findings`` are the file's (as read, or as written),
    ``baselined`` counts this run's findings the file holds, ``stale`` the
    file's findings this run did not have; ``pruned`` and ``not_pruned`` are
    ``--prune-baseline``'s, null without it.
    """
    baseline = report.baseline
    if baseline is None:
        return None
    return {
        "path": baseline.path,
        "action": baseline.action,
        "entries": baseline.entries,
        "findings": baseline.findings,
        "baselined": sum(
            1 for result in report.results for finding in result.findings if finding.baselined
        ),
        "stale": baseline.stale,
        "pruned": baseline.pruned,
        "not_pruned": baseline.not_pruned,
    }
