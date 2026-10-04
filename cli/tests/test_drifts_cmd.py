"""``tripl drifts`` end to end.

The headline test here is ``test_a_404_from_one_drifts_read_is_reported_and_exits_one``.
There is no project-level drift route, so a project's drifts are a fan-out over
its event types - and a single 404 inside that fan-out rendered as a shorter list
at exit 0 is exactly the misreading that invalidated the 2026-07-30 audit.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from tripl_cli.cli import main

from .conftest import FakeInstance, make_drift, make_event_type, make_project


def _document(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    return payload


def _posts(tripl_api: FakeInstance) -> list[httpx.Request]:
    return [call.request for call in tripl_api.router.calls if call.request.method == "POST"]


class _Stdin:
    def __init__(self, answer: str, *, isatty: bool) -> None:
        self._answer = answer
        self._isatty = isatty

    def isatty(self) -> bool:
        return self._isatty

    def readline(self) -> str:
        return self._answer


# --- drifts list ----------------------------------------------------------


def test_drifts_list_fans_out_over_event_types_and_defaults_to_untriaged(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    now: datetime,
) -> None:
    tripl_api.event_types(
        "prod", [make_event_type("et-1"), make_event_type("et-2", "app.purchase")]
    )
    tripl_api.drifts(
        "prod",
        "et-1",
        [
            make_drift(drift_id="drift-1", status="open"),
            make_drift(drift_id="drift-2", status="accepted"),
        ],
    )
    tripl_api.drifts(
        "prod",
        "et-2",
        [make_drift(drift_id="drift-3", status="snoozed", snoozed_until=now - timedelta(days=1))],
    )
    assert main(["drifts", "list", "--json"]) == 0
    document = _document(capsys)
    assert document["command"] == "drifts list"
    assert document["status_filter"] == "untriaged"
    ids = [row["id"] for row in document["projects"][0]["drifts"]]
    # The accepted one is filtered out; the lapsed snooze is untriaged again.
    assert ids == ["drift-1", "drift-3"]
    assert all(row["untriaged"] for row in document["projects"][0]["drifts"])
    assert document["projects"][0]["drifts"][0]["event_type_name"] == "app.screen_view"


def test_drifts_list_status_all_includes_accepted_and_false_positive(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.drifts(
        "prod",
        "et-1",
        [
            make_drift(drift_id="drift-1", status="open"),
            make_drift(drift_id="drift-2", status="accepted"),
            make_drift(drift_id="drift-3", status="false_positive"),
        ],
    )
    assert main(["drifts", "list", "--status", "all", "--json"]) == 0
    document = _document(capsys)
    assert [row["id"] for row in document["projects"][0]["drifts"]] == [
        "drift-1",
        "drift-2",
        "drift-3",
    ]
    assert document["status_filter"] == "all"


def test_drifts_list_filters_on_a_concrete_status(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.drifts(
        "prod",
        "et-1",
        [make_drift(drift_id="drift-1"), make_drift(drift_id="drift-2", status="accepted")],
    )
    assert main(["drifts", "list", "--status", "accepted", "--json"]) == 0
    assert [row["id"] for row in _document(capsys)["projects"][0]["drifts"]] == ["drift-2"]


def test_an_unknown_status_exits_usage_without_a_socket(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["drifts", "list", "--status", "dismissed"])
    assert exit_info.value.code == 2
    assert not tripl_api.router.calls


def test_a_404_from_one_drifts_read_is_reported_and_exits_one(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 404 must NEVER render as "no drifts" (TRAP 3).

    One event type answers with two drifts, another 404s. The two are printed,
    the failure is named with its endpoint and status, and the command exits 1 -
    because the honest answer is "I do not know", not "there are none".
    """
    tripl_api.event_types(
        "prod", [make_event_type("et-1"), make_event_type("et-9", "app.purchase")]
    )
    tripl_api.drifts(
        "prod", "et-1", [make_drift(drift_id="drift-1"), make_drift(drift_id="drift-2")]
    )
    tripl_api.drifts("prod", "et-9", None, status=404, payload={"detail": "Event type not found"})
    assert main(["drifts", "list", "--json"]) == 1
    captured = capsys.readouterr()
    assert "unavailable" in captured.err
    assert "/projects/prod/event-types/et-9/drifts" in captured.err
    document = json.loads(captured.out)
    project = document["projects"][0]
    assert [row["id"] for row in project["drifts"]] == ["drift-1", "drift-2"]
    assert project["errors"][0]["status_code"] == 404
    assert project["errors"][0]["endpoint"] == "/projects/prod/event-types/et-9/drifts"


