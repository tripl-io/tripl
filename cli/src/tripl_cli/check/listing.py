"""``tripl check --list``'s text: what each matched call read, why not, and what to do next.

One line per call site the check config matches, in scan order, with what the
scan read for each target; under a call with a value it did not read, one
indented line per reason, the way ``tripl check`` prints findings under a
result. Then the summary: the tally and the reasons per target, the functions
that forward their own parameter into a tracking call, as a value or as its
object argument (each wants a call spec of its own), and the calls that look
like tracking but match no call spec. When the scan read a
``tripl-check-ignore`` marker: under each call the markers covering it, and
after the summary how many there are and the ones that suppress nothing
(malformed, or on no matched call). The
JSON document is ``tripl_cli.report.check_list_document``; both read their
counts from ``inventory.summarize``, so the two cannot disagree.

A value the scan did not read is "not read", never "only known at runtime":
many are knowable at scan time (a format's template, two constants joined), and
the scan just does not read them yet. ASCII only, like every other listing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from tripl_cli.check import inventory
from tripl_cli.check.inventory import (
    ForwardedParameter,
    Forwarder,
    Inventory,
    ListedCall,
    ListedMarker,
    Location,
    ReadValue,
    Summary,
    UnmatchedGroup,
)
from tripl_cli.check.model import (
    REASON_CODES,
    REASON_CONCATENATION,
    REASON_CONDITIONAL,
    REASON_EXPRESSION,
    REASON_FORMAT,
    REASON_FUNCTION,
    REASON_LOCAL,
    REASON_NOT_PASSED,
    REASON_PARAMETER,
    REASON_PARAMETER_PROPERTY,
    REASON_UNKNOWN_CONSTANT,
    REASON_VARIABLE,
    REASON_WRONG_SHAPE,
    SIGNAL_CONSTANT,
    MarkerState,
)
from tripl_cli.check.render import location, marker_line, nothing_matched, render_skipped
from tripl_cli.check.suppress import label
from tripl_cli.check.values import Value
from tripl_cli.render import columns, plural

GUIDE_URL = "https://docs.tripl.io/integrate/tripl-check#config-file"
# The choices a value shows before the rest are only counted.
CHOICES_SHOWN = 5
# The groups of tracking-like calls shown besides every `constant` one; `--json` lists all.
UNMATCHED_SHOWN = 10

# What each reason code means, for the `Why` rows. The docs carry the same table.
REASON_TEXT: dict[str, str] = {
    REASON_VARIABLE: (
        "a variable the scan does not follow (a property, a lambda parameter, "
        "a captured variable), not a constant"
    ),
    REASON_UNKNOWN_CONSTANT: (
        "a constant or enum case the scan could not resolve: add its file to enums: "
        "or sources:, or name its type more fully when several share the name; "
        "an enum case needs a string raw value; a local named like a constant needs nothing"
    ),
    REASON_FORMAT: "a printf-style format, which the scan does not fill in yet",
    # Examples of the declarations whose returns `symbols` reads, a shape per
    # language; any declared return type String, string or NSString will do.
    REASON_FUNCTION: (
        "a function call; the scan reads one whose every return is a string literal, "
        'declared like fun f(): String, fun f() = "...", func f() -> String, String f() '
        "or function f(): string, in a file under sources: or enums:"
    ),
    REASON_CONDITIONAL: "a choice between values (if / when / a ternary / a fallback)",
    REASON_CONCATENATION: "a concatenation the scan does not join yet",
    REASON_EXPRESSION: "an expression the scan does not evaluate",
    REASON_PARAMETER: (
        "a parameter of the function the call sits in: the value comes from its "
        "callers; add a call spec for that function"
    ),
    REASON_PARAMETER_PROPERTY: (
        "a property of such a parameter: the value is set where that object is built; "
        "configure that, not the function's callers"
    ),
    REASON_LOCAL: "a local variable; the scan does not follow locals",
    # Reported on the name: nothing the call passes gives the event a name or a field.
    REASON_NOT_PASSED: (
        "the call passes no name and no field value the scan can read (no argument or "
        "object key the spec maps to one, only null ones, or an object argument that is "
        "not a literal)"
    ),
    REASON_WRONG_SHAPE: (
        "the value has the wrong shape for its target (properties that are not "
        "a dictionary, a name or field that is one)"
    ),
}


def render_listing(found: Inventory, *, config: str | None) -> str:
    """The whole ``--list`` text. ``config`` is the check config's path, None in discovery mode."""
    summary = inventory.summarize(found)
    lines = [
        f"tripl check --list - {config or 'no check config'}, root {found.root} (nothing is sent)",
        "",
    ]
    for call in found.calls:
        lines.append(_call_line(call))
        lines.extend(_reason_lines(call))
        lines.extend(_suppression_lines(call))
    if found.calls:
        lines.append("")
    lines.extend(_tally(summary, found.files))
    if summary.dynamic:
        lines += ["", *_why(summary)]
    if found.forwarders:
        lines += ["", *_forwarders(found.forwarders)]
    if found.markers:
        lines += ["", *_markers(found, summary)]
    if not found.calls:
        lines += ["", _no_call(found, config)]
    lines += ["", *_unmatched(found.unmatched)]
    notes: list[str] = []
    if found.truncated:
        notes.append(
            f"Discovery stopped at {plural(inventory.MAX_DISCOVERY_FILES, 'source file')}; "
            "write .tripl/check.yml with sources: to scan the rest."
        )
    if found.skipped:
        notes.append(render_skipped(found.skipped))
    if notes:
        lines += ["", *notes]
    return "\n".join(lines)


