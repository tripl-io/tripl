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
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from tripl_cli import __version__
from tripl_cli.check.fingerprint import FINGERPRINT_KEY, finding_fingerprints, marker_fingerprints
from tripl_cli.check.model import (
    MODE_PAYLOADS,
    MODE_STATIC,
    STDIN_LABEL,
    CheckFinding,
    CheckItem,
    CheckReport,
    CheckResult,
    Marker,
    MarkerState,
    Origin,
    Suppression,
)
from tripl_cli.check.render import render_text, sarif_document
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


def _report(
    *results: CheckResult, strict: bool = False, mode: str = MODE_STATIC, **extra: object
) -> CheckReport:
    return CheckReport(
        run=RUN,
        project="shop",
        mode=mode,
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


def _event(number: int, name: str) -> CheckResult:
    """The ``number``-th event of a captured JSON array, as payloads.py reads it."""
    item = CheckItem(
        origin=Origin("events.json", 1, snippet=f"event {number}"),
        event_type="se",
        name=name,
        fields={},
        properties=None,
        complete=True,
    )
    return CheckResult(item=item, status="error", findings=(UNKNOWN,))


def test_a_captured_event_keeps_its_fingerprint_when_one_is_added_before_it() -> None:
    before = _report(_event(1, "pay_tapped"), _event(2, "trial_started"), mode=MODE_PAYLOADS)
    after = _report(
        _event(1, "settings_opened"),
        _event(2, "pay_tapped"),
        _event(3, "trial_started"),
        mode=MODE_PAYLOADS,
    )
    assert finding_fingerprints(after)[1:] == finding_fingerprints(before)


def test_a_file_name_that_is_not_utf8_still_gets_every_report() -> None:
    # os.walk hands such a name over as lone surrogates (surrogateescape).
    report = _report(_result(12, path="Sources/Caf\udce9.swift"))
    [[fingerprint]] = finding_fingerprints(report)
    assert len(fingerprint) == 64
    [run] = sarif_document(report, root=None)["runs"]
    [result] = run["results"]
    location = result["locations"][0]["physicalLocation"]["artifactLocation"]
    assert location["uri"] == "Sources/Caf%E9.swift"  # the name's own bytes
    json.dumps(codequality_document(report))
    json.dumps(check_document(report))
    _suite, [case] = _cases(junit_document(report))
    assert case.get("classname") == "Sources/Caf\ufffd.swift"


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
    assert [case.get("name") for case in cases] == [
        "se pay_tapped (Analytics.shared.log)",
        "se pay_tapped (Analytics.shared.log) #2",
    ]
    _suite, moved = _cases(junit_document(_report(_result(15), _result(43))))
    assert [case.get("name") for case in moved] == [case.get("name") for case in cases]


def test_junit_tells_calls_with_one_name_apart_by_their_fields() -> None:
    card = _result(12, fields={"label": "card"})
    cash = _result(30, fields={"label": "cash"})
    _suite, cases = _cases(junit_document(_report(card, cash)))
    names = [case.get("name") for case in cases]
    assert names == [
        "se pay_tapped label=card (Analytics.shared.log)",
        "se pay_tapped label=cash (Analytics.shared.log)",
    ]
    # Deleting one call leaves the other's name, and so its history, alone.
    _suite, [kept] = _cases(junit_document(_report(cash)))
    assert kept.get("name") == names[1]


def test_junit_names_a_call_without_a_name_by_its_fields() -> None:
    result = _result(12, name=None, fields={"category": "checkout", "label": None})
    _suite, [case] = _cases(junit_document(_report(result)))
    assert case.get("name") == "se category=checkout label=? (Analytics.shared.log)"


def test_junit_names_stay_distinct_as_written() -> None:
    report = _report(
        # Two names XML can only carry as the same text...
        _result(12, name="pay\x01", snippet=""),
        _result(20, name="pay\x02", snippet=""),
        # ...and a name that already reads like a numbered duplicate.
        _result(30, name="open", snippet=""),
        _result(40, name="open", snippet=""),
        _result(50, name="open #2", snippet=""),
    )
    _suite, cases = _cases(junit_document(report))
    assert [case.get("name") for case in cases] == [
        "se pay\ufffd",
        "se pay\ufffd #2",
        "se open",
        "se open #2",
        "se open #2 #2",
    ]


def test_under_strict_a_warning_fails_its_case() -> None:
    warned = _result(20, name="settings_opened", status="warning", findings=(DEPRECATED,))
    suite, [case] = _cases(junit_document(_report(warned, strict=True)))
    assert suite.get("failures") == "1"
    assert case.find("failure") is not None


def test_junit_survives_characters_xml_cannot_carry() -> None:
    result = _result(12, name="pay\x01tapped")
    _suite, [case] = _cases(junit_document(_report(result)))
    assert case.get("name") == "se pay�tapped (Analytics.shared.log)"


def test_junit_is_ascii_whatever_the_names_hold() -> None:
    # A Windows agent's console is not UTF-8; character references survive it.
    document = junit_document(_report(_result(12, name="café_opened")))
    document.encode("ascii")
    _suite, [case] = _cases(document)
    assert case.get("name") == "se café_opened (Analytics.shared.log)"


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


# --- suppression -------------------------------------------------------------------------
WAIVER = Suppression("Sources/Pay.swift", 11, 5, (("unknown_event", None),), "staged rollout")


def _waived(line: int = 12) -> CheckResult:
    result = _result(line)
    return replace(result, findings=(replace(UNKNOWN, suppression=WAIVER),))


def _marker(
    line: int, *, problem: str | None = None, text: str = "tripl-check-ignore[x] -- r"
) -> Marker:
    return Marker("Sources/Pay.swift", line, 5, text, (("x", None),), "r", problem)


def _marked(*results: CheckResult, strict: bool = False) -> CheckReport:
    used = Marker(
        "Sources/Pay.swift",
        11,
        5,
        "tripl-check-ignore[unknown_event] -- staged rollout",
        WAIVER.targets,
        WAIVER.reason,
    )
    return _report(
        *results,
        strict=strict,
        markers=(
            MarkerState(used, True),
            MarkerState(_marker(30, problem="no_reason"), False),
            MarkerState(_marker(40), False),
        ),
    )


def test_a_suppressed_finding_keeps_its_fingerprint() -> None:
    assert finding_fingerprints(_marked(_waived())) == finding_fingerprints(_report(_result(12)))


def test_a_marker_fingerprint_survives_an_edit_above_it() -> None:
    before = marker_fingerprints(_report(markers=(MarkerState(_marker(30), False),)))
    after = marker_fingerprints(_report(markers=(MarkerState(_marker(55), False),)))
    assert before == after
    other = _marker(30, text="tripl-check-ignore[y] -- r")
    assert marker_fingerprints(_report(markers=(MarkerState(other, False),))) != before
    # Two identical markers in one file still get two fingerprints.
    twice = (MarkerState(_marker(30), False), MarkerState(_marker(31), False))
    first, second = marker_fingerprints(_report(markers=twice))
    assert first != second


def test_codequality_keeps_a_suppressed_finding_at_info_with_its_reason() -> None:
    plain = codequality_document(_report(_result(12)))
    [waived, invalid, unused] = codequality_document(_marked(_waived()))
    assert waived["fingerprint"] == plain[0]["fingerprint"]
    assert waived["severity"] == "info"
    assert waived["description"] == f"Suppressed: staged rollout — {plain[0]['description']}"
    assert (invalid["check_name"], invalid["severity"]) == ("tripl/invalid_suppression", "minor")
    assert (unused["check_name"], unused["severity"]) == ("tripl/unused_suppression", "info")
    assert invalid["location"] == {"path": "Sources/Pay.swift", "lines": {"begin": 30}}
    assert invalid["description"] == (
        'tripl-check-ignore needs "-- <reason>", so it suppresses nothing'
    )
    assert unused["description"] == "tripl-check-ignore[x] matched no finding"


def test_junit_fails_no_case_on_a_suppressed_finding_and_tags_it() -> None:
    suites = ET.fromstring(junit_document(_marked(_waived())))
    assert suites.get("failures") == "0"
    [case] = suites.iter("testcase")
    assert case.find("failure") is None
    output = case.findtext("system-out")
    assert output is not None
    assert "error unknown_event: The plan has no event" in output
    assert output.endswith("[suppressed: staged rollout]")


def test_sarif_marks_a_suppressed_result_and_lists_marker_problems() -> None:
    [run] = sarif_document(_marked(_waived()), root=None)["runs"]
    waived, invalid, unused = run["results"]
    assert waived["ruleId"] == "unknown_event"
    assert waived["level"] == "error"
    [suppression] = waived["suppressions"]
    assert suppression["kind"] == "inSource"
    assert suppression["status"] == "accepted"
    assert suppression["justification"] == "staged rollout"
    region = suppression["location"]["physicalLocation"]["region"]
    assert region == {"startLine": 11, "startColumn": 5}
    assert (invalid["ruleId"], invalid["level"]) == ("invalid_suppression", "warning")
    assert (unused["ruleId"], unused["level"]) == ("unused_suppression", "note")
    assert invalid["locations"][0]["physicalLocation"]["region"]["startLine"] == 30
    rules = {rule["id"]: rule for rule in run["tool"]["driver"]["rules"]}
    assert rules["unused_suppression"]["defaultConfiguration"] == {"level": "note"}
    assert rules["invalid_suppression"]["defaultConfiguration"] == {"level": "warning"}
    assert unused["partialFingerprints"][FINGERPRINT_KEY]
    # The run's summary is the plan's verdict, as without markers.
    assert run["properties"]["summary"]["errors"] == 1


def test_the_listing_counts_a_suppressed_finding_and_names_marker_problems() -> None:
    text = render_text(_marked(_waived(), _result(20, name="other")))
    assert "Sources/Pay.swift:12:9" not in text
    assert "Sources/Pay.swift:20:9  error  se  other" in text
    assert "2 call sites in 0 files: 0 ok, 0 warnings, 2 errors; 1 finding suppressed." in text
    assert (
        'Suppression ignored: Sources/Pay.swift:30 tripl-check-ignore needs "-- <reason>".' in text
    )
    assert "Unused suppression: Sources/Pay.swift:40 tripl-check-ignore[x] matched no finding." in (
        text
    )


def test_a_listed_result_shows_the_status_of_what_is_left() -> None:
    warning = CheckFinding("unknown_field", "warning", "unknown", field="ab")
    result = replace(_waived(), findings=(replace(UNKNOWN, suppression=WAIVER), warning))
    text = render_text(_marked(result))
    assert "Sources/Pay.swift:12:9  warning  se  pay_tapped" in text
    assert "unknown_event" not in text.split("\n\n")[0]


def test_the_json_document_says_what_each_marker_did() -> None:
    document = check_document(_marked(_waived()))
    assert document["summary"]["suppressed"] == 1
    assert document["summary"]["failing"] == 0
    assert document["exit_code"] == 0
    [result] = document["results"]
    assert result["status"] == "error"
    assert result["fails"] is False
    [finding] = result["findings"]
    assert finding["fails"] is False
    assert finding["suppression"] == {
        "reason": "staged rollout",
        "location": {"path": "Sources/Pay.swift", "line": 11, "column": 5},
    }
    assert [entry["used"] for entry in document["suppressions"]] == [True, False, False]
    assert document["suppressions"][1] == {
        "location": {"path": "Sources/Pay.swift", "line": 30, "column": 5},
        "targets": [{"code": "x", "field": None}],
        "reason": "r",
        "used": False,
        "problem": "no_reason",
    }


def test_with_no_marker_the_json_document_has_no_suppression_keys() -> None:
    document = check_document(_report(_result(12)))
    assert "suppressions" not in document
    assert set(document["summary"]) == {"checked", "ok", "warnings", "errors", "dynamic"}
    [result] = document["results"]
    assert "fails" not in result
    assert set(result["findings"][0]) == {"code", "severity", "field", "message", "fingerprint"}


def test_a_result_that_still_fails_is_listed_even_with_its_findings_suppressed() -> None:
    # A reply whose error status has only a (suppressed) warning behind it: the
    # status still fails the run, so the listing must still show the call.
    warning = CheckFinding("unknown_field", "warning", "unknown", field="ab", suppression=WAIVER)
    report = _report(replace(_result(12), findings=(warning,)))
    assert report.exit_code == 1
    assert "Sources/Pay.swift:12:9  error  se  pay_tapped" in render_text(report)


# --- no marker, no change ----------------------------------------------------------------
# Written by the CLI as it was before markers existed, from the report below:
# with no marker these formats must not gain a key, a value or a byte.
GOLDEN = Path(__file__).parent / "check_golden"


def _golden_report() -> CheckReport:
    def item(line: int, name: str) -> CheckItem:
        return CheckItem(
            origin=Origin("Sources/Pay.swift", line, 9, line + 1, 40, "Analytics.shared.log"),
            event_type="se",
            name=name,
            fields={"label": "pay"},
            properties=None,
            complete=False,
        )

    return _report(
        CheckResult(
            item(12, "pay_tapped"),
            "error",
            (
                CheckFinding("unknown_event", "error", "no event"),
                CheckFinding("value_not_allowed", "error", "not allowed", field="label"),
            ),
        ),
        CheckResult(
            item(30, "settings_opened"),
            "warning",
            (CheckFinding("deprecated_event", "warning", "deprecated"),),
            event_id="ev-2",
            identity="settings_opened",
        ),
        CheckResult(
            item(44, "pay_tapped"),
            "ok",
            (CheckFinding("dynamic_value", "info", "runtime", field="x"),),
            event_id="ev-3",
        ),
        repo_paths={"Sources/Pay.swift": "apps/ios/Sources/Pay.swift"},
    )


def test_without_markers_sarif_is_byte_for_byte_what_it_was() -> None:
    written = json.dumps(sarif_document(_golden_report(), root=None), indent=2) + "\n"
    golden = (GOLDEN / "no-marker.sarif.json").read_text(encoding="utf-8")
    assert written == golden.replace("@VERSION@", __version__)


def test_without_markers_codequality_and_junit_are_byte_for_byte_what_they_were() -> None:
    report = _golden_report()
    codequality = json.dumps(codequality_document(report), indent=2) + "\n"
    assert codequality == (GOLDEN / "no-marker.codequality.json").read_text(encoding="utf-8")
    assert junit_document(report) == (GOLDEN / "no-marker.junit.xml").read_text(encoding="utf-8")
