"""``tripl doctor`` end to end, through ``main([...])`` against a mocked instance.

Nothing here asserts on prose. The contract is the exit code, the check ids and
the finding codes with their evidence.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from tests.conftest import (
    API_BASE,
    BASE_URL,
    FakeInstance,
    make_event_type,
    make_job,
    make_project,
    make_scan_config,
)
from tripl_cli.cli import main
from tripl_cli.model import CHECK_IDS

pytestmark = pytest.mark.usefixtures("configured_env")


def run_doctor(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = main(["doctor", "--json", *argv])
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def check_by_id(document: dict[str, Any], check_id: str) -> dict[str, Any]:
    return next(check for check in document["checks"] if check["id"] == check_id)


def finding_codes(document: dict[str, Any], check_id: str) -> list[str]:
    return [finding["code"] for finding in check_by_id(document, check_id)["findings"]]


def all_codes(document: dict[str, Any]) -> list[str]:
    return [finding["code"] for check in document["checks"] for finding in check["findings"]]


def register_demo_project(api: FakeInstance, slug: str = "demo") -> None:
    api.project(slug, make_project(slug=slug, is_demo=True))
    api.scans(slug, [make_scan_config()])
    api.jobs(slug, "scan-1", [make_job(at=api.now, time_to=api.now)])
    api.event_types(slug, [make_event_type()])
    api.drifts(slug, "et-1", [])


def test_broken_scan_config_exits_three_and_names_the_cause(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str], now: datetime
) -> None:
    """The acceptance criterion of tripl-ey6j.2."""
    tripl_api.jobs(
        "prod",
        "scan-1",
        [
            make_job(
                job_id=f"job-{index}",
                status="failed",
                at=now - timedelta(hours=index + 1),
                error_message="Scan failed due to an internal error.",
            )
            for index in range(7)
        ],
    )

    code, document = run_doctor(capsys)

    assert code == 3
    assert document["status"] == "fail"
    scans = check_by_id(document, "scans")
    assert scans["status"] == "fail"
    failing = next(f for f in scans["findings"] if f["code"] == "scan_config_failing")
    assert failing["evidence"]["consecutive_failures"] == 7
    assert failing["evidence"]["last_failed_job_id"] == "job-0"
    assert failing["evidence"]["last_error"] == "Scan failed due to an internal error."
    assert failing["evidence"]["last_error_is_generic"] is True
    assert failing["target"]["kind"] == "scan_config"
    # The backoff is reported as expected behaviour, with a number.
    backoff = next(f for f in scans["findings"] if f["code"] == "scan_backoff_active")
    assert backoff["severity"] == "warn"
    assert backoff["evidence"]["deferred_by_seconds_estimate"] > 0


def test_json_document_is_the_only_thing_on_stdout(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """Makes `tripl doctor --json | jq` a promise rather than a habit."""
    main(["doctor", "--json"])
    captured = capsys.readouterr()

    assert len(captured.out.strip().splitlines()) == 1
    assert json.loads(captured.out)["command"] == "doctor"
    # The human report still happened — on stderr.
    assert "PASS" in captured.err
    # render.py's docstring and the docs page both promise ASCII-only output, and
    # the header line quietly shipped a U+2014 on every single run. A promise a
    # test does not hold is a promise that lasts until the next edit.
    assert captured.err.isascii(), "non-ASCII in the human report"


def test_healthy_instance_exits_zero_with_every_check_pass(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The negative of the acceptance test: the checks are not stuck on."""
    code, document = run_doctor(capsys)

    assert code == 0
    assert document["status"] == "pass"
    assert document["summary"] == {"pass": 6, "warn": 0, "fail": 0, "skip": 0}
    assert all_codes(document) == []