# --- one call ------------------------------------------------------------------------
def _call_line(call: ListedCall) -> str:
    """``path:line:col  event_type  callee  name=... field=...``: what was read per target."""
    parts = [location(call.origin), call.event_type, _flat(call.origin.snippet)]
    read = " ".join(f"{target}={_shown(value.value)}" for target, value in call.read.targets())
    return "  ".join([*parts, read] if read else parts)


def _shown(value: Value) -> str:
    """A value on one line: text as is, choices as ``a|b``, a dictionary as ``{key=value}``.

    ``?`` is a value the scan did not read, ``null`` an entry given as nothing.
    """
    if value.kind == "text":
        return _flat(value.text or "")
    if value.kind == "choices":
        shown = "|".join(_flat(choice) for choice in value.choices[:CHOICES_SHOWN])
        more = len(value.choices) - CHOICES_SHOWN
        return f"{shown}|... (+{more})" if more > 0 else shown
    if value.kind == "entries":
        entries = ", ".join(f"{_flat(key)}={_shown(entry)}" for key, entry in value.entries)
        return "{" + entries + "}"
    if value.kind == "absent":
        return "null"
    return "?"


def _reason_lines(call: ListedCall) -> list[str]:
    """Under a call not fully read: one line per distinct reason, then its unmapped arguments."""
    if call.fully_read:
        return []
    values = dict(call.read.targets())
    targets_by_text: dict[str, list[str]] = {}
    for target, code in call.dynamic.items():
        text = _reason_text(code, values.get(target), call.read.unread_object)
        targets_by_text.setdefault(text, []).append(target)
    lines = [f"    {', '.join(targets)}: {text}" for text, targets in targets_by_text.items()]
    if call.read.unmapped:
        lines.append(f"    not mapped: {', '.join(call.read.unmapped)}")
    return lines


def _suppression_lines(call: ListedCall) -> list[str]:
    """Under a call, read or not: one line per valid marker covering it, with its reason.

    The line starts with the marker as written, not with a ``word:`` prefix: a
    reason line starts with a target's name, and a field can have any name.
    """
    return [
        f"    {label(suppression)} -- {_flat(suppression.reason)} (line {suppression.line})"
        for suppression in call.suppressions
    ]


def _reason_text(code: str, read: ReadValue | None, unread_object: ReadValue | None = None) -> str:
    """One target's reason as printed under its call.

    The code's short form, then what was written and, for a value refined from
    the function the call sits in, that function and the line to look at.
    ``not passed`` names an object argument the scan could not read: that is
    why none of its keys reached a target. When the argument is a parameter
    or a local of the function around the call, the line says so too.
    """
    if code == REASON_NOT_PASSED:
        if unread_object is None:
            return "not passed"
        text = f"not passed, object argument {unread_object.value.expression} is not a literal"
        where = _declared_text(unread_object, "")
        return f"{text}: {where}" if where else text
    if read is None:
        return code.replace("_", " ")
    if code == REASON_WRONG_SHAPE:
        value = read.value
        shown = f'"{_flat(value.text or "")}"' if value.kind == "text" else _shown(value)
        return f"wrong shape {shown}"
    expression = read.value.expression
    where = _declared_text(read, expression) if code == read.reason else None
    if where is not None:
        return where
    if code == REASON_FORMAT and expression.startswith("format of "):
        return expression
    return f"{code.replace('_', ' ')} {expression}".rstrip()


