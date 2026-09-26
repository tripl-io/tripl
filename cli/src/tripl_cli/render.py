"""Human output. ASCII only, and byte-identical whether stdout is a TTY or a pipe.

No colour, no spinners, no cursor control, no emoji, no unicode glyphs. That is
not minimalism for its own sake: `tripl doctor | tee incident.log` and the
terminal view have to be the same artifact, and the logfile an operator pastes
into a ticket must not carry escape codes. A NO_COLOR/isatty branch is pure
polish and can be added later without changing anything here.

At the package root rather than inside ``diagnostics`` for the same reason as
``report.py``: ``render_scan_configs`` / ``render_scan_jobs`` / ``render_drifts``
/ ``render_mutation`` reach no verdict, and ``plural`` and ``columns`` are read
by ``watch.render`` and ``install.render`` as well (tripl-azhh). Both of those
re-export rather than re-implement, which is the whole point of one home.

The one import out of this module is ``api.event_types``, for the two derived
facts its tables print — how many fields an event type has, and a field's enum
options coerced to text. Those are facts about the API's payloads rather than
about layout, and computing either here would be a second copy of one the
request layer already owns (tripl-3ixs).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from tripl_cli.api import event_types as event_types_api
from tripl_cli.model import (
    Check,
    DriftsSnapshot,
    JsonDict,
    JsonList,
    MutationOutcome,
    PlanRead,
    Report,
    ScanJobsSnapshot,
    ScansSnapshot,
    Severity,
    StatusSnapshot,
    float_of,
    int_of,
    text_of,
)

_INDENT = " " * 6
# Two spaces between columns, which is the narrowest gap that still reads as a
# gap once a value is one character shorter than its neighbour.
_GAP = "  "


# Endings that take ``-es``. Not general English pluralisation — that needs a
# dictionary — just the sibilants, which is the only class the CLI's nouns
# reach. It exists because `tripl plan branches` printed "2 branchs" (tripl-3ixs),
# and the fix belongs in the one pluraliser rather than in a special case at the
# one call site that noticed.
_SIBILANT_ENDINGS = ("s", "x", "z", "ch", "sh")


def plural(count: int, noun: str) -> str:
    """``1 project`` / ``2 projects`` / ``2 branches``.

    Lives here so watch and the group commands cannot disagree about a footer;
    ``watch.render`` re-exports it. A noun this rule gets wrong is a noun to
    rename, not a place to hand-write a plural: every count in every footer
    comes through here.
    """
    if count == 1:
        return f"{count} {noun}"
    return f"{count} {noun}{'es' if noun.endswith(_SIBILANT_ENDINGS) else 's'}"


def render_header(command: str, base_url: str, source: str) -> str:
    return f"tripl {command} - {base_url} (from {source})"


def columns(rows: Sequence[Sequence[str]]) -> list[str]:
    """Pad every column to its widest cell; never pad the last one.

    Deterministic from the input alone — no terminal width read, no isatty
    branch — so the piped bytes and the terminal bytes are the same artifact.

    Public rather than module-private since tripl-ey6j.3: ``install.render``
    lays out its own tables and a second padder would let the two command
    families' output drift apart by a space.
    """
    if not rows:
        return []
    width = max(len(row) for row in rows)
    sizes = [max(len(row[index]) for row in rows if index < len(row)) for index in range(width)]
    lines: list[str] = []
    for row in rows:
        cells = [
            cell.ljust(sizes[index]) if index < len(row) - 1 else cell
            for index, cell in enumerate(row)
        ]
        lines.append(_GAP.join(cells).rstrip())
    return lines


def render_check(check: Check) -> str:
    """One check as a fixed-width status token, a summary, and its findings."""
    lines = [f"{check.status.value.upper():<4}  {check.id:<13} {check.summary}"]
    if check.skip_reason is not None and check.skip_reason != check.summary:
        lines.append(f"{_INDENT}skipped: {check.skip_reason}")
    for finding in check.findings:
        where = f" [{finding.project}]" if finding.project else ""
        if finding.target is not None and finding.target.name:
            where += f" {finding.target.name!r}"
        lines.append(f"{_INDENT}- {finding.severity.value}: {finding.code}{where}")
        lines.append(f"{_INDENT}  {finding.message}")
    return "\n".join(lines)


def render_footer(report: Report, exit_code: int) -> str:
    counts = report.counts
    total = len(report.checks)
    if counts[Severity.PASS.value] == total:
        return f"{total} checks: {total} pass. No problems found."
    parts = [
        f"{counts[level.value]} {level.value}"
        for level in (Severity.PASS, Severity.WARN, Severity.FAIL, Severity.SKIP)
        if counts[level.value]
    ]
    line = f"{total} checks: {', '.join(parts)}. Exit {exit_code}."
    if exit_code != 0:
        line += "\nRe-run with --json for the machine-readable form of every finding."
    return line


def render_status(snapshot: StatusSnapshot) -> str:
    lines: list[str] = []
    if not snapshot.projects:
        lines.append("No projects selected.")
    for project in snapshot.projects:
        demo = " [demo]" if project.is_demo else ""
        lines.append(f"{project.slug} ({project.name}){demo}")
        lines.append(
            f"  events     {project.event_count} total, {project.active_event_count} active, "
            f"{project.event_type_count} event types"
        )
        lines.append(
            f"  scans      {project.scan_count} configured, {project.failing_scan_count} failing"
        )
        lines.append(f"  signals    {project.significant_open_signals} significant open")
        lines.append(f"  monitors   {project.firing_monitors} firing")
        if project.coverage is not None:
            coverage = project.coverage
            lines.append(
                f"  coverage   {coverage.pct:.1f}% over {coverage.days} days "
                f"({coverage.matched}/{coverage.total} matched)"
            )
        for error in project.errors:
            lines.append(f"  {error.section}: unavailable ({error.message})")
        lines.append("")
    return "\n".join(lines).rstrip()


def _unavailable(label: str, message: str) -> str:
    """The one spelling of "this read did not arrive", so it is greppable.

    Never an omission and never a zero: a 404 rendered as a shorter list is the
    misreading that invalidated the 2026-07-30 audit (TRAP 3).
    """
    return f"  {label}: unavailable ({message})"


def _incomplete(error_count: int) -> str:
    """The one spelling of "this list is short because a read failed", counted.

    ``scans list`` and ``drifts list`` reach the identical condition, so they say
    the identical sentence: two wordings is how a ``grep`` over an incident log
    finds one of them and misses the other, and a footer that did not COUNT the
    failures left the operator unable to tell one 403 from twenty.
    """
    return f"{plural(error_count, 'read')} failed; the list above is incomplete."


def _schedule_of(scan: dict[str, object]) -> str:
    """Why the dispatcher would or would not select this config."""
    if scan.get("dispatchable"):
        return "scheduled"
    if not scan.get("interval"):
        return "not scheduled (no interval)"
    return "not scheduled (no time column)"


def render_scan_configs(snapshot: ScansSnapshot) -> str:
    lines: list[str] = []
    if not snapshot.projects:
        lines.append("No projects selected.")
    for project in snapshot.projects:
        demo = " [demo]" if project.is_demo else ""
        lines.append(f"{project.slug} ({project.name}){demo}")
        rows = [
            [
                text_of(scan, "id") or "(no id)",
                text_of(scan, "name") or "(unnamed)",
                text_of(scan, "interval") or "-",
                text_of(scan, "time_column") or "-",
                _schedule_of(scan),
            ]
            for scan in project.scans
        ]
        lines.extend(f"  {line}" for line in columns(rows))
        if not project.scans and not project.errors:
            lines.append("  (no scan configs)")
        for error in project.errors:
            lines.append(_unavailable(error.endpoint or error.section, error.message))
        lines.append("")
    lines.append(
        f"{plural(snapshot.scan_count, 'scan config')} in "
        f"{plural(len(snapshot.projects), 'project')}."
    )
    if snapshot.failed:
        # Counted and named, because a shorter list at exit 0 is exactly the
        # failure this command was written to stop reproducing.
        lines.append(_incomplete(snapshot.error_count))
    return "\n".join(lines).rstrip()


def render_scan_jobs(snapshot: ScanJobsSnapshot) -> str:
    lines = [
        f"{snapshot.project} {snapshot.scan_name!r} ({snapshot.scan_id}), "
        f"newest {plural(snapshot.limit, 'job')} requested:"
    ]
    rows = [
        [
            text_of(job, "id") or "(no id)",
            text_of(job, "status") or "unknown",
            f"created {text_of(job, 'created_at') or '-'}",
            f"finished {text_of(job, 'completed_at') or '-'}",
            text_of(job, "error_message") or "",
        ]
        for job in snapshot.jobs
    ]
    lines.extend(f"  {line}" for line in columns(rows))
    if not snapshot.jobs:
        lines.append("  (no jobs)")
    lines.append("")
    lines.append(f"{plural(len(snapshot.jobs), 'job')}.")
    return "\n".join(lines).rstrip()


def _drift_when(drift: dict[str, object]) -> str:
    """The timestamp that matters for this drift's state.

    For a snooze that is when it LAPSES, because that is the date the operator
    acts on; for everything else it is when the drift was detected. Kept out of
    the status column deliberately: an expiry there would pad every ``open`` row
    to the width of a timestamp.
    """
    if text_of(drift, "status") == "snoozed":
        until = text_of(drift, "snoozed_until")
        return f"until {until}" if until else "snooze has no expiry"
    return f"detected {text_of(drift, 'detected_at') or '-'}"


def render_drifts(snapshot: DriftsSnapshot) -> str:
    lines: list[str] = []
    if not snapshot.projects:
        lines.append("No projects selected.")
    for project in snapshot.projects:
        demo = " [demo]" if project.is_demo else ""
        lines.append(f"{project.slug} ({project.name}){demo}")
        rows = [
            [
                text_of(row.drift, "id") or "(no id)",
                f"{row.event_type_name}.{text_of(row.drift, 'field_name') or '(unnamed)'}",
                text_of(row.drift, "drift_type") or "unknown",
                text_of(row.drift, "status") or "unknown",
                _drift_when(row.drift),
            ]
            for row in project.drifts
        ]
        lines.extend(f"  {line}" for line in columns(rows))
        if not project.drifts and not project.errors:
            lines.append("  (no drifts)")
        for error in project.errors:
            lines.append(_unavailable(error.endpoint or error.section, error.message))
        if project.truncated:
            lines.append(
                f"  {project.event_types_examined} of "
                f"{plural(project.event_types_total, 'event type')} examined; raise "
                "--max-event-types to look at the rest."
            )
        lines.append("")
    lines.append(
        f"{plural(snapshot.drift_count, 'drift')} in "
        f"{plural(len(snapshot.projects), 'project')}, "
        f"{snapshot.untriaged_count} untriaged."
    )
    if snapshot.failed:
        lines.append(_incomplete(snapshot.error_count))
    return "\n".join(lines).rstrip()


def _plan_scope(read: PlanRead) -> str:
    """Which project, and which plan revision, this read was answered from.

    A branch is named only when one was asked for. That is not brevity: the API
    has no id for main and spells it by omitting ``?branch=`` entirely (see
    ``api/branches.py``), so printing ``(main)`` would invent a name for the
    absence — and ``plan branches`` reads no revision at all, which any such
    label would misdescribe.
    """
    if read.branch_id is None:
        return read.project
    return f"{read.project} (branch {read.branch_name!r})"


def _more(read: PlanRead) -> str:
    """Which flags would fetch the rest of a truncated read.

    Derived from the read rather than passed in: truncation is only reachable
    when the verb sent a ``limit``, and ``--offset`` exists on exactly the verbs
    that also sent an ``offset``. A caller-supplied string would let a verb with
    no ``--offset`` advise one — and ``plan search``'s route has no offset
    parameter at all, so that advice would send an operator looking for a flag
    that cannot exist.
    """
    if read.offset is not None:
        return "raise --limit or pass --offset"
    return "raise --limit"


def render_plan_read(read: PlanRead, rows: Sequence[Sequence[str]], *, empty: str) -> str:
    """The frame every ``events``/``plan`` listing prints. One footer, one truncation line.

    The COLUMNS differ per resource and are built by the ``*_rows`` functions
    below; everything around them — the scope line, the empty case, the count,
    and the sentence that says the list stopped early — is here, so seven verbs
    cannot end up saying the same thing seven ways. That is the same rule
    ``_incomplete`` already applies to ``scans list`` and ``drifts list``.
    """
    lines = [_plan_scope(read)]
    lines.extend(f"  {line}" for line in columns(rows))
    if not rows:
        lines.append(f"  ({empty})")
    lines.append("")
    if read.truncated and read.total is not None:
        # Named and counted, like the drift budget's truncation line: a page
        # that filled up looks exactly like a resource that ended, and the
        # second reading is the one that loses rows without saying so.
        lines.append(
            f"{len(read.items)} of {plural(read.total, read.noun)} shown; "
            f"{_more(read)} to read the rest."
        )
    elif read.truncated:
        # The page filled and the route reports no pre-paging count, so how many
        # were dropped is genuinely unknown — say that instead of inventing a
        # number. ``plan search`` is the case: its total is computed AFTER the
        # trim, so the branch above can never fire for it.
        lines.append(
            f"{plural(len(read.items), read.noun)} shown — the most this page holds, "
            f"and more may have matched; {_more(read)}."
        )
    else:
        lines.append(f"{plural(len(read.items), read.noun)}.")
    return "\n".join(lines).rstrip()


def _flat(text: str | None) -> str:
    """Free text as ONE line, or ``-``.

    A description, a search title and a search subtitle are all author-written
    and may carry a newline; one of those inside a padded cell breaks every
    column below it, and the table is the artifact an operator pastes into a
    ticket. Collapsed rather than truncated, so nothing is silently lost.
    """
    return " ".join(text.split()) if text else "-"


def _seen(event: JsonDict) -> str:
    """When the warehouse last carried this event, or that it never has.

    ``never seen`` rather than a blank: an event nobody has ever sent is the
    single most actionable row in the catalog, and an empty cell reads as
    missing data.
    """
    last_seen = text_of(event, "last_seen_at")
    return f"seen {last_seen}" if last_seen else "never seen"


def event_rows(events: Sequence[JsonDict]) -> list[list[str]]:
    """id, name, lifecycle status, last seen, and an open-drift note."""
    return [
        [
            text_of(event, "id") or "(no id)",
            text_of(event, "name") or "(unnamed)",
            text_of(event, "status") or "unknown",
            _seen(event),
            plural(int_of(event, "drift_count"), "drift") if event.get("drift_count") else "",
        ]
        for event in events
    ]


def event_type_rows(event_types: Sequence[JsonDict]) -> list[list[str]]:
    return [
        [
            text_of(event_type, "id") or "(no id)",
            text_of(event_type, "name") or "(unnamed)",
            text_of(event_type, "display_name") or "-",
            plural(event_types_api.field_count(event_type), "field"),
        ]
        for event_type in event_types
    ]


def field_rows(fields: Sequence[JsonDict]) -> list[list[str]]:
    """id, name, type, whether it is required, its sensitivity, and its enum.

    The enum options go LAST and unabbreviated: they are what an author needs to
    write a valid value, and the last column is never padded, so a forty-value
    enum costs the other rows nothing.
    """
    return [
        [
            text_of(field, "id") or "(no id)",
            text_of(field, "name") or "(unnamed)",
            text_of(field, "field_type") or "unknown",
            "required" if field.get("is_required") else "optional",
            text_of(field, "sensitivity") or "-",
            _enum_note(field),
        ]
        for field in fields
    ]


def _enum_note(field: JsonDict) -> str:
    options = event_types_api.enum_options(field)
    return f"enum: {'|'.join(options)}" if options else ""


def variable_rows(variables: Sequence[JsonDict]) -> list[list[str]]:
    return [
        [
            text_of(variable, "id") or "(no id)",
            text_of(variable, "name") or "(unnamed)",
            text_of(variable, "variable_type") or "unknown",
            plural(int_of(variable, "event_count"), "event"),
            (
                f"{plural(int_of(variable, 'open_drift_count'), 'open drift')}"
                if variable.get("open_drift_count")
                else ""
            ),
        ]
        for variable in variables
    ]


def branch_rows(branches: Sequence[JsonDict]) -> list[list[str]]:
    """id, name, kind, status, how far ahead it is, and whether it is behind base.

    ``behind base`` is the one an operator acts on: a branch whose base moved
    under it is the one whose diff no longer says what merging it would do.
    """
    return [
        [
            text_of(branch, "id") or "(no id)",
            text_of(branch, "name") or "(unnamed)",
            text_of(branch, "kind") or "unknown",
            text_of(branch, "status") or "unknown",
            f"{int_of(branch, 'ahead')} ahead" if branch.get("ahead") is not None else "-",
            "behind base" if branch.get("behind_base") else "",
        ]
        for branch in branches
    ]


def search_rows(results: Sequence[JsonDict]) -> list[list[str]]:
    """entity type, id, title, subtitle, confidence.

    The id is in the table rather than only in ``--json`` because it is the
    argument the follow-up command takes: search, then read the entity by id.
    """
    return [
        [
            text_of(result, "entity_type") or "unknown",
            text_of(result, "entity_id") or "(no id)",
            _flat(text_of(result, "title")),
            _flat(text_of(result, "subtitle")),
            f"{float_of(result, 'confidence'):.2f}",
        ]
        for result in results
    ]


def render_event_detail(read: PlanRead, event: JsonDict, fields: JsonList) -> str:
    """One event, with its field values resolved to the field NAMES they set.

    ``EventFieldValueResponse`` carries only ``field_definition_id``, so the
    names come from a second read of the event type's fields — without it the
    most useful part of this command would be a column of UUIDs. The meta values
    below are shown with their definition id for exactly that reason inverted:
    resolving them needs a meta-field route the shared request layer does not
    build, and inventing one here would be a path literal outside ``api/``.
    """
    names = {
        field_id: name
        for field in fields
        for field_id in (text_of(field, "id"),)
        if field_id
        for name in (text_of(field, "name") or field_id,)
    }
    event_type = event.get("event_type")
    event_type_id = text_of(event, "event_type_id") or "(no id)"
    if isinstance(event_type, dict):
        brief = f"{text_of(event_type, 'name') or '(unnamed)'} ({event_type_id})"
    else:
        brief = event_type_id
    tags = [text_of(tag, "name") or "" for tag in event.get("tags") or [] if isinstance(tag, dict)]
    attributes = [
        ["status", text_of(event, "status") or "unknown"],
        ["reviewed", "yes" if event.get("reviewed") else "no"],
        ["event type", brief],
        ["tags", ", ".join(tag for tag in tags if tag) or "-"],
        # The bare timestamp here, not `_seen`: the row is already labelled, and
        # "last seen  seen 2026-..." is the label said twice.
        ["last seen", text_of(event, "last_seen_at") or "never"],
        ["sunset", text_of(event, "sunset_at") or "-"],
        ["drifts", str(int_of(event, "drift_count"))],
        ["description", _flat(text_of(event, "description"))],
    ]
    lines = [
        _plan_scope(read),
        f"  {text_of(event, 'id') or '(no id)'}  {text_of(event, 'name') or '(unnamed)'}",
    ]
    lines.extend(f"    {line}" for line in columns(attributes))
    lines.extend(_value_block("fields", event.get("field_values"), "field_definition_id", names))
    # Definition ids, not names. See the docstring: there is no meta-field
    # builder in tripl_cli.api, and a second spelling of one belongs nowhere.
    lines.extend(
        _value_block(
            "meta (by definition id)", event.get("meta_values"), "meta_field_definition_id"
        )
    )
    return "\n".join(lines).rstrip()


def _value_block(
    title: str, values: object, key: str, names: Mapping[str, str] | None = None
) -> list[str]:
    """One ``fields``/``meta`` block, or nothing at all when the event carries none.

    Omitted rather than printed empty: an event with no meta values has nothing
    to say about them, and a `meta` heading over a blank line reads as a failed
    read.
    """
    resolved = names or {}
    rows = [
        [resolved.get(reference, reference), text_of(value, "value") or "-"]
        for value in (values if isinstance(values, list) else [])
        if isinstance(value, dict)
        for reference in (text_of(value, key) or "(no id)",)
    ]
    if not rows:
        return []
    return [f"    {title}", *(f"      {line}" for line in columns(rows))]


def render_mutation(outcome: MutationOutcome) -> str:
    """One line for what happened, plus the next command worth typing."""
    where = f"{outcome.project} {outcome.scan_name!r} ({outcome.scan_id})"
    if outcome.dry_run:
        request = outcome.request
        return (
            f"dry run: would send {request['method']} {request['path']}"
            f" with body {request['body']!r}\nNothing was sent."
        )
    result = outcome.result or {}
    if outcome.command == "annotate":
        return _annotation_line(outcome, result)
    if outcome.command == "scans run":
        job_id = text_of(result, "id") or "(no id)"
        status = text_of(result, "status") or "unknown"
        return (
            f"{where}: started job {job_id} ({status}).\n"
            f"Follow it with: tripl watch --project {outcome.project} "
            f"--scan {outcome.scan_name!r}"
        )
    if outcome.command == "scans cancel":
        status = text_of(result, "status") or "unknown"
        return f"{where}: job {outcome.job_id} is now {status}."
    field = text_of(result, "field_name") or "(unnamed)"
    drift_type = text_of(result, "drift_type") or "unknown"
    status = text_of(result, "status") or "unknown"
    return f"{outcome.project}: drift {outcome.drift_id} ({field}, {drift_type}) is now {status}."


def _annotation_line(outcome: MutationOutcome, result: JsonDict) -> str:
    """Created, or de-duplicated - and the second says so in words.

    A 200 is not a failure: it is the API refusing to draw the same deploy marker
    twice, which is what makes a retried CI job safe. It still has to read
    differently from a 201, or an operator chasing a missing marker would be told
    one was just created.
    """
    label = text_of(result, "label") or "(unlabelled)"
    annotation_id = text_of(result, "id") or "(no id)"
    bucket = text_of(result, "bucket") or "unknown time"
    if outcome.deduplicated:
        return (
            f"{outcome.project}: annotation {label!r} already exists ({annotation_id}, "
            f"at {bucket}); the API de-duplicated it and nothing new was created."
        )
    return f"{outcome.project}: annotated {label!r} at {bucket} ({annotation_id})."
