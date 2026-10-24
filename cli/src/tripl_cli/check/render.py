"""``tripl check`` output: the human listing and the SARIF 2.1.0 log.

The ``--json`` document is built in ``tripl_cli.report`` with every other one.
SARIF lives here because it is not a tripl document — it is OASIS's format,
read by GitHub code scanning and every other SARIF viewer — and so carries no
tripl envelope.

A finding a ``tripl-check-ignore`` marker waives is counted in the listing, not
shown, and is a SARIF result with an ``inSource`` suppression. A marker that
suppresses nothing, or matched no finding, is listed after the tally and is a
SARIF result of its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from tripl_cli import __version__
from tripl_cli.check.fingerprint import (
    FINGERPRINT_KEY,
    finding_fingerprints,
    marker_fingerprints,
)
from tripl_cli.check.model import (
    CODE_INVALID_SUPPRESSION,
    CODE_UNUSED_SUPPRESSION,
    MODE_STATIC,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    SKIP_TOO_LARGE,
    SKIP_UNREADABLE,
    STATUS_DYNAMIC,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_WARNING,
    STDIN_LABEL,
    CheckFinding,
    CheckReport,
    CheckResult,
    MarkerState,
    Origin,
    SkippedFile,
    severe,
)
from tripl_cli.check.suppress import PROBLEM_TEXT, label
from tripl_cli.check.validate import summary
from tripl_cli.model import JsonDict
from tripl_cli.render import plural

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_ROOT_BASE = "%SRCROOT%"
INFORMATION_URI = "https://docs.tripl.io/run/cli"

# What each finding code means, for SARIF's rule table and the human legend.
# A code the validator returns that is not listed here still renders; it just
# gets a generic description.
RULE_TEXT: dict[str, str] = {
    "unknown_event_type": "The event type is not in the tracking plan.",
    "unknown_event": "No event in the tracking plan has this name or identity.",
    "deprecated_event": "The event is deprecated or archived in the tracking plan.",
    "unknown_field": "The field is not defined for this event type.",
    "missing_required_field": "A field the plan marks required is missing from the payload.",
    "value_not_allowed": "The value is outside the field's allowed values or contract.",
    "dynamic_value": "The value is only known at runtime, so it could not be checked.",
    "too_dynamic": "The name has too many runtime parts to look up in the plan.",
    "wrong_type": "A property's value is not of the type the plan gives it.",
    "policy_violation": "The call breaks a plan rule the server adds (an organization policy).",
    "oversize_value": "A value exceeded the validator's size limits and was sent as null.",
    "no_verdict": "The validator returned no verdict for this item.",
    CODE_INVALID_SUPPRESSION: (
        "A tripl-check-ignore marker is malformed (no code, no reason, or no_verdict) "
        "and suppresses nothing."
    ),
    CODE_UNUSED_SUPPRESSION: "A tripl-check-ignore marker matched no finding in this run.",
}

_SARIF_LEVEL = {SEVERITY_ERROR: "error", SEVERITY_WARNING: "warning", SEVERITY_INFO: "note"}


# --- human -----------------------------------------------------------------------
def describe(result: CheckResult) -> str:
    """How an item reads in one line: its identity, else its name, else its field values."""
    item = result.item
    if result.identity:
        label = result.identity
    elif item.name:
        label = item.name
    else:
        label = (
            " ".join(
                f"{key}={'?' if value is None else value}" for key, value in item.fields.items()
            )
            or "(nothing known)"
        )
    return f"{item.event_type or '-'}  {label}"


def finding_message(result: CheckResult, finding: CheckFinding) -> str:
    """One finding as one line of prose: what is wrong, then which call it is about."""
    field = f" (field {finding.field})" if finding.field else ""
    text = finding.message or RULE_TEXT.get(finding.code, finding.code)
    return f"{text}{field} - {describe(result)}"


def location(origin: Origin) -> str:
    if origin.column is not None:
        return f"{origin.path}:{origin.line}:{origin.column}"
    # A payload: NDJSON has a line; an event in a JSON array only has its position.
    return f"{origin.path} ({origin.snippet})" if origin.snippet else f"{origin.path}:{origin.line}"


def render_text(report: CheckReport) -> str:
    """Every result that is not a clean pass, then the tally. Ok items are counted, not listed.

    A suppressed finding is counted, not listed, and so is a result once every
    finding that would list it is suppressed (unless it still fails the run).
    """
    lines: list[str] = []
    for result in report.results:
        if result.status == STATUS_OK:
            continue
        if result.status == STATUS_DYNAMIC and not report.strict:
            continue
        shown = [finding for finding in result.findings if finding.suppression is None]
        status = result.status
        if len(shown) < len(result.findings):
            listed = [finding for finding in shown if _listed(finding, report.strict)]
            if not listed and not report.fails(result):
                continue
            if listed:
                # The verdict of what is left on show, not of what was waived.
                errors = any(finding.severity == SEVERITY_ERROR for finding in listed)
                status = STATUS_ERROR if errors else STATUS_WARNING
        lines.append(f"{location(result.item.origin)}  {status}  {describe(result)}")
        for finding in shown:
            field = f" [{finding.field}]" if finding.field else ""
            lines.append(f"    {finding.severity}  {finding.code}{field}: {finding.message}")
    counts = summary(report.results)
    what = "call site" if report.mode == MODE_STATIC else "event"
    scope = f" in {plural(report.files_scanned, 'file')}" if report.mode == MODE_STATIC else ""
    tally = (
        f"{plural(counts['checked'], what)}{scope}: {counts['ok']} ok, "
        f"{plural(counts['warnings'], 'warning')}, {plural(counts['errors'], 'error')}"
    )
    if counts["dynamic"]:
        tally += f", {counts['dynamic']} only known at runtime"
        if not report.strict:
            tally += " (listed with --strict)"
    if report.suppressed:
        tally += f"; {plural(report.suppressed, 'finding')} suppressed"
    if lines:
        lines.append("")
    lines.append(tally + ".")
    lines.extend(marker_line(state) for state in report.markers if state.code is not None)
    if report.skipped:
        lines.append(render_skipped(report.skipped))
    return "\n".join(lines)


def _listed(finding: CheckFinding, strict: bool) -> bool:
    """Whether ``finding`` alone lists its result: an error or a warning, info under strict."""
    return finding.severity == SEVERITY_WARNING or severe(finding, strict)


def marker_message(state: MarkerState) -> str:
    """What is wrong with a marker the run reports, without where it is."""
    marker = state.marker
    if marker.problem is not None:
        return PROBLEM_TEXT.get(marker.problem, marker.problem) + ", so it suppresses nothing"
    return f"{label(marker)} matched no finding"


def marker_line(state: MarkerState) -> str:
    """The listing's line for a marker that suppresses nothing, or that nothing used."""
    marker = state.marker
    where = f"{marker.path}:{marker.line}"
    if marker.problem is not None:
        text = PROBLEM_TEXT.get(marker.problem, marker.problem)
        return f"Suppression ignored: {where} {text}."
    return f"Unused suppression: {where} {label(marker)} matched no finding."