def _declared_text(read: ReadValue, expression: str) -> str | None:
    """Where a value refined from its function comes from, None for any other value.

    ``expression`` is what was written, shown before a parameter's property;
    empty when the line has already said it.
    """
    declared = read.declared
    if declared is None:
        return None
    if read.reason in (REASON_PARAMETER, REASON_PARAMETER_PROPERTY):
        if declared.parameter is None:
            return None
        name = declared.parameter.name
        where = f"parameter {name} of {declared.function} (line {declared.line})"
        if read.reason == REASON_PARAMETER:
            return where
        return f"{expression}: property of {where}" if expression else f"property of {where}"
    if read.reason == REASON_LOCAL:
        return f"local {read.value.expression} (line {declared.line})"
    return None


# --- the summary ---------------------------------------------------------------------
def _tally(summary: Summary, files: int) -> list[str]:
    where = f"{plural(summary.calls, 'call site')} in {plural(files, 'file')}"
    if not summary.calls:
        return [f"{where}."]
    sent_unread = summary.calls - summary.fully_read - summary.not_sent
    lines = [
        f"{where}: {summary.fully_read} fully read; {summary.not_sent} not sent "
        f"(no name or field read); {sent_unread} sent with a value not read."
    ]
    if summary.dynamic:
        lines.append(f"Values not read: {_counts(_by_count(summary.dynamic))}.")
    per_type = [
        f"{event_type}: {_counts(counts.items())}"
        for event_type, counts in summary.targets.items()
        if counts
    ]
    if per_type:
        lines.append(f"Read per event type: {'; '.join(per_type)}.")
    lines.extend(
        _default_label_note(event_type, pairs)
        for event_type, pairs in summary.default_label_fields.items()
    )
    return lines


def _default_label_note(event_type: str, pairs: Sequence[tuple[str, str]]) -> str:
    """Fields a label reached because the spec has no ``args:``: the motivating misread.

    ``args:`` maps labels, and ``field_map`` can lead two of them to one field,
    so what to map is counted in labels, not in fields.
    """
    fields = _unique(field for field, _ in pairs)
    labels = _unique(label for _, label in pairs)
    read = f"field {fields[0]} is" if len(fields) == 1 else f"fields {', '.join(fields)} are"
    if len(labels) == 1:
        return (
            f"{event_type}: {read} read from the label {labels[0]} because the "
            "call spec has no args:; map it in args: (null drops it)."
        )
    return (
        f"{event_type}: {read} read from the labels {_and(labels)} because "
        "the call spec has no args:; map them in args: (null drops one)."
    )


def _why(summary: Summary) -> list[str]:
    rows: list[list[str]] = []
    for target, _ in _by_count(summary.dynamic):
        codes = summary.reasons.get(target, {})
        for code, count in sorted(codes.items(), key=lambda item: (-item[1], _order(item[0]))):
            rows.append([target, code, str(count), REASON_TEXT.get(code, code)])
    return ["Why (call sites, per target):", *(f"  {line}" for line in columns(rows))]


def _markers(found: Inventory, summary: Summary) -> list[str]:
    """How many markers the scan read, then each one that suppresses nothing, and why.

    A valid marker on no matched call is only named when the config can match
    a call: with no event types none does, and every marker would be listed.
    """
    count = f"Suppression markers: {summary.markers} read"
    if found.matching:
        count += f", covering {plural(summary.calls_covered, 'call site')}"
    lines = [f"{count}."]
    lines.extend(
        marker_line(MarkerState(entry.marker, used=False))
        for entry in found.markers
        if entry.marker.problem is not None
    )
    if found.matching:
        lines.extend(_no_call_marker(entry) for entry in found.markers if _on_no_call(entry))
    return lines


def _on_no_call(entry: ListedMarker) -> bool:
    return entry.marker.problem is None and not entry.calls


def _no_call_marker(entry: ListedMarker) -> str:
    marker = entry.marker
    return f"Suppression on no matched call: {marker.path}:{marker.line} {label(marker)}."


def _forwarders(forwarders: Sequence[Forwarder]) -> list[str]:
    rows = [
        [
            forwarder.function,
            f"{forwarder.location.path}:{forwarder.location.line}",
            "forwards " + ", ".join(_forwarded(parameter) for parameter in forwarder.parameters),
            _callers(forwarder),
        ]
        for forwarder in forwarders
    ]
    return [
        "Functions that forward their own parameter into a tracking call "
        "(add a call spec for each, so their calls are read):",
        *(f"  {line}" for line in columns(rows)),
    ]


