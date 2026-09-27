"""``tripl check`` end to end: config, scan or payloads, POST /plan/validate, output, exit code.

The validator behind the route is a small SYNTHETIC one defined here — enough
of the contract to produce each finding code — because what is under test is
the CLI's plumbing: what it sends, how it batches, how verdicts land back on
call sites, and what it prints and exits with. The backend's own rules are
tested in the backend.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest

from tripl_cli.api import plan_validation
from tripl_cli.check.model import CheckItem, Origin
from tripl_cli.check.payloads import enforce_limits, parse
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
    assert warned["findings"] == [
        {
            "code": "dynamic_value",
            "severity": "warning",
            "field": "label",
            "message": "the value of 'label' is not known until runtime",
        }
    ]
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
        assert result["partialFingerprints"]["triplCheck/v1"]


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
