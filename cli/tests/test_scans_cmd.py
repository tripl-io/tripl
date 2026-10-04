"""``tripl scans`` end to end, through ``main([...])`` against a fake instance.

The negatives carry the weight here. A 201 that is really a failure, a 403 from a
read-only key, a prompt in a pipeline and a --dry-run that sends something anyway
are all silent in the happy path and expensive in production.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
import pytest

from tripl_cli.cli import main

from .conftest import FakeInstance, make_job, make_project, make_scan_config


def _document(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """The one JSON object stdout is allowed to carry under --json."""
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    return payload


def _posts(tripl_api: FakeInstance) -> list[httpx.Request]:
    return [call.request for call in tripl_api.router.calls if call.request.method == "POST"]


# --- scans list -----------------------------------------------------------


def test_scans_list_prints_a_table_and_marks_unscheduled_configs(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.scans(
        "prod",
        [
            make_scan_config(),
            make_scan_config("scan-2", name="ad-hoc backfill", interval=None),
            make_scan_config("scan-3", name="no clock", time_column=None),
        ],
    )
    assert main(["scans", "list"]) == 0
    out = capsys.readouterr().out
    assert "scan-1" in out and "scheduled" in out
    assert "not scheduled (no interval)" in out
    assert "not scheduled (no time column)" in out
    assert "3 scan configs in 1 project." in out


def test_scans_list_omits_base_query_from_the_json_document(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The one shared projection, seen from the CLI side."""
    assert main(["scans", "list", "--json"]) == 0
    document = _document(capsys)
    scan = document["projects"][0]["scans"][0]
    assert "base_query" not in scan
    assert scan["dispatchable"] is True


def test_scans_list_json_is_one_document_on_stdout(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["scans", "list", "--json"]) == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)  # exactly one object, or this raises
    assert document["command"] == "scans list"
    assert document["schema_version"] == 1
    # Every human line went to stderr, so `| jq` is a promise not a habit.
    assert "tripl scans list -" in captured.err
    assert "tripl scans list -" not in captured.out