def render_skipped(skipped: Sequence[SkippedFile]) -> str:
    """The ``Not read:`` line: the configured files a scan did not read, the first few named."""
    shown = ", ".join(
        f"{skip.path} ({_SKIP_TEXT.get(skip.reason, skip.reason)})"
        for skip in skipped[:_SKIPPED_SHOWN]
    )
    more = len(skipped) - _SKIPPED_SHOWN
    tail = f", and {more} more" if more > 0 else ""
    return f"Not read: {plural(len(skipped), 'configured file')}: {shown}{tail}."


def nothing_matched(root: Path, files: int) -> str:
    """Why a static scan matched no call, as a clause: no file to read, or no call in them."""
    if files == 0:
        return f"no readable source file under {root} matches `sources:`"
    return f"none of the {plural(files, 'file')} scanned has a call the call specs match"


_SKIP_TEXT = {SKIP_TOO_LARGE: "too large", SKIP_UNREADABLE: "unreadable"}
# The listing names a few; `--json` carries every one.
_SKIPPED_SHOWN = 5


# --- SARIF 2.1.0 -------------------------------------------------------------------
def sarif_document(report: CheckReport, *, root: Path | None) -> JsonDict:
    """A complete SARIF 2.1.0 log with one run, one result per finding."""
    codes: list[str] = []
    results: list[JsonDict] = []
    for result, fingerprints in zip(report.results, finding_fingerprints(report), strict=True):
        for finding, fingerprint in zip(result.findings, fingerprints, strict=True):
            if finding.code not in codes:
                codes.append(finding.code)
            results.append(
                _sarif_result(result, finding, codes.index(finding.code), report, fingerprint)
            )
    for state, fingerprint in zip(report.markers, marker_fingerprints(report), strict=True):
        code = state.code
        if code is None:
            continue
        if code not in codes:
            codes.append(code)
        results.append(_sarif_marker(state, code, codes.index(code), report, fingerprint))
    run: JsonDict = {
        "tool": {
            "driver": {
                "name": "tripl",
                "version": __version__,
                "semanticVersion": __version__,
                "informationUri": INFORMATION_URI,
                "rules": [_sarif_rule(code) for code in codes],
            }
        },
        "columnKind": "unicodeCodePoints",
        "results": results,
        "properties": {
            "project": report.project,
            "branch": report.branch_name or report.branch_id,
            "mode": report.mode,
            "strict": report.strict,
            "summary": summary(report.results),
        },
    }
    if root is not None and report.mode == MODE_STATIC:
        run["originalUriBaseIds"] = {SARIF_ROOT_BASE: {"uri": root.resolve().as_uri() + "/"}}
    return {"$schema": SARIF_SCHEMA, "version": SARIF_VERSION, "runs": [run]}


