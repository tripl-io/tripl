"""``tripl check`` output: the human listing and the SARIF 2.1.0 log.

The ``--json`` document is built in ``tripl_cli.report`` with every other one.
SARIF lives here because it is not a tripl document — it is OASIS's format,
read by GitHub code scanning and every other SARIF viewer — and so carries no
tripl envelope.
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from tripl_cli import __version__
from tripl_cli.check.model import (
    MODE_STATIC,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    STATUS_DYNAMIC,
    STATUS_OK,
    STDIN_LABEL,
    CheckFinding,
    CheckReport,
    CheckResult,
    Origin,
)
from tripl_cli.check.validate import summary
from tripl_cli.model import JsonDict
from tripl_cli.render import plural

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_ROOT_BASE = "%SRCROOT%"
INFORMATION_URI = "https://tripl-io.github.io/tripl/run/cli"

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
    "oversize_value": "A value exceeded the validator's size limits and was sent as null.",
    "no_verdict": "The validator returned no verdict for this item.",
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


def location(origin: Origin) -> str:
    if origin.column is not None:
        return f"{origin.path}:{origin.line}:{origin.column}"
    # A payload: NDJSON has a line; an event in a JSON array only has its position.
    return f"{origin.path} ({origin.snippet})" if origin.snippet else f"{origin.path}:{origin.line}"


def render_text(report: CheckReport) -> str:
    """Every result that is not a clean pass, then the tally. Ok items are counted, not listed."""
    lines: list[str] = []
    for result in report.results:
        if result.status == STATUS_OK:
            continue
        if result.status == STATUS_DYNAMIC and not report.strict:
            continue
        lines.append(f"{location(result.item.origin)}  {result.status}  {describe(result)}")
        for finding in result.findings:
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
    if lines:
        lines.append("")
    lines.append(tally + ".")
    return "\n".join(lines)


# --- SARIF 2.1.0 -------------------------------------------------------------------
def sarif_document(report: CheckReport, *, root: Path | None) -> JsonDict:
    """A complete SARIF 2.1.0 log with one run, one result per finding."""
    codes: list[str] = []
    results: list[JsonDict] = []
    for result in report.results:
        for finding in result.findings:
            if finding.code not in codes:
                codes.append(finding.code)
            results.append(_sarif_result(result, finding, codes.index(finding.code), report))
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
        "defaultConfiguration": {"level": "error" if code in _ERROR_CODES else "warning"},
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


def _sarif_result(
    result: CheckResult, finding: CheckFinding, rule_index: int, report: CheckReport
) -> JsonDict:
    origin = result.item.origin
    region: JsonDict = {"startLine": max(origin.line, 1)}
    if origin.column is not None:
        region["startColumn"] = origin.column
    if origin.end_line is not None:
        region["endLine"] = origin.end_line
    if origin.end_column is not None:
        region["endColumn"] = origin.end_column
    subject = describe(result)
    field = f" (field {finding.field})" if finding.field else ""
    message = f"{finding.message or RULE_TEXT.get(finding.code, finding.code)}{field} - {subject}"
    fingerprint = hashlib.sha256(
        "\0".join(
            (
                finding.code,
                origin.path,
                origin.snippet,
                result.item.event_type or "",
                result.item.name or "",
                finding.field or "",
                str(origin.line),
            )
        ).encode()
    ).hexdigest()
    return {
        "ruleId": finding.code,
        "ruleIndex": rule_index,
        "level": _SARIF_LEVEL.get(finding.severity, "error"),
        "message": {"text": message},
        "locations": [_sarif_location(origin, region, report)],
        "partialFingerprints": {"triplCheck/v1": fingerprint},
        "properties": _properties(result),
    }


def artifact_uri(path: str) -> str:
    """``path`` as a SARIF artifact URI: each segment percent-encoded.

    A relative path stays relative (resolved against ``%SRCROOT%`` for a scan);
    an absolute one becomes a ``file:`` URI.
    """
    posix = path.replace("\\", "/")
    if PurePosixPath(posix).is_absolute() or Path(path).is_absolute():
        return Path(path).absolute().as_uri()
    return "/".join(quote(segment, safe="") for segment in posix.split("/"))


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
