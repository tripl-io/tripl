"""``tripl check --baseline`` / ``--write-baseline`` / ``--prune-baseline``: what is already known.

The unit half holds ``check/baseline.py`` to its rules (the key, the multiset
match, the file); the rest runs the command end to end against
``test_check_cmd``'s synthetic validator, with patches made as
``test_check_gate_cmd`` makes them.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from tripl_cli.check import baseline as baseline_mod
from tripl_cli.check.baseline import Entry, baseline_key, collect, load, match, pruned, render_file
from tripl_cli.check.fingerprint import FINGERPRINT_KEY
from tripl_cli.check.model import (
    CODE_NO_VERDICT,
    MODE_PAYLOADS,
    MODE_STATIC,
    CheckFinding,
    CheckItem,
    CheckReport,
    CheckResult,
    Origin,
    Suppression,
)
from tripl_cli.cli import main
from tripl_cli.model import Instance, Run

from .conftest import FakeInstance
from .test_check_cmd import CONFIG, VALIDATE_URL, _respond, _verdict, _write
from .test_check_gate_cmd import change, save

LEGACY_CALL = 'Analytics.shared.log(category: "map", action: "open", label: "x")'
TRIAL_CALL = 'Analytics.shared.logEvent("trial_ended")'
BASELINE = ".tripl/check-baseline.json"


@pytest.fixture
def validator(tripl_api: FakeInstance) -> Any:
    return tripl_api.handler(VALIDATE_URL, _respond, method="POST")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout with one clean call, a call with two errors and two identical erroring calls."""
    root = tmp_path / "repo"
    _write(
        root,
        {
            ".tripl/check.yml": CONFIG,
            "Tracking/Events.swift": """
                enum Category: String { case home, profile }
                enum Action: String { case open }
            """,
            "Sources/Feed.swift": """
                func show() {
                    Analytics.shared.log(category: .home, action: .open, label: "card")
                }
            """,
            "Sources/Legacy.swift": f"""
                func legacy() {{
                    {LEGACY_CALL}
                }}
            """,
            "Sources/Twice.swift": f"""
                func twice() {{
                    {TRIAL_CALL}
                    {TRIAL_CALL}
                }}
            """,
        },
    )
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    return root


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def _entries(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["entries"]


# --- the key ---------------------------------------------------------------------------------
def _item(
    line: int = 3,
    name: str = "trial_ended",
    *,
    path: str = "Sources/A.swift",
    column: int | None = 5,
    snippet: str = "Analytics.shared.logEvent",
) -> CheckItem:
    return CheckItem(
        origin=Origin(path, line, column, line, 40, snippet),
        event_type="legacy",
        name=name,
        fields={},
        properties=None,
        complete=False,
    )


def test_a_key_is_what_the_call_sends_never_its_place_or_its_markers() -> None:
    assert baseline_key(_item(3)) == baseline_key(_item(300, path="Sources/B.swift"))
    assert baseline_key(_item(3)) != baseline_key(_item(3, "other"))
    marked = replace(
        _item(3),
        suppressions=(Suppression("Sources/A.swift", 2, 1, (("unknown_field", None),), "x"),),
    )
    assert baseline_key(marked) == baseline_key(_item(3))


def test_a_captured_event_is_keyed_without_its_position() -> None:
    first = _item(1, path="capture.json", column=None, snippet="event 1")
    third = _item(1, path="capture.json", column=None, snippet="event 3")
    assert baseline_key(first) == baseline_key(third)


def test_a_captured_event_is_keyed_on_its_property_names_not_their_values() -> None:
    event = _item(1, path="capture.ndjson", column=None, snippet="")
    at = replace(event, properties={"ts": 1, "session": "a"})
    later = replace(event, properties={"session": "b", "ts": 2})
    assert baseline_key(at) == baseline_key(later)
    assert baseline_key(at) != baseline_key(replace(event, properties={"ts": 1}))
    assert baseline_key(at) != baseline_key(event)  # no properties at all
    assert baseline_key(replace(event, fields={"category": "a"})) != baseline_key(
        replace(event, fields={"category": "b"})
    )
    # A call site's properties are what the code sends, values included.
    call = replace(_item(), properties={"plan": "annual"})
    assert baseline_key(call) != baseline_key(replace(call, properties={"plan": "monthly"}))


def test_by_presence_an_entry_holds_every_copy_and_prune_keeps_its_count() -> None:
    event = _item(1, path="capture.ndjson", column=None, snippet="")
    results = [_result(event, _error()) for _ in range(3)]
    gone = Entry(baseline_key(_item(name="gone")), "unknown_event", None, "error", 4, "x")
    entries = [_entry(event, count=1), gone]
    matching = match(results, entries, presence=True)
    assert all(r.findings[0].baselined for r in matching.results)
    assert matching.stale(entries) == 4
    assert pruned(entries, matching) == (_entry(event, count=1),)


# --- matching --------------------------------------------------------------------------------
def _result(item: CheckItem, *findings: CheckFinding) -> CheckResult:
    status = "error" if any(f.severity == "error" for f in findings) else "warning"
    return CheckResult(item=item, status=status, findings=findings)


def _error(code: str = "unknown_event", severity: str = "error") -> CheckFinding:
    return CheckFinding(code, severity, "no such event")


def _entry(item: CheckItem, count: int = 1, severity: str = "error") -> Entry:
    return Entry(baseline_key(item), "unknown_event", None, severity, count)


def test_an_entry_holds_as_many_findings_as_its_count_and_no_more() -> None:
    results = [_result(_item(line), _error()) for line in (3, 7, 9)]
    matching = match(results, [_entry(_item(), count=2)])
    assert [r.findings[0].baselined for r in matching.results] == [True, True, False]
    assert matching.seen == (2,)
    assert matching.stale([_entry(_item(), count=2)]) == 0


def test_a_finding_more_severe_than_its_entry_is_new() -> None:
    matching = match([_result(_item(), _error())], [_entry(_item(), severity="warning")])
    assert matching.results[0].findings[0].baselined is False
    assert matching.stale([_entry(_item(), severity="warning")]) == 1
    calmer = match([_result(_item(), _error(severity="warning"))], [_entry(_item())])
    assert calmer.results[0].findings[0].baselined is True


def test_markers_win_and_no_verdict_is_never_held() -> None:
    waived = replace(
        _error(), suppression=Suppression("Sources/A.swift", 2, 1, (("unknown_event", None),), "x")
    )
    blank = CheckFinding(CODE_NO_VERDICT, "error", "no verdict")
    entries = [_entry(_item()), Entry(baseline_key(_item()), CODE_NO_VERDICT, None, "error", 1)]
    matching = match([_result(_item(), waived, blank)], entries)
    assert [f.baselined for f in matching.results[0].findings] == [False, False]
    assert matching.seen == (0, 0)


def test_under_a_gate_the_findings_the_change_left_alone_take_up_the_count_first() -> None:
    new = _result(_item(1), replace(_error(), in_change=True))
    old = _result(_item(9), replace(_error(), in_change=False))
    matching = match([new, old], [_entry(_item())])
    assert [r.findings[0].baselined for r in matching.results] == [False, True]


def test_prune_lowers_counts_and_severities_and_drops_what_is_gone() -> None:
    gone = Entry(baseline_key(_item(name="gone")), "unknown_event", None, "error", 3, "x")
    entries = [_entry(_item(), count=3), gone]
    matching = match([_result(_item(), _error(severity="warning"))], entries)
    assert pruned(entries, matching) == (replace(_entry(_item()), severity="warning"),)


# --- the file --------------------------------------------------------------------------------
def _report(*results: CheckResult, mode: str = MODE_STATIC) -> CheckReport:
    return CheckReport(
        run=Run(
            instance=Instance("http://tripl.test", "$TRIPL_BASE_URL", "$TRIPL_API_KEY", "demo"),
            generated_at="2026-10-30T00:00:00Z",
            duration_ms=1,
            requests=1,
        ),
        project="demo",
        mode=mode,
        strict=False,
        results=results,
    )


def test_the_file_is_the_same_bytes_for_the_same_findings_in_any_order(tmp_path: Path) -> None:
    results = [
        _result(_item(9, path="Sources/B.swift"), _error()),
        _result(_item(3), _error(), _error("value_not_allowed")),
        _result(_item(5, "other"), _error()),
    ]
    once = render_file(collect(_report(*results)), project="demo", mode=MODE_STATIC)
    again = render_file(collect(_report(*reversed(results))), project="demo", mode=MODE_STATIC)
    assert once == again
    document = json.loads(once)
    assert list(document) == [
        "version",
        "fingerprint_key",
        "project",
        "mode",
        "cli_version",
        "entries",
    ]
    assert document["fingerprint_key"] == FINGERPRINT_KEY
    [trial] = [e for e in document["entries"] if e["code"] == "unknown_event" and e["count"] == 2]
    assert trial["example"] == "legacy trial_ended at Sources/A.swift:3"
    assert "generated_at" not in once
    path = tmp_path / "b.json"
    path.write_text(once, encoding="utf-8")
    assert load(str(path), project="demo", mode=MODE_STATIC).entries == collect(_report(*results))


def test_only_findings_that_fail_are_written() -> None:
    warned = _result(_item(1), _error("unknown_field", "warning"))
    blank = _result(_item(2, "x"), CheckFinding(CODE_NO_VERDICT, "error", "no verdict"))
    assert collect(_report(warned, blank)) == ()
    assert [entry.code for entry in collect(replace(_report(warned), strict=True))] == [
        "unknown_field"
    ]


# --- write, then check -----------------------------------------------------------------------
def test_write_then_check_passes_with_the_same_findings(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--json"]) == 1
    plain = _json(capsys)
    assert main(["check", "--write-baseline", BASELINE]) == 0
    out = capsys.readouterr().out
    assert f"Baseline: wrote {BASELINE} (4 findings in 3 entries)." in out
    assert "0 call sites fail the run; 3 errors are in the baseline." in out
    assert "Legacy.swift" not in out.split("Baseline:")[0]
    entries = _entries(repo / BASELINE)
    assert sorted((e["code"], e["field"] or "", e["count"]) for e in entries) == [
        ("unknown_event", "", 1),
        ("unknown_event", "", 2),
        ("value_not_allowed", "category", 1),
    ]
    assert {e["example"] for e in entries} == {
        "se category=map action=open label=x at Sources/Legacy.swift:2",
        "legacy trial_ended at Sources/Twice.swift:2",
    }

    assert main(["check", "--baseline", BASELINE, "--json"]) == 0
    based = _json(capsys)
    assert based["exit_code"] == 0
    assert based["baseline"] == {
        "path": BASELINE,
        "action": "check",
        "entries": 3,
        "findings": 4,
        "baselined": 4,
        "stale": 0,
        "pruned": None,
        "not_pruned": None,
    }
    assert based["summary"]["failing"] == 0

    def bare(document: dict[str, Any]) -> list[Any]:
        return [
            {
                **{k: v for k, v in result.items() if k != "fails"},
                "findings": [
                    {k: v for k, v in finding.items() if k not in ("baselined", "fails")}
                    for finding in result["findings"]
                ],
            }
            for result in document["results"]
        ]

    assert bare(based) == bare(plain)
    assert all(
        finding["baselined"] and not finding["fails"]
        for result in based["results"]
        for finding in result["findings"]
    )


def test_writing_twice_gives_the_same_bytes(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--write-baseline", BASELINE]) == 0
    first = (repo / BASELINE).read_bytes()
    assert main(["check", "--write-baseline", BASELINE]) == 0
    assert (repo / BASELINE).read_bytes() == first
    document = json.loads(first)
    assert document["project"] == "demo"
    assert document["mode"] == MODE_STATIC
    assert first.endswith(b"}\n")


def test_a_new_finding_fails_and_is_the_only_one_listed(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--write-baseline", BASELINE]) == 0
    _write(repo, {"Sources/New.swift": 'func n() { Analytics.shared.logEvent("trial_lost") }\n'})
    capsys.readouterr()
    assert main(["check", "--baseline", BASELINE]) == 1
    out = capsys.readouterr().out
    assert "Sources/New.swift:1:12  error  legacy  trial_lost" in out
    assert "Legacy.swift" not in out
    assert "Twice.swift" not in out
    assert "1 call site fails the run; 3 errors are in the baseline." in out


def test_an_extra_copy_of_a_baselined_call_fails(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--write-baseline", BASELINE]) == 0
    _write(repo, {"Sources/Third.swift": f"func t() {{ {TRIAL_CALL} }}\n"})
    capsys.readouterr()
    assert main(["check", "--baseline", BASELINE, "--json"]) == 1
    document = _json(capsys)
    failing = [r for r in document["results"] if r["fails"]]
    assert len(failing) == 1
    assert failing[0]["name"] == "trial_ended"
    assert document["baseline"]["baselined"] == 4


def test_a_warning_that_became_an_error_fails(
    repo: Path, tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    def calmer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        verdicts = [_verdict(item) for item in body["items"]]
        for verdict in verdicts:
            for finding in verdict["findings"]:
                if finding["code"] == "unknown_event":
                    finding["severity"] = "warning"
            if verdict["findings"] and all(f["severity"] == "warning" for f in verdict["findings"]):
                verdict["status"] = "warning"
        return httpx.Response(200, json={"items": verdicts, "summary": {}})

    tripl_api.handler(VALIDATE_URL, calmer, method="POST")
    assert main(["check", "--strict", "--write-baseline", BASELINE]) == 0
    assert {e["severity"] for e in _entries(repo / BASELINE) if e["code"] == "unknown_event"} == {
        "warning"
    }
    tripl_api.handler(VALIDATE_URL, _respond, method="POST")
    capsys.readouterr()
    assert main(["check", "--baseline", BASELINE, "--json"]) == 1
    document = _json(capsys)
    escalated = [
        finding
        for result in document["results"]
        for finding in result["findings"]
        if finding["code"] == "unknown_event"
    ]
    assert len(escalated) == 3
    assert all(not f["baselined"] and f["fails"] for f in escalated)
    assert document["baseline"]["stale"] == 3


def test_a_suppressed_finding_is_not_written(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    marked = (
        "func legacy() {\n"
        "    // tripl-check-ignore[unknown_event] -- old screen, removed in 5.0\n"
        f"    {LEGACY_CALL}\n"
        "}\n"
    )
    _write(repo, {"Sources/Legacy.swift": marked})
    assert main(["check", "--write-baseline", BASELINE]) == 0
    legacy = [e for e in _entries(repo / BASELINE) if "Legacy.swift" in e["example"]]
    assert [(e["code"], e["field"]) for e in legacy] == [("value_not_allowed", "category")]
    capsys.readouterr()
    # Dropping the marker brings its finding back as new: the baseline never held it.
    _write(repo, {"Sources/Legacy.swift": f"func legacy() {{\n    {LEGACY_CALL}\n}}\n"})
    assert main(["check", "--baseline", BASELINE]) == 1
    out = capsys.readouterr().out
    assert "    error  unknown_event: no such event\n" in out
    assert "    error  value_not_allowed [category]: not allowed (in the baseline)" in out


def test_stale_entries_are_counted_and_never_fail(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--write-baseline", BASELINE]) == 0
    (repo / "Sources/Legacy.swift").unlink()
    capsys.readouterr()
    assert main(["check", "--baseline", BASELINE]) == 0
    out = capsys.readouterr().out
    assert (
        "Baseline: 2 findings it holds were not seen in this run; --prune-baseline drops them."
        in out
    )
    assert main(["check", "--baseline", BASELINE, "--json"]) == 0
    assert _json(capsys)["baseline"]["stale"] == 2


# --- prune -----------------------------------------------------------------------------------
def test_prune_lowers_counts_and_drops_entries_and_never_adds(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--write-baseline", BASELINE]) == 0
    (repo / "Sources/Legacy.swift").unlink()
    _write(
        repo,
        {
            "Sources/Twice.swift": f"func twice() {{\n    {TRIAL_CALL}\n}}\n",
            "Sources/New.swift": 'func n() { Analytics.shared.logEvent("trial_lost") }\n',
        },
    )
    capsys.readouterr()
    assert main(["check", "--baseline", BASELINE, "--prune-baseline"]) == 1  # the new call
    out = capsys.readouterr().out
    assert f"Baseline: pruned 3 findings this run no longer has from {BASELINE}." in out
    assert "not seen in this run" not in out
    entries = _entries(repo / BASELINE)
    assert [(e["code"], e["count"], e["example"]) for e in entries] == [
        ("unknown_event", 1, "legacy trial_ended at Sources/Twice.swift:2")
    ]
    before = (repo / BASELINE).read_bytes()
    assert main(["check", "--baseline", BASELINE, "--prune-baseline", "--json"]) == 1
    document = _json(capsys)
    assert document["baseline"]["pruned"] == 0
    assert (repo / BASELINE).read_bytes() == before


def test_prune_writes_nothing_when_the_instance_was_unavailable(
    repo: Path, tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    validator = tripl_api.handler(VALIDATE_URL, _respond, method="POST")
    assert main(["check", "--write-baseline", BASELINE]) == 0
    before = (repo / BASELINE).read_bytes()
    validator.mock(side_effect=lambda request: httpx.Response(503, json={"detail": "down"}))
    (repo / "Sources/Legacy.swift").unlink()
    assert main(["check", "--baseline", BASELINE, "--prune-baseline"]) == 4
    assert (repo / BASELINE).read_bytes() == before


def test_prune_writes_nothing_when_an_item_got_no_verdict(
    repo: Path, tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    validator = tripl_api.handler(VALIDATE_URL, _respond, method="POST")
    assert main(["check", "--write-baseline", BASELINE]) == 0
    before = (repo / BASELINE).read_bytes()

    def partial(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        verdicts = [_verdict(item) for item in body["items"]][:-1]
        return httpx.Response(200, json={"items": verdicts, "summary": {}})

    validator.mock(side_effect=partial)
    (repo / "Sources/Legacy.swift").unlink()
    capsys.readouterr()
    assert main(["check", "--baseline", BASELINE, "--prune-baseline"]) == 1  # no_verdict fails
    out = capsys.readouterr().out
    assert f"Baseline: {BASELINE} not pruned: 2 items got no verdict" in out
    assert (repo / BASELINE).read_bytes() == before


# --- refusals --------------------------------------------------------------------------------
def _baseline_file(repo: Path, **overrides: Any) -> None:
    document: dict[str, Any] = {
        "version": baseline_mod.BASELINE_VERSION,
        "fingerprint_key": FINGERPRINT_KEY,
        "project": "demo",
        "mode": MODE_STATIC,
        "cli_version": "0",
        "entries": [],
    }
    document.update(overrides)
    (repo / BASELINE).write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize(
    ("argv", "file", "message"),
    [
        (["--baseline", "missing.json"], None, "missing.json does not exist"),
        (["--baseline", BASELINE], "{not json", "is not valid JSON"),
        (["--baseline", BASELINE], {"version": 3}, "baseline version 3"),
        (["--baseline", BASELINE], {"version": 3}, "rewrite it with tripl check --write-baseline"),
        (["--baseline", BASELINE], {"fingerprint_key": "triplCheck/v1"}, "'triplCheck/v1'"),
        (["--baseline", BASELINE], {"project": "other"}, "is project 'other''s"),
        (["--baseline", BASELINE], {"mode": MODE_PAYLOADS}, "keep one baseline per mode"),
        (["--baseline", BASELINE], {"entries": [{"key": "x"}]}, "key must be the list"),
        (
            ["--baseline", BASELINE, "--write-baseline", "b.json"],
            {},
            "--write-baseline writes the baseline --baseline reads",
        ),
        (["--write-baseline", "b.json", "--prune-baseline"], None, "drop --prune-baseline"),
        (["--prune-baseline"], None, "pass --baseline FILE"),
        (
            ["--write-baseline", "b.json", "--new-from-patch", "x.diff"],
            None,
            "drop --new-from-patch",
        ),
        (["--write-baseline", "Sources"], None, "a directory, a link or a special file"),
        (["--write-baseline", "Sources/"], None, "give it the path of a file"),
        (["--list", "--baseline", BASELINE], {}, "--list checks nothing; drop --baseline"),
        (["--list", "--write-baseline", "b.json"], None, "--list checks nothing; drop it"),
    ],
)
def test_a_baseline_that_cannot_be_used_is_exit_2_before_any_request(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    file: Any,
    message: str,
) -> None:
    if isinstance(file, str):
        (repo / BASELINE).write_text(file, encoding="utf-8")
    elif isinstance(file, dict):
        _baseline_file(repo, **file)
    stale = repo / "out.json"
    stale.write_text("{}", encoding="utf-8")
    report = [] if "--list" in argv else ["--report", "json=out.json"]
    assert main(["check", *argv, *report]) == 2
    assert message in capsys.readouterr().err
    assert len(tripl_api.router.calls) == 0
    if report:
        assert not stale.exists()
    assert not (repo / "b.json").exists()


_WIRE = {"event_type": "screen", "name": "home", "fields": {}, "properties": None, "complete": True}


@pytest.mark.parametrize(
    "key",
    [
        [],
        ["log", _WIRE, True, [], []],
        ["log", {**_WIRE, "complete": "yes"}, True, [], [], []],
        ["log", {"name": "home"}, True, [], [], []],
        ["log", _WIRE, 1, [], [], []],
        ["log", _WIRE, True, [7], [], []],
        ["log", _WIRE, True, [], [["unknown_field"]], []],
        ["log", _WIRE, True, [], [], [["unknown_field", ""]]],
    ],
)
def test_a_key_not_in_the_shape_tripl_writes_is_exit_2(
    repo: Path,
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    key: Any,
) -> None:
    # Such a key matches no finding, so a prune would drop it without a word.
    entry = {"key": key, "code": "unknown_field", "field": None, "severity": "error", "count": 1}
    _baseline_file(repo, entries=[entry])
    before = (repo / BASELINE).read_bytes()
    assert main(["check", "--baseline", BASELINE, "--prune-baseline"]) == 2
    assert "entry 1: key must be the list tripl wrote" in capsys.readouterr().err
    assert len(tripl_api.router.calls) == 0
    assert (repo / BASELINE).read_bytes() == before


def test_the_key_tripl_writes_is_well_formed() -> None:
    assert baseline_mod._well_formed(json.loads(baseline_key(_item())))


@pytest.mark.parametrize("flag", ["--baseline", "--write-baseline"])
def test_a_baseline_that_is_also_a_report_path_is_refused_and_left_as_it_was(
    repo: Path,
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    flag: str,
) -> None:
    # Read or written: a report would be written over it, and a failed run
    # removes its report paths, which must not take the baseline with them.
    _baseline_file(repo)
    before = (repo / BASELINE).read_bytes()
    assert main(["check", flag, BASELINE, "--report", f"json=./{BASELINE}"]) == 2
    assert "is also a --report path" in capsys.readouterr().err
    assert len(tripl_api.router.calls) == 0
    assert (repo / BASELINE).read_bytes() == before


# --- with a gate -----------------------------------------------------------------------------
def test_with_a_gate_a_finding_fails_only_when_new_in_the_change_and_not_in_the_baseline(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--write-baseline", BASELINE]) == 0
    capsys.readouterr()
    # In the change, and in the baseline: the patch adds Legacy.swift as it is.
    legacy = (repo / "Sources/Legacy.swift").read_text(encoding="utf-8")
    (repo / "Sources/Legacy.swift").unlink()
    added = save(repo, change(repo, {"Sources/Legacy.swift": legacy}))
    assert main(["check", "--new-from-patch", added]) == 1
    assert main(["check", "--new-from-patch", added, "--baseline", BASELINE]) == 0
    # Not in the baseline, and not in the change: the gate passes it.
    _write(repo, {"Sources/New.swift": 'func n() { Analytics.shared.logEvent("trial_lost") }\n'})
    empty = save(repo, "")
    assert main(["check", "--baseline", BASELINE]) == 1
    assert main(["check", "--new-from-patch", empty, "--baseline", BASELINE]) == 0
    # A pasted copy of a baselined call, in the change: new in both.
    pasted = save(repo, change(repo, {"Sources/Third.swift": f"func t() {{ {TRIAL_CALL} }}\n"}))
    capsys.readouterr()
    assert main(["check", "--new-from-patch", pasted, "--baseline", BASELINE, "--json"]) == 1
    document = _json(capsys)
    [failing] = [r for r in document["results"] if r["fails"]]
    assert failing["location"]["path"] == "Sources/Third.swift"
    assert document["gate"]["failing"] == 1


# --- captured events -------------------------------------------------------------------------
def test_payloads_are_baselined_by_what_each_event_sends(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = {"event_type": "se", "fields": {"category": "map", "action": "open", "label": "x"}}
    good = {"event_type": "se", "fields": {"category": "home", "action": "open", "label": "card"}}
    capture = repo / "capture.ndjson"
    capture.write_text(f"{json.dumps(good)}\n{json.dumps(bad)}\n", encoding="utf-8")
    assert main(["check", "--payloads", str(capture)]) == 1
    assert main(["check", "--payloads", str(capture), "--write-baseline", BASELINE]) == 0
    assert json.loads((repo / BASELINE).read_text(encoding="utf-8"))["mode"] == MODE_PAYLOADS
    # The same events in another order, as a JSON array: positions are not compared.
    array = repo / "capture.json"
    array.write_text(json.dumps([bad, good]), encoding="utf-8")
    assert main(["check", "--payloads", str(array), "--baseline", BASELINE]) == 0
    # How often a test fired an event is not compared: more copies are still held.
    array.write_text(json.dumps([bad, good, bad, bad]), encoding="utf-8")
    assert main(["check", "--payloads", str(array), "--baseline", BASELINE]) == 0
    # Another event is new.
    other = {"event_type": "se", "fields": {"category": "map", "action": "open", "label": "y"}}
    array.write_text(json.dumps([bad, good, other]), encoding="utf-8")
    assert main(["check", "--payloads", str(array), "--baseline", BASELINE]) == 1
    # And a static run cannot read a payload baseline.
    capsys.readouterr()
    assert main(["check", "--baseline", BASELINE]) == 2
    assert "keep one baseline per mode" in capsys.readouterr().err


def test_payloads_recaptured_with_other_property_values_stay_baselined(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    def bad(**properties: Any) -> str:
        fields = {"category": "map", "action": "open", "label": "x"}
        return json.dumps({"event_type": "se", "fields": fields, "properties": properties})

    capture = repo / "capture.ndjson"
    capture.write_text(bad(ts=1, session="a") + "\n", encoding="utf-8")
    assert main(["check", "--payloads", str(capture), "--write-baseline", BASELINE]) == 0
    capture.write_text(bad(session="b", ts=2) + "\n", encoding="utf-8")
    assert main(["check", "--payloads", str(capture), "--baseline", BASELINE]) == 0
    capture.write_text(bad(ts=3, session="c", device="x") + "\n", encoding="utf-8")
    assert main(["check", "--payloads", str(capture), "--baseline", BASELINE]) == 1
    capture.write_text(bad(ts=4) + "\n", encoding="utf-8")
    assert main(["check", "--payloads", str(capture), "--baseline", BASELINE]) == 1


def test_payload_prune_drops_only_what_was_not_seen_and_never_lowers_a_count(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = {"event_type": "se", "fields": {"category": "map", "action": "open", "label": "x"}}
    other = {"event_type": "se", "fields": {"category": "map", "action": "open", "label": "y"}}
    capture = repo / "capture.json"
    capture.write_text(json.dumps([bad, bad, bad, other]), encoding="utf-8")
    assert main(["check", "--payloads", str(capture), "--write-baseline", BASELINE]) == 0
    assert sorted(e["count"] for e in _entries(repo / BASELINE)) == [1, 1, 3, 3]
    before = (repo / BASELINE).read_bytes()
    argv = ["check", "--payloads", str(capture), "--baseline", BASELINE, "--prune-baseline"]
    capture.write_text(json.dumps([bad]), encoding="utf-8")
    capsys.readouterr()
    assert main([*argv, "--json"]) == 0
    document = _json(capsys)
    assert document["baseline"]["pruned"] == 2  # the other event's two findings
    assert document["baseline"]["stale"] == 2
    assert sorted(e["count"] for e in _entries(repo / BASELINE)) == [3, 3]
    assert (repo / BASELINE).read_bytes() != before
    pruned_once = (repo / BASELINE).read_bytes()
    assert main(argv) == 0
    assert "Baseline: nothing to prune" in capsys.readouterr().out
    assert (repo / BASELINE).read_bytes() == pruned_once


# --- what each format shows ------------------------------------------------------------------
def _untimed(text: str) -> str:
    return re.sub(r'time="[^"]*"', 'time=""', text)


def test_code_quality_and_junit_are_the_same_with_and_without_a_baseline(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = ["--report", "codequality=cq.json", "--report", "junit=junit.xml"]
    assert main(["check", *reports]) == 1
    plain = {
        name: _untimed((repo / name).read_text(encoding="utf-8"))
        for name in ("cq.json", "junit.xml")
    }
    assert main(["check", "--write-baseline", BASELINE]) == 0
    assert main(["check", "--baseline", BASELINE, *reports, "--report", "sarif=out.sarif"]) == 0
    for name, text in plain.items():
        assert _untimed((repo / name).read_text(encoding="utf-8")) == text
    sarif = json.loads((repo / "out.sarif").read_text(encoding="utf-8"))
    [run] = sarif["runs"]
    assert run["properties"]["baseline"]["baselined"] == 4
    assert all(result["properties"]["baselined"] for result in run["results"])
    assert all("baselineState" not in result for result in run["results"])


def test_without_a_baseline_nothing_says_baseline(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--json", "--report", "sarif=out.sarif"]) == 1
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert "baseline" not in document
    assert "failing" not in document["summary"]
    assert all(
        "baselined" not in finding and "fails" not in finding
        for result in document["results"]
        for finding in result["findings"]
    )
    assert "Baseline" not in captured.err
    sarif = (repo / "out.sarif").read_text(encoding="utf-8")
    assert "baselined" not in sarif
    assert '"baseline"' not in sarif


def test_the_module_reads_the_version_it_writes() -> None:
    assert (
        json.loads(render_file((), project="demo", mode=MODE_STATIC))["version"]
        == baseline_mod.BASELINE_VERSION
    )