def test_scans_list_reports_a_failed_listing_and_exits_one(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 403 on one project is never a shorter table at exit 0 (TRAP 3)."""
    tripl_api.projects([make_project("prod"), make_project("staging")])
    tripl_api.scans("staging", {"detail": "nope"}, status=403)
    assert main(["scans", "list", "--json"]) == 1
    document = _document(capsys)
    failed = next(row for row in document["projects"] if row["slug"] == "staging")
    assert failed["scans"] == []
    assert failed["errors"][0]["status_code"] == 403
    assert failed["errors"][0]["endpoint"] == "/projects/staging/scans"


def test_both_list_commands_report_a_failed_read_in_the_same_words(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One idea, one spelling — and both of them COUNT.

    ``scans list`` used to end on "Some scan listings could not be read" while
    ``drifts list`` ended on "1 read failed": two sentences for the identical
    condition, only one of which told the operator how much was missing. An
    incident log is grepped for that line, so there has to be exactly one of it.
    """
    tripl_api.projects([make_project("prod"), make_project("staging")])
    tripl_api.scans("staging", {"detail": "nope"}, status=403)
    assert main(["scans", "list"]) == 1
    scans_out = capsys.readouterr().out

    tripl_api.event_types("prod", {"detail": "nope"}, status=403)
    tripl_api.event_types("staging", [])
    assert main(["drifts", "list"]) == 1
    drifts_out = capsys.readouterr().out

    sentence = "1 read failed; the list above is incomplete."
    assert sentence in scans_out
    assert sentence in drifts_out


def test_scans_list_opens_exactly_one_connection_pool(
    tripl_api: FakeInstance,
    configured_env: None,
    tracking_pool: list[httpx.AsyncClient],
) -> None:
    assert main(["scans", "list"]) == 0
    assert len(tracking_pool) == 1
    assert tracking_pool[0].is_closed


# --- scans jobs -----------------------------------------------------------


def test_scans_jobs_requests_the_documented_limit(
    tripl_api: FakeInstance,
    configured_env: None,
    now: datetime,
) -> None:
    tripl_api.jobs("prod", "scan-1", [make_job(at=now)])
    assert main(["scans", "jobs", "prod events", "--project", "prod", "--limit", "7"]) == 0
    jobs_calls = [
        call.request
        for call in tripl_api.router.calls
        if call.request.url.path.endswith("/scans/scan-1/jobs")
    ]
    assert jobs_calls, "the jobs endpoint was never called"
    assert jobs_calls[-1].url.params["limit"] == "7"


def test_scans_jobs_defaults_to_the_api_default_window(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tripl_cli.commands.scans import DEFAULT_JOBS_LIMIT

    assert main(["scans", "jobs", "scan-1", "--project", "prod", "--json"]) == 0
    assert _document(capsys)["limit"] == DEFAULT_JOBS_LIMIT


def test_scans_jobs_limit_above_the_maximum_exits_usage_without_a_socket(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["scans", "jobs", "scan-1", "--project", "prod", "--limit", "500"])
    assert exit_info.value.code == 2
    assert not tripl_api.router.calls


def test_scans_jobs_without_project_exits_usage(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    assert main(["scans", "jobs", "scan-1"]) == 2
    assert not tripl_api.router.calls


def test_scans_jobs_refuses_a_200_that_is_not_an_array(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-list read is a failure, never "(no jobs)" at exit 0.

    The route answers ``list[ScanJobResponse]``; an object arriving instead means
    a proxy, an error envelope or a future paginated shape - and coercing it to
    an empty list would print "0 jobs" about a config that may be mid-incident.
    That is TRAP 3 with a 200 status code, which is why the shape check lives in
    ``Reader.try_read_list`` and not in each caller's head.
    """
    tripl_api.jobs("prod", "scan-1", {"items": [], "total": 0})
    assert main(["scans", "jobs", "scan-1", "--project", "prod"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "(no jobs)" not in captured.out
    assert "expected a JSON array" in captured.err
    assert "/projects/prod/scans/scan-1/jobs" in captured.err


# --- scans run ------------------------------------------------------------


def test_scans_run_posts_to_the_run_route_and_reports_the_job(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.scan_run("prod", "scan-1")
    assert main(["scans", "run", "prod events", "--project", "prod"]) == 0
    posts = _posts(tripl_api)
    assert [request.url.path for request in posts] == ["/api/v1/projects/prod/scans/scan-1/run"]
    out = capsys.readouterr().out
    assert "started job job-91c2 (pending)" in out
    assert "tripl watch --project prod --scan 'prod events'" in out


def test_scans_run_exits_one_when_the_returned_job_is_already_failed(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 201 is NOT success.

    ``scan_service.trigger_scan`` catches a broker failure and still answers 201,
    with the job already failed. A script reading only the status code would
    report a scan that never started as started.
    """
    tripl_api.scan_run(
        "prod",
        "scan-1",
        make_job(
            job_id="job-dead",
            status="failed",
            error_message="Failed to dispatch task to worker (broker unavailable)",
        ),
    )
    assert main(["scans", "run", "scan-1", "--project", "prod"]) == 1
    captured = capsys.readouterr()
    assert "broker unavailable" in captured.err


def test_scans_run_with_a_read_only_key_surfaces_the_403_guidance(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No local tk_r_/tk_w_ gate: the server is the authority and it says why."""
    tripl_api.scan_run("prod", "scan-1", {"detail": "Write scope required"}, status=403)
    assert main(["scans", "run", "scan-1", "--project", "prod"]) == 1
    assert "tk_r_ keys cannot write" in capsys.readouterr().err


def test_scans_run_with_an_unknown_scan_name_exits_usage(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["scans", "run", "nope", "--project", "prod"]) == 2
    assert not _posts(tripl_api)
    err = capsys.readouterr().err
    assert "no scan config matches 'nope'" in err
    assert "prod events (scan-1)" in err


def test_scans_run_with_an_ambiguous_scan_name_names_both_ids_and_exits_usage(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two configs, one name: refuse. Picking one would trigger the wrong SQL."""
    tripl_api.scans(
        "prod",
        [make_scan_config("scan-1", name="nightly"), make_scan_config("scan-2", name="nightly")],
    )
    assert main(["scans", "run", "nightly", "--project", "prod"]) == 2
    assert not _posts(tripl_api)
    err = capsys.readouterr().err
    assert "scan-1" in err and "scan-2" in err


def test_scans_run_dry_run_sends_nothing_and_prints_the_request(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.scan_run("prod", "scan-1")
    assert main(["scans", "run", "prod events", "--project", "prod", "--dry-run", "--json"]) == 0
    assert not _posts(tripl_api)
    document = _document(capsys)
    assert document["dry_run"] is True
    assert document["result"] is None
    assert document["request"]["method"] == "POST"
    assert document["request"]["path"].endswith("/scans/scan-1/run")


def test_the_request_document_never_carries_a_credential(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dry-run prints the request; a printable credential is a leaked one."""
    assert main(["scans", "run", "scan-1", "--project", "prod", "--dry-run", "--json"]) == 0
    document = _document(capsys)
    assert set(document["request"]) == {"method", "path", "params", "body"}
    assert "tk_r_test-key" not in json.dumps(document)


def test_scans_run_without_project_exits_usage(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    assert main(["scans", "run", "scan-1"]) == 2
    assert not tripl_api.router.calls


def test_scans_run_never_offers_a_yes_flag(
    tripl_api: FakeInstance,
    configured_env: None,
) -> None:
    """It does not prompt, so a no-op --yes would be a lie about the next command."""
    with pytest.raises(SystemExit) as exit_info:
        main(["scans", "run", "scan-1", "--project", "prod", "--yes"])
    assert exit_info.value.code == 2


# --- scans cancel ---------------------------------------------------------


def test_scans_cancel_prompts_and_aborts_on_a_negative_answer(
    tripl_api: FakeInstance,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _interactive(monkeypatch, "n\n")
    tripl_api.job_cancel("prod", "scan-1", "job-9")
    assert main(["scans", "cancel", "scan-1", "job-9", "--project", "prod"]) == 1
    assert not _posts(tripl_api)
    assert "aborted" in capsys.readouterr().err


def test_scans_cancel_without_yes_on_a_non_tty_exits_usage_and_sends_nothing(
    tripl_api: FakeInstance,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The cron rule: never hang on a prompt, never proceed without one either."""
    monkeypatch.setattr("sys.stdin", _Stdin("", isatty=False))
    tripl_api.job_cancel("prod", "scan-1", "job-9")
    assert main(["scans", "cancel", "scan-1", "job-9", "--project", "prod"]) == 2
    assert not _posts(tripl_api)
    assert "--yes" in capsys.readouterr().err


def test_scans_cancel_with_yes_posts_to_the_cancel_route(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.job_cancel("prod", "scan-1", "job-9")
    assert main(["scans", "cancel", "scan-1", "job-9", "--project", "prod", "--yes"]) == 0
    posts = _posts(tripl_api)
    assert [request.url.path for request in posts] == [
        "/api/v1/projects/prod/scans/scan-1/jobs/job-9/cancel"
    ]
    assert "job job-9 is now cancelled" in capsys.readouterr().out


def test_scans_cancel_of_a_finished_job_surfaces_the_409(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.job_cancel(
        "prod",
        "scan-1",
        "job-9",
        {"detail": "Scan job is not active (status: completed)"},
        status=409,
    )
    assert main(["scans", "cancel", "scan-1", "job-9", "--project", "prod", "--yes"]) == 1
    assert "Scan job is not active (status: completed)" in capsys.readouterr().err


def test_scans_cancel_with_a_read_only_key_surfaces_the_403_guidance(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The refusal an operator is most likely to hit on a write, on the verb that
    stops work in flight.

    ``run`` has this test and ``cancel`` did not, although it is the one reached
    under pressure and with the wrong key exported. No local tk_r_/tk_w_ gate:
    the server is the authority and its message names all three possibilities.
    """
    tripl_api.job_cancel("prod", "scan-1", "job-9", {"detail": "Write scope required"}, status=403)
    assert main(["scans", "cancel", "scan-1", "job-9", "--project", "prod", "--yes"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "tk_r_ keys cannot write" in captured.err
    assert "Write scope required" in captured.err


def test_scans_cancel_of_a_malformed_job_id_surfaces_the_apis_own_validation_detail(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """422, the other refusal a write actually meets.

    ``<job-id>`` is a free string here and ``uuid.UUID`` on the route, so a job
    id copied with a stray character never reaches the service - FastAPI answers
    422 with its own validation body. The CLI must pass that body through: "the
    tripl API rejected the request" alone would leave an operator re-reading
    their own command line for a fault the server already located.
    """
    detail = [
        {
            "type": "uuid_parsing",
            "loc": ["path", "job_id"],
            "msg": "Input should be a valid UUID, invalid character: found `n` at 1",
            "input": "not-a-uuid",
        }
    ]
    tripl_api.job_cancel("prod", "scan-1", "not-a-uuid", {"detail": detail}, status=422)
    assert main(["scans", "cancel", "scan-1", "not-a-uuid", "--project", "prod", "--yes"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    assert "(422)" in err
    # The API's own words, not a paraphrase: the field it blamed, the reason, and
    # the value it was given.
    assert "uuid_parsing" in err
    assert "Input should be a valid UUID" in err
    assert "job_id" in err
    assert "not-a-uuid" in err


def test_a_command_that_could_not_complete_writes_nothing_to_stdout(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pins the `--json` contract on the path that has no document to emit.

    ``scans list`` and ``drifts list`` report a failed read INSIDE their
    document; these three raise on the first refusal, so stdout stays EMPTY and
    the reason goes to stderr. A consumer that parses before it reads the exit
    code gets an empty string rather than an error object, and
    website/docs/run/cli.md says so - this is the test that keeps the page true.
    """
    tripl_api.jobs("prod", "scan-1", {"detail": "nope"}, status=403)
    tripl_api.scan_run("prod", "scan-1", {"detail": "Write scope required"}, status=403)
    tripl_api.job_cancel(
        "prod", "scan-1", "job-9", {"detail": "Scan job is not active"}, status=409
    )
    for argv in (
        ["scans", "jobs", "scan-1", "--project", "prod", "--json"],
        ["scans", "run", "scan-1", "--project", "prod", "--json"],
        ["scans", "cancel", "scan-1", "job-9", "--project", "prod", "--yes", "--json"],
    ):
        assert main(argv) == 1, argv
        captured = capsys.readouterr()
        assert captured.out == "", f"{argv} put a partial document on stdout"
        assert captured.err.strip(), f"{argv} failed without saying why"


def test_scans_cancel_prompt_goes_to_stderr_not_stdout(
    tripl_api: FakeInstance,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``input()`` would put the question inside the --json document."""
    _interactive(monkeypatch, "y\n")
    tripl_api.job_cancel("prod", "scan-1", "job-9")
    assert main(["scans", "cancel", "scan-1", "job-9", "--project", "prod", "--json"]) == 0
    captured = capsys.readouterr()
    assert "[y/N]" in captured.err
    document = json.loads(captured.out)
    assert document["command"] == "scans cancel"
    assert document["job_id"] == "job-9"


def test_scans_cancel_dry_run_neither_prompts_nor_sends(
    tripl_api: FakeInstance,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A non-TTY that would otherwise refuse: --dry-run short-circuits before the
    # confirmation, because there is nothing to confirm.
    monkeypatch.setattr("sys.stdin", _Stdin("", isatty=False))
    tripl_api.job_cancel("prod", "scan-1", "job-9")
    assert main(["scans", "cancel", "scan-1", "job-9", "--project", "prod", "--dry-run"]) == 0
    assert not _posts(tripl_api)
    assert "Nothing was sent." in capsys.readouterr().out


def test_a_scan_id_that_resolves_but_404s_on_the_post_reports_the_404(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.scan_run("prod", "scan-1", {"detail": "Scan config not found"}, status=404)
    assert main(["scans", "run", "scan-1", "--project", "prod"]) == 1
    assert "Not found (404)" in capsys.readouterr().err


def test_bare_scans_prints_help_to_stderr_and_exits_usage(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["scans"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cancel" in captured.err


class _Stdin:
    """Minimal stdin stand-in: a scripted answer and a chosen isatty()."""

    def __init__(self, answer: str, *, isatty: bool) -> None:
        self._answer = answer
        self._isatty = isatty

    def isatty(self) -> bool:
        return self._isatty

    def readline(self) -> str:
        return self._answer


def _interactive(monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    monkeypatch.setattr("sys.stdin", _Stdin(answer, isatty=True))
