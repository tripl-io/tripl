"""What fails a ``tripl check`` run: the per-result rule, with and without markers or a gate.

The reports are built by hand, the way ``test_check_reports`` builds them: the
rule is a property of ``CheckReport`` alone. With no marker it must be today's
status rule exactly, for every status and severity, strict or not, so the
first block holds the two side by side. The gate's own pieces (``gate.py``:
the item multiset, the plan-branch comparison, ``apply_gate``) follow.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from tripl_cli.check.gate import (
    apply_gate,
    candidates,
    changed_items,
    gate_document,
    item_key,
    plan_branch_causes,
)
from tripl_cli.check.model import (
    GATE_PATCH,
    MODE_STATIC,
    CheckFinding,
    CheckItem,
    CheckReport,
    CheckResult,
    GateInfo,
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


# --- the gate ---------------------------------------------------------------------------
GATE = GateInfo(source=GATE_PATCH, files=1, patch="change.diff")


def _gated(*results: CheckResult, changed: tuple[bool, ...], strict: bool = False) -> CheckReport:
    report = _report(*results, strict=strict)
    return replace(report, results=apply_gate(results, changed, {}), gate=GATE)


def _at(line: int, name: str = "pay_tapped", path: str = "Sources/Pay.swift") -> CheckItem:
    return replace(
        _item(), origin=Origin(path, line, 9, line, 40, "Analytics.shared.log"), name=name
    )


def test_a_gate_fails_only_on_the_changed_items() -> None:
    old = CheckResult(_at(3, "legacy"), "error", (_finding("unknown_event", "error"),))
    new = CheckResult(_at(9, "fresh"), "error", (_finding("unknown_event", "error"),))
    assert _gated(old, new, changed=(False, True)).exit_code == 1
    report = _gated(old, new, changed=(False, False))
    assert report.exit_code == 0
    # JUnit's rule is the plan's verdict, gate or no gate.
    assert [report.verdict_fails(result) for result in report.results] == [True, True]
    document = gate_document(report)
    assert document is not None
    assert (document["failing"], document["not_new"], document["changed_items"]) == (0, 2, 0)


def test_with_a_gate_the_status_fallback_fails_only_a_changed_item() -> None:
    result = CheckResult(_item(), "error")
    assert _gated(result, changed=(True,)).exit_code == 1
    assert _gated(result, changed=(False,)).exit_code == 0


def test_no_verdict_fails_a_gated_run_at_an_unchanged_item() -> None:
    result = result_for(_item(), None, strict=False, sent=True)
    report = _gated(result, changed=(False,))
    assert report.results[0].findings[0].in_change is False
    assert report.exit_code == 1


def test_a_suppressed_finding_in_the_change_still_passes() -> None:
    result = CheckResult(_item(), "error", (_finding("unknown_event", "error", waived=True),))
    assert _gated(result, changed=(True,)).exit_code == 0


def test_apply_gate_leaves_its_input_alone() -> None:
    results = (CheckResult(_item(), "error", (_finding("unknown_event", "error"),)),)
    snapshot = repr(results)
    gated = apply_gate(results, (False,), {0: (True,)})
    assert repr(results) == snapshot
    assert results[0].in_change is None and results[0].findings[0].in_change is None
    [finding] = gated[0].findings
    assert (gated[0].in_change, finding.in_change, finding.cause) == (False, True, "plan_branch")


def test_changed_items_compares_multisets_and_prefers_the_touched_lines() -> None:
    items = [_at(3), _at(5), _at(7)]
    base = [_at(30), _at(31)]  # two identical calls at the base, wherever they sat
    assert changed_items(items, base, {}) == (False, False, True)
    # The diff touched line 5: that one is the new one.
    pay = frozenset({"Sources/Pay.swift"})
    touched = {"Sources/Pay.swift": frozenset({5})}
    assert changed_items(items, base, touched, files=pay) == (False, True, False)
    # The same call in another file is the same call: moved, not new.
    assert changed_items([_at(3, path="Sources/Other.swift")], [_at(3)], {}) == (False,)
    # A copy in a changed file is the new one, the call in a file the change
    # left alone keeps the base's count, wherever each sorts.
    copied = [_at(3, path="Sources/New.swift"), _at(3, path="Sources/Old.swift")]
    new = frozenset({"Sources/New.swift"})
    assert changed_items(copied, [_at(3, path="Sources/Old.swift")], {}, files=new) == (
        True,
        False,
    )
    # A file with no base (untracked, unreconstructable, a moved submodule's)
    # is changed whatever the base has, and takes up none of it.
    assert changed_items(
        copied, [_at(3, path="Sources/Old.swift")], {}, files=new, forced=("Sources/Old.swift",)
    ) == (False, True)
    assert changed_items(
        [_at(3, path="Vendor/Kit/A.swift"), _at(3)], [_at(3)], {}, forced=("Vendor/Kit/",)
    ) == (True, False)


def test_an_item_key_is_what_it_sends_never_its_line_or_file() -> None:
    assert item_key(_at(3)) == item_key(_at(300))
    assert item_key(_at(3)) == item_key(_at(3, path="Sources/Other.swift"))
    assert item_key(_at(3)) != item_key(_at(3, "other_name"))
    assert item_key(_at(3)) != item_key(replace(_at(3), dynamic=("label",)))
    assert item_key(_at(3)) != item_key(replace(_at(3), sendable=False))
    # What is a set in what is sent compares as one: argument order is not sent.
    assert item_key(replace(_at(3), dynamic=("a", "b"))) == item_key(
        replace(_at(3), dynamic=("b", "a"))
    )


def test_an_item_key_is_the_call_as_matched_and_its_holes_unnamed() -> None:
    def at(snippet: str, **changes: Any) -> CheckItem:
        item = _at(3)
        return replace(item, origin=replace(item.origin, snippet=snippet), **changes)

    # The receiver as written is not the call: the function it names is.
    assert item_key(at("self.analytics.log")) == item_key(at("analytics.log"))
    assert item_key(at("com.acme.Analytics.log")) == item_key(at("Analytics.log"))
    assert item_key(at("analytics.log")) != item_key(at("analytics.logEvent"))
    assert item_key(at("track:properties:")) != item_key(at("track:"))
    # A placeholder's name is the validator's to ignore, everywhere in what is sent.
    named = at(
        "log",
        name="promo_${sheetId}_opened",
        fields={"label": "x_${a}"},
        properties={"screen": "${s}", "nested": {"k": "${k}"}},
    )
    renamed = at(
        "log",
        name="promo_${id}_opened",
        fields={"label": "x_${b}"},
        properties={"screen": "${t}", "nested": {"k": "${j}"}},
    )
    assert item_key(named) == item_key(renamed)
    assert named.wire()["name"] == "promo_${sheetId}_opened"  # what is sent is unchanged
    for other in ("promo_${id}_closed", "promo_${id}_${id}_opened", "${id}_promo_opened"):
        assert item_key(named) != item_key(replace(named, name=other))


def test_plan_branch_causes_counts_code_field_and_severity() -> None:
    def result(*findings: CheckFinding) -> CheckResult:
        return CheckResult(_item(), "error", findings)

    error = CheckFinding("deprecated_event", "error", "archived")
    warning = CheckFinding("deprecated_event", "warning", "deprecated")
    field = CheckFinding("value_not_allowed", "error", "x", field="plan")
    assert plan_branch_causes(
        [result(error), result(error), result(error), result(field, field)],
        [result(), result(warning), result(error), result(field)],
    ) == ((True,), (True,), (False,), (False, True))


def test_candidates_are_the_unchanged_results_that_would_fail() -> None:
    results = [
        CheckResult(_at(1), "error", (_finding("unknown_event", "error"),)),
        CheckResult(_at(2), "error", (_finding("unknown_event", "error"),)),
        CheckResult(_at(3), "warning", (_finding("unknown_field", "warning"),)),
        CheckResult(_at(4), "error", (_finding("unknown_event", "error", waived=True),)),
        result_for(_at(5), None, strict=False, sent=True),
    ]
    assert candidates(results, [False, True, False, False, False], strict=False) == [0]
    assert candidates(results, [False, True, False, False, False], strict=True) == [0, 2]
