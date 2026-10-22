"""The reports a CI system shows on a merge request, built from one ``CheckReport``.

GitLab compares two pipelines' Code Quality reports as a set difference of
fingerprints and their JUnit reports by suite, class and test name, so the
properties pinned here are the ones that decide "new" against "resolved": a
call keeps its identity when code above it moves, and loses it when the call
itself changes.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from tripl_cli.check.fingerprint import FINGERPRINT_KEY, finding_fingerprints
from tripl_cli.check.model import (
    MODE_PAYLOADS,
    MODE_STATIC,
    STDIN_LABEL,
    CheckFinding,
    CheckItem,
    CheckReport,
    CheckResult,
    Origin,
)
from tripl_cli.check.render import sarif_document
from tripl_cli.check.reports import (
    CODEQUALITY_SEVERITY,
    codequality_document,
    junit_document,
    repo_paths,
)
from tripl_cli.model import Instance, Run
from tripl_cli.report import check_document

RUN = Run(
    instance=Instance("http://tripl.test", "$TRIPL_BASE_URL", "$TRIPL_API_KEY", "project"),
    generated_at=datetime(2026, 10, 22, 9, 0, tzinfo=UTC),
    duration_ms=1234,
    requests=1,
)
UNKNOWN = CheckFinding("unknown_event", "error", "The plan has no event 'checkout:tap:pay_buton'")
DEPRECATED = CheckFinding("deprecated_event", "warning", "'settings_opened' is deprecated")


def _result(
    line: int,
    *,
    name: str | None = "pay_tapped",
    fields: dict[str, str | None] | None = None,
    status: str = "error",
    findings: tuple[CheckFinding, ...] = (UNKNOWN,),
    path: str = "Sources/Pay.swift",
    snippet: str = "Analytics.shared.log",
) -> CheckResult:
    item = CheckItem(
        origin=Origin(path, line, 9, line, 40, snippet),
        event_type="se",
        name=name,
        fields=fields or {},
        properties=None,
        complete=False,
    )
    return CheckResult(item=item, status=status, findings=findings)


def _report(*results: CheckResult, strict: bool = False, **extra: object) -> CheckReport:
    return CheckReport(
        run=RUN,
        project="shop",
        mode=MODE_STATIC,
        strict=strict,
        results=results,
        **extra,  # type: ignore[arg-type]
    )


# --- fingerprints -------------------------------------------------------------------
def test_a_fingerprint_survives_an_edit_above_the_call() -> None:
    before = finding_fingerprints(_report(_result(12)))
    after = finding_fingerprints(_report(_result(57)))
    assert before == after
    [[fingerprint]] = before
    assert len(fingerprint) == 64  # a sha256, not a recipe a reader could decode


def test_a_fingerprint_changes_with_the_call_itself() -> None:
    [[original]] = finding_fingerprints(_report(_result(12)))
    for changed in (
        _result(12, name="pay_tapped_v2"),
        _result(12, fields={"label": "pay"}),
        _result(12, snippet="Tracker.log"),
        _result(12, path="Sources/Other.swift"),
        _result(12, findings=(CheckFinding("unknown_event", "error", "x", field="label"),)),
    ):
        [[other]] = finding_fingerprints(_report(changed))
        assert other != original


def test_a_fingerprint_ignores_what_the_plan_says_about_the_call() -> None:
    reworded = CheckFinding("unknown_event", "error", "No event 'pay_tapped' in the plan")
    plain = _report(_result(12))
    replanned = _report(
        CheckResult(
            item=_result(12).item,
            status="error",
            findings=(reworded,),
            identity="checkout:tap:pay",
        )
    )
    assert finding_fingerprints(plain) == finding_fingerprints(replanned)


def test_identical_calls_in_one_file_get_distinct_stable_fingerprints() -> None:
    report = _report(_result(12), _result(30))
    first, second = finding_fingerprints(report)
    assert first != second
    # Both move down by 5 lines: same two fingerprints, in the same order.
    assert finding_fingerprints(_report(_result(17), _result(35))) == (first, second)


def test_the_same_file_path_under_two_check_roots_does_not_collide() -> None:
    ios = _report(_result(12), repo_paths={"Sources/Pay.swift": "apps/ios/Sources/Pay.swift"})
    web = _report(_result(12), repo_paths={"Sources/Pay.swift": "apps/web/Sources/Pay.swift"})
    assert finding_fingerprints(ios) != finding_fingerprints(web)


def test_json_and_sarif_carry_the_same_fingerprint() -> None:
    report = _report(_result(12))
    [[fingerprint]] = finding_fingerprints(report)
    [row] = check_document(report)["results"]
    assert row["findings"][0]["fingerprint"] == fingerprint
    [run] = sarif_document(report, root=None)["runs"]
    [result] = run["results"]
    assert result["partialFingerprints"] == {FINGERPRINT_KEY: fingerprint}


# --- GitLab Code Quality ------------------------------------------------------------
def test_code_quality_has_every_key_gitlab_reads_on_every_entry() -> None:
    report = _report(
        _result(12),
        _result(20, name="settings_opened", status="warning", findings=(DEPRECATED,)),
        repo_paths={"Sources/Pay.swift": "apps/ios/Sources/Pay.swift"},
    )
    issues = codequality_document(report)
    assert len(issues) == 2
    for issue in issues:
        # GitLab stops reading the file at the first entry missing one of these.
        assert isinstance(issue["description"], str) and issue["description"]
        assert isinstance(issue["check_name"], str)
        assert isinstance(issue["fingerprint"], str)
        assert issue["severity"] in {"info", "minor", "major", "critical", "blocker"}
        assert isinstance(issue["location"]["path"], str)
        assert isinstance(issue["location"]["lines"]["begin"], int)
    error, warning = issues
    assert error["check_name"] == "tripl/unknown_event"
    assert error["severity"] == "major"
    assert warning["severity"] == "minor"
    assert error["location"] == {
        "path": "apps/ios/Sources/Pay.swift",
        "lines": {"begin": 12, "end": 12},
    }
    assert "The plan has no event" in error["description"]
    assert "pay_tapped" in error["description"]
    assert error["fingerprint"] == finding_fingerprints(report)[0][0]
    assert CODEQUALITY_SEVERITY["info"] == "info"


def test_code_quality_lists_findings_only() -> None:
    clean = _result(12, status="ok", findings=())
    dynamic = _result(13, name=None, status="dynamic", findings=())
    assert codequality_document(_report(clean, dynamic)) == []


def test_code_quality_of_a_payload_from_stdin_still_parses() -> None:
    item = CheckItem(
        origin=Origin(STDIN_LABEL, 3),
        event_type="se",
        name="x",
        fields={},
        properties=None,
        complete=True,
    )
    report = CheckReport(
        run=RUN,
        project="shop",
        mode=MODE_PAYLOADS,
        strict=False,
        results=(CheckResult(item=item, status="error", findings=(UNKNOWN,)),),
    )
    [issue] = codequality_document(report)
    assert issue["location"] == {"path": STDIN_LABEL, "lines": {"begin": 3}}
    json.dumps(issue)  # every value serialises


# --- JUnit ----------------------------------------------------------------------------
def _cases(document: str) -> tuple[ET.Element, list[ET.Element]]:
    root = ET.fromstring(document)
    [suite] = root.findall("testsuite")
    return suite, suite.findall("testcase")


def test_junit_has_one_case_per_call_and_fails_exactly_the_failing_ones() -> None:
    report = _report(
        _result(12),
        _result(20, name="settings_opened", status="warning", findings=(DEPRECATED,)),
        _result(25, name="trial_started", status="ok", findings=()),
        _result(26, name=None, status="dynamic", findings=()),
    )
    document = junit_document(report)
    assert document.startswith('<?xml version="1.0" encoding="UTF-8"?>\n')
    suite, cases = _cases(document)
    assert suite.get("name") == "tripl check shop"
    assert (suite.get("tests"), suite.get("failures"), suite.get("skipped")) == ("4", "1", "1")
    failed, warned, passed, skipped = cases
    [failure] = failed.findall("failure")
    assert failure.get("type") == "unknown_event"
    assert "Sources/Pay.swift:12:9" in (failure.text or "")
    # A warning does not fail a run without --strict, so it does not fail its case.
    assert warned.find("failure") is None
    assert "deprecated_event" in (warned.findtext("system-out") or "")
    assert list(passed) == []
    assert skipped.find("skipped") is not None
    assert {case.get("classname") for case in cases} == {"Sources/Pay.swift"}


def test_junit_names_never_carry_a_line_and_number_identical_calls() -> None:
    _suite, cases = _cases(junit_document(_report(_result(12), _result(40))))
    assert [case.get("name") for case in cases] == ["se pay_tapped", "se pay_tapped #2"]
    _suite, moved = _cases(junit_document(_report(_result(15), _result(43))))
    assert [case.get("name") for case in moved] == [case.get("name") for case in cases]


def test_junit_names_a_call_without_a_name_by_its_fields() -> None:
    result = _result(12, name=None, fields={"category": "checkout", "label": None})
    _suite, [case] = _cases(junit_document(_report(result)))
    assert case.get("name") == "se category=checkout label=?"


def test_under_strict_a_warning_fails_its_case() -> None:
    warned = _result(20, name="settings_opened", status="warning", findings=(DEPRECATED,))
    suite, [case] = _cases(junit_document(_report(warned, strict=True)))
    assert suite.get("failures") == "1"
    assert case.find("failure") is not None


def test_junit_survives_characters_xml_cannot_carry() -> None:
    result = _result(12, name="pay\x01tapped")
    _suite, [case] = _cases(junit_document(_report(result)))
    assert case.get("name") == "se pay�tapped"


def test_junit_uses_the_repository_path_for_class_and_file() -> None:
    report = _report(_result(12), repo_paths={"Sources/Pay.swift": "apps/ios/Sources/Pay.swift"})
    _suite, [case] = _cases(junit_document(report))
    assert case.get("classname") == "apps/ios/Sources/Pay.swift"
    assert case.get("file") == "apps/ios/Sources/Pay.swift"


# --- repository paths -------------------------------------------------------------------
def test_static_paths_are_rebased_from_the_check_root_onto_the_repository(tmp_path: Path) -> None:
    repo = tmp_path.resolve()
    mapped = repo_paths(
        ["Sources/Pay.swift"],
        mode=MODE_STATIC,
        scan_root=repo / "apps" / "ios",
        repo_root=repo,
        cwd=repo,
    )
    assert mapped == {"Sources/Pay.swift": "apps/ios/Sources/Pay.swift"}


def test_payload_paths_are_rebased_from_the_working_directory(tmp_path: Path) -> None:
    repo = tmp_path.resolve()
    outside = (tmp_path / ".." / "elsewhere.ndjson").resolve()
    mapped = repo_paths(
        ["build/events.ndjson", "../build/x.ndjson", STDIN_LABEL, str(outside)],
        mode=MODE_PAYLOADS,
        scan_root=None,
        repo_root=repo,
        cwd=repo / "app",
    )
    assert mapped == {
        "build/events.ndjson": "app/build/events.ndjson",
        "../build/x.ndjson": "build/x.ndjson",
    }


def test_without_a_repository_every_path_is_used_as_it_is(tmp_path: Path) -> None:
    assert (
        repo_paths(
            ["Sources/Pay.swift"],
            mode=MODE_STATIC,
            scan_root=tmp_path,
            repo_root=None,
            cwd=tmp_path,
        )
        == {}
    )
