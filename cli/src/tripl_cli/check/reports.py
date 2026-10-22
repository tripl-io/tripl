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
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from tripl_cli.check.fingerprint import finding_fingerprints
from tripl_cli.check.model import (
    MODE_STATIC,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    STATUS_DYNAMIC,
    STATUS_ERROR,
    STATUS_WARNING,
    STDIN_LABEL,
    CheckItem,
    CheckReport,
    CheckResult,
    Origin,
)
from tripl_cli.check.render import finding_message
from tripl_cli.model import JsonDict

# GitLab's five levels; a finding that fails the run is "major", the CodeClimate
# convention for a failed lint rule.
CODEQUALITY_SEVERITY = {SEVERITY_ERROR: "major", SEVERITY_WARNING: "minor", SEVERITY_INFO: "info"}

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
            issues.append(
                {
                    "type": "issue",
                    "check_name": f"tripl/{finding.code}",
                    "description": finding_message(result, finding),
                    "categories": ["Bug Risk"],
                    "fingerprint": fingerprint,
                    "severity": CODEQUALITY_SEVERITY.get(finding.severity, "major"),
                    "location": {"path": report.repo_path(origin), "lines": dict(lines)},
                }
            )
    return issues


# --- JUnit XML --------------------------------------------------------------------------
def case_name(item: CheckItem) -> str:
    """A call as a test name, from the code alone: never the line, never the plan's identity."""
    if item.name:
        subject = item.name
    elif item.fields:
        subject = " ".join(
            f"{key}={'?' if value is None else value}" for key, value in item.fields.items()
        )
    else:
        subject = "(nothing known)"
    return f"{item.event_type or '-'} {subject}"


def failing(report: CheckReport, result: CheckResult) -> bool:
    """Whether this result is one of those that make the run exit 1."""
    return result.status == STATUS_ERROR or (report.strict and result.status == STATUS_WARNING)


def junit_document(report: CheckReport) -> str:
    """JUnit XML: one test case per call site (or captured event), the passing ones included.

    A case fails exactly when its call makes the run exit 1, is skipped when
    nothing about the call was readable (and ``--strict`` was not given), and
    passes otherwise, with any lesser findings as its output. GitLab compares
    cases between pipelines by suite, class and name, so the suite is named
    after the project alone (not the branch), the class is the file and the name
    is the call, numbered when one file has several identical calls.
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
    seen: Counter[tuple[str, str]] = Counter()
    for result in results:
        path = report.repo_path(result.item.origin)
        name = case_name(result.item)
        seen[path, name] += 1
        if seen[path, name] > 1:
            name = f"{name} #{seen[path, name]}"
        case = ET.SubElement(
            suite,
            "testcase",
            {"classname": _x(path), "name": _x(name), "file": _x(path), "time": "0"},
        )
        _case_body(case, report, result)
    body = ET.tostring(suites, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'


def _case_body(case: ET.Element, report: CheckReport, result: CheckResult) -> None:
    if result.status == STATUS_DYNAMIC:
        ET.SubElement(case, "skipped", {"message": SKIPPED_MESSAGE})
        return
    if not result.findings:
        return
    lines = [_where(report, result.item.origin)]
    for finding in result.findings:
        field = f" [{finding.field}]" if finding.field else ""
        lines.append(f"{finding.severity} {finding.code}{field}: {finding.message}")
    details = _x("\n".join(lines))
    if failing(report, result):
        first = next(
            (finding for finding in result.findings if finding.severity == SEVERITY_ERROR),
            result.findings[0],
        )
        failure = ET.SubElement(
            case,
            "failure",
            {"type": _x(first.code), "message": _x(finding_message(result, first))},
        )
        failure.text = details
    else:
        output = ET.SubElement(case, "system-out")
        output.text = details


def _x(text: str) -> str:
    return _XML_INVALID.sub("�", text)
