"""What fails a ``tripl check`` run: the per-result rule, with and without markers.

The reports are built by hand, the way ``test_check_reports`` builds them: the
rule is a property of ``CheckReport`` alone. With no marker it must be today's
status rule exactly, for every status and severity, strict or not, so the
first block holds the two side by side.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tripl_cli.check.model import (
    MODE_STATIC,
    CheckFinding,
    CheckItem,
    CheckReport,
    CheckResult,
    Marker,
    Origin,
    Suppression,
)
from tripl_cli.check.suppress import marker_states
from tripl_cli.check.validate import result_for, summary
from tripl_cli.model import Instance, Run

RUN = Run(
    instance=Instance("http://tripl.test", "$TRIPL_BASE_URL", "$TRIPL_API_KEY", "project"),
    generated_at=datetime(2026, 10, 22, 9, 0, tzinfo=UTC),
    duration_ms=10,
    requests=1,
)
WAIVER = Suppression("Sources/Pay.swift", 11, 8, (("unknown_event", None),), "staged rollout")
STATUSES = ("ok", "warning", "error", "dynamic")
SEVERITIES = ("error", "warning", "info")


def _item(*suppressions: Suppression, dynamic: tuple[str, ...] = ()) -> CheckItem:
    return CheckItem(
        origin=Origin("Sources/Pay.swift", 12, 9, 12, 40, "Analytics.shared.log"),
        event_type="se",
        name="pay_tapped",
        fields={},
        properties=None,
        complete=False,
        dynamic=dynamic,
        suppressions=suppressions,
    )


def _report(*results: CheckResult, strict: bool = False) -> CheckReport:
    return CheckReport(run=RUN, project="shop", mode=MODE_STATIC, strict=strict, results=results)


def _finding(code: str, severity: str, *, waived: bool = False) -> CheckFinding:
    return CheckFinding(code, severity, "m", suppression=WAIVER if waived else None)


def _today(report: CheckReport) -> int:
    """The rule before markers existed: any error, or any warning under --strict."""
    return 1 if report.errors or (report.strict and report.warnings) else 0


# --- equivalence ------------------------------------------------------------------------
@pytest.mark.parametrize("strict", [False, True])
def test_with_no_marker_the_exit_code_is_the_status_rule(strict: bool) -> None:
    shapes = [()] + [
        tuple(_finding("c", severity) for severity in combo)
        for size in (1, 2)
        for combo in itertools.product(SEVERITIES, repeat=size)
    ]
    for status, findings in itertools.product(STATUSES, shapes):
        report = _report(CheckResult(_item(), status, findings), strict=strict)
        assert report.exit_code == _today(report), (status, findings, strict)


def test_a_status_that_fails_with_no_severe_finding_still_fails() -> None:
    # A reply whose status and findings disagree: the status decides, as before.
    assert _report(CheckResult(_item(), "error")).exit_code == 1
    assert _report(CheckResult(_item(), "error", (_finding("c", "warning"),))).exit_code == 1
    assert _report(CheckResult(_item(), "warning"), strict=True).exit_code == 1


# --- suppression ------------------------------------------------------------------------
def test_a_suppressed_error_leaves_the_warning_which_fails_only_under_strict() -> None:
    findings = (
        _finding("unknown_event", "error", waived=True),
        _finding("unknown_field", "warning"),
    )
    result = CheckResult(_item(), "error", findings)
    assert _report(result).exit_code == 0
    assert _report(result, strict=True).exit_code == 1


def test_every_severe_finding_suppressed_passes() -> None:
    findings = (
        _finding("unknown_event", "error", waived=True),
        _finding("value_not_allowed", "error", waived=True),
    )
    report = _report(CheckResult(_item(), "error", findings))
    assert report.exit_code == 0
    assert report.suppressed == 2
    # The verdict and the counts stay the plan's.
    assert report.errors == 1
    assert summary(report.results) == {
        "checked": 1,
        "ok": 0,
        "warnings": 0,
        "errors": 1,
        "dynamic": 0,
    }


def test_a_suppressed_strict_dynamic_value_does_not_fail() -> None:
    waiver = Suppression("Sources/Pay.swift", 11, 8, (("dynamic_value", "label"),), "runtime")
    item = _item(waiver, dynamic=("label",))
    result = result_for(item, {"ref": "i0", "status": "ok", "findings": []}, strict=True, sent=True)
    assert result.status == "warning"
    [finding] = result.findings
    assert finding.suppression == waiver
    assert _report(result, strict=True).exit_code == 0


def test_no_verdict_always_fails_whatever_a_marker_says() -> None:
    waiver = Suppression("Sources/Pay.swift", 11, 8, (("no_verdict", None),), "never")
    result = result_for(_item(waiver), None, strict=False, sent=True)
    [finding] = result.findings
    assert finding.code == "no_verdict"
    assert finding.suppression is None
    assert _report(result).exit_code == 1


def test_result_for_marks_only_the_findings_a_marker_lists() -> None:
    verdict = {
        "ref": "i0",
        "status": "error",
        "findings": [
            {"code": "unknown_event", "severity": "error", "field": None, "message": "x"},
            {"code": "value_not_allowed", "severity": "error", "field": "plan", "message": "y"},
        ],
    }
    result = result_for(_item(WAIVER), verdict, strict=False, sent=True)
    assert [finding.suppression for finding in result.findings] == [WAIVER, None]
    assert result.status == "error"
    assert _report(result).exit_code == 1


# --- marker states ----------------------------------------------------------------------
def test_a_marker_is_used_when_it_covers_a_finding_of_its_call() -> None:
    used = Marker(WAIVER.path, WAIVER.line, WAIVER.column, "t", WAIVER.targets, WAIVER.reason)
    unused = replace(used, line=40)
    result = CheckResult(_item(WAIVER), "error", (_finding("unknown_event", "error", waived=True),))
    states = marker_states([used, unused], [result])
    assert [(state.marker.line, state.used) for state in states] == [(11, True), (40, False)]
    assert [state.code for state in states] == [None, "unused_suppression"]


def test_two_markers_on_one_call_are_both_used() -> None:
    # One above the call and one trailing it: the finding carries the first,
    # and the second waives it just as well, so neither is "unused".
    trailing = Suppression(WAIVER.path, 12, 50, WAIVER.targets, "trailing")
    verdict = {
        "ref": "i0",
        "status": "error",
        "findings": [{"code": "unknown_event", "severity": "error", "message": "x"}],
    }
    result = result_for(_item(WAIVER, trailing), verdict, strict=False, sent=True)
    assert [finding.suppression for finding in result.findings] == [WAIVER]
    markers = [
        Marker(waiver.path, waiver.line, waiver.column, "t", waiver.targets, waiver.reason)
        for waiver in (WAIVER, trailing)
    ]
    assert [state.used for state in marker_states(markers, [result])] == [True, True]
    # A marker on the call that targets another code is still unused.
    other = Suppression(WAIVER.path, 12, 60, (("unknown_field", None),), "other")
    result = result_for(_item(WAIVER, other), verdict, strict=False, sent=True)
    marker = Marker(other.path, other.line, other.column, "t", other.targets, other.reason)
    assert [state.used for state in marker_states([marker], [result])] == [False]


def test_an_unused_or_malformed_marker_never_fails_the_run() -> None:
    unused = Marker("a.ts", 1, 4, "t", (("unknown_event", None),), "r")
    malformed = Marker("a.ts", 2, 4, "t", (("unknown_event", None),), None, problem="no_reason")
    for strict in (False, True):
        report = replace(
            _report(CheckResult(_item(), "ok"), strict=strict),
            markers=marker_states([unused, malformed], []),
        )
        assert report.exit_code == 0
        assert [state.code for state in report.markers] == [
            "unused_suppression",
            "invalid_suppression",
        ]
