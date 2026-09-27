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


@dataclass(frozen=True)
class CheckResult:
    item: CheckItem
    status: str
    findings: tuple[CheckFinding, ...] = ()
    event_id: str | None = None
    identity: str | None = None


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

    def count(self, status: str) -> int:
        return sum(1 for result in self.results if result.status == status)

    @property
    def errors(self) -> int:
        return self.count(STATUS_ERROR)

    @property
    def warnings(self) -> int:
        return self.count(STATUS_WARNING)

    @property
    def exit_code(self) -> int:
        """1 on any error, or on any warning under ``--strict``; else 0."""
        if self.errors or (self.strict and self.warnings):
            return 1
        return 0
