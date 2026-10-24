"""The reports a CI system shows on a merge request: GitLab Code Quality and JUnit XML.

SARIF (``render.py``) is what GitHub code scanning reads, but GitLab ingests it
only on Ultimate, files it as vulnerabilities and only from a job that
succeeded. These two are what every GitLab tier shows, and JUnit is what most
other CI systems read as well (Jenkins, Azure Pipelines, Bitbucket, CircleCI,
TeamCity). Both are built from the same ``CheckReport`` as every other format,
so one run's findings read the same everywhere.

Paths here are relative to the REPOSITORY root, not the check root: a CI system
resolves them against its own checkout. And nothing a CI system compares
between two pipelines carries a line number — not a Code Quality fingerprint,
not a JUnit test name — or every edit above a call would turn its finding into
one "new" and one "resolved".

A finding a ``tripl-check-ignore`` marker waives stays in both, under its own
fingerprint and test name: Code Quality lowers it to ``info`` with the reason
in front, JUnit tags it ``[suppressed: <reason>]`` and fails no case on it. So
adding a marker shows in GitLab as neither a new finding nor a resolved one.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path

from tripl_cli.check.fingerprint import callee, finding_fingerprints, marker_fingerprints
from tripl_cli.check.model import (
    CODE_INVALID_SUPPRESSION,
    CODE_UNUSED_SUPPRESSION,
    MODE_STATIC,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    STATUS_DYNAMIC,
    STDIN_LABEL,
    CheckFinding,
    CheckItem,
    CheckReport,
    CheckResult,
    Origin,
)
from tripl_cli.check.render import finding_message, marker_message
from tripl_cli.model import JsonDict

# GitLab's five levels; a finding that fails the run is "major", the CodeClimate
# convention for a failed lint rule.
CODEQUALITY_SEVERITY = {SEVERITY_ERROR: "major", SEVERITY_WARNING: "minor", SEVERITY_INFO: "info"}
# A marker that suppresses nothing is worth fixing; one that matched nothing is a note.
MARKER_SEVERITY = {CODE_INVALID_SUPPRESSION: "minor", CODE_UNUSED_SUPPRESSION: "info"}

# What XML 1.0 can carry at all. A scanned string can hold anything (a stray
# control character in a constant), and one of them would make the whole file
# unparseable, so each is replaced rather than escaped.
_XML_INVALID = re.compile("[^\u0009\u000a\u000d -퟿-�\U00010000-\U0010ffff]")

SKIPPED_MESSAGE = "nothing about this call is known until runtime, so nothing was checked"


# --- paths ------------------------------------------------------------------------
def repo_paths(
    paths: Iterable[str],
    *,
    mode: str,
    scan_root: Path | None,
    repo_root: Path | None,
    cwd: Path,
) -> dict[str, str]:
    """Each origin path -> the same file relative to ``repo_root``.

    A static path is relative to the scan root; a payload path is as typed,
    relative to ``cwd``. A path outside the repository, standard input, or a
    run with no repository at all is left out, and used as it is.
    """
    mapped: dict[str, str] = {}
    if repo_root is None:
        return mapped
    for path in set(paths):
        if path == STDIN_LABEL:
            continue
        if mode == MODE_STATIC:
            if scan_root is None:
                continue
            absolute = scan_root / path
        else:
            typed = Path(path)
            absolute = typed if typed.is_absolute() else cwd / typed
        relative = _relative(Path(os.path.normpath(absolute)), repo_root)
        if relative is not None:
            mapped[path] = relative
    return mapped


def _relative(path: Path, root: Path) -> str | None:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        pass
    try:
        # Only now resolve symlinks: a checkout reached through a symlinked
        # directory still lies inside the (resolved) repository root.
        return path.resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return None


def _where(report: CheckReport, origin: Origin) -> str:
    where = f"{report.repo_path(origin)}:{origin.line}"
    return f"{where}:{origin.column}" if origin.column is not None else where


# --- GitLab Code Quality --------------------------------------------------------------
def codequality_document(report: CheckReport) -> list[JsonDict]:
    """A GitLab Code Quality report: one issue per finding.

    GitLab reads ``description``, ``check_name``, ``fingerprint``, ``severity``,
    ``location.path`` and ``location.lines.begin``, and stops reading the file
    at the first entry that lacks one, so every entry carries all of them.
    ``type`` and ``categories`` complete the CodeClimate shape for other readers.
    """
    issues: list[JsonDict] = []
    for result, fingerprints in zip(report.results, finding_fingerprints(report), strict=True):
        origin = result.item.origin
        lines: JsonDict = {"begin": max(origin.line, 1)}
        if origin.end_line is not None and origin.end_line >= lines["begin"]:
            lines["end"] = origin.end_line
        for finding, fingerprint in zip(result.findings, fingerprints, strict=True):
            description = finding_message(result, finding)
            severity = CODEQUALITY_SEVERITY.get(finding.severity, "major")
            if finding.suppression is not None:
                # Kept, under the same fingerprint: dropped, GitLab would call it resolved.
                description = f"Suppressed: {finding.suppression.reason} — {description}"
                severity = "info"
            issues.append(
                {
                    "type": "issue",
                    "check_name": f"tripl/{finding.code}",
                    "description": description,
                    "categories": ["Bug Risk"],
                    "fingerprint": fingerprint,
                    "severity": severity,
                    "location": {"path": report.repo_path(origin), "lines": dict(lines)},
                }
            )
    for state, fingerprint in zip(report.markers, marker_fingerprints(report), strict=True):
        code = state.code
        if code is None:
            continue
        marker = state.marker
        issues.append(
            {
                "type": "issue",
                "check_name": f"tripl/{code}",
                "description": marker_message(state),
                "categories": ["Clarity"],
                "fingerprint": fingerprint,
                "severity": MARKER_SEVERITY[code],
                "location": {
                    "path": report.repo_file(marker.path),
                    "lines": {"begin": max(marker.line, 1)},
                },
            }
        )
    return issues


# --- JUnit XML --------------------------------------------------------------------------
def case_name(item: CheckItem) -> str:
    """A call as a test name, from the code alone: never the line, never the plan's identity.

    The same parts as the fingerprint's ``site_key`` (event type, name, the
    fields by key, the callee), so a case is renamed exactly when its call
    changes: two calls with one name and different fields are two names, not
    one name and a ``#2`` that moves when the other call is deleted.
    """
    parts = [item.event_type or "-"]
    if item.name:
        parts.append(item.name)
    parts.extend(
        f"{key}={'?' if value is None else value}" for key, value in sorted(item.fields.items())
    )
    if len(parts) == 1:
        parts.append("(nothing known)")
    called = callee(item)
    if called:
        parts.append(f"({called})")
    return " ".join(parts)


def failing(report: CheckReport, result: CheckResult) -> bool:
    """Whether this result is one of those that make the run exit 1."""
    return report.fails(result)


def junit_document(report: CheckReport) -> str:
    """JUnit XML: one test case per call site (or captured event), the passing ones included.

    A case fails exactly when its call makes the run exit 1, is skipped when
    nothing about the call was readable (and ``--strict`` was not given), and
    passes otherwise, with any lesser findings as its output. GitLab compares
    cases between pipelines by suite, class and name, so the suite is named
    after the project alone (not the branch), the class is the file and the name
    is the call (``case_name``), numbered when one file has several identical
    calls.
    """
    results = report.results
    failures = sum(1 for result in results if failing(report, result))
    skipped = sum(1 for result in results if result.status == STATUS_DYNAMIC)
    seconds = f"{report.run.duration_ms / 1000:.3f}"
    counts = {
        "tests": str(len(results)),
        "failures": str(failures),
        "errors": "0",
        "skipped": str(skipped),
        "time": seconds,
    }
    suites = ET.Element("testsuites", {"name": "tripl check", **counts})
    suite = ET.SubElement(
        suites, "testsuite", {"name": _x(f"tripl check {report.project}"), **counts}
    )
    # Uniqueness is held on the strings actually written: two names that differ
    # only in characters XML cannot carry, or a name that already ends in
    # " #2", must still be two cases, because GitLab keeps the first of a pair.
    taken: set[tuple[str, str]] = set()
    for result in results:
        path = _x(report.repo_path(result.item.origin))
        base = _x(case_name(result.item))
        name, number = base, 1
        while (path, name) in taken:
            number += 1
            name = f"{base} #{number}"
        taken.add((path, name))
        case = ET.SubElement(
            suite,
            "testcase",
            {"classname": path, "name": name, "file": path, "time": "0"},
        )
        _case_body(case, report, result)
    # ASCII, with character references for the rest: stdout on a Windows agent
    # is not UTF-8, and a raw "é" under encoding="UTF-8" is not well-formed.
    body = ET.tostring(suites, encoding="unicode").encode("ascii", "xmlcharrefreplace")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body.decode("ascii")}\n'


def _case_body(case: ET.Element, report: CheckReport, result: CheckResult) -> None:
    if result.status == STATUS_DYNAMIC:
        ET.SubElement(case, "skipped", {"message": SKIPPED_MESSAGE})
        return
    if not result.findings:
        return
    lines = [_where(report, result.item.origin)]
    for finding in result.findings:
        field = f" [{finding.field}]" if finding.field else ""
        waived = ""
        if finding.suppression is not None:
            waived = f" [suppressed: {finding.suppression.reason}]"
        lines.append(f"{finding.severity} {finding.code}{field}: {finding.message}{waived}")
    details = _x("\n".join(lines))
    if failing(report, result):
        first = _first_failure(result.findings)
        failure = ET.SubElement(
            case,
            "failure",
            {"type": _x(first.code), "message": _x(finding_message(result, first))},
        )
        failure.text = details
    else:
        output = ET.SubElement(case, "system-out")
        output.text = details


def _first_failure(findings: tuple[CheckFinding, ...]) -> CheckFinding:
    """The finding a failed case is named after: its first unsuppressed error, if any."""
    open_ = [finding for finding in findings if finding.suppression is None] or list(findings)
    return next((finding for finding in open_ if finding.severity == SEVERITY_ERROR), open_[0])


def _x(text: str) -> str:
    return _XML_INVALID.sub("�", text)
