"""The values ``tripl check`` passes between scanning, validating and rendering."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tripl_cli.model import JsonDict, Run

MODE_STATIC = "static"
MODE_PAYLOADS = "payloads"

# The path a payload read from standard input reports as its origin.
STDIN_LABEL = "<stdin>"

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"
# A call whose name and every field are unknowable at scan time. Nothing was
# sent for it; `--strict` turns it into a warning.
STATUS_DYNAMIC = "dynamic"

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

# Raised locally, never by the validator.
CODE_DYNAMIC_VALUE = "dynamic_value"
# Raised locally: a value over the validator's size limits, sent as null.
CODE_OVERSIZE_VALUE = "oversize_value"
# Raised locally: the route answered nothing for an item that was sent. Never
# suppressible, because nothing was decided about the call.
CODE_NO_VERDICT = "no_verdict"

# Why a ``tripl-check-ignore`` marker suppresses nothing (``check/suppress.py``):
# it names no code, names ``no_verdict``, or gives no reason.
MARKER_NO_CODES = "no_codes"
MARKER_NOT_SUPPRESSIBLE = "not_suppressible"
MARKER_NO_REASON = "no_reason"
# The two codes a report gives a marker it lists (SARIF rules, Code Quality
# check names): one that suppresses nothing, and a valid one that matched no finding.
CODE_INVALID_SUPPRESSION = "invalid_suppression"
CODE_UNUSED_SUPPRESSION = "unused_suppression"

# Why the scan could not read a value, as ``tripl check --list`` reports it: a
# hint for whoever writes the check config, never a verdict (nothing sent or
# decided depends on one). The evaluator's codes come first (``values.py``
# explains each branch), the listing's own after them (``inventory.py``): it
# refines a ``variable`` from the function the call sits in, and says why a
# dynamic target with no dynamic value has nothing readable.
REASON_VARIABLE = "variable"
REASON_UNKNOWN_CONSTANT = "unknown_constant"
REASON_FORMAT = "format"
REASON_FUNCTION = "function"
REASON_CONDITIONAL = "conditional"
REASON_CONCATENATION = "concatenation"
REASON_EXPRESSION = "expression"
REASON_PARAMETER = "parameter"
REASON_PARAMETER_PROPERTY = "parameter_property"
REASON_LOCAL = "local"
REASON_NOT_PASSED = "not_passed"
REASON_WRONG_SHAPE = "wrong_shape"
REASON_CODES: tuple[str, ...] = (
    REASON_VARIABLE,
    REASON_UNKNOWN_CONSTANT,
    REASON_FORMAT,
    REASON_FUNCTION,
    REASON_CONDITIONAL,
    REASON_CONCATENATION,
    REASON_EXPRESSION,
    REASON_PARAMETER,
    REASON_PARAMETER_PROPERTY,
    REASON_LOCAL,
    REASON_NOT_PASSED,
    REASON_WRONG_SHAPE,
)

# Why ``--list`` counts a call no spec matches as one that looks like tracking,
# strongest first: it passes an event-name constant, its name says so, or a
# tracking vendor's receiver calls a tracking verb.
SIGNAL_CONSTANT = "constant"
SIGNAL_NAME = "name"
SIGNAL_RECEIVER = "receiver"
SIGNALS: tuple[str, ...] = (SIGNAL_CONSTANT, SIGNAL_NAME, SIGNAL_RECEIVER)


@dataclass(frozen=True)
class Origin:
    """Where an item came from: a call site (``column`` set) or a payload line."""

    path: str
    line: int
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None
    # The callee or selector for a call site; empty for a payload.
    snippet: str = ""


@dataclass(frozen=True)
class Suppression:
    """A valid ``tripl-check-ignore`` marker, as attached to the calls it covers.

    ``path``, ``line`` and ``column`` are where the marker's keyword is (the
    marker's identity in the run); each target is a ``(code, field)`` pair,
    the field None to waive the code on any field.
    """

    path: str
    line: int
    column: int
    targets: tuple[tuple[str, str | None], ...]
    reason: str

    def covers(self, finding: CheckFinding) -> bool:
        if finding.code == CODE_NO_VERDICT:
            return False
        return any(
            code == finding.code and (field is None or field == finding.field)
            for code, field in self.targets
        )


@dataclass(frozen=True)
class Marker:
    """A ``tripl-check-ignore`` comment the scan read, valid or not.

    ``text`` is the marker as written, from its keyword to the end of its
    comment line, with each run of whitespace made one space. ``problem`` is
    one of the ``MARKER_*`` codes for a marker that suppresses nothing, else None.
    """

    path: str
    line: int
    column: int
    text: str
    targets: tuple[tuple[str, str | None], ...]
    reason: str | None
    problem: str | None = None

    def suppression(self) -> Suppression | None:
        if self.problem is not None or self.reason is None:
            return None
        return Suppression(self.path, self.line, self.column, self.targets, self.reason)


@dataclass(frozen=True)
class MarkerState:
    """A marker after the run: ``used`` when it suppressed at least one finding."""

    marker: Marker
    used: bool

    @property
    def code(self) -> str | None:
        """The code a report lists this marker under; None for one that did its job."""
        if self.marker.problem is not None:
            return CODE_INVALID_SUPPRESSION
        return None if self.used else CODE_UNUSED_SUPPRESSION


@dataclass(frozen=True)
class CheckItem:
    """One thing to validate. ``wire`` is the request item, minus its ``ref``."""

    origin: Origin
    event_type: str | None
    name: str | None
    fields: Mapping[str, str | None]
    properties: Mapping[str, Any] | None
    complete: bool
    # Targets whose value could not be known statically (names, field names).
    dynamic: tuple[str, ...] = ()
    # False when nothing about the call is known: it is reported, never sent.
    sendable: bool = True
    # Findings the CLI raised before sending (a value over the route's limits).
    # Never on the wire.
    notes: tuple[CheckFinding, ...] = ()
    # The markers covering the call this item came from. Outside equality and
    # never on the wire, so identical items are still sent once and keep their
    # fingerprints.
    suppressions: tuple[Suppression, ...] = field(default=(), compare=False)

    def wire(self) -> JsonDict:
        return {
            "event_type": self.event_type,
            "name": self.name,
            "fields": dict(self.fields),
            "properties": None if self.properties is None else dict(self.properties),
            "complete": self.complete,
        }


@dataclass(frozen=True)
class CheckFinding:
    code: str
    severity: str
    message: str
    field: str | None = None
    # The marker that waives this finding; the finding is still reported.
    suppression: Suppression | None = None


def severe(finding: CheckFinding, strict: bool) -> bool:
    """Whether ``finding`` is of a severity that fails a run: an error, or anything under strict."""
    return finding.severity == SEVERITY_ERROR or (
        strict and finding.severity in (SEVERITY_WARNING, SEVERITY_INFO)
    )


def finding_fails(finding: CheckFinding, strict: bool) -> bool:
    """Whether ``finding`` fails the run on its own: severe, and not suppressed."""
    return severe(finding, strict) and finding.suppression is None


@dataclass(frozen=True)
class CheckResult:
    item: CheckItem
    status: str
    findings: tuple[CheckFinding, ...] = ()
    event_id: str | None = None
    identity: str | None = None


@dataclass(frozen=True)
class SkippedFile:
    """A file the static scan was configured to read and did not."""

    path: str
    # ``too_large`` (over the scanner's size cap) or ``unreadable``.
    reason: str


SKIP_TOO_LARGE = "too_large"
SKIP_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class CheckReport:
    run: Run
    project: str
    mode: str
    strict: bool
    results: tuple[CheckResult, ...]
    branch_id: str | None = None
    branch_name: str | None = None
    config_path: str | None = None
    files_scanned: int = 0
    meta: Mapping[str, Any] = field(default_factory=dict)
    # ``Origin.path`` -> the same file relative to the repository root, for the
    # formats a CI system resolves against its checkout (Code Quality, JUnit).
    # A path missing here is used as it is.
    repo_paths: Mapping[str, str] = field(default_factory=dict)
    skipped: tuple[SkippedFile, ...] = ()
    # Every ``tripl-check-ignore`` marker in the scanned files, in scan order.
    # Empty in payload mode, which reads no source.
    markers: tuple[MarkerState, ...] = ()

    def repo_path(self, origin: Origin) -> str:
        return self.repo_file(origin.path)

    def repo_file(self, path: str) -> str:
        return self.repo_paths.get(path, path)

    def count(self, status: str) -> int:
        return sum(1 for result in self.results if result.status == status)

    @property
    def errors(self) -> int:
        return self.count(STATUS_ERROR)

    @property
    def warnings(self) -> int:
        return self.count(STATUS_WARNING)

    @property
    def suppressed(self) -> int:
        """How many findings a marker waives, across every result."""
        return sum(
            1
            for result in self.results
            for finding in result.findings
            if finding.suppression is not None
        )

    def fails(self, result: CheckResult) -> bool:
        """Whether ``result`` makes the run exit 1.

        Its status must fail (an error, or a warning under ``--strict``), and
        a marker must not have waived every finding behind that: a result
        whose severe findings are all suppressed passes. One with no severe
        finding at all (a reply whose status and findings disagree) fails on
        its status alone, so with no marker this is exactly the status rule.
        """
        if not (result.status == STATUS_ERROR or (self.strict and result.status == STATUS_WARNING)):
            return False
        weighed = [finding for finding in result.findings if severe(finding, self.strict)]
        return not weighed or any(finding.suppression is None for finding in weighed)

    @property
    def exit_code(self) -> int:
        """1 when any result fails (``fails``); else 0."""
        return 1 if any(self.fails(result) for result in self.results) else 0