def test_unreachable_instance_exits_three_and_still_emits_a_valid_document(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """doctor's contract is that it turns every failure into a diagnosis."""
    tripl_api.health_raises(httpx.ConnectError("connection refused"))

    code, document = run_doctor(capsys)

    assert code == 3
    assert check_by_id(document, "connectivity")["status"] == "fail"
    assert finding_codes(document, "connectivity") == ["instance_unreachable"]
    for check_id in ("auth", "projects", "data_sources", "scans", "drifts"):
        check = check_by_id(document, check_id)
        assert check["status"] == "skip"
        assert check["skip_reason"]
    assert document["summary"]["skip"] == 5


def test_health_503_is_reported_as_a_database_problem_not_a_bad_key(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.health(503, {"status": "error", "component": "database"})

    code, document = run_doctor(capsys)

    assert code == 3
    assert finding_codes(document, "connectivity") == ["instance_database_unreachable"]
    # Not a key problem: the auth check could not run, so it says so.
    assert check_by_id(document, "auth")["status"] == "skip"


def test_a_200_from_a_health_endpoint_that_is_not_triples_is_the_wrong_host(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """Anything with a /health route would otherwise pass connectivity.

    A proxy, a load balancer or a neighbouring service answering 200 has to be
    diagnosed as a wrong TRIPL_BASE_URL here, or every later 404 gets read as a
    broken tripl instance instead.
    """
    tripl_api.health(200, {"status": "degraded"})

    code, document = run_doctor(capsys)

    assert code == 3
    assert finding_codes(document, "connectivity") == ["instance_not_tripl"]
    finding = check_by_id(document, "connectivity")["findings"][0]
    assert finding["severity"] == "fail"
    assert finding["evidence"]["url"] == f"{BASE_URL}/health"
    # A 200 that is nonetheless not a tripl instance - the status code alone is
    # not the signal, the body is.
    assert finding["evidence"]["status_code"] == 200
    assert finding["evidence"]["body_status"] == "degraded"
    assert document["summary"]["skip"] == 5


def test_the_health_probe_carries_no_authorization_header(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """TRAP 1, the security half: /health requires no credential, so none is sent."""
    route = tripl_api.health()

    main(["doctor", "--json"])
    capsys.readouterr()

    assert route.called
    assert "authorization" not in {key.lower() for key in route.calls.last.request.headers}


def test_health_is_requested_at_the_root_not_under_api_v1(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    route = tripl_api.health()

    main(["doctor", "--json"])
    capsys.readouterr()

    assert route.calls.last.request.url.path == "/health"
    assert str(route.calls.last.request.url) == f"{BASE_URL}/health"


def test_invalid_api_key_exits_three_with_api_key_invalid(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.auth(401, {"detail": "Invalid or expired API key"})

    code, document = run_doctor(capsys)

    assert code == 3
    assert finding_codes(document, "auth") == ["api_key_invalid"]
    assert document["instance"]["api_key_scope"] == "unknown"


def test_a_500_from_auth_me_says_the_key_is_unverified_not_that_it_is_invalid(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The three known statuses are 200, 401 and 403; anything else is a proxy.

    Collapsing this into api_key_invalid sends the operator to rotate a key that
    was never actually rejected, and the rest of the instance still gets checked.
    """
    tripl_api.auth(500, {"detail": "upstream connect error"})

    code, document = run_doctor(capsys)

    assert code == 3
    assert finding_codes(document, "auth") == ["auth_unexpected_status"]
    finding = check_by_id(document, "auth")["findings"][0]
    assert finding["severity"] == "fail"
    assert finding["evidence"]["status_code"] == 500
    assert finding["evidence"]["detail"] is not None
    assert document["instance"]["api_key_scope"] == "unknown"
    # An unverifiable key is not an unreadable instance: the reads still happened.
    assert check_by_id(document, "projects")["status"] == "pass"
    assert check_by_id(document, "scans")["status"] == "pass"


def test_project_scoped_key_without_a_selector_names_the_flag(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """TRAP 5: a 403 by design must not be reported as a broken instance."""
    tripl_api.auth(403, {"detail": "API key is scoped to a single project"})
    listing = tripl_api.projects([])

    code, document = run_doctor(capsys)

    assert code == 3
    assert document["instance"]["api_key_scope"] == "project"
    # The key itself is fine, and doctor says so.
    assert check_by_id(document, "auth")["status"] == "pass"
    assert finding_codes(document, "projects") == ["project_selector_required"]
    # A request that can only 403 is not spent.
    assert not listing.called


def test_project_scoped_key_with_a_selector_can_pass(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.auth(403, {"detail": "API key is scoped to a single project"})
    listing = tripl_api.projects([])
    register_demo_project(tripl_api)

    code, document = run_doctor(capsys, "--project", "demo")

    assert code == 0
    assert not listing.called
    assert check_by_id(document, "data_sources")["status"] == "skip"
    assert "403" in check_by_id(document, "data_sources")["skip_reason"]


def test_instance_key_refused_on_the_listing_is_reported_as_such(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """Distinct from the project-scope case: /auth/me answered 200 here."""
    tripl_api.projects({"detail": "Editor role required"}, status=403)

    code, document = run_doctor(capsys)

    assert code == 3
    assert finding_codes(document, "projects") == ["project_list_forbidden"]
    assert document["instance"]["api_key_scope"] == "instance"


def test_a_mistyped_project_slug_is_reported_as_not_found_not_as_a_bad_endpoint(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """`tripl doctor --project prodd`, the typo, is the commonest way to reach this.

    A mis-ordered if/elif ladder in check_projects would answer it with
    endpoint_unexpected_status and send the operator hunting a broken instance
    instead of re-reading their own command line.
    """
    tripl_api.project("prodd", {"detail": "Project not found"}, status=404)

    code, document = run_doctor(capsys, "--project", "prodd")

    assert code == 3
    assert finding_codes(document, "projects") == ["project_not_found"]
    finding = check_by_id(document, "projects")["findings"][0]
    assert finding["severity"] == "fail"
    assert finding["project"] == "prodd"
    assert finding["evidence"] == {"slug": "prodd", "status_code": 404}
    # No project was selected, so nothing project-scoped is claimed either way.
    for check_id in ("data_sources", "scans", "drifts"):
        assert check_by_id(document, check_id)["status"] == "skip"


def test_a_project_scoped_key_aimed_at_another_project_names_the_mismatch(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 403 on a named project is a key/slug mismatch, not a missing project.

    Reporting it as project_not_found would have the operator create a project
    that already exists; the key, meanwhile, authenticates fine and doctor says so.
    """
    tripl_api.auth(403, {"detail": "API key is scoped to a single project"})
    tripl_api.project("other", {"detail": "API key is scoped to a single project"}, status=403)

    code, document = run_doctor(capsys, "--project", "other")

    assert code == 3
    assert finding_codes(document, "projects") == ["project_not_authorized"]
    finding = check_by_id(document, "projects")["findings"][0]
    assert finding["severity"] == "fail"
    assert finding["evidence"] == {"slug": "other", "status_code": 403}
    assert check_by_id(document, "auth")["status"] == "pass"
    assert document["instance"]["api_key_scope"] == "project"


def test_an_instance_with_nothing_but_a_demo_warns_and_still_exits_zero(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing is broken on a fresh instance, so a default run must not go red.

    The docs recommend --strict in CI, and a permanent red there gets the whole
    check muted - after which the failing scan config goes unseen too.
    """
    tripl_api.projects([make_project(slug="demo", is_demo=True)])

    code, document = run_doctor(capsys)

    assert code == 0
    assert document["status"] == "warn"
    assert finding_codes(document, "projects") == ["no_projects_selected"]
    finding = check_by_id(document, "projects")["findings"][0]
    assert finding["severity"] == "warn"
    assert finding["evidence"] == {"excluded_demo_count": 1}
    # --strict is what turns this one red, and only on request.
    assert main(["doctor", "--json", "--strict"]) == 3
    capsys.readouterr()


def test_demo_projects_are_excluded_unless_include_demo(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.projects([make_project(), make_project(slug="demo", is_demo=True)])
    register_demo_project(tripl_api)
    demo_scans = tripl_api.scans("demo", [make_scan_config()])

    code, _ = run_doctor(capsys)
    assert code == 0
    # Asserted by absence: the demo project was never even read.
    assert not demo_scans.called

    code, document = run_doctor(capsys, "--include-demo")
    assert code == 0
    assert demo_scans.called


def test_missing_configuration_exits_two_without_issuing_a_request(
    tripl_api: FakeInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TRIPL_BASE_URL")

    assert main(["doctor"]) == 2
    assert tripl_api.router.calls.call_count == 0


def test_jobs_are_requested_at_the_api_maximum(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str], now: datetime
) -> None:
    """Deliberately the cap (200), NOT schedule.py's own 40-row window.

    The backend reads 40 because it only needs the backoff delay, which saturates
    at six failures. doctor answers "how long has this been broken", and at 40
    rows a 15m config failing for four days reports 40 failures since this
    morning instead of 384 since Monday. Sliding back to 40 - or to the API's
    default of 50 - silently misdates every long incident by days.
    """
    route = tripl_api.jobs("prod", "scan-1", [make_job(at=now, time_to=now)])

    main(["doctor", "--json"])
    capsys.readouterr()

    assert route.calls.last.request.url.params["limit"] == "200"


def test_check_ids_and_their_order_are_the_documented_list(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    _, document = run_doctor(capsys)

    assert [check["id"] for check in document["checks"]] == list(CHECK_IDS)
    assert CHECK_IDS == ("connectivity", "auth", "projects", "data_sources", "scans", "drifts")


def test_envelope_keys_are_present_and_typed(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    code, document = run_doctor(capsys)

    assert document["schema_version"] == 1
    assert isinstance(document["schema_version"], int)
    assert document["tool"] == "tripl"
    assert document["command"] == "doctor"
    assert isinstance(document["tool_version"], str)
    assert document["generated_at"].endswith("Z")
    assert isinstance(document["duration_ms"], int)
    assert isinstance(document["requests"], int) and document["requests"] > 0
    assert document["exit_code"] == code
    assert set(document["summary"]) == {"pass", "warn", "fail", "skip"}
    assert all(isinstance(value, int) for value in document["summary"].values())
    assert document["instance"] == {
        "base_url": BASE_URL,
        "base_url_source": "$TRIPL_BASE_URL",
        "api_key_source": "$TRIPL_API_KEY",
        "api_key_scope": "instance",
    }
    for check in document["checks"]:
        assert set(check) == {"id", "title", "status", "summary", "skip_reason", "findings"}
        assert (check["skip_reason"] is not None) == (check["status"] == "skip")


def test_a_stale_accepted_drift_is_a_warning_and_exits_zero(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str], now: datetime
) -> None:
    """An untriaged drift means nobody has looked yet, not that anything broke."""
    tripl_api.drifts(
        "prod",
        "et-1",
        [
            {
                "id": "drift-1",
                "event_type_id": "et-1",
                "field_name": "user_id",
                "drift_type": "missing_field",
                "status": "accepted",
                "detected_at": (now - timedelta(days=2)).isoformat(),
                "resolved_at": (now - timedelta(days=1)).isoformat(),
                "resolved_by": None,
                "snoozed_until": None,
            }
        ],
    )

    code, document = run_doctor(capsys)

    assert code == 0
    assert finding_codes(document, "drifts") == ["schema_field_deleted_by_accept"]
    assert check_by_id(document, "drifts")["status"] == "warn"
    # --strict is what makes a warning fail a run.
    assert main(["doctor", "--json", "--strict"]) == 3
    capsys.readouterr()


def test_a_drifts_404_is_reported_rather_than_read_as_no_drifts(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.drifts("prod", "et-1", None, status=404, payload={"detail": "Not found"})

    code, document = run_doctor(capsys)

    assert code == 3
    assert finding_codes(document, "drifts") == ["endpoint_unexpected_status"]
    finding = check_by_id(document, "drifts")["findings"][0]
    assert finding["evidence"]["status_code"] == 404
    assert finding["evidence"]["path"] == "/projects/prod/event-types/et-1/drifts"


def test_one_connection_pool_is_opened_for_the_whole_run(
    tripl_api: FakeInstance,
    tracking_pool: list[httpx.AsyncClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One pool per invocation, closed on the way out — however many reads it made.

    The direct analogue of mcp-server's test_stdio_lifespan_reuses_one_http_client.
    The unauthenticated /health probe is deliberately NOT this pool.
    """
    main(["doctor", "--json"])
    capsys.readouterr()

    assert len(tracking_pool) == 1
    assert tracking_pool[0].is_closed


def test_doctor_accepts_the_global_flags_after_the_subcommand(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every add_parser passes parents=[parent]; this is what that buys."""
    monkeypatch.delenv("TRIPL_BASE_URL")

    code, document = run_doctor(capsys, "--url", BASE_URL)

    assert code == 0
    assert document["instance"]["base_url_source"] == "--url"


def test_the_anomaly_signals_endpoint_is_never_called(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """TRAP 2, from doctor's side: no client-side significance threshold exists."""
    signals = tripl_api.router.get(f"{API_BASE}/projects/prod/anomalies/signals").mock(
        return_value=httpx.Response(200, json=[])
    )

    main(["doctor", "--json"])
    capsys.readouterr()

    assert not signals.called


def test_a_200_that_is_not_json_produces_a_report_not_a_traceback(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """An SSO gateway or captive portal answers 200 with an HTML login page.

    json.JSONDecodeError subclasses ValueError, not httpx.HTTPError, so before
    this it escaped TriplClient, the Fetched regime, gather_bounded and main()'s
    `except TriplError` and killed the process with a traceback - breaking the
    one contract doctor makes, that it always emits a parseable document.
    """
    tripl_api.router.get(f"{API_BASE}/projects").mock(
        return_value=httpx.Response(200, text="<html>corporate sign-in</html>")
    )

    code, document = run_doctor(capsys)

    assert code == 3
    assert "endpoint_unexpected_status" in finding_codes(document, "projects")


def test_the_drift_budget_is_spread_across_projects_not_spent_in_order(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """A budget spent in project order can starve a later project to zero reads.

    The starved project appears nowhere in the output - the summary reads "N of M
    event types examined" as though coverage were even. If that project is the
    one holding the accepted missing_field drift that deleted a FieldDefinition,
    the check written to surface exactly that reports nothing and says nothing
    about not having looked.
    """
    tripl_api.projects([make_project("alpha"), make_project("beta")])
    for slug, type_ids in (("alpha", ["et-1", "et-2", "et-3"]), ("beta", ["et-9"])):
        tripl_api.project(slug, make_project(slug=slug))
        tripl_api.scans(slug, [])
        tripl_api.event_types(slug, [make_event_type(type_id) for type_id in type_ids])
        tripl_api.coverage(slug)
        for type_id in type_ids:
            tripl_api.drifts(slug, type_id, [])

    main(["doctor", "--json", "--max-event-types", "2"])
    capsys.readouterr()

    requested = {
        call.request.url.path
        for call in tripl_api.router.calls
        if call.request.url.path.endswith("/drifts")
    }
    assert any("/projects/beta/" in path for path in requested), (
        f"beta received no drift read at all: {sorted(requested)}"
    )


def test_truncation_names_the_partly_examined_project_and_spares_the_complete_one(
    tripl_api: FakeInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    """An instance-wide "2 of 4 examined" points at no project at all.

    Spreading the budget keeps every project in the output, but it also lands
    unevenly by construction: here beta's one event type fits and alpha is read a
    third of the way. If alpha's unread part holds the accepted missing_field
    drift that deleted a FieldDefinition, "we did not look there" has to name
    alpha - and must not smear beta, which was read in full.
    """
    tripl_api.projects([make_project("alpha"), make_project("beta")])
    for slug, type_ids in (("alpha", ["et-1", "et-2", "et-3"]), ("beta", ["et-9"])):
        tripl_api.project(slug, make_project(slug=slug))
        tripl_api.scans(slug, [])
        tripl_api.event_types(slug, [make_event_type(type_id) for type_id in type_ids])
        tripl_api.coverage(slug)
        for type_id in type_ids:
            tripl_api.drifts(slug, type_id, [])

    _, document = run_doctor(capsys, "--max-event-types", "2")

    truncations = [
        finding
        for finding in check_by_id(document, "drifts")["findings"]
        if finding["code"] == "drift_scan_truncated"
    ]
    assert [(finding["project"], finding["evidence"]) for finding in truncations] == [
        ("alpha", {"project": "alpha", "examined": 1, "total": 3})
    ]