def test_a_failed_event_type_listing_is_an_error_not_an_empty_project(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.event_types("prod", {"detail": "nope"}, status=403)
    assert main(["drifts", "list", "--json"]) == 1
    project = _document(capsys)["projects"][0]
    assert project["drifts"] == []
    assert project["errors"][0]["section"] == "event_types"
    assert project["errors"][0]["endpoint"] == "/projects/prod/event-types"


def test_drifts_list_reports_truncation_per_project(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The budget is spent round-robin, so coverage is stated per project.

    One instance-wide ratio would name no project, and "we did not look there"
    is only useful when it says where.
    """
    tripl_api.projects([make_project("prod"), make_project("staging")])
    for slug in ("prod", "staging"):
        tripl_api.event_types(
            slug, [make_event_type("et-1"), make_event_type("et-2", "app.purchase")]
        )
        tripl_api.drifts(slug, "et-1", [])
        tripl_api.drifts(slug, "et-2", [])
    assert main(["drifts", "list", "--max-event-types", "2", "--json"]) == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    for project in document["projects"]:
        assert project["event_types_total"] == 2
        # 2 budget over 2 projects, round-robin: one each, not two and zero.
        assert project["event_types_examined"] == 1
        assert project["truncated"] is True
    assert captured.err.count("1 of 2 event types examined") == 2


def test_drifts_list_opens_exactly_one_connection_pool(
    tripl_api: FakeInstance,
    configured_env: None,
    tracking_pool: list[httpx.AsyncClient],
) -> None:
    assert main(["drifts", "list"]) == 0
    assert len(tracking_pool) == 1
    assert tracking_pool[0].is_closed


# --- drifts dismiss -------------------------------------------------------


def test_drifts_dismiss_sends_false_positive_by_default(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.drift_action("prod", "drift-1")
    assert main(["drifts", "dismiss", "drift-1", "--project", "prod", "--yes"]) == 0
    posts = _posts(tripl_api)
    assert len(posts) == 1
    assert posts[0].url.path == "/api/v1/projects/prod/event-types/drifts/drift-1/actions"
    assert json.loads(posts[0].content) == {"action": "false_positive"}
    assert "is now false_positive" in capsys.readouterr().out


def test_drifts_dismiss_with_snooze_until_sends_snooze_and_an_rfc3339_timestamp(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    tripl_api.drift_action("prod", "drift-1")
    assert (
        main(
            [
                "drifts",
                "dismiss",
                "drift-1",
                "--project",
                "prod",
                "--snooze-until",
                "2026-08-04T00:00:00Z",
                "--yes",
            ]
        )
        == 0
    )
    body = json.loads(_posts(tripl_api)[0].content)
    assert body == {"action": "snooze", "snoozed_until": "2026-08-04T00:00:00Z"}


def test_a_naive_snooze_timestamp_is_read_as_utc(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    """Not as local time: a snooze shifted by an offset lapses at the wrong hour."""
    tripl_api.drift_action("prod", "drift-1")
    assert (
        main(
            [
                "drifts",
                "dismiss",
                "drift-1",
                "--project",
                "prod",
                "--snooze-until",
                "2026-08-04T06:30:00",
                "--yes",
            ]
        )
        == 0
    )
    assert json.loads(_posts(tripl_api)[0].content)["snoozed_until"] == "2026-08-04T06:30:00Z"


def test_drifts_dismiss_note_is_forwarded(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    tripl_api.drift_action("prod", "drift-1")
    assert (
        main(["drifts", "dismiss", "drift-1", "--project", "prod", "--note", "known", "--yes"]) == 0
    )
    assert json.loads(_posts(tripl_api)[0].content) == {
        "action": "false_positive",
        "note": "known",
    }


def test_drifts_dismiss_has_no_accept_option(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    """`accept` deletes a FieldDefinition on a missing_field drift.

    doctor's schema_field_deleted_by_accept finding exists because that happened,
    so there is no flag - not behind --force, not "for completeness" - that
    reaches it from the CLI.
    """
    tripl_api.drift_action("prod", "drift-1")
    for flags in (["--accept"], ["--action", "accept"]):
        with pytest.raises(SystemExit) as exit_info:
            main(["drifts", "dismiss", "drift-1", "--project", "prod", "--yes", *flags])
        assert exit_info.value.code == 2
    assert not _posts(tripl_api)


def test_no_reachable_drift_invocation_can_send_accept(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    """The exclusion, asserted on what goes ON THE WIRE.

    ``CLI_ALLOWED_DRIFT_ACTIONS`` has no production consumer - each verb picks
    its action from a literal - so asserting on the constant's own contents
    would pin a decoration. This drives every flag combination on BOTH verbs that
    reach the action route and reads the action back off the POST body, so the
    constant is an expectation about behaviour: nothing outside it is ever sent,
    and everything in it is reachable.

    The second half is the half that keeps earning its place. It caught the
    original "safety change quietly made `snooze` unreachable" case, and it is
    what catches an action declared in the constant but wired to a verb nobody
    can reach - the failure mode of adding an action and its command in the same
    change.
    """
    from tripl_cli.api.event_types import CLI_ALLOWED_DRIFT_ACTIONS

    tripl_api.drift_action("prod", "drift-1")
    for extra in (
        [],
        ["--note", "known"],
        ["--snooze-until", "2026-08-04T00:00:00Z"],
        ["--snooze-until", "2026-08-04T00:00:00Z", "--note", "known"],
        ["--dry-run"],
    ):
        assert main(["drifts", "dismiss", "drift-1", "--project", "prod", "--yes", *extra]) == 0
    for extra in ([], ["--dry-run"]):
        assert main(["drifts", "reopen", "drift-1", "--project", "prod", "--yes", *extra]) == 0

    sent = [json.loads(request.content)["action"] for request in _posts(tripl_api)]
    assert sent, "no POST was captured; the test proves nothing"
    assert "accept" not in sent
    assert set(sent) == set(CLI_ALLOWED_DRIFT_ACTIONS)


def test_drifts_dismiss_note_too_long_surfaces_the_apis_own_validation_detail(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """422 on a write, with the server's validation output passed through.

    The 2000-character cap is the API's, and the CLI deliberately does not carry
    a fourth copy of it - so the refusal has to arrive legible. A paraphrase
    would leave an operator guessing which of `note` and `snoozed_until` the
    server rejected.
    """
    detail = [
        {
            "type": "string_too_long",
            "loc": ["body", "note"],
            "msg": "String should have at most 2000 characters",
            "ctx": {"max_length": 2000},
        }
    ]
    tripl_api.drift_action("prod", "drift-1", {"detail": detail}, status=422)
    argv = ["drifts", "dismiss", "drift-1", "--project", "prod", "--note", "x" * 2001, "--yes"]
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "(422)" in captured.err
    assert "string_too_long" in captured.err
    assert "String should have at most 2000 characters" in captured.err
    assert "note" in captured.err


def test_a_refused_dismiss_writes_nothing_to_stdout(
    tripl_api: FakeInstance,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The `--json` contract on the write path: no document, ever, on a refusal.

    Both routes into exit 1 are covered - the server said no, and the operator
    said no - because a consumer parsing stdout before reading the exit code sees
    the same empty string for both (website/docs/run/cli.md).
    """
    tripl_api.drift_action("prod", "drift-1", {"detail": "Write scope required"}, status=403)
    assert main(["drifts", "dismiss", "drift-1", "--project", "prod", "--yes", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()

    monkeypatch.setattr("sys.stdin", _Stdin("n\n", isatty=True))
    assert main(["drifts", "dismiss", "drift-1", "--project", "prod", "--json"]) == 1
    declined = capsys.readouterr()
    assert declined.out == ""
    assert "aborted" in declined.err


def test_drifts_dismiss_bad_snooze_timestamp_exits_usage_without_a_socket(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "drifts",
                "dismiss",
                "drift-1",
                "--project",
                "prod",
                "--snooze-until",
                "next tuesday",
                "--yes",
            ]
        )
    assert exit_info.value.code == 2
    assert not tripl_api.router.calls


def test_drifts_dismiss_without_yes_on_a_non_tty_exits_usage_and_sends_nothing(
    tripl_api: FakeInstance,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", _Stdin("", isatty=False))
    tripl_api.drift_action("prod", "drift-1")
    assert main(["drifts", "dismiss", "drift-1", "--project", "prod"]) == 2
    assert not _posts(tripl_api)
    assert "--yes" in capsys.readouterr().err


def test_drifts_dismiss_declined_at_the_prompt_exits_one(
    tripl_api: FakeInstance,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Never 0: a script must not read "the operator said no" as "it happened"."""
    monkeypatch.setattr("sys.stdin", _Stdin("no\n", isatty=True))
    tripl_api.drift_action("prod", "drift-1")
    assert main(["drifts", "dismiss", "drift-1", "--project", "prod"]) == 1
    assert not _posts(tripl_api)
    assert "aborted" in capsys.readouterr().err


def test_drifts_dismiss_dry_run_sends_no_post(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.drift_action("prod", "drift-1")
    assert main(["drifts", "dismiss", "drift-1", "--project", "prod", "--dry-run", "--json"]) == 0
    assert not _posts(tripl_api)
    document = _document(capsys)
    assert document["dry_run"] is True
    assert document["result"] is None
    assert document["action"] == "false_positive"
    assert document["request"]["body"] == {"action": "false_positive"}


def test_drifts_dismiss_403_from_a_read_key_exits_one_with_scope_guidance(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.drift_action("prod", "drift-1", {"detail": "Write scope required"}, status=403)
    assert main(["drifts", "dismiss", "drift-1", "--project", "prod", "--yes"]) == 1
    assert "tk_r_ keys cannot write" in capsys.readouterr().err


def test_drifts_dismiss_unknown_drift_id_reports_the_404(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.drift_action("prod", "nope", {"detail": "Drift not found"}, status=404)
    assert main(["drifts", "dismiss", "nope", "--project", "prod", "--yes"]) == 1
    assert "Not found (404)" in capsys.readouterr().err


def test_drifts_dismiss_without_project_exits_usage(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    assert main(["drifts", "dismiss", "drift-1", "--yes"]) == 2
    assert not tripl_api.router.calls


# --- drifts reopen --------------------------------------------------------


def test_drifts_reopen_sends_reopen_and_nothing_else(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The body is the action alone.

    Not `{"action": "reopen", "note": null}`: the route distinguishes an OMITTED
    note from an explicitly null one via `model_fields_set`, and that distinction
    is the only way to clear a note WITHOUT reopening a drift. Sending the key
    would spend it for nothing here.
    """
    tripl_api.drift_action("prod", "drift-1", make_drift(drift_id="drift-1", status="open"))
    assert main(["drifts", "reopen", "drift-1", "--project", "prod", "--yes"]) == 0
    posts = _posts(tripl_api)
    assert len(posts) == 1
    assert posts[0].url.path == "/api/v1/projects/prod/event-types/drifts/drift-1/actions"
    assert json.loads(posts[0].content) == {"action": "reopen"}
    assert "is now open" in capsys.readouterr().out


def test_drifts_reopen_has_no_note_or_snooze_flag(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    """Both would be flags whose value the server discards.

    `schema_drift_service` sets `resolution_note = None` for this action before it
    ever looks at the request's note, and clears `snoozed_until` outright. A
    `--note` here would accept a sentence, send it, report success, and store
    nothing - and the operator would have no way to tell from the output. So
    argparse refuses them: exit 2, no socket.
    """
    tripl_api.drift_action("prod", "drift-1")
    for flags in (["--note", "back to open"], ["--snooze-until", "2026-08-04T00:00:00Z"]):
        with pytest.raises(SystemExit) as exit_info:
            main(["drifts", "reopen", "drift-1", "--project", "prod", "--yes", *flags])
        assert exit_info.value.code == 2
    assert not _posts(tripl_api)


def test_drifts_reopen_has_no_accept_option(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    """The second verb reaching the action route does not become the way in.

    `dismiss` was audited for this the day it shipped; a new verb on the same
    route is exactly where the exclusion would be lost by omission rather than by
    decision.
    """
    tripl_api.drift_action("prod", "drift-1")
    for flags in (["--accept"], ["--action", "accept"]):
        with pytest.raises(SystemExit) as exit_info:
            main(["drifts", "reopen", "drift-1", "--project", "prod", "--yes", *flags])
        assert exit_info.value.code == 2
    assert not _posts(tripl_api)


def test_drifts_reopen_prompt_names_what_it_destroys(
    tripl_api: FakeInstance,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reopening is not the harmless direction, and the prompt has to say so.

    Dismissing again restores the status but NOT the note or the resolver: those
    are gone the moment this POST lands. An operator agreeing to "reopen this
    drift" would reasonably assume otherwise.
    """
    monkeypatch.setattr("sys.stdin", _Stdin("no\n", isatty=True))
    tripl_api.drift_action("prod", "drift-1")
    assert main(["drifts", "reopen", "drift-1", "--project", "prod"]) == 1
    assert not _posts(tripl_api)
    prompt = capsys.readouterr().err
    assert "DISCARDED" in prompt
    assert "resolution note" in prompt
    assert "aborted" in prompt


def test_drifts_reopen_without_yes_on_a_non_tty_exits_usage_and_sends_nothing(
    tripl_api: FakeInstance,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", _Stdin("", isatty=False))
    tripl_api.drift_action("prod", "drift-1")
    assert main(["drifts", "reopen", "drift-1", "--project", "prod"]) == 2
    assert not _posts(tripl_api)
    assert "--yes" in capsys.readouterr().err


def test_drifts_reopen_dry_run_sends_no_post(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.drift_action("prod", "drift-1")
    assert main(["drifts", "reopen", "drift-1", "--project", "prod", "--dry-run", "--json"]) == 0
    assert not _posts(tripl_api)
    document = _document(capsys)
    assert document["command"] == "drifts reopen"
    assert document["dry_run"] is True
    assert document["result"] is None
    assert document["action"] == "reopen"
    assert document["request"]["body"] == {"action": "reopen"}


def test_drifts_reopen_json_document_has_the_same_shape_as_dismiss(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One document shape for the whole write surface, keys and all.

    Both verbs go through `_run_drift_action` for exactly this: a consumer that
    parses `tripl drifts dismiss --json` must not have to discover that `reopen`
    answers something else. Every per-command key is present on both, null where
    it does not apply - the rule `MutationOutcome` states and `scans` follows.
    """
    tripl_api.drift_action("prod", "drift-1")
    assert main(["drifts", "dismiss", "drift-1", "--project", "prod", "--yes", "--json"]) == 0
    dismissed = _document(capsys)
    assert main(["drifts", "reopen", "drift-1", "--project", "prod", "--yes", "--json"]) == 0
    reopened = _document(capsys)

    assert dismissed.keys() == reopened.keys()
    assert reopened["command"] == "drifts reopen"
    assert dismissed["command"] == "drifts dismiss"
    assert reopened["job_id"] is None
    assert reopened["scan"] is None
    assert reopened["drift_id"] == "drift-1"


def test_drifts_reopen_403_from_a_read_key_exits_one_with_scope_guidance(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.drift_action("prod", "drift-1", {"detail": "Write scope required"}, status=403)
    assert main(["drifts", "reopen", "drift-1", "--project", "prod", "--yes"]) == 1
    assert "tk_r_ keys cannot write" in capsys.readouterr().err


def test_drifts_reopen_unknown_drift_id_reports_the_404(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.drift_action("prod", "nope", {"detail": "Drift not found"}, status=404)
    assert main(["drifts", "reopen", "nope", "--project", "prod", "--yes"]) == 1
    assert "Not found (404)" in capsys.readouterr().err


def test_drifts_reopen_without_project_exits_usage(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    """Exit 2 from `require_single_project`, NOT a crash formatting the prompt.

    The prompt names the slug, and the slug is only trustworthy after that check
    runs - so the sentence is built from what the check returns. Formatting it
    from `args.project` beforehand raises TypeError here instead, replacing a
    precise usage error with a traceback.
    """
    assert main(["drifts", "reopen", "drift-1", "--yes"]) == 2
    assert not tripl_api.router.calls
    assert main(["drifts", "reopen", "drift-1", "--project", "a", "--project", "b", "--yes"]) == 2
    assert not tripl_api.router.calls


def test_drifts_reopen_opens_exactly_one_connection_pool(
    tripl_api: FakeInstance,
    configured_env: None,
    tracking_pool: list[httpx.AsyncClient],
) -> None:
    tripl_api.drift_action("prod", "drift-1")
    assert main(["drifts", "reopen", "drift-1", "--project", "prod", "--yes"]) == 0
    assert len(tracking_pool) == 1
    assert tracking_pool[0].is_closed


def test_bare_drifts_prints_help_to_stderr_and_exits_usage(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["drifts"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "dismiss" in captured.err
