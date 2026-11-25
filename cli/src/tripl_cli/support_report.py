"""``tripl doctor --support-report PATH`` — a diagnostic file that is safe to share.

An operator reporting a broken self-hosted installation should not have to
decide which parts of ``doctor`` output are safe to paste into a public issue.
This module builds the answer for them from an ALLOWLIST: every key in the
document is written here, by name, from a value whose shape is checked. Nothing
is copied wholesale from a check, a finding or the snapshot, so a field added to
those types later cannot leak into a support report by accident.

What is never included, by construction: the instance URL and where it came
from, project slugs, object ids and names, finding messages and evidence, skip
reasons, request counts per project, headers, credentials, SQL, event payloads,
raw exception text and logs. A finding contributes only its ``code`` — a fixed
identifier from this package, re-checked against a pattern — and a count.

The file is written locally to the destination the operator names, never
uploaded, and never overwrites an existing file.
"""

from __future__ import annotations

import json
import platform
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from tripl_cli import __version__
from tripl_cli.errors import EXIT_USAGE, TriplError
from tripl_cli.model import (
    CHECK_IDS,
    SCOPE_INSTANCE,
    SCOPE_PROJECT,
    SCOPE_UNKNOWN,
    JsonDict,
    Report,
    Severity,
    to_rfc3339,
)

# Versioned on its own: a support engineer reads this file, not the doctor
# document, and the two must be free to evolve separately.
SUPPORT_REPORT_VERSION = 1
SUPPORT_REPORT_KIND = "tripl-support-report"

# A finding code or check id is an identifier this package wrote. Anything that
# does not look like one is reported as "other" rather than echoed.
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Release strings: "0.3.3", "0.4.0rc1", "0.0.0+unknown". Anything else — a
# proxy's error page, a hostname — becomes null.
_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+\-]{0,39}$")
_SCOPES = frozenset({SCOPE_INSTANCE, SCOPE_PROJECT, SCOPE_UNKNOWN})
OTHER_CATEGORY = "other"


class SupportReportError(TriplError):
    """The destination cannot take a support report (exists, or no such directory)."""

    exit_code = EXIT_USAGE


def safe_version(value: object) -> str | None:
    """The server version from the health probe, or None if it is absent or odd."""
    if isinstance(value, str) and _VERSION.fullmatch(value):
        return value
    return None


def _category(code: str) -> str:
    return code if _IDENTIFIER.fullmatch(code) else OTHER_CATEGORY


def build_support_report(report: Report, *, exit_code: int, server_version: str | None) -> JsonDict:
    """The whole document, from the allowlist above and nothing else."""
    known_ids = set(CHECK_IDS)
    checks: list[JsonDict] = []
    not_run: list[str] = []
    for check in report.checks:
        if check.id not in known_ids:
            continue
        could_run = check.status is not Severity.SKIP
        if not could_run:
            not_run.append(check.id)
        categories = Counter(_category(finding.code) for finding in check.findings)
        checks.append(
            {
                "id": check.id,
                "status": check.status.value,
                "could_run": could_run,
                "error_categories": [
                    {"code": code, "count": count} for code, count in sorted(categories.items())
                ],
            }
        )
    scope = report.instance.api_key_scope
    return {
        "kind": SUPPORT_REPORT_KIND,
        "support_report_version": SUPPORT_REPORT_VERSION,
        "generated_at": to_rfc3339(report.generated_at),
        "client": {
            "tool": "tripl",
            "version": __version__,
            "python": platform.python_version(),
            "os": platform.system() or OTHER_CATEGORY,
        },
        "server": {"version": safe_version(server_version)},
        "api_key_scope": scope if scope in _SCOPES else SCOPE_UNKNOWN,
        "status": report.status.value,
        "exit_code": exit_code,
        "summary": dict(report.counts),
        "checks": checks,
        "checks_not_run": not_run,
    }


def check_destination(path: Path) -> None:
    """Refuse a destination before any request is made, not after the run."""
    if path.exists():
        raise SupportReportError(
            f"{path} already exists; choose a new file name (a support report never overwrites)"
        )
    if not path.parent.is_dir():
        raise SupportReportError(f"directory {path.parent} does not exist")


def write_support_report(path: Path, document: Mapping[str, object]) -> None:
    """Create ``path`` exclusively and write the document; never overwrite."""
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError as exc:
        raise SupportReportError(
            f"{path} already exists; choose a new file name (a support report never overwrites)"
        ) from exc
    except OSError as exc:
        # strerror only: the errno text, not a repr that could carry more.
        raise SupportReportError(f"could not write {path}: {exc.strerror}") from exc