def _sarif_rule(code: str) -> JsonDict:
    text = RULE_TEXT.get(code, f"tripl check finding {code}.")
    return {
        "id": code,
        "name": "".join(part.capitalize() for part in code.split("_")),
        "shortDescription": {"text": text},
        "fullDescription": {"text": text},
        "helpUri": f"{INFORMATION_URI}#tripl-check",
        "defaultConfiguration": {"level": _RULE_LEVEL.get(code, "warning")},
    }


_ERROR_CODES = frozenset(
    {
        "unknown_event_type",
        "unknown_event",
        "missing_required_field",
        "value_not_allowed",
        "no_verdict",
    }
)
_RULE_LEVEL = {
    **dict.fromkeys(_ERROR_CODES, "error"),
    CODE_INVALID_SUPPRESSION: "warning",
    CODE_UNUSED_SUPPRESSION: "note",
}


def _sarif_result(
    result: CheckResult,
    finding: CheckFinding,
    rule_index: int,
    report: CheckReport,
    fingerprint: str,
) -> JsonDict:
    origin = result.item.origin
    region: JsonDict = {"startLine": max(origin.line, 1)}
    if origin.column is not None:
        region["startColumn"] = origin.column
    if origin.end_line is not None:
        region["endLine"] = origin.end_line
    if origin.end_column is not None:
        region["endColumn"] = origin.end_column
    sarif: JsonDict = {
        "ruleId": finding.code,
        "ruleIndex": rule_index,
        "level": _SARIF_LEVEL.get(finding.severity, "error"),
        "message": {"text": finding_message(result, finding)},
        "locations": [_sarif_location(origin, region, report)],
        "partialFingerprints": {FINGERPRINT_KEY: fingerprint},
        "properties": _properties(result),
    }
    waived = finding.suppression
    if waived is not None:
        # SARIF's own way to say "reviewed and accepted": a viewer shows the
        # result as suppressed, with the marker's reason and where it is.
        at = Origin(waived.path, waived.line, waived.column)
        sarif["suppressions"] = [
            {
                "kind": "inSource",
                "status": "accepted",
                "justification": waived.reason,
                "location": _sarif_location(at, _marker_region(at), report),
            }
        ]
    return sarif


def _sarif_marker(
    state: MarkerState, code: str, rule_index: int, report: CheckReport, fingerprint: str
) -> JsonDict:
    marker = state.marker
    at = Origin(marker.path, marker.line, marker.column)
    return {
        "ruleId": code,
        "ruleIndex": rule_index,
        "level": _RULE_LEVEL[code],
        "message": {"text": marker_message(state)},
        "locations": [_sarif_location(at, _marker_region(at), report)],
        "partialFingerprints": {FINGERPRINT_KEY: fingerprint},
        "properties": {"marker": marker.text, "problem": marker.problem},
    }


def _marker_region(at: Origin) -> JsonDict:
    region: JsonDict = {"startLine": max(at.line, 1)}
    if at.column is not None:
        region["startColumn"] = at.column
    return region


def artifact_uri(path: str) -> str:
    """``path`` as a SARIF artifact URI: each segment percent-encoded.

    A relative path stays relative (resolved against ``%SRCROOT%`` for a scan);
    an absolute one becomes a ``file:`` URI.
    """
    posix = path.replace("\\", "/")
    if PurePosixPath(posix).is_absolute() or Path(path).is_absolute():
        return Path(path).absolute().as_uri()
    # surrogateescape: a file name that is not UTF-8 arrives from os.walk as
    # lone surrogates, and its original bytes are what the URI must encode.
    return "/".join(
        quote(segment.encode("utf-8", "surrogateescape"), safe="") for segment in posix.split("/")
    )


def _sarif_location(origin: Origin, region: JsonDict, report: CheckReport) -> JsonDict:
    if report.mode != MODE_STATIC and origin.path == STDIN_LABEL:
        # Standard input is not an artifact a viewer can open: name the event
        # by its line (or array position) instead of pointing at a file.
        where = origin.snippet or f"line {origin.line}"
        return {
            "logicalLocations": [
                {
                    "name": where,
                    "fullyQualifiedName": f"{STDIN_LABEL} {where}",
                    "kind": "object",
                }
            ]
        }
    artifact: JsonDict = {"uri": artifact_uri(origin.path)}
    if report.mode == MODE_STATIC:
        artifact["uriBaseId"] = SARIF_ROOT_BASE
    return {"physicalLocation": {"artifactLocation": artifact, "region": region}}


def _properties(result: CheckResult) -> JsonDict:
    item = result.item
    properties: dict[str, Any] = {
        "status": result.status,
        "eventType": item.event_type,
        "name": item.name,
        "fields": dict(item.fields),
    }
    if result.event_id is not None:
        properties["eventId"] = result.event_id
    if result.identity is not None:
        properties["identity"] = result.identity
    return properties
