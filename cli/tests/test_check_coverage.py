"""``tripl check --coverage``: the planned events that no scanned call sends."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import API_BASE, FakeInstance, make_branch
from tests.test_check_cmd import CONFIG, VALIDATE_URL, _respond, _write
from tests.test_check_list_doc_samples import _fenced_block
from tests.test_contract import DOCS_PATH
from tripl_cli.check import coverage
from tripl_cli.check.model import CheckItem, CheckResult, Origin
from tripl_cli.cli import main

EVENTS_URL = f"{API_BASE}/projects/demo/events"
TYPES_URL = f"{API_BASE}/projects/demo/event-types"

TYPES = [
    {"id": "t-se", "name": "se"},
    {"id": "t-legacy", "name": "legacy"},
    {"id": "t-screen", "name": "screen"},
]

# The plan `_respond` validates against (test_check_cmd.PLAN), as GET /events rows.
EVENTS = [
    {"id": "ev-1", "event_type_id": "t-se", "name": "home:open:card", "status": "live"},
    {"id": "ev-2", "event_type_id": "t-se", "name": "profile:open:*", "status": "live"},
    {
        "id": "ev-3",
        "event_type_id": "t-se",
        "name": "checkout:submit:card",
        "status": "ready_for_dev",
    },
    {
        "id": "ev-10",
        "event_type_id": "t-legacy",
        "name": "promo_sheet_${id}_shown",
        "status": "live",
    },
    {
        "id": "ev-11",
        "event_type_id": "t-legacy",
        "name": "trial_started",
        "status": "implemented",
    },
    {"id": "ev-20", "event_type_id": "t-screen", "name": "home", "status": "live"},
    {"id": "ev-21", "event_type_id": "t-screen", "name": "cart", "status": "live"},
]


@pytest.fixture
def plan(tripl_api: FakeInstance) -> dict[str, Any]:
    """The validator plus the two reads `--coverage` adds."""
    return {
        "validate": tripl_api.handler(VALIDATE_URL, _respond, method="POST"),
        "types": tripl_api.handler(TYPES_URL, lambda request: httpx.Response(200, json=TYPES)),
        "events": tripl_api.handler(
            EVENTS_URL,
            lambda request: httpx.Response(200, json={"items": EVENTS, "total": len(EVENTS)}),
        ),
    }


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two calls: `home:open:card` (ev-1) and the promo template (ev-10)."""
    root = tmp_path / "repo"
    _write(
        root,
        {
            ".tripl/check.yml": CONFIG,
            "Sources/Feed.swift": """
                func show(sheetId: String) {
                    Analytics.shared.log(category: "home", action: "open", label: "card")
                    Analytics.shared.logEvent("promo_sheet_\\(sheetId)_shown")
                }
            """,
        },
    )
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    return root


def _names(events: list[dict[str, Any]]) -> list[str]:
    return [event["name"] for event in events]


