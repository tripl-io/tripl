"""``tripl check`` end to end: config, scan or payloads, POST /plan/validate, output, exit code.

The validator behind the route is a small SYNTHETIC one defined here — enough
of the contract to produce each finding code — because what is under test is
the CLI's plumbing: what it sends, how it batches, how verdicts land back on
call sites, and what it prints and exits with. The backend's own rules are
tested in the backend.
"""

from __future__ import annotations

import asyncio
import errno
import io
import json
import os
import re
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
import pytest

from tripl_cli.api import plan_validation
from tripl_cli.check import inventory as inventory_module
from tripl_cli.check import yamlish
from tripl_cli.check.config import PRESET_STYLES, parse_call, repository_root
from tripl_cli.check.model import REASON_CODES, SIGNALS, CheckItem, Origin
from tripl_cli.check.payloads import enforce_limits, parse
from tripl_cli.check.presets import PRESETS
from tripl_cli.check.render import artifact_uri
from tripl_cli.check.validate import result_for, validate
from tripl_cli.cli import main

from .conftest import API_BASE, FakeInstance, make_branch

VALIDATE_URL = f"{API_BASE}/projects/demo/plan/validate"

# The synthetic plan: event type -> (name rule, required fields, allowed values, known identities).
PLAN: dict[str, dict[str, Any]] = {
    "se": {
        "format": "{category}:{action}:{label}",
        "fields": {"category", "action", "label", "value"},
        "required": {"category", "action"},
        "allowed": {"category": {"home", "profile", "checkout"}},
        "events": {
            "home:open:card": "ev-1",
            "profile:open:*": "ev-2",
            "checkout:submit:card": "ev-3",
        },
    },
    "legacy": {
        "format": None,
        "fields": set(),
        "required": set(),
        "allowed": {},
        "events": {"promo_sheet_${id}_shown": "ev-10", "trial_started": "ev-11"},
    },
}


def _identity(event_type: dict[str, Any], item: dict[str, Any]) -> str | None:
    if item.get("name"):
        return str(item["name"])
    if event_type["format"] is None:
        return None
    fields = item.get("fields") or {}
    return re.sub(
        r"\{(\w+)\}",
        lambda match: fields.get(match.group(1)) or "*",
        event_type["format"],
    )


def _normalised(text: str) -> str:
    return re.sub(r"\$\{\w+\}", "${}", text)


def _pattern(text: str) -> str:
    """``*`` is one unknown segment, ``${x}`` any text - in either the plan or the item."""
    return re.escape(_normalised(text)).replace(r"\*", "[^:]*").replace(re.escape("${}"), ".+")


def _matches(identity: str, known: str) -> bool:
    return (
        _normalised(identity) == _normalised(known)
        or re.fullmatch(_pattern(known), identity) is not None
        or re.fullmatch(_pattern(identity), known) is not None
    )


def _finding(code: str, severity: str, field: str | None, message: str) -> dict[str, Any]:
    return {"code": code, "severity": severity, "field": field, "message": message}