def _forwarded(parameter: ForwardedParameter) -> str:
    """One forwarded parameter: its targets, and whether a call takes it as its object.

    ``object_arg:`` reads an argument by position, and only one written
    without a label. So for a parameter passed on as the object whose callers
    may label it (always in Swift, by choice in Kotlin), the row says which
    calls a call spec for the function will not read.
    """
    where: list[str] = []
    if parameter.targets:
        where.append(f"to {_and(parameter.targets)}")
    if parameter.object_argument:
        where.append("as the object argument")
    text = f"{parameter.name} (#{parameter.index}) {' and '.join(where)}"
    if parameter.object_argument and parameter.label is not None:
        text += f" (object_arg does not read an argument labelled {parameter.label})"
    return text


def _callers(forwarder: Forwarder) -> str:
    """Where a forwarder's own calls are, by name: in its file first, then elsewhere."""
    callers = forwarder.callers
    total = callers.same_file + callers.other_files
    if not total:
        return "no calls found"
    if callers.same_file_matched + callers.other_files_matched == total:
        return "its calls are matched"
    first = callers.first_unmatched
    here = forwarder.location.path
    parts: list[str] = []
    if callers.same_file:
        part = (
            f"{plural(callers.same_file, 'call')} in this file, "
            f"{_matched(callers.same_file, callers.same_file_matched)}"
        )
        if first is not None and first.path == here:
            part += f" (first :{first.line})"
        parts.append(part)
    if callers.other_files:
        part = (
            f"{plural(callers.other_files, 'call')} named {forwarder.function} elsewhere, "
            f"{_matched(callers.other_files, callers.other_files_matched)}"
        )
        if first is not None and first.path != here:
            part += f" (first {first.path}:{first.line})"
        parts.append(part)
    return "; ".join(parts)


def _matched(calls: int, matched: int) -> str:
    if not matched:
        return "none matched"
    return "all matched" if matched == calls else f"{matched} matched"


def _no_call(found: Inventory, config: str | None) -> str:
    if config is None:
        return (
            "No check config found: these are the calls that look like tracking under "
            f"{found.root}. Write .tripl/check.yml: {GUIDE_URL}"
        )
    clause = nothing_matched(found.root, found.files)
    return f"{clause[:1].upper()}{clause[1:]}."


def _unmatched(groups: Sequence[UnmatchedGroup]) -> list[str]:
    """Every ``constant`` group, then the first others; ``--json`` lists them all."""
    if not groups:
        return ["Calls that look like tracking but match no call spec: none."]
    constant = [group for group in groups if group.signal == SIGNAL_CONSTANT]
    others = [group for group in groups if group.signal != SIGNAL_CONSTANT]
    shown = [*constant, *others[:UNMATCHED_SHOWN]]
    rows = [
        [str(group.calls), _method(group), group.signal, _where(group.first), group.example]
        for group in shown
    ]
    lines = [
        "Calls that look like tracking but match no call spec:",
        *(f"  {line}" for line in columns(rows)),
    ]
    more = len(groups) - len(shown)
    if more > 0:
        lines.append(f"  ...and {more} more (--list --json lists all).")
    return lines


def _method(group: UnmatchedGroup) -> str:
    """The method, and how many receivers call it when that is more than one."""
    if len(group.receivers) <= 1:
        return group.method
    return f"{group.method} ({plural(len(group.receivers), 'receiver')})"


# --- helpers -------------------------------------------------------------------------
def _flat(text: str) -> str:
    """Free text on one line: newlines and whitespace runs as one space."""
    return " ".join(text.split())


def _where(place: Location) -> str:
    if place.column is None:
        return f"{place.path}:{place.line}"
    return f"{place.path}:{place.line}:{place.column}"


def _by_count(counts: Mapping[str, int]) -> list[tuple[str, int]]:
    """Most first; a tie keeps the order the calls first showed it in."""
    return sorted(counts.items(), key=lambda item: -item[1])


def _counts(pairs: Iterable[tuple[str, int]]) -> str:
    return ", ".join(f"{name} {count}" for name, count in pairs)


def _order(code: str) -> int:
    return REASON_CODES.index(code) if code in REASON_CODES else len(REASON_CODES)


def _unique(names: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(names))


def _and(names: Sequence[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"