def test_coverage_lists_the_planned_events_no_call_sends(
    repo: Path, plan: dict[str, Any], configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--coverage"]) == 0  # a report: it never fails the run
    out = capsys.readouterr().out
    assert out.rstrip().endswith(
        "2 call sites in 1 file: 2 ok, 0 warnings, 0 errors.\n"
        "\n"
        "Plan coverage (ready_for_dev, implemented, live): 5 planned events of the scanned "
        "event types, 2 found in code, 3 not found, 0 possibly sent by a call the scan did "
        "not read.\n"
        "\n"
        "Not found in the scanned files:\n"
        "  se      profile:open:*        live\n"
        "  se      checkout:submit:card  ready_for_dev\n"
        "  legacy  trial_started         implemented\n"
        "\n"
        "Not scanned: 2 planned events of event types the check config has no calls for: "
        "screen (2)."
    )
    request = plan["events"].calls.last.request
    assert request.url.params.get_list("status") == ["ready_for_dev", "implemented", "live"]
    assert "branch" not in request.url.params


def test_the_json_document_carries_coverage_only_when_asked(
    repo: Path, plan: dict[str, Any], configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--json"]) == 0
    assert "coverage" not in json.loads(capsys.readouterr().out)
    assert not plan["events"].called  # no flag, no extra request

    assert main(["check", "--coverage", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    found = document["coverage"]
    assert found["statuses"] == ["ready_for_dev", "implemented", "live"]
    assert (found["planned"], found["found"], found["unread_calls"]) == (5, 2, 0)
    assert found["not_found"][0] == {
        "event_id": "ev-2",
        "name": "profile:open:*",
        "event_type": "se",
        "status": "live",
    }
    assert _names(found["not_found"]) == [
        "profile:open:*",
        "checkout:submit:card",
        "trial_started",
    ]
    assert found["maybe_dynamic"] == []
    assert found["not_scanned"] == {"screen": 2}
    assert document["exit_code"] == 0


def test_a_call_the_scan_could_not_read_keeps_its_type_out_of_not_found(
    repo: Path, plan: dict[str, Any], configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """`logEvent(name)` may be `trial_started`: saying "not found" would be a guess."""
    _write(
        repo, {"Sources/Any.swift": "func send(name: String) { Analytics.shared.logEvent(name) }\n"}
    )
    assert main(["check", "--coverage", "--json"]) == 0
    captured = capsys.readouterr()
    found = json.loads(captured.out)["coverage"]
    assert _names(found["maybe_dynamic"]) == ["trial_started"]
    # The other event type has no such call, so its events are still plainly absent.
    assert _names(found["not_found"]) == ["profile:open:*", "checkout:submit:card"]
    assert found["unread_calls"] == 1
    assert (
        "Possibly sent: 1 call matches no event and could not be fully read "
        "(`tripl check --list` shows why):\n  legacy  trial_started  implemented"
    ) in captured.err


def test_coverage_reads_the_plan_branch_the_check_ran_against(
    repo: Path,
    plan: dict[str, Any],
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.branches("demo", [make_branch()])
    assert main(["check", "--coverage", "--branch", "checkout-redesign", "--json"]) == 0
    capsys.readouterr()
    assert plan["events"].calls.last.request.url.params["branch"] == "b-9f21"
    assert plan["types"].calls.last.request.url.params["branch"] == "b-9f21"


def test_every_page_of_the_plan_is_read(
    repo: Path,
    plan: dict[str, Any],
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A page that ends before `total` is not the plan: stopping there would
    report the events of the next page as found nowhere and listed nowhere."""

    def paged(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        return httpx.Response(200, json={"items": EVENTS[offset : offset + 3], "total": 7})

    events = tripl_api.handler(EVENTS_URL, paged)
    assert main(["check", "--coverage", "--json"]) == 0
    found = json.loads(capsys.readouterr().out)["coverage"]
    assert [call.request.url.params["offset"] for call in events.calls] == ["0", "3", "6"]
    assert (found["planned"], found["not_scanned"]) == (5, {"screen": 2})


@pytest.mark.parametrize(
    ("flags", "reason"),
    [
        (["--payloads", "events.ndjson"], "a capture holds only the events one session"),
        (["--list"], "--list sends nothing"),
    ],
)
def test_coverage_is_refused_where_there_is_no_scan_to_compare_with_the_plan(
    repo: Path,
    plan: dict[str, Any],
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    reason: str,
) -> None:
    assert main(["check", "--coverage", *flags]) == 2
    assert reason in capsys.readouterr().err
    assert not plan["validate"].called


# --- the grouping rule, on results built by hand -------------------------------------
def _result(
    event_type: str,
    name: str | None,
    *,
    event_id: str | None = None,
    fields: dict[str, str | None] | None = None,
    dynamic: tuple[str, ...] = (),
) -> CheckResult:
    item = CheckItem(
        origin=Origin("app/A.kt", 1, 1),
        event_type=event_type,
        name=name,
        fields=fields or {},
        properties=None,
        complete=False,
        dynamic=dynamic,
    )
    return CheckResult(item, "ok" if event_id else "error", event_id=event_id)


def _row(event_id: str, name: str, **extra: Any) -> dict[str, Any]:
    return {"id": event_id, "event_type_id": "t", "name": name, "status": "live", **extra}


def _build(rows: list[dict[str, Any]], results: list[CheckResult]) -> coverage.Coverage:
    return coverage.build(rows, {"t": "event"}, ["event"], results)


def test_a_templated_name_covers_only_the_events_it_could_spell() -> None:
    rows = [_row("e1", "promo_summer_shown"), _row("e2", "checkout_started")]
    found = _build(rows, [_result("event", "promo_${id}_shown")])
    assert [event.name for event in found.maybe_dynamic] == ["promo_summer_shown"]
    assert [event.name for event in found.not_found] == ["checkout_started"]


def test_a_templated_name_is_tried_against_the_source_name_too() -> None:
    """The server matches a renamed event by the name it had when scanned."""
    rows = [_row("e1", "Promo shown", source_name="promo_summer_shown")]
    found = _build(rows, [_result("event", "promo_${id}_shown")])
    assert [event.name for event in found.maybe_dynamic] == ["Promo shown"]


@pytest.mark.parametrize(
    "unread",
    [
        _result("event", None, dynamic=("name",)),
        _result("event", None, fields={"action": "open_${tab}"}),
    ],
    ids=["a target not read", "a hole in a field"],
)
def test_an_unread_target_or_field_covers_every_event_of_its_type(unread: CheckResult) -> None:
    """An identity can be built from any field, so no event of the type is ruled out."""
    found = _build([_row("e1", "home:open"), _row("e2", "cart:open")], [unread])
    assert (len(found.maybe_dynamic), len(found.not_found), found.unread_calls) == (2, 0, 1)


def test_a_fully_read_call_that_matches_no_event_covers_nothing() -> None:
    """`unknown_event` on a literal name is a typo or an unplanned event, not a maybe."""
    found = _build([_row("e1", "checkout_started")], [_result("event", "chekout_started")])
    assert [event.name for event in found.not_found] == ["checkout_started"]
    assert found.unread_calls == 0


def test_a_matched_call_is_found_even_with_a_value_not_read() -> None:
    matched = _result("event", "checkout_started", event_id="e1", dynamic=("step",))
    found = _build([_row("e1", "checkout_started"), _row("e2", "cart_viewed")], [matched])
    assert (found.found, [event.name for event in found.not_found]) == (1, ["cart_viewed"])
    assert found.maybe_dynamic == ()


def test_the_coverage_sample_in_the_cli_reference_is_real_output(
    repo: Path, plan: dict[str, Any], configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    documented = _fenced_block(DOCS_PATH, "### Plan coverage {#tripl-check-coverage}", "text")
    _write(
        repo, {"Sources/Any.swift": "func send(name: String) { Analytics.shared.logEvent(name) }\n"}
    )

    assert main(["check", "--coverage"]) == 0
    printed = capsys.readouterr().out

    assert printed.endswith(documented)