def _verdict(item: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    event_type = PLAN.get(item.get("event_type") or "")
    event_id = identity = None
    if event_type is None:
        findings.append(_finding("unknown_event_type", "error", None, "no such type"))
    else:
        identity = _identity(event_type, item)
        event_id = next(
            (
                eid
                for known, eid in event_type["events"].items()
                if identity and _matches(identity, known)
            ),
            None,
        )
        if event_id is None:
            findings.append(_finding("unknown_event", "error", None, "no such event"))
        fields = item.get("fields") or {}
        for name, value in fields.items():
            if event_type["fields"] and name not in event_type["fields"]:
                findings.append(_finding("unknown_field", "warning", name, "unknown"))
            allowed = event_type["allowed"].get(name)
            if allowed and value is not None and value not in allowed:
                findings.append(_finding("value_not_allowed", "error", name, "not allowed"))
        if item.get("complete"):
            for name in sorted(event_type["required"] - set(fields)):
                findings.append(_finding("missing_required_field", "error", name, "missing"))
    severities = {finding["severity"] for finding in findings}
    status = "error" if "error" in severities else "warning" if "warning" in severities else "ok"
    return {
        "ref": item.get("ref"),
        "status": status,
        "event_id": event_id,
        "identity": identity,
        "findings": findings,
    }


def _respond(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    verdicts = [_verdict(item) for item in body["items"]]
    summary = {
        "ok": sum(v["status"] == "ok" for v in verdicts),
        "warnings": sum(v["status"] == "warning" for v in verdicts),
        "errors": sum(v["status"] == "error" for v in verdicts),
    }
    return httpx.Response(200, json={"items": verdicts, "summary": summary})


@pytest.fixture
def validator(tripl_api: FakeInstance) -> Any:
    return tripl_api.handler(VALIDATE_URL, _respond, method="POST")


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")


CONFIG = """
project: demo
sources: ["Sources/**"]
enums: [{file: "Tracking/*.swift", languages: [swift]}]
event_types:
  se:
    calls:
      - function: "Analytics.shared.log"
  legacy:
    calls: [{function: "Analytics.shared.logEvent", name_arg: 0}]
"""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
                func show(sheetId: String) {
                    Analytics.shared.log(category: .home,
                                         action: .open,
                                         label: "card")
                    Analytics.shared.logEvent("promo_sheet_\\(sheetId)_shown")
                }
            """,
        },
    )
    (root / ".git").mkdir()
    monkeypatch.chdir(root / "Sources")
    return root


# --- static mode -----------------------------------------------------------------
def test_a_clean_checkout_exits_0_and_sends_each_call_once(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("tripl check - http://tripl.test (from $TRIPL_BASE_URL)\n")
    assert out.rstrip().endswith("2 call sites in 1 file: 2 ok, 0 warnings, 0 errors.")
    [request] = [call.request for call in validator.calls]
    sent = json.loads(request.content)["items"]
    assert [item["ref"] for item in sent] == ["i0", "i1"]
    assert sent[0] == {
        "ref": "i0",
        "event_type": "se",
        "name": None,
        "fields": {"category": "home", "action": "open", "label": "card"},
        "properties": None,
        "complete": False,
    }
    assert sent[1]["name"] == "promo_sheet_${sheetId}_shown"
    # Main is spelled by omitting ?branch= entirely; strict is the route's default.
    assert "branch" not in request.url.params
    assert "strict" not in json.loads(request.content)


def test_an_unknown_event_is_an_error_at_its_call_site(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        repo,
        {
            "Sources/Profile.swift": "func p() { Analytics.shared.log("
            'category: .profile, action: .open, label: "x") }\n'
        },
    )
    assert main(["check"]) == 0  # profile:open:* is a known event
    capsys.readouterr()
    _write(
        repo,
        {
            "Sources/Profile.swift": "func p() { Analytics.shared.log("
            'category: "map", action: "open", label: "x") }\n'
        },
    )
    assert main(["check"]) == 1
    out = capsys.readouterr().out
    assert "Sources/Profile.swift:1:12  error  se  map:open:x" in out
    assert "    error  unknown_event: no such event" in out
    assert "    error  value_not_allowed [category]: not allowed" in out


def test_identical_calls_are_sent_once_and_the_verdict_fans_back_out(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    call = 'Analytics.shared.log(category: "nope", action: "open", label: "x")'
    _write(repo, {"Sources/Twice.swift": f"func a() {{ {call} }}\nfunc b() {{ {call} }}\n"})
    assert main(["check", "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    sent = json.loads(validator.calls.last.request.content)["items"]
    assert len(sent) == 3  # two Feed calls + ONE for the duplicated call
    errors = [r for r in document["results"] if r["status"] == "error"]
    assert [(r["location"]["path"], r["location"]["line"]) for r in errors] == [
        ("Sources/Twice.swift", 1),
        ("Sources/Twice.swift", 2),
    ]


def test_strict_reports_values_only_known_at_runtime(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        repo,
        {
            "Sources/Dyn.swift": "func d(l: String) { Analytics.shared.log("
            "category: .home, action: .open, label: l) }\n"
        },
    )
    assert main(["check"]) == 0
    capsys.readouterr()
    assert main(["check", "--strict", "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    [warned] = [r for r in document["results"] if r["status"] == "warning"]
    assert warned["fields"]["label"] is None
    [finding] = warned["findings"]
    assert len(finding.pop("fingerprint")) == 64
    assert finding == {
        "code": "dynamic_value",
        "severity": "warning",
        "field": "label",
        "message": "the value of 'label' is not known until runtime",
    }
    assert document["exit_code"] == 1
    assert document["strict"] is True
    assert json.loads(validator.calls.last.request.content)["strict"] is True


def test_info_findings_only_count_under_strict() -> None:
    item = _item(1, "trial_started")
    verdict = {
        "ref": "i0",
        "status": "ok",
        "findings": [
            {"code": "dynamic_value", "severity": "info", "field": "x", "message": "runtime"}
        ],
    }
    relaxed = result_for(item, verdict, strict=False, sent=True)
    assert relaxed.status == "ok"
    assert [finding.severity for finding in relaxed.findings] == ["info"]
    assert result_for(item, verdict, strict=True, sent=True).status == "warning"


def test_branch_is_resolved_by_name_and_sent_as_its_id(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.branches("demo", [make_branch()])
    assert main(["check", "--branch", "checkout-redesign", "--json"]) == 0
    assert validator.calls.last.request.url.params["branch"] == "b-9f21"
    document = json.loads(capsys.readouterr().out)
    assert document["branch"] == {"id": "b-9f21", "name": "checkout-redesign"}


# --- payload mode ------------------------------------------------------------------
def test_payloads_report_a_missing_required_field_with_a_non_zero_exit(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    capture = repo / "capture.ndjson"
    capture.write_text(
        '{"event_type": "se", "fields": {"category": "home", "action": "open", "label": "card"}}\n'
        "\n"
        '{"event_type": "se", "fields": {"category": "profile", "label": "x", "value": 3}}\n',
        encoding="utf-8",
    )
    assert main(["check", "--payloads", str(capture)]) == 1
    out = capsys.readouterr().out
    assert f"{capture}:3  error  se" in out
    assert "missing_required_field [action]: missing" in out
    assert "2 events: 1 ok, 0 warnings, 1 error." in out
    sent = json.loads(validator.calls.last.request.content)["items"]
    assert all(item["complete"] is True for item in sent)
    assert sent[1]["fields"]["value"] == "3"


def test_payloads_need_no_check_config_when_the_project_is_given(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    capture = tmp_path / "events.json"
    capture.write_text(json.dumps([{"event_type": "legacy", "name": "trial_started"}]))
    assert main(["check", "--payloads", str(capture), "--project", "demo"]) == 0
    assert "1 event: 1 ok" in capsys.readouterr().out


def test_an_oversize_payload_value_is_sent_as_null_with_a_warning_on_its_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    long_label = "x" * (plan_validation.FIELD_VALUE_MAX_LENGTH + 1)
    capture = tmp_path / "capture.ndjson"
    capture.write_text(
        json.dumps({"event_type": "legacy", "name": "trial_started"})
        + "\n"
        + json.dumps(
            {
                "event_type": "se",
                "fields": {"category": "home", "action": "open", "label": long_label},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert main(["check", "--payloads", str(capture), "--project", "demo", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    sent = json.loads(validator.calls.last.request.content)["items"]
    assert sent[1]["fields"]["label"] is None
    ok, warned = document["results"]
    assert ok["status"] == "ok"
    assert warned["status"] == "warning"
    assert warned["location"]["line"] == 2
    assert [(f["code"], f["severity"], f["field"]) for f in warned["findings"]] == [
        ("oversize_value", "warning", "label")
    ]


def test_every_route_limit_is_enforced_client_side() -> None:
    fields = {f"f{n}": "v" for n in range(plan_validation.MAX_FIELDS + 5)}
    properties = {f"p{n}": n for n in range(plan_validation.MAX_FIELDS + 1)}
    [item] = parse(
        json.dumps(
            {
                "event_type": "t" * (plan_validation.EVENT_TYPE_MAX_LENGTH + 1),
                "name": "n" * (plan_validation.NAME_MAX_LENGTH + 1),
                "fields": fields,
                "properties": properties,
            }
        ),
        "capture.json",
    )
    assert item.event_type is None
    assert item.name is None
    assert list(item.fields) == list(fields)[: plan_validation.MAX_FIELDS]
    assert item.properties is None
    assert {note.code for note in item.notes} == {"oversize_value"}
    assert len(item.notes) == 4
    # Within bounds: the same item back, nothing noted.
    [fine] = parse('{"name": "trial_started", "fields": {"a": "b"}}', "capture.json")
    assert enforce_limits(fine) is fine
    assert fine.notes == ()


# --- usage errors: exit 2 ------------------------------------------------------------
@pytest.mark.parametrize(
    ("argv", "files", "message"),
    [
        (["check"], {}, "no check config found"),
        (["check", "--check-config", "missing.yml"], {}, "does not exist"),
        (["check"], {".tripl/check.yml": "event_types: {se: {preset: nope}}\n"}, "unknown preset"),
        (
            ["check"],
            {".tripl/check.yml": "event_types: {se: {preset: segment_track}}\n"},
            "name the project",
        ),
        (["check", "--json", "--format", "sarif"], {}, "pick one"),
        (
            ["check", "--payloads", "bad.ndjson", "--project", "demo"],
            {"bad.ndjson": "{nope\n"},
            "line 1 is not valid JSON",
        ),
        (
            ["check", "--payloads", "x.ndjson", "--project", "demo"],
            {"x.ndjson": '{"fields": {}}\n'},
            "names neither",
        ),
        (
            ["check", "--payloads", "empty.ndjson", "--project", "demo"],
            {"empty.ndjson": "\n"},
            "holds no events (pass --allow-empty",
        ),
        (["check", "--report", "codequality"], {}, "write it as FORMAT=PATH"),
        (["check", "--report", "html=x.html"], {}, "'html' is not a format"),
        (["check", "--report", "json=-"], {}, "give --report a file path"),
        (["check", "--report", "json=reports/"], {}, "names a directory"),
        (["check", "--report", "json=out/."], {}, "names a directory"),
        (["check", "--report", "json=out/.."], {}, "names a directory"),
        (["check", "--report", "json=a.json", "--report", "sarif=a.json"], {}, "twice"),
    ],
)
def test_config_and_payload_problems_exit_2_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    files: dict[str, str],
    message: str,
) -> None:
    (tmp_path / ".git").mkdir()
    _write(tmp_path, files)
    monkeypatch.chdir(tmp_path)
    assert main(argv) == 2
    assert message in capsys.readouterr().err
    assert not validator.called


# --- output formats -------------------------------------------------------------------
def test_the_json_document_carries_every_result_and_the_summary(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--json"]) == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert captured.err.startswith("tripl check - ")
    assert document["schema_version"] == 1
    assert document["command"] == "check"
    assert document["mode"] == "static"
    assert document["summary"] == {"checked": 2, "ok": 2, "warnings": 0, "errors": 0, "dynamic": 0}
    first = document["results"][0]
    location = first["location"]
    assert (location["path"], location["line"], location["column"]) == ("Sources/Feed.swift", 2, 5)
    # The call spans three lines; the region ends just past its closing parenthesis.
    assert location["end_line"] == 4
    assert isinstance(location["end_column"], int)
    assert first["event_id"] == "ev-1"
    assert first["identity"] == "home:open:card"


def test_sarif_output_is_a_valid_2_1_0_log_with_regions(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        repo,
        {
            "Sources/Bad.swift": "func b() {\n"
            '    Analytics.shared.log(category: "map", action: "open", label: "x")\n'
            "}\n"
        },
    )
    assert main(["check", "--format", "sarif"]) == 1
    log = json.loads(capsys.readouterr().out)
    assert log["version"] == "2.1.0"
    assert log["$schema"].endswith("sarif-2.1.0.json")
    [run] = log["runs"]
    driver = run["tool"]["driver"]
    assert driver["name"] == "tripl"
    rule_ids = [rule["id"] for rule in driver["rules"]]
    assert set(rule_ids) == {"unknown_event", "value_not_allowed"}
    assert run["originalUriBaseIds"]["%SRCROOT%"]["uri"].startswith("file://")
    assert run["originalUriBaseIds"]["%SRCROOT%"]["uri"].endswith("/")
    for result in run["results"]:
        # The keys SARIF 2.1.0 requires, and the ones code scanning needs to place a result.
        assert result["message"]["text"]
        assert result["level"] == "error"
        assert rule_ids[result["ruleIndex"]] == result["ruleId"]
        [location] = result["locations"]
        physical = location["physicalLocation"]
        assert physical["artifactLocation"] == {
            "uri": "Sources/Bad.swift",
            "uriBaseId": "%SRCROOT%",
        }
        assert physical["region"]["startLine"] == 2
        assert physical["region"]["startColumn"] == 5
        assert result["partialFingerprints"]["triplCheck/v2"]


def test_sarif_for_payloads_points_at_the_capture_file_line(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    capture = repo / "capture.ndjson"
    capture.write_text('{"event_type": "nope", "name": "x"}\n', encoding="utf-8")
    assert main(["check", "--payloads", str(capture), "--format", "sarif"]) == 1
    [run] = json.loads(capsys.readouterr().out)["runs"]
    [result] = run["results"]
    assert result["ruleId"] == "unknown_event_type"
    physical = result["locations"][0]["physicalLocation"]
    assert physical["artifactLocation"] == {"uri": capture.as_uri()}
    assert physical["region"] == {"startLine": 1}
    assert "originalUriBaseIds" not in run


def test_sarif_for_payloads_on_stdin_names_the_line_without_a_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    events = (
        '{"event_type": "legacy", "name": "trial_started"}\n{"event_type": "nope", "name": "x"}\n'
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(events))
    assert main(["check", "--payloads", "-", "--project", "demo", "--format", "sarif"]) == 1
    [run] = json.loads(capsys.readouterr().out)["runs"]
    [result] = run["results"]
    [location] = result["locations"]
    assert "physicalLocation" not in location
    assert location["logicalLocations"] == [
        {"name": "line 2", "fullyQualifiedName": "<stdin> line 2", "kind": "object"}
    ]


@pytest.mark.parametrize(
    ("path", "uri"),
    [
        ("Sources/Feed.swift", "Sources/Feed.swift"),
        ("Sources/My Feed #2.swift", "Sources/My%20Feed%20%232.swift"),
        ("Sources/100%.ts", "Sources/100%25.ts"),
    ],
)
def test_sarif_artifact_uris_are_percent_encoded(path: str, uri: str) -> None:
    assert artifact_uri(path) == uri


# --- reports for CI, and the exit codes a pipeline branches on ----------------------------
BAD_CALL = 'func b() {\n    Analytics.shared.log(category: "map", action: "open", label: "x")\n}\n'


def test_one_run_writes_every_report_from_one_validation(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, {"Sources/Bad.swift": BAD_CALL})
    out = repo / "reports"
    argv = [
        "check",
        "--json",
        "--report",
        f"codequality={out / 'gl-code-quality-report.json'}",
        "--report",
        f"junit={out / 'tripl-check.xml'}",
        "--report",
        f"sarif={out / 'tripl-check.sarif'}",
    ]
    assert main(argv) == 1
    document = json.loads(capsys.readouterr().out)
    assert validator.call_count == 1
    issues = json.loads((out / "gl-code-quality-report.json").read_text(encoding="utf-8"))
    assert {issue["check_name"] for issue in issues} == {
        "tripl/unknown_event",
        "tripl/value_not_allowed",
    }
    assert {issue["location"]["path"] for issue in issues} == {"Sources/Bad.swift"}
    fingerprints = {f["fingerprint"] for r in document["results"] for f in r["findings"]}
    assert {issue["fingerprint"] for issue in issues} == fingerprints
    suite = ET.fromstring((out / "tripl-check.xml").read_text(encoding="utf-8")).find("testsuite")
    assert suite is not None
    assert (suite.get("tests"), suite.get("failures")) == ("3", "1")
    sarif = json.loads((out / "tripl-check.sarif").read_text(encoding="utf-8"))
    assert sarif["version"] == "2.1.0"


def test_report_paths_are_relative_to_the_repository_not_the_check_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "mono"
    app = root / "apps" / "ios"
    _write(app, {".tripl/check.yml": CONFIG, "Sources/Bad.swift": BAD_CALL})
    (root / ".git").mkdir()
    monkeypatch.chdir(app)
    assert main(["check", "--json", "--report", "codequality=cq.json"]) == 1
    [row] = json.loads(capsys.readouterr().out)["results"]
    assert row["location"]["path"] == "Sources/Bad.swift"  # --json keeps the check root's
    issues = json.loads((app / "cq.json").read_text(encoding="utf-8"))
    assert {issue["location"]["path"] for issue in issues} == {"apps/ios/Sources/Bad.swift"}


@pytest.mark.parametrize("failure", ["connect", 503, 429])
def test_an_unavailable_instance_is_exit_4_and_leaves_no_report(
    repo: Path,
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    failure: str | int,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if failure == "connect":
            raise httpx.ConnectError("connection refused", request=request)
        assert isinstance(failure, int)
        return httpx.Response(failure, json={"detail": "busy"})

    tripl_api.handler(VALIDATE_URL, respond, method="POST")
    stale = repo / "gl-code-quality-report.json"
    stale.write_text("[]", encoding="utf-8")
    assert main(["check", "--json", "--report", f"codequality={stale}"]) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "nothing was checked (exit 4)" in captured.err
    # A report an earlier run left must not pass for this one's.
    assert not stale.exists()


def test_a_refused_key_is_still_exit_1_because_a_retry_will_not_fix_it(
    repo: Path, tripl_api: FakeInstance, configured_env: None
) -> None:
    tripl_api.handler(
        VALIDATE_URL, lambda request: httpx.Response(401, json={"detail": "no"}), method="POST"
    )
    assert main(["check"]) == 1


def test_a_config_that_matches_no_call_is_exit_2_unless_empty_is_allowed(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        repo,
        {
            ".tripl/check.yml": """
                project: demo
                sources: ["Sources/**"]
                event_types:
                  legacy:
                    calls: [{function: "Tracker.track", name_arg: 0}]
            """
        },
    )
    assert main(["check"]) == 2
    assert "has a call the call specs match, so nothing was checked" in capsys.readouterr().err
    assert not validator.called
    assert main(["check", "--allow-empty"]) == 0
    assert "No tracking calls matched the check config" in capsys.readouterr().out


def test_sources_that_match_no_file_are_exit_2(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    config = (repo / ".tripl/check.yml").read_text(encoding="utf-8")
    _write(repo, {".tripl/check.yml": config.replace("Sources/**", "Moved/**")})
    assert main(["check"]) == 2
    assert "no readable source file under" in capsys.readouterr().err


def test_an_empty_capture_file_is_fine_with_allow_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / "events.ndjson").write_text("\n", encoding="utf-8")
    argv = ["check", "--payloads", "events.ndjson", "--project", "demo", "--allow-empty"]
    assert main(argv) == 0
    assert "0 events: 0 ok" in capsys.readouterr().out
    assert not validator.called


def test_a_file_the_scan_did_not_read_is_reported(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "Sources" / "Bundle.swift").write_text("// " + "x" * 2_000_001, encoding="utf-8")
    assert main(["check", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["skipped"] == [
        {"path": "Sources/Bundle.swift", "reason": "too_large"}
    ]
    assert "Not read: 1 configured file: Sources/Bundle.swift (too large)." in captured.err


def test_a_report_that_cannot_be_written_fails_the_run_before_stdout(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    written = repo / "tripl-check.xml"
    blocker = repo / "occupied"  # a file where the report's directory should be
    blocker.write_text("not a directory", encoding="utf-8")
    argv = ["check", "--json", "--report", f"junit={written}"]
    assert main([*argv, "--report", f"json={blocker / 'tripl.json'}"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot write the report" in captured.err
    # The report written before the failure goes too: a run that failed has none.
    assert not written.exists()
    assert blocker.read_text(encoding="utf-8") == "not a directory"


def test_a_bad_report_flag_also_removes_the_reports_the_others_name(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = repo / "gl-code-quality-report.json"
    stale.write_text("[]", encoding="utf-8")
    assert main(["check", "--report", f"codequality={stale}", "--report", "html=x.html"]) == 2
    assert "'html' is not a format" in capsys.readouterr().err
    assert not stale.exists()
    assert not validator.called


@pytest.mark.parametrize("kind", ["link", "directory"])
def test_a_report_path_that_is_not_a_regular_file_is_refused_and_left_alone(
    repo: Path,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    kept = repo / "kept.json"
    kept.write_text("{}", encoding="utf-8")
    target = repo / "report.json"
    if kind == "link":
        target.symlink_to(kept)
    else:
        target.mkdir()
    assert main(["check", "--report", f"json={target}"]) == 2
    assert "a directory, a link or a special file" in capsys.readouterr().err
    assert target.is_symlink() if kind == "link" else target.is_dir()
    assert kept.read_text(encoding="utf-8") == "{}"
    assert not validator.called


@pytest.mark.parametrize("setting", ["url", "config"])
def test_settings_that_do_not_resolve_remove_the_reports_too(
    repo: Path,
    validator: Any,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    setting: str,
) -> None:
    stale = repo / "gl-code-quality-report.json"
    stale.write_text("[]", encoding="utf-8")
    argv = ["check", "--report", f"codequality={stale}"]
    if setting == "url":
        monkeypatch.setenv("TRIPL_BASE_URL", "tripl.example.com")  # no scheme
    else:
        argv += ["--config", str(repo / "missing.toml")]
    assert main(argv) == 2
    assert capsys.readouterr().err.startswith("tripl: ")
    # Resolved before `check` starts, and still a run that could not finish.
    assert not stale.exists()
    assert not validator.called


def test_a_command_line_the_parser_rejects_leaves_every_file_alone(
    repo: Path, validator: Any, configured_env: None
) -> None:
    kept = repo / "gl-code-quality-report.json"
    kept.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["check", "--report", f"codequality={kept}", "--no-such-flag"])
    assert kept.read_text(encoding="utf-8") == "[]"


def test_a_report_is_never_written_through_a_link_at_its_temporary_name(
    repo: Path, validator: Any, configured_env: None
) -> None:
    victim = repo / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    (repo / ".tripl-check.xml.tmp").symlink_to(victim)
    report = repo / "tripl-check.xml"
    assert main(["check", "--report", f"junit={report}"]) == 0
    assert victim.read_text(encoding="utf-8") == "keep"
    assert report.is_file() and not report.is_symlink()
    assert not os.path.lexists(repo / ".tripl-check.xml.tmp")


def test_a_listing_the_console_cannot_encode_is_escaped_and_the_reports_kept(
    repo: Path, validator: Any, configured_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Windows agent's pipe is cp1252: a Cyrillic value must not crash the
    # run after its reports are written, and so lose them.
    _write(repo, {"Sources/Bad.swift": BAD_CALL.replace('"map"', '"оплата"')})
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", console)
    report = repo / "tripl-check.xml"
    assert main(["check", "--report", f"junit={report}"]) == 1
    console.flush()
    assert "\\u043e\\u043f" in raw.getvalue().decode("cp1252")
    assert report.is_file()


def test_a_run_that_crashes_leaves_no_report_behind(
    repo: Path, tripl_api: FakeInstance, configured_env: None
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("the validator broke")

    tripl_api.handler(VALIDATE_URL, respond, method="POST")
    stale = repo / "gl-code-quality-report.json"
    stale.write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError):
        main(["check", "--report", f"codequality={stale}"])
    assert not stale.exists()


def test_a_file_name_that_is_not_utf8_gets_every_report(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    try:
        (repo / "Sources" / os.fsdecode(b"Caf\xe9.swift")).write_text(BAD_CALL, encoding="utf-8")
    except (OSError, UnicodeError):
        pytest.skip("this file system takes only UTF-8 names")
    out = repo / "reports"
    argv = ["check", "--json"]
    for report_format in ("text", "json", "sarif", "codequality", "junit"):
        argv += ["--report", f"{report_format}={out / report_format}"]
    assert main(argv) == 1
    captured = capsys.readouterr()
    json.loads(captured.out)
    assert "Sources/Caf\\udce9.swift:2:" in captured.err
    assert "Caf\\udce9.swift" in (out / "text").read_text(encoding="utf-8")
    for machine in ("json", "sarif", "codequality"):
        json.loads((out / machine).read_text(encoding="utf-8"))
    ET.fromstring((out / "junit").read_text(encoding="utf-8"))


# --- batching ------------------------------------------------------------------------
class _RecordingReader:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.requests = 0

    async def send(self, request: Any) -> Any:
        self.requests += 1
        self.bodies.append(request.json_body)
        return {"items": [_verdict(item) for item in request.json_body["items"]]}


def _item(line: int, name: str) -> CheckItem:
    return CheckItem(
        origin=Origin("a.swift", line, 1),
        event_type="legacy",
        name=name,
        fields={},
        properties=None,
        complete=False,
    )


def test_items_are_batched_at_the_route_s_cap_and_mapped_back_in_order() -> None:
    reader = _RecordingReader()
    # All distinct, so de-duplication does not change the batch sizes.
    items = [_item(n, f"promo_sheet_{n}_shown" if n % 2 else f"event_{n}") for n in range(1, 8)]
    results = asyncio.run(
        validate(
            reader,  # type: ignore[arg-type]
            "demo",
            items,
            branch_id=None,
            strict=False,
            batch_size=3,
        )
    )
    assert [len(body["items"]) for body in reader.bodies] == [3, 3, 1]
    assert [result.item.origin.line for result in results] == list(range(1, 8))
    assert [result.status for result in results] == [
        "ok" if n % 2 else "error" for n in range(1, 8)
    ]


def test_the_route_cap_is_enforced_by_the_builder() -> None:
    assert plan_validation.MAX_ITEMS == 5000
    with pytest.raises(ValueError):
        plan_validation.validate("demo", [{}] * (plan_validation.MAX_ITEMS + 1))
    assert plan_validation.validate("demo", [], strict=True).json_body == {
        "items": [],
        "strict": True,
    }
    batches = plan_validation.batches(list(range(12001)))
    assert [len(batch) for batch in batches] == [5000, 5000, 2001]


def test_a_verdict_the_route_dropped_is_an_error_not_a_pass() -> None:
    class _Dropping(_RecordingReader):
        async def send(self, request: Any) -> Any:
            return {"items": []}

    reader = _Dropping()
    [result] = asyncio.run(
        validate(
            reader,  # type: ignore[arg-type]
            "demo",
            [_item(1, "trial_started")],
            branch_id=None,
            strict=False,
        )
    )
    assert result.status == "error"
    assert result.findings[0].code == "no_verdict"


# --- --list: the scan alone, for writing the config ----------------------------------
LIST_CONFIG = """
project: demo
sources: ["app/**"]
event_types:
  event:
    calls:
      - {function: "analytics.logEvent", positional: [name, properties]}
      - {function: "tracker.send"}
"""

LIST_SOURCES = {
    "app/Events.kt": """
        object Events {
            object Name {
                const val Open = "open"
            }
            const val PROMO = "promo_%s_shown"
        }
    """,
    "app/Screen.kt": """
        class Screen {
            fun logEvent(event: String, params: Bundle) {
                analytics.logEvent(event, params)
            }
            fun promo(id: String) {
                analytics.logEvent(Events.PROMO.format(id), mapOf("id" to id, "kind" to "x"))
                analytics.logEvent("fixed", "not a dictionary", extra)
                tracker.send(key = "opened")
            }
            fun show() {
                logEvent("home_shown", bundle)
                wrap(Events.Name.Open)
            }
        }
    """,
}


@pytest.fixture
def listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout whose config matches four calls, one of each kind --list explains."""
    root = tmp_path / "app-repo"
    _write(root, {".tripl/check.yml": LIST_CONFIG, **LIST_SOURCES})
    (root / ".git").mkdir()
    monkeypatch.chdir(root / "app")
    return root.resolve()


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any request fails the test: --list must never open a connection."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("tripl check --list sent a request")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)


def test_list_shows_what_each_call_read_and_why_with_no_instance_configured(
    listed: Path, offline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # No configured_env: no URL, no key, no connection config at all.
    assert main(["check", "--list"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        f"tripl check --list - {listed / '.tripl' / 'check.yml'}, root {listed} "
        "(nothing is sent)\n"
        "\n"
        "app/Screen.kt:3:9  event  analytics.logEvent  name=? properties=?\n"
        "    name: parameter event of logEvent (line 2)\n"
        "    properties: parameter params of logEvent (line 2)\n"
        "app/Screen.kt:6:9  event  analytics.logEvent  name=? properties={id=?, kind=x}\n"
        "    name: format of promo_%s_shown\n"
        "app/Screen.kt:7:9  event  analytics.logEvent  name=fixed properties=not a dictionary\n"
        '    properties: wrong shape "not a dictionary"\n'
        "    not mapped: #2\n"
        "app/Screen.kt:8:9  event  tracker.send  key=opened\n"
        "\n"
        "4 call sites in 2 files: 1 fully read; 2 not sent (no name or field read); "
        "1 sent with a value not read.\n"
        "Values not read: name 2, properties 2.\n"
        "Read per event type: event: name 3, properties 3, key 1.\n"
        "event: field key is read from the label key because the call spec has no args:; "
        "map it in args: (null drops it).\n"
        "\n"
        "Why (call sites, per target):\n"
        "  name        format       1  a printf-style format, which the scan does not fill "
        "in yet\n"
        "  name        parameter    1  a parameter of the function the call sits in: the "
        "value comes from its callers; add a call spec for that function\n"
        "  properties  parameter    1  a parameter of the function the call sits in: the "
        "value comes from its callers; add a call spec for that function\n"
        "  properties  wrong_shape  1  the value has the wrong shape for its target "
        "(properties that are not a dictionary, a name or field that is one)\n"
        "\n"
        "Functions that forward their own parameter into a tracking call (add a call spec "
        "for each, so their calls are read):\n"
        "  logEvent  app/Screen.kt:2  forwards event (#0) to name, params (#1) to properties"
        "  3 calls in this file, 2 matched (first :11)\n"
        "\n"
        "Calls that look like tracking but match no call spec:\n"
        "  1  wrap      constant  app/Screen.kt:12:9  wrap(Events.Name.Open)\n"
        '  1  logEvent  name      app/Screen.kt:11:9  logEvent("home_shown", bundle)\n'
    )


# Every key of the document, with its type: a consumer never tests for a key.
LIST_DOCUMENT_KEYS: dict[str, Any] = {
    "schema_version": int,
    "tool": str,
    "tool_version": str,
    "command": str,
    "generated_at": str,
    "duration_ms": int,
    "project": (str, type(None)),
    "config": (str, type(None)),
    "root": str,
    "truncated": bool,
    "files_scanned": int,
    "skipped": list,
    "summary": dict,
    "calls": list,
    "forwarders": list,
    "unmatched": list,
}
READ_KEYS = {"name", "fields", "properties", "unmapped", "object_argument"}
VALUE_KEYS = {"kind", "text", "choices", "entries", "reason", "expression", "declared"}


def test_list_json_is_one_document_on_stdout_with_every_key(
    listed: Path, offline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--list", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.err.startswith("tripl check --list - ")
    document = json.loads(captured.out)
    assert set(document) == set(LIST_DOCUMENT_KEYS)
    for key, kind in LIST_DOCUMENT_KEYS.items():
        assert isinstance(document[key], kind), key
    # It talks to no instance: the keys are absent, not null.
    assert "instance" not in document and "requests" not in document
    assert document["command"] == "check --list"
    assert (document["project"], document["root"]) == ("demo", str(listed))
    assert document["config"] == str(listed / ".tripl" / "check.yml")
    assert (document["files_scanned"], document["truncated"], document["skipped"]) == (2, False, [])
    summary = document["summary"]
    assert summary == {
        "calls": 4,
        "items": 4,
        "fully_read": 1,
        "not_sent": 2,
        "dynamic": {"name": 2, "properties": 2},
        "reasons": {
            "name": {"parameter": 1, "format": 1},
            "properties": {"parameter": 1, "wrong_shape": 1},
        },
        "targets": {"event": {"name": 3, "properties": 3, "key": 1}},
        "default_label_fields": {"event": ["key"]},
    }
    forwarding, formatted, shaped, sent = document["calls"]
    assert set(forwarding) == {"location", "snippet", "event_type", "read", "dynamic", "items"}
    assert forwarding["location"] == {
        "path": "app/Screen.kt",
        "line": 3,
        "column": 9,
        "end_line": 3,
        "end_column": 42,
    }
    assert (forwarding["snippet"], forwarding["event_type"]) == ("analytics.logEvent", "event")
    assert forwarding["read"]["name"] == {
        "kind": "dynamic",
        "text": None,
        "choices": [],
        "entries": None,
        "reason": "parameter",
        "expression": "event",
        "declared": {"function": "logEvent", "line": 2},
    }
    assert forwarding["dynamic"] == {"name": "parameter", "properties": "parameter"}
    # Nothing about it is known: `tripl check` reports it as dynamic and sends nothing.
    assert forwarding["items"] == [{"name": None, "fields": {}, "properties": None, "sent": False}]
    entries = formatted["read"]["properties"]["entries"]
    assert set(entries) == {"id", "kind"}
    assert (entries["id"]["kind"], entries["id"]["reason"]) == ("dynamic", "variable")
    assert entries["id"]["declared"] is None  # an entry is never refined
    assert (entries["kind"]["kind"], entries["kind"]["text"]) == ("text", "x")
    assert formatted["read"]["name"]["expression"] == "format of promo_%s_shown"
    assert shaped["read"]["unmapped"] == ["#2"]
    assert shaped["dynamic"] == {"properties": "wrong_shape"}
    assert shaped["items"] == [{"name": "fixed", "fields": {}, "properties": None, "sent": True}]
    assert sent["read"]["fields"]["key"]["kind"] == "text"
    assert (sent["dynamic"], sent["items"][0]["fields"]) == ({}, {"key": "opened"})
    for call in document["calls"]:
        assert set(call["read"]) == READ_KEYS
        for value in (call["read"]["name"], call["read"]["properties"]):
            assert value is None or set(value) == VALUE_KEYS
        # No call spec here reads an object argument.
        assert call["read"]["object_argument"] is None
        assert all(code in REASON_CODES for code in call["dynamic"].values())
    [forwarder] = document["forwarders"]
    assert forwarder == {
        "function": "logEvent",
        "location": {"path": "app/Screen.kt", "line": 2},
        "parameters": [
            {"name": "event", "index": 0, "label": "event", "targets": ["name"]},
            {"name": "params", "index": 1, "label": "params", "targets": ["properties"]},
        ],
        "calls": 1,
        # By name, its own body left out: the call it forwards to is no caller.
        "callers": {
            "same_file": 3,
            "same_file_matched": 2,
            "other_files": 0,
            "other_files_matched": 0,
            "first_unmatched": {"path": "app/Screen.kt", "line": 11, "column": 9},
        },
    }
    assert document["unmatched"] == [
        {
            "method": "wrap",
            "receivers": {"": 1},
            "signal": "constant",
            "calls": 1,
            "first": {"path": "app/Screen.kt", "line": 12, "column": 9},
            "example": "wrap(Events.Name.Open)",
        },
        {
            "method": "logEvent",
            "receivers": {"": 1},
            "signal": "name",
            "calls": 1,
            "first": {"path": "app/Screen.kt", "line": 11, "column": 9},
            "example": 'logEvent("home_shown", bundle)',
        },
    ]
    assert all(group["signal"] in SIGNALS for group in document["unmatched"])


def test_list_takes_the_project_from_the_flag_else_the_config_else_null(
    listed: Path, offline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--list", "--json", "--project", "other"]) == 0
    assert json.loads(capsys.readouterr().out)["project"] == "other"
    _write(listed, {".tripl/check.yml": LIST_CONFIG.replace("project: demo\n", "")})
    assert main(["check", "--list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["project"] is None
    assert main(["check", "--list", "--project", "a", "--project", "b"]) == 2
    assert "--project was given 2 times; tripl check reads one project." in capsys.readouterr().err


def test_list_with_no_check_config_lists_the_calls_that_look_like_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(
        tmp_path,
        {
            "web/src/signup.ts": 'analytics.track("Signed Up", { plan: "pro" });\n',
            # Installed or generated, never the app's own: hidden directories are skipped.
            ".venv/lib/site.js": 'analytics.track("Installed");\n',
            "web/.next/server/page.js": 'analytics.track("Built");\n',
        },
    )
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path / "web")
    root = tmp_path.resolve()
    assert main(["check", "--list"]) == 0
    assert capsys.readouterr().out == (
        f"tripl check --list - no check config, root {root} (nothing is sent)\n"
        "\n"
        "0 call sites in 1 file.\n"
        "\n"
        "No check config found: these are the calls that look like tracking under "
        f"{root}. Write .tripl/check.yml: "
        "https://docs.tripl.io/integrate/tripl-check#config-file\n"
        "\n"
        "Calls that look like tracking but match no call spec:\n"
        '  1  track  name  web/src/signup.ts:1:1  analytics.track("Signed Up", { plan: "pro" })\n'
    )
    assert main(["check", "--list", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert (document["config"], document["root"], document["truncated"]) == (None, str(root), False)
    assert [group["method"] for group in document["unmatched"]] == ["track"]


def _checkout(root: Path, files: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Path:
    """``files`` in a git checkout at ``root``, run from there; ``root`` resolved."""
    _write(root, files)
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    return root.resolve()


def test_list_with_no_check_config_stops_at_the_discovery_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(inventory_module, "MAX_DISCOVERY_FILES", 1)
    files = {"web/a.ts": 'analytics.track("A");\n', "web/b.ts": 'analytics.track("B");\n'}
    root = _checkout(tmp_path / "repo", files, monkeypatch)
    assert main(["check", "--list"]) == 0
    assert capsys.readouterr().out == (
        f"tripl check --list - no check config, root {root} (nothing is sent)\n"
        "\n"
        "0 call sites in 1 file.\n"
        "\n"
        "No check config found: these are the calls that look like tracking under "
        f"{root}. Write .tripl/check.yml: "
        "https://docs.tripl.io/integrate/tripl-check#config-file\n"
        "\n"
        "Calls that look like tracking but match no call spec:\n"
        '  1  track  name  web/a.ts:1:1  analytics.track("A")\n'
        "\n"
        "Discovery stopped at 1 source file; write .tripl/check.yml with sources: to scan "
        "the rest.\n"
    )
    assert main(["check", "--list", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert (document["truncated"], document["files_scanned"]) == (True, 1)


def test_list_with_no_check_config_needs_a_git_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if repository_root(tmp_path) is not None:
        pytest.skip("the temporary directory sits inside a git checkout")
    _write(tmp_path, {"a.ts": 'analytics.track("x");\n'})
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--list"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "is not in a git repository: run tripl check --list from a checkout" in captured.err


def test_list_that_matches_nothing_exits_0_and_says_why(
    listed: Path, offline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        listed,
        {
            ".tripl/check.yml": LIST_CONFIG.replace("analytics.logEvent", "Tracker.track").replace(
                "tracker.send", "Tracker.send"
            )
        },
    )
    assert main(["check", "--list"]) == 0
    out = capsys.readouterr().out
    # Constant groups first, then by calls: `send` sorts after `logEvent` on its one call.
    assert out == (
        f"tripl check --list - {listed / '.tripl' / 'check.yml'}, root {listed} "
        "(nothing is sent)\n"
        "\n"
        "0 call sites in 2 files.\n"
        "\n"
        "None of the 2 files scanned has a call the call specs match.\n"
        "\n"
        "Calls that look like tracking but match no call spec:\n"
        "  1  wrap                    constant  app/Screen.kt:12:9  wrap(Events.Name.Open)\n"
        "  4  logEvent (2 receivers)  name      app/Screen.kt:3:9   "
        "analytics.logEvent(event, params)\n"
        '  1  send                    receiver  app/Screen.kt:8:9   tracker.send(key = "opened")\n'
    )
    # A config with no event types at all is listed the same way; `tripl check` refuses one.
    _write(listed, {".tripl/check.yml": 'project: demo\nsources: ["app/**"]\n'})
    assert main(["check", "--list"]) == 0
    assert capsys.readouterr().out == out
    # Sources that match no file at all say so instead.
    _write(listed, {".tripl/check.yml": LIST_CONFIG.replace("app/**", "moved/**")})
    assert main(["check", "--list"]) == 0
    assert capsys.readouterr().out == (
        f"tripl check --list - {listed / '.tripl' / 'check.yml'}, root {listed} "
        "(nothing is sent)\n"
        "\n"
        "0 call sites in 0 files.\n"
        "\n"
        f"No readable source file under {listed} matches `sources:`.\n"
        "\n"
        "Calls that look like tracking but match no call spec: none.\n"
    )


def test_list_shows_every_constant_group_then_the_ten_most_frequent_others(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Eleven constant groups, one more than the other groups shown: all of them
    # are shown. Then twelve others: the two that sort first by name have one
    # call each, the others two, so those two are the ones left out.
    names = [f"track{letter}" for letter in "ABCDEFGHIJKL"]
    body = "".join(f"    {name}(x)\n" * (1 if name in names[:2] else 2) for name in names)
    relays = [f"relay{letter}" for letter in "ABCDEFGHIJK"]
    body += "".join(f"    {relay}(Events.Name.Open)\n" for relay in relays)
    files = {
        ".tripl/check.yml": 'project: demo\nsources: ["app/**"]\n',
        "app/Events.kt": 'object Events { object Name { const val Open = "open" } }\n',
        "app/Feed.kt": f"fun feed() {{\n{body}}}\n",
    }
    root = _checkout(tmp_path / "repo", files, monkeypatch)
    assert main(["check", "--list"]) == 0
    assert capsys.readouterr().out == (
        f"tripl check --list - {root / '.tripl' / 'check.yml'}, root {root} (nothing is sent)\n"
        "\n"
        "0 call sites in 2 files.\n"
        "\n"
        "None of the 2 files scanned has a call the call specs match.\n"
        "\n"
        "Calls that look like tracking but match no call spec:\n"
        "  1  relayA  constant  app/Feed.kt:24:5  relayA(Events.Name.Open)\n"
        "  1  relayB  constant  app/Feed.kt:25:5  relayB(Events.Name.Open)\n"
        "  1  relayC  constant  app/Feed.kt:26:5  relayC(Events.Name.Open)\n"
        "  1  relayD  constant  app/Feed.kt:27:5  relayD(Events.Name.Open)\n"
        "  1  relayE  constant  app/Feed.kt:28:5  relayE(Events.Name.Open)\n"
        "  1  relayF  constant  app/Feed.kt:29:5  relayF(Events.Name.Open)\n"
        "  1  relayG  constant  app/Feed.kt:30:5  relayG(Events.Name.Open)\n"
        "  1  relayH  constant  app/Feed.kt:31:5  relayH(Events.Name.Open)\n"
        "  1  relayI  constant  app/Feed.kt:32:5  relayI(Events.Name.Open)\n"
        "  1  relayJ  constant  app/Feed.kt:33:5  relayJ(Events.Name.Open)\n"
        "  1  relayK  constant  app/Feed.kt:34:5  relayK(Events.Name.Open)\n"
        "  2  trackC  name      app/Feed.kt:4:5   trackC(x)\n"
        "  2  trackD  name      app/Feed.kt:6:5   trackD(x)\n"
        "  2  trackE  name      app/Feed.kt:8:5   trackE(x)\n"
        "  2  trackF  name      app/Feed.kt:10:5  trackF(x)\n"
        "  2  trackG  name      app/Feed.kt:12:5  trackG(x)\n"
        "  2  trackH  name      app/Feed.kt:14:5  trackH(x)\n"
        "  2  trackI  name      app/Feed.kt:16:5  trackI(x)\n"
        "  2  trackJ  name      app/Feed.kt:18:5  trackJ(x)\n"
        "  2  trackK  name      app/Feed.kt:20:5  trackK(x)\n"
        "  2  trackL  name      app/Feed.kt:22:5  trackL(x)\n"
        "  ...and 2 more (--list --json lists all).\n"
    )
    assert main(["check", "--list", "--json"]) == 0
    unmatched = json.loads(capsys.readouterr().out)["unmatched"]
    assert [group["method"] for group in unmatched] == [*relays, *names[2:], *names[:2]]


LIST_KINDS_CONFIG = """
project: demo
sources: ["app/**", "web/**"]
event_types:
  event:
    calls:
      - {function: "analytics.logEvent", positional: [name, properties]}
      - {function: "tracker.pair", positional: [name, screen]}
  view:
    calls: [{function: "tracker.send"}]
  se: {preset: snowplow_structured}
"""

LIST_KINDS_SOURCES = {
    "app/Promo.kt": """
        fun many(): String {
            when (step) {
                1 -> return "a"
                2 -> return "b"
                3 -> return "c"
                4 -> return "d"
                5 -> return "e"
                6 -> return "f"
            }
            return "g"
        }
        fun show(id: String) {
            analytics.logEvent("promo_${id}_shown", bundleOf("id" to null))
            analytics.logEvent(many())
            tracker.pair(label, label)
            tracker.send(screen = "home", step = "2")
        }
    """,
    "web/track.ts": """
        trackStructEvent(props);
    """,
}


def test_list_prints_each_kind_of_value_the_way_its_json_kind_says(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = {".tripl/check.yml": LIST_KINDS_CONFIG, **LIST_KINDS_SOURCES}
    root = _checkout(tmp_path / "repo", files, monkeypatch)
    assert main(["check", "--list"]) == 0
    assert capsys.readouterr().out == (
        f"tripl check --list - {root / '.tripl' / 'check.yml'}, root {root} (nothing is sent)\n"
        "\n"
        "app/Promo.kt:13:5  event  analytics.logEvent  "
        "name=promo_${id}_shown properties={id=null}\n"
        "app/Promo.kt:14:5  event  analytics.logEvent  name=a|b|c|d|e|... (+2)\n"
        "app/Promo.kt:15:5  event  tracker.pair  name=? screen=?\n"
        # Two targets with the same reason share its line.
        "    name, screen: variable label\n"
        "app/Promo.kt:16:5  view  tracker.send  screen=home step=2\n"
        "web/track.ts:1:1  se  trackStructEvent\n"
        "    name: not passed, object argument props is not a literal\n"
        "\n"
        "5 call sites in 2 files: 3 fully read; 2 not sent (no name or field read); "
        "0 sent with a value not read.\n"
        "Values not read: name 2, screen 1.\n"
        "Read per event type: event: name 3, properties 1, screen 1; view: screen 1, step 1.\n"
        "view: fields screen, step are read from the labels screen and step because the call "
        "spec has no args:; map them in args: (null drops one).\n"
        "\n"
        "Why (call sites, per target):\n"
        "  name    variable    1  a variable the scan does not follow (a property, a lambda "
        "parameter, a captured variable), not a constant\n"
        "  name    not_passed  1  the call passes no name and no field value the scan can "
        "read (no argument or object key the spec maps to one, only null ones, or an object "
        "argument that is not a literal)\n"
        "  screen  variable    1  a variable the scan does not follow (a property, a lambda "
        "parameter, a captured variable), not a constant\n"
        "\n"
        "Calls that look like tracking but match no call spec: none.\n"
    )
    assert main(["check", "--list", "--json"]) == 0
    templated, chosen, *_ = json.loads(capsys.readouterr().out)["calls"]
    name = templated["read"]["name"]
    assert (name["kind"], name["text"], name["choices"]) == ("template", "promo_${id}_shown", [])
    entry = templated["read"]["properties"]["entries"]["id"]
    assert (entry["kind"], entry["text"]) == ("absent", None)
    name = chosen["read"]["name"]
    assert (name["kind"], name["text"]) == ("choices", None)
    assert name["choices"] == ["a", "b", "c", "d", "e", "f", "g"]


def test_list_says_why_a_call_passes_nothing_the_scan_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = {
        ".tripl/check.yml": """
            project: demo
            sources: ["app/**", "web/**"]
            event_types:
              event:
                calls: [{function: "analytics.logEvent", positional: [name, properties]}]
              st:
                calls: [{function: "tracker.trackStructured", positional: [category, action]}]
              se: {preset: snowplow_structured}
              ev:
                calls:
                  - {function: "track", positional: [name], object_arg: 1, args: {plan: plan}}
        """,
        "app/A.kt": """
            fun a() {
                analytics.logEvent(null)
                tracker.trackStructured(null, null)
            }
        """,
        "web/t.ts": """
            export function send(evt: StructuredEvent) {
              trackStructEvent(evt);
            }
            export function other() {
              trackStructEvent({ category: "checkout", action: "tap" });
              trackStructEvent(null);
            }
            export function opened(props: Props) {
              track("opened", props);
            }
        """,
    }
    root = _checkout(tmp_path / "repo", files, monkeypatch)
    assert main(["check", "--list"]) == 0
    # Arguments the spec maps but passes as null are nothing passed too.
    assert capsys.readouterr().out == (
        f"tripl check --list - {root / '.tripl' / 'check.yml'}, root {root} (nothing is sent)\n"
        "\n"
        "app/A.kt:2:5  event  analytics.logEvent\n"
        "    name: not passed\n"
        "app/A.kt:3:5  st  tracker.trackStructured\n"
        "    name: not passed\n"
        "web/t.ts:2:3  se  trackStructEvent\n"
        "    name: not passed, object argument evt is not a literal\n"
        "web/t.ts:5:3  se  trackStructEvent  category=checkout action=tap\n"
        "web/t.ts:6:3  se  trackStructEvent\n"
        "    name: not passed\n"
        "web/t.ts:9:3  ev  track  name=opened\n"
        "\n"
        "6 call sites in 2 files: 2 fully read; 4 not sent (no name or field read); "
        "0 sent with a value not read.\n"
        "Values not read: name 4.\n"
        "Read per event type: se: category 1, action 1; ev: name 1.\n"
        "\n"
        "Why (call sites, per target):\n"
        "  name  not_passed  4  the call passes no name and no field value the scan can read "
        "(no argument or object key the spec maps to one, only null ones, or an object "
        "argument that is not a literal)\n"
        "\n"
        "Calls that look like tracking but match no call spec: none.\n"
    )
    assert main(["check", "--list", "--json"]) == 0
    calls = json.loads(capsys.readouterr().out)["calls"]
    assert [call["dynamic"] for call in calls] == [
        {"name": "not_passed"},
        {"name": "not_passed"},
        {"name": "not_passed"},
        {},
        {"name": "not_passed"},
        {},
    ]
    # The document names the object argument the text does: one the scan could
    # not read. A literal one, or null, is no such argument. It names one on a
    # call whose name is read too, where the text says nothing of it.
    unread = {
        "kind": "dynamic",
        "text": None,
        "choices": [],
        "entries": None,
        "reason": "variable",
        "declared": None,
    }
    assert [call["read"]["object_argument"] for call in calls] == [
        None,
        None,
        {**unread, "expression": "evt"},
        None,
        None,
        {**unread, "expression": "props"},
    ]


def test_list_names_the_function_declarations_the_scan_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = {
        ".tripl/check.yml": """
            project: demo
            event_types:
              event:
                calls: [{function: "analytics.track", positional: [name]}]
        """,
        "app/Names.kt": """
            fun blockName(): String { return "block_name" }
            fun bodyName() = "body_name"
            fun show() {
                analytics.track(blockName())
                analytics.track(bodyName())
            }
        """,
        "ios/Names.swift": """
            func swiftName() -> String { return "swift_name" }
            func show() { analytics.track(swiftName()) }
        """,
        "src/Names.java": """
            class Names {
                String javaName() { return "java_name"; }
                void show() { analytics.track(javaName()); }
            }
        """,
        "web/names.ts": """
            function typedName(): string { return "typed_name"; }
            function untypedName() { return "untyped_name"; }
            const arrowName = (): string => "arrow_name";
            analytics.track(typedName());
            analytics.track(untypedName());
            analytics.track(arrowName());
        """,
    }
    root = _checkout(tmp_path / "repo", files, monkeypatch)
    assert main(["check", "--list"]) == 0
    # Each declaration the reason names is read, and a TypeScript function
    # with no return type, or an arrow, is not.
    assert capsys.readouterr().out == (
        f"tripl check --list - {root / '.tripl' / 'check.yml'}, root {root} (nothing is sent)\n"
        "\n"
        "app/Names.kt:4:5  event  analytics.track  name=block_name\n"
        "app/Names.kt:5:5  event  analytics.track  name=body_name\n"
        "ios/Names.swift:2:15  event  analytics.track  name=swift_name\n"
        "src/Names.java:3:19  event  analytics.track  name=java_name\n"
        "web/names.ts:4:1  event  analytics.track  name=typed_name\n"
        "web/names.ts:5:1  event  analytics.track  name=?\n"
        "    name: function untypedName()\n"
        "web/names.ts:6:1  event  analytics.track  name=?\n"
        "    name: function arrowName()\n"
        "\n"
        "7 call sites in 4 files: 5 fully read; 2 not sent (no name or field read); "
        "0 sent with a value not read.\n"
        "Values not read: name 2.\n"
        "Read per event type: event: name 7.\n"
        "\n"
        "Why (call sites, per target):\n"
        "  name  function  2  a function call; the scan reads one whose every return is a "
        'string literal, declared like fun f(): String, fun f() = "...", func f() -> String, '
        "String f() or function f(): string, in a file under sources: or enums:\n"
        "\n"
        "Calls that look like tracking but match no call spec: none.\n"
    )


LIST_FORWARDERS_CONFIG = """
project: demo
sources: ["app/**"]
event_types:
  event:
    calls:
      - {function: "analytics.logEvent", positional: [name]}
      - {function: "report", positional: [name]}
"""

LIST_FORWARDERS_SOURCES = {
    "app/Wrappers.kt": """
        fun lonely(event: String) {
            analytics.logEvent(event)
        }
        fun relay(event: String) {
            analytics.logEvent(event)
        }
        fun report(event: String) {
            analytics.logEvent(event)
        }
        fun show() {
            report("home_shown")
        }
    """,
    "app/Screen.kt": """
        fun open() {
            relay("open")
        }
    """,
}


def test_list_says_where_a_forwarder_s_calls_are_and_which_files_it_did_not_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = {".tripl/check.yml": LIST_FORWARDERS_CONFIG, **LIST_FORWARDERS_SOURCES}
    root = _checkout(tmp_path / "repo", files, monkeypatch)
    (root / "app" / "Bundle.kt").write_text("// " + "x" * 2_000_001, encoding="utf-8")
    assert main(["check", "--list"]) == 0
    assert capsys.readouterr().out == (
        f"tripl check --list - {root / '.tripl' / 'check.yml'}, root {root} (nothing is sent)\n"
        "\n"
        "app/Wrappers.kt:2:5  event  analytics.logEvent  name=?\n"
        "    name: parameter event of lonely (line 1)\n"
        "app/Wrappers.kt:5:5  event  analytics.logEvent  name=?\n"
        "    name: parameter event of relay (line 4)\n"
        "app/Wrappers.kt:8:5  event  analytics.logEvent  name=?\n"
        "    name: parameter event of report (line 7)\n"
        "app/Wrappers.kt:11:5  event  report  name=home_shown\n"
        "\n"
        "4 call sites in 2 files: 1 fully read; 3 not sent (no name or field read); "
        "0 sent with a value not read.\n"
        "Values not read: name 3.\n"
        "Read per event type: event: name 4.\n"
        "\n"
        "Why (call sites, per target):\n"
        "  name  parameter  3  a parameter of the function the call sits in: the value comes "
        "from its callers; add a call spec for that function\n"
        "\n"
        "Functions that forward their own parameter into a tracking call (add a call spec "
        "for each, so their calls are read):\n"
        "  relay   app/Wrappers.kt:4  forwards event (#0) to name  "
        "1 call named relay elsewhere, none matched (first app/Screen.kt:2)\n"
        "  lonely  app/Wrappers.kt:1  forwards event (#0) to name  no calls found\n"
        "  report  app/Wrappers.kt:7  forwards event (#0) to name  its calls are matched\n"
        "\n"
        "Calls that look like tracking but match no call spec: none.\n"
        "\n"
        "Not read: 1 configured file: app/Bundle.kt (too large).\n"
    )


def test_list_names_a_field_two_labels_reach_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = {
        ".tripl/check.yml": """
            project: demo
            sources: ["app/**"]
            event_types:
              event:
                field_map: {screen: screen_name, screenName: screen_name}
                calls: [{function: "tracker.send"}]
        """,
        "app/A.kt": """
            fun a() { tracker.send(screen = "home") }
            fun b() { tracker.send(screenName = "cart") }
        """,
    }
    root = _checkout(tmp_path / "repo", files, monkeypatch)
    assert main(["check", "--list"]) == 0
    # One field, two labels: what to map in args: is the labels.
    assert capsys.readouterr().out == (
        f"tripl check --list - {root / '.tripl' / 'check.yml'}, root {root} (nothing is sent)\n"
        "\n"
        "app/A.kt:1:11  event  tracker.send  screen_name=home\n"
        "app/A.kt:2:11  event  tracker.send  screen_name=cart\n"
        "\n"
        "2 call sites in 1 file: 2 fully read; 0 not sent (no name or field read); "
        "0 sent with a value not read.\n"
        "Read per event type: event: screen_name 2.\n"
        "event: field screen_name is read from the labels screen and screenName because the "
        "call spec has no args:; map them in args: (null drops one).\n"
        "\n"
        "Calls that look like tracking but match no call spec: none.\n"
    )
    assert main(["check", "--list", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)["summary"]
    assert summary["default_label_fields"] == {"event": ["screen_name"]}


def test_list_json_items_are_within_the_validator_s_limits(
    listed: Path, offline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    long = "x" * (plan_validation.NAME_MAX_LENGTH + 1)
    _write(listed, {"app/Long.kt": f'fun long() {{ analytics.logEvent("{long}") }}\n'})
    assert main(["check", "--list", "--json"]) == 0
    calls = json.loads(capsys.readouterr().out)["calls"]
    [call] = [call for call in calls if call["location"]["path"] == "app/Long.kt"]
    assert call["read"]["name"]["text"] == long
    # What `tripl check` sends: the name as null, and the call still sent.
    assert call["items"] == [{"name": None, "fields": {}, "properties": None, "sent": True}]


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        ([], "--report writes a check's verdicts, and --list checks nothing"),
        (["--branch", "redesign"], "--branch names the plan branch to check against"),
        (["--strict"], "--strict changes what fails a check, and --list checks nothing"),
        (["--allow-empty"], "--list never fails because nothing matched; drop --allow-empty."),
        (["--format", "sarif"], "--list prints text or JSON, not sarif; drop --format sarif."),
        (["--format", "codequality"], "--list prints text or JSON, not codequality"),
        (["--format", "junit"], "--list prints text or JSON, not junit"),
    ],
)
def test_list_refuses_a_flag_it_has_no_use_for_and_removes_old_reports(
    listed: Path,
    offline: None,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    message: str,
) -> None:
    stale = listed / "gl-code-quality-report.json"
    stale.write_text("[]", encoding="utf-8")
    assert main(["check", "--list", *flags, "--report", f"codequality={stale}"]) == 2
    captured = capsys.readouterr()
    assert message in captured.err
    assert captured.out == ""
    # Refused inside the run like any other failed one: no stale report survives.
    assert not stale.exists()


def test_list_and_payloads_are_one_or_the_other(listed: Path, offline: None) -> None:
    kept = listed / "gl-code-quality-report.json"
    kept.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        main(["check", "--list", "--payloads", "x.ndjson", "--report", f"codequality={kept}"])
    assert excinfo.value.code == 2
    # The parser refused it before any command ran.
    assert kept.read_text(encoding="utf-8") == "[]"


class _ClosedPipe(io.StringIO):
    """A reader that went away: every write raises BrokenPipeError."""

    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_a_reader_that_closes_the_pipe_ends_the_listing_not_the_run(
    listed: Path,
    offline: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
) -> None:
    monkeypatch.setattr(sys, "stdout", _ClosedPipe())
    assert main(["check", "--list", *flags]) == 0
    err = capsys.readouterr().err
    if flags:  # the human lines went to stderr; only the document met the closed pipe
        assert err.startswith("tripl check --list - ")
        assert "Traceback" not in err and "Broken pipe" not in err
    else:
        assert err == ""


def test_a_reader_that_left_stderr_still_gets_the_json_document(
    listed: Path,
    offline: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `tripl check --list --json 2>&1 >list.json | head -1`: the human lines' reader goes first.
    monkeypatch.setattr(sys, "stderr", _ClosedPipe())
    assert main(["check", "--list", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["command"] == "check --list"
    assert document["summary"]["calls"] > 0


class _FullDisk(io.StringIO):
    """A stream on a full disk: every write raises ENOSPC, which no reader leaving explains."""

    def write(self, text: str) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")


@pytest.mark.parametrize("stderr", [None, _FullDisk], ids=["closed", "full"])
def test_a_stderr_that_cannot_be_written_still_gets_the_json_document_through(
    listed: Path,
    offline: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stderr: type[_FullDisk] | None,
) -> None:
    # `2>&-` leaves no stderr at all (None); `2>/dev/full` one that fails on every write.
    monkeypatch.setattr(sys, "stderr", None if stderr is None else stderr())
    assert main(["check", "--list", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["command"] == "check --list"
    assert document["summary"]["calls"] > 0


def test_a_closed_stdout_is_no_failure_but_one_that_cannot_be_written_is(
    listed: Path, offline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `tripl check --list >&-`: there is nowhere to write, so nothing failed.
    monkeypatch.setattr(sys, "stdout", None)
    assert main(["check", "--list"]) == 0
    # On the output itself only a reader that went away ends it quietly.
    monkeypatch.setattr(sys, "stdout", _FullDisk())
    with pytest.raises(OSError, match="No space left on device"):
        main(["check", "--list"])


@pytest.mark.skipif(sys.platform == "win32", reason="a closed POSIX pipe raises EPIPE")
def test_a_closed_pipe_is_pointed_at_the_null_device_so_the_last_flush_cannot_fail(
    listed: Path, offline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader, writer = os.pipe()
    os.close(reader)  # `| head` has read its lines and gone
    stream = open(writer, "w", encoding="utf-8")  # noqa: SIM115 - closed below
    monkeypatch.setattr(sys, "stdout", stream)
    assert main(["check", "--list"]) == 0
    # What the interpreter does at exit: with the pipe still there it would
    # raise again, and the process would exit 120.
    stream.flush()
    stream.close()


# --- --print-preset ------------------------------------------------------------------
def _printed(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    """What ``argv`` prints, through the parser's own exit, as ``--version`` does."""
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 0
    return capsys.readouterr().out


def test_print_preset_writes_yaml_that_reads_back_as_the_preset(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _printed(["check", "--print-preset", "segment_track"], capsys)
    assert out.startswith(
        "# preset segment_track: paste these under an event type's calls: in place of "
        "preset: segment_track, then edit them.\n"
    )
    specs = yamlish.loads(out)
    assert specs == list(PRESETS["segment_track"])
    for number, spec in enumerate(specs):
        parse_call(spec, f"printed segment_track[{number}]")


def test_print_preset_names_the_codegen_style_the_preset_gave(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Codegen takes the style of the event type's first preset when the codegen:
    # block sets none, and the pasted specs do not carry it.
    out = _printed(["check", "--print-preset", "snowplow_screen_view"], capsys)
    assert out.startswith(
        "# preset snowplow_screen_view: paste these under an event type's calls: in place of "
        "preset: snowplow_screen_view, then edit them.\n"
        "# If this is the event type's first preset and its codegen: block has no style:, "
        "set style: screen_view in it; codegen took that style from this preset.\n"
        "- pattern: "
    )
    # A preset that gives codegen no style says nothing about one.
    monkeypatch.delitem(PRESET_STYLES, "segment_track")
    out = _printed(["check", "--print-preset", "segment_track"], capsys)
    assert out.startswith(
        "# preset segment_track: paste these under an event type's calls: in place of "
        "preset: segment_track, then edit them.\n- pattern: "
    )


def test_print_preset_without_a_name_prints_every_preset_in_order(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _printed(["check", "--print-preset"], capsys)
    # Comments and blank lines drop out: one list, every preset's specs in turn.
    assert yamlish.loads(out) == [spec for specs in PRESETS.values() for spec in specs]
    assert [line for line in out.splitlines() if line.startswith("# preset ")] == [
        f"# preset {name}: paste these under an event type's calls: in place of "
        f"preset: {name}, then edit them."
        for name in PRESETS
    ]
    assert out.isascii()


def test_print_preset_acts_where_the_parser_meets_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offline: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    alone = _printed(["check", "--print-preset", "snowplow_screen_view"], capsys)
    # Everything after it is never read; it prints YAML even with --json.
    assert _printed(["check", "--list", "--print-preset", "snowplow_screen_view"], capsys) == alone
    assert _printed(["check", "--print-preset", "snowplow_screen_view", "--list"], capsys) == alone
    assert _printed(["check", "--json", "--print-preset", "snowplow_screen_view"], capsys) == alone
    # It runs no command, so like --help it leaves a report alone.
    kept = tmp_path / "gl-code-quality-report.json"
    kept.write_text("[]", encoding="utf-8")
    argv = ["check", "--report", f"codequality={kept}", "--print-preset", "snowplow_structured"]
    _printed(argv, capsys)
    assert kept.read_text(encoding="utf-8") == "[]"
    # A bad value before it is still the parser's refusal.
    with pytest.raises(SystemExit) as excinfo:
        main(["check", "--timeout", "abc", "--print-preset"])
    assert excinfo.value.code == 2


@pytest.mark.skipif(sys.platform == "win32", reason="a closed POSIX pipe raises EPIPE")
def test_print_preset_to_a_reader_that_left_still_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reader, writer = os.pipe()
    os.close(reader)  # `tripl check --print-preset | true`: gone before reading a line
    stream = open(writer, "w", encoding="utf-8")  # noqa: SIM115 - closed below
    monkeypatch.setattr(sys, "stdout", stream)
    with pytest.raises(SystemExit) as excinfo:
        main(["check", "--print-preset"])
    assert excinfo.value.code == 0
    # What the interpreter does at exit: with the pipe still there it would
    # raise, and the process would exit 120.
    stream.flush()
    stream.close()
    assert capsys.readouterr().err == ""


def test_an_unknown_preset_is_the_parser_s_refusal_naming_the_presets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["check", "--print-preset", "mixpanel_track"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice: 'mixpanel_track'" in err
    assert all(name in err for name in PRESETS)


# --- inline suppression ------------------------------------------------------------
# A legacy event the synthetic plan does not have: one `unknown_event` error.
DEBUG_CALL = 'Analytics.shared.logEvent("debug_tap")'
WIRE_KEYS = {"ref", "event_type", "name", "fields", "properties", "complete"}


def _debug(marker: str) -> dict[str, str]:
    return {"Sources/Debug.swift": f"func d() {{\n    {marker}\n    {DEBUG_CALL}\n}}\n"}


def test_a_suppressed_finding_passes_the_run_and_is_counted(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, _debug("// tripl-check-ignore[unknown_event] -- debug menu only (APP-12)"))
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "Sources/Debug.swift:3" not in out
    assert out.rstrip().endswith(
        "3 call sites in 2 files: 2 ok, 0 warnings, 1 error; 1 finding suppressed."
    )
    # Suppression is the CLI's own: the call is still sent, with nothing extra.
    sent = json.loads(validator.calls.last.request.content)["items"]
    assert {item["name"] for item in sent} >= {"debug_tap"}
    assert all(set(item) == WIRE_KEYS for item in sent)


def test_a_marker_without_a_reason_suppresses_nothing_and_says_so(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, _debug("// tripl-check-ignore[unknown_event]"))
    assert main(["check"]) == 1
    out = capsys.readouterr().out
    assert "Sources/Debug.swift:3:5  error  legacy  debug_tap" in out
    assert (
        'Suppression ignored: Sources/Debug.swift:2 tripl-check-ignore needs "-- <reason>".'
    ) in out


def test_an_unused_marker_is_listed_and_never_fails(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        repo,
        {
            "Sources/Old.swift": "func o() {\n"
            "    // tripl-check-ignore[unknown_event] -- the call below was fixed\n"
            '    Analytics.shared.logEvent("trial_started")\n'
            "}\n"
        },
    )
    for argv in (["check"], ["check", "--strict"]):
        assert main(argv) == 0
        out = capsys.readouterr().out
        assert (
            "Unused suppression: Sources/Old.swift:2 tripl-check-ignore[unknown_event] "
            "matched no finding."
        ) in out


def test_every_format_shows_the_suppression_from_one_run(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        repo,
        {
            **_debug("// tripl-check-ignore[unknown_event] -- debug menu only"),
            "Sources/Typo.swift": "// tripl-check-ignore[unknown_event] -- \n"
            'func t() { Analytics.shared.logEvent("typo_tap") }\n',
        },
    )
    out = repo / "reports"
    argv = [
        "check",
        "--json",
        "--report",
        f"codequality={out / 'cq.json'}",
        "--report",
        f"junit={out / 'junit.xml'}",
        "--report",
        f"sarif={out / 'check.sarif'}",
    ]
    assert main(argv) == 1  # Typo.swift's marker has no reason, so its error stands
    document = json.loads(capsys.readouterr().out)
    assert document["exit_code"] == 1
    assert document["summary"]["suppressed"] == 1
    assert document["summary"]["failing"] == 1
    by_path = {row["location"]["path"]: row for row in document["results"]}
    debug, typo = by_path["Sources/Debug.swift"], by_path["Sources/Typo.swift"]
    assert (debug["status"], debug["fails"]) == ("error", False)
    [finding] = debug["findings"]
    assert finding["suppression"] == {
        "reason": "debug menu only",
        "location": {"path": "Sources/Debug.swift", "line": 2, "column": 8},
    }
    assert (typo["fails"], typo["findings"][0]["suppression"]) == (True, None)
    assert [(s["location"]["path"], s["used"], s["problem"]) for s in document["suppressions"]] == [
        ("Sources/Debug.swift", True, None),
        ("Sources/Typo.swift", False, "no_reason"),
    ]

    issues = json.loads((out / "cq.json").read_text(encoding="utf-8"))
    [waived] = [issue for issue in issues if issue["fingerprint"] == finding["fingerprint"]]
    assert waived["severity"] == "info"
    assert waived["description"].startswith("Suppressed: debug menu only — ")
    [invalid] = [issue for issue in issues if issue["check_name"] == "tripl/invalid_suppression"]
    assert invalid["location"] == {"path": "Sources/Typo.swift", "lines": {"begin": 1}}

    suite = ET.fromstring((out / "junit.xml").read_text(encoding="utf-8")).find("testsuite")
    assert suite is not None
    assert suite.get("failures") == "1"
    [debug_case] = [case for case in suite if case.get("file") == "Sources/Debug.swift"]
    assert debug_case.find("failure") is None
    assert "[suppressed: debug menu only]" in (debug_case.findtext("system-out") or "")

    [run] = json.loads((out / "check.sarif").read_text(encoding="utf-8"))["runs"]
    [suppressed] = [result for result in run["results"] if "suppressions" in result]
    assert suppressed["suppressions"][0]["justification"] == "debug menu only"
    assert {result["ruleId"] for result in run["results"]} == {
        "unknown_event",
        "invalid_suppression",
    }


def test_payload_mode_has_no_markers_and_no_suppression_keys(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    capture = repo / "capture.ndjson"
    capture.write_text('{"event_type": "legacy", "name": "debug_tap"}\n', encoding="utf-8")
    assert main(["check", "--payloads", str(capture), "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert "suppressions" not in document
    assert "suppressed" not in document["summary"]


def test_a_codequality_marker_issue_is_relative_to_the_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "mono"
    app = root / "apps" / "ios"
    marked = "// tripl-check-ignore[unknown_event]\n" + BAD_CALL  # no reason: invalid
    _write(app, {".tripl/check.yml": CONFIG, "Sources/Bad.swift": marked})
    (root / ".git").mkdir()
    monkeypatch.chdir(app)
    assert main(["check", "--json", "--report", "codequality=cq.json"]) == 1
    [entry] = json.loads(capsys.readouterr().out)["suppressions"]
    assert entry["location"]["path"] == "Sources/Bad.swift"  # --json keeps the check root's
    issues = json.loads((app / "cq.json").read_text(encoding="utf-8"))
    [invalid] = [issue for issue in issues if issue["check_name"] == "tripl/invalid_suppression"]
    assert invalid["location"] == {"path": "apps/ios/Sources/Bad.swift", "lines": {"begin": 1}}
