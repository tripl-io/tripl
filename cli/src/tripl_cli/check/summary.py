"""The Markdown summary of a run, for a merge request note (``--format markdown``).

A reviewer of a code change wants to know what it does to the analytics: which
events it starts sending, which it stops sending, and which now carry other
fields. Under a gate (``--new-from-rev``, ``--new-from-patch``) the base tree
is already scanned (``gate.mark``), so this compares what the two trees send,
event by event, and puts the run's failing findings under it.

An event here is ``(event type, identity)``: the identity the validator matched
the call by, else the name as the call spells it. A call with neither (nothing
about its event was read) cannot be compared, and is counted, never guessed at.

* **added** - no call at the base sent the event, one does now;
* **removed** - a call at the base sent it, none does now;
* **changed** - both do, and the field and property KEYS its calls send differ.
  Values are not compared: a renamed constant value is another event, and a
  changed literal value is the validator's business, not a reviewer's table.

With no gate there is no base, and the summary holds the findings alone.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from tripl_cli.check.model import (
    GATE_REV,
    MARKER_NO_CODES,
    MARKER_NO_REASON,
    MARKER_NOT_SUPPRESSIBLE,
    MODE_STATIC,
    CheckReport,
    CheckResult,
)
from tripl_cli.check.suppress import label
from tripl_cli.check.validate import summary as tally
from tripl_cli.render import plan_branch_note, plural

ADDED = "added"
REMOVED = "removed"
CHANGED = "changed"

# A note has a size limit (GitLab: 1 MB, GitHub: 65536 characters) and a reader.
MAX_ROWS = 50
MAX_FINDINGS = 20
MAX_PLACES = 3

TITLE = "### tripl check: analytics changes"

# Why a malformed marker suppresses nothing, as the summary says it after the
# marker itself (shown as code). The listing's own wording writes placeholders
# in angle brackets, which Markdown would read as HTML.
_PROBLEMS: Mapping[str, str] = {
    MARKER_NO_CODES: "names no code to waive",
    MARKER_NOT_SUPPRESSIBLE: "names `no_verdict`, which no marker can waive",
    MARKER_NO_REASON: "gives no reason after `--`",
}


@dataclass(frozen=True)
class EventChange:
    kind: str
    event_type: str
    event: str
    # Whether the plan knows the event (a call matched one), on the side that has it.
    planned: bool
    # Where it is sent: this tree's call sites, or the base's for a removed one.
    places: tuple[str, ...] = ()
    added_keys: tuple[str, ...] = ()
    removed_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChangeSummary:
    changes: tuple[EventChange, ...] = ()
    # Calls whose event the scan could not read, on either side: not compared.
    unread: int = 0

    def count(self, kind: str) -> int:
        return sum(1 for change in self.changes if change.kind == kind)


@dataclass
class _Side:
    planned: bool = False
    places: list[str] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)


def _event(result: CheckResult) -> tuple[str, str] | None:
    name = result.identity or result.item.name
    if result.item.event_type is None or not name:
        return None
    return result.item.event_type, name


def _keys(result: CheckResult) -> set[str]:
    properties = result.item.properties
    return {*result.item.fields, *(properties if isinstance(properties, Mapping) else ())}


def _sides(results: Sequence[CheckResult]) -> tuple[dict[tuple[str, str], _Side], int]:
    sides: dict[tuple[str, str], _Side] = {}
    unread = 0
    for result in results:
        event = _event(result)
        if event is None:
            unread += 1
            continue
        side = sides.setdefault(event, _Side())
        side.planned = side.planned or result.event_id is not None
        side.places.append(f"{result.item.origin.path}:{result.item.origin.line}")
        side.keys |= _keys(result)
    return sides, unread


def compare(base: Sequence[CheckResult], head: Sequence[CheckResult]) -> ChangeSummary:
    """What ``head`` sends that ``base`` did not, and the reverse, in a stable order."""
    before, unread_before = _sides(base)
    after, unread_after = _sides(head)
    changes: list[EventChange] = []
    for event, side in after.items():
        old = before.get(event)
        places = tuple(side.places)
        if old is None:
            changes.append(EventChange(ADDED, *event, side.planned, places))
            continue
        gained = tuple(sorted(side.keys - old.keys))
        lost = tuple(sorted(old.keys - side.keys))
        if gained or lost:
            changes.append(EventChange(CHANGED, *event, side.planned, places, gained, lost))
    for event, side in before.items():
        if event not in after:
            changes.append(EventChange(REMOVED, *event, side.planned, tuple(side.places)))
    order = {ADDED: 0, CHANGED: 1, REMOVED: 2}
    changes.sort(key=lambda change: (order[change.kind], change.event_type, change.event))
    return ChangeSummary(tuple(changes), unread_before + unread_after)


def _code(text: str) -> str:
    """``text`` as inline code in a table cell: no backtick ends it, no pipe ends the cell."""
    return "`" + text.replace("`", "'").replace("|", "\\|").replace("\n", " ") + "`"


def _inline(text: str) -> str:
    """``text`` as inline code in running text or a list item: no backtick ends it.

    Not ``_code``: outside a table a pipe is plain text, and a backslash before
    it inside a code span is shown as written.
    """
    return "`" + text.replace("`", "'").replace("\n", " ") + "`"


def _places(places: Sequence[str]) -> str:
    shown = ", ".join(_code(place) for place in places[:MAX_PLACES])
    more = len(places) - MAX_PLACES
    return f"{shown} and {more} more" if more > 0 else shown


def _details(change: EventChange) -> str:
    if change.kind == ADDED:
        return _places(change.places)
    if change.kind == REMOVED:
        return f"was at {_places(change.places)}"
    parts = []
    if change.added_keys:
        parts.append("now sends " + ", ".join(_code(key) for key in change.added_keys))
    if change.removed_keys:
        parts.append("no longer sends " + ", ".join(_code(key) for key in change.removed_keys))
    return "; ".join(parts)


def _compared_with(report: CheckReport) -> str:
    gate = report.gate
    assert gate is not None
    if gate.source == GATE_REV:
        sha = f" (`{gate.base[:12]}`)" if gate.base else ""
        return f"Compared with {_code(gate.rev or '')}{sha}."
    return f"Compared with the base of the patch {_code(gate.patch or '')}."


def _change_lines(report: CheckReport) -> list[str]:
    if report.gate is None:
        return [
            "No change was compared: run with `--new-from-rev REF` or `--new-from-patch FILE` "
            "to list the events a change adds, removes or changes."
        ]
    lines = [_compared_with(report), ""]
    found = report.changes
    if found is None:
        return [
            *lines,
            "The base could not be rebuilt from the change, so the events it adds or removes "
            "are not listed.",
        ]
    if not found.changes:
        lines.append("**No event added, removed or changed.**")
    else:
        lines.append(
            f"**{plural(found.count(ADDED), 'event')} added, {found.count(CHANGED)} changed, "
            f"{found.count(REMOVED)} removed.**"
        )
        lines += [
            "",
            "| Change | Event type | Event | In the plan | Details |",
            "|--------|------------|-------|-------------|---------|",
        ]
        for change in found.changes[:MAX_ROWS]:
            lines.append(
                f"| {change.kind.capitalize()} | {_code(change.event_type)} | "
                f"{_code(change.event)} | {'yes' if change.planned else '**no**'} | "
                f"{_details(change)} |"
            )
        if len(found.changes) > MAX_ROWS:
            lines += ["", f"And {len(found.changes) - MAX_ROWS} more."]
    if found.unread:
        lines += [
            "",
            f"{plural(found.unread, 'call')} whose event the scan could not read "
            f"{'is' if found.unread == 1 else 'are'} not compared "
            "(`tripl check --list` shows why).",
        ]
    return lines


def _finding_lines(report: CheckReport) -> list[str]:
    counts = tally(report.results)
    what = "call site" if report.mode == MODE_STATIC else "event"
    failing = [result for result in report.results if report.fails(result)]
    verdict = "failed" if report.exit_code else "passed"
    lines = [
        f"**Check {verdict}:** {plural(counts['checked'], what)}, {counts['ok']} ok, "
        f"{plural(counts['warnings'], 'warning')}, {plural(counts['errors'], 'error')}"
        + (f", {plural(len(failing), 'call')} failing the run." if failing else "."),
    ]
    shown = 0
    for result in failing:
        for finding in result.findings:
            if finding.suppression is not None:
                continue
            if shown == MAX_FINDINGS:
                break
            shown += 1
            origin = result.item.origin
            field = f" [{finding.field}]" if finding.field else ""
            lines.append(
                f"- {_code(f'{origin.path}:{origin.line}')} {finding.severity} "
                f"{_code(finding.label)}{field}: {finding.message}"
            )
    total = sum(
        1 for result in failing for finding in result.findings if finding.suppression is None
    )
    if total > shown:
        lines.append(f"- and {total - shown} more.")
    return lines + _marker_lines(report)


def _marker_lines(report: CheckReport) -> list[str]:
    """The markers that fail the run (``--strict-suppressions``), so a failed check says why."""
    failing = report.failing_markers
    if not failing:
        return []
    lines = [
        "",
        f"**{plural(len(failing), 'suppression marker')} "
        f"{'fails' if len(failing) == 1 else 'fail'} the run** (`--strict-suppressions`):",
    ]
    for state in failing[:MAX_FINDINGS]:
        marker = state.marker
        where = _inline(f"{marker.path}:{marker.line}")
        if marker.problem is None:
            lines.append(f"- {where} {_inline(label(marker))} matched no finding")
        else:
            why = _PROBLEMS.get(marker.problem, _inline(marker.problem))
            lines.append(f"- {where} {_inline(marker.text)} {why}, so it suppresses nothing")
    if len(failing) > MAX_FINDINGS:
        lines.append(f"- and {len(failing) - MAX_FINDINGS} more.")
    return lines


def markdown_document(report: CheckReport) -> str:
    project = f"Project {_code(report.project)}"
    branch = f", plan branch {_code(report.branch_name)}" if report.branch_name else ""
    if report.plan is not None:
        branch = f", plan revision {_code(report.plan.revision)}"
    lines = [TITLE, "", f"{project}{branch}.", ""]
    if report.branch_requested is not None:
        note = plan_branch_note(
            report.branch_requested, report.branch_status, used_main=report.branch_id is None
        )
        if note is not None:
            lines += [note, ""]
    lines += [*_change_lines(report), ""]
    lines += _finding_lines(report)
    return "\n".join(lines) + "\n"
