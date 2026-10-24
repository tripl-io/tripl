"""Golden samples of the human output, byte for byte.

These are the operator-facing contract - what lands in `tee incident.log` and
gets pasted into a ticket - and they are pinned so a "harmless" reformat is a
review conversation rather than a surprise. ASCII only, and identical in a pipe
and on a TTY, which is what makes a single expected string correct at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tripl_cli.check import inventory as inventory_module
from tripl_cli.check.gate import apply_gate
from tripl_cli.check.model import (
    GATE_REV,
    MODE_STATIC,
    CheckFinding,
    CheckItem,
    CheckReport,
    CheckResult,
    GateInfo,
    Marker,
    MarkerState,
    Origin,
    PlanBranchComparison,
    Suppression,
)
from tripl_cli.check.render import render_text
from tripl_cli.cli import main
from tripl_cli.model import Instance, Run

from .conftest import (
    FakeInstance,
    make_annotation,
    make_branch,
    make_drift,
    make_event,
    make_event_type,
    make_field,
    make_job,
    make_scan_config,
    make_search_result,
    make_variable,
)

_DETECTED = datetime(2026, 7, 28, 4, 10, tzinfo=UTC)
# A snooze deadline has to be relative to now. `is_untriaged` counts a snooze
# that has RUN OUT, so a hardcoded date is a time bomb: this fixture said
# 2026-08-04, read "1 untriaged" until that morning, and then failed CI on every
# open branch at once — including ones that had not touched the CLI.
_STILL_SNOOZED = datetime.now(UTC).replace(microsecond=0) + timedelta(days=30)
_SEEN = datetime(2026, 7, 31, 19, 0, tzinfo=UTC)


def test_scans_list_sample(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.scans(
        "prod",
        [
            make_scan_config(),
            make_scan_config("scan-2", name="ad-hoc backfill", interval=None),
        ],
    )
    assert main(["scans", "list"]) == 0
    assert capsys.readouterr().out == (
        "tripl scans list - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        "prod (Prod)\n"
        "  scan-1  prod events      1h  event_time  scheduled\n"
        "  scan-2  ad-hoc backfill  -   event_time  not scheduled (no interval)\n"
        "\n"
        "2 scan configs in 1 project.\n"
    )


def test_scans_jobs_sample(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stamp = datetime(2026, 7, 31, 19, 0, tzinfo=UTC)
    tripl_api.jobs("prod", "scan-1", [make_job(job_id="job-91c2", at=stamp)])
    assert main(["scans", "jobs", "prod events", "--project", "prod", "--limit", "2"]) == 0
    assert capsys.readouterr().out == (
        "tripl scans jobs - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        "prod 'prod events' (scan-1), newest 2 jobs requested:\n"
        # `Z`, not `+00:00`: these come off ScanJobResponse, which pydantic
        # serialises with a `Z`. A sample pinning the other spelling would be a
        # promise about bytes no instance sends.
        "  job-91c2  completed  created 2026-07-31T19:00:00Z  "
        "finished 2026-07-31T19:00:00Z\n"
        "\n"
        "1 job.\n"
    )


def test_scans_run_sample(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.scan_run("prod", "scan-1")
    assert main(["scans", "run", "prod events", "--project", "prod"]) == 0
    assert capsys.readouterr().out == (
        "tripl scans run - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        "prod 'prod events' (scan-1): started job job-91c2 (pending).\n"
        "Follow it with: tripl watch --project prod --scan 'prod events'\n"
    )


def test_scans_cancel_sample(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.job_cancel("prod", "scan-1", "job-91c2")
    assert main(["scans", "cancel", "scan-1", "job-91c2", "--project", "prod", "--yes"]) == 0
    assert capsys.readouterr().out == (
        "tripl scans cancel - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        "prod 'prod events' (scan-1): job job-91c2 is now cancelled.\n"
    )


def test_drifts_list_sample_with_an_unreadable_event_type(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Includes the ``unavailable`` line, because that is the case worth pinning."""
    tripl_api.event_types(
        "prod",
        [make_event_type("et-1"), make_event_type("et-2", "app.purchase"), make_event_type("et-9")],
    )
    tripl_api.drifts("prod", "et-1", [make_drift(drift_id="drift-1", detected_at=_DETECTED)])
    tripl_api.drifts(
        "prod",
        "et-2",
        [
            make_drift(
                drift_id="drift-7",
                field_name="amount",
                drift_type="type_changed",
                status="snoozed",
                snoozed_until=_STILL_SNOOZED,
                detected_at=_DETECTED,
            )
        ],
    )
    tripl_api.drifts("prod", "et-9", None, status=404, payload={"detail": "Event type not found"})
    assert main(["drifts", "list", "--status", "all"]) == 1
    assert capsys.readouterr().out == (
        "tripl drifts list - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        "prod (Prod)\n"
        "  drift-1  app.screen_view.user_id  missing_field  open     "
        "detected 2026-07-28T04:10:00Z\n"
        "  drift-7  app.purchase.amount      type_changed   snoozed  "
        f"until {_STILL_SNOOZED.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        "  /projects/prod/event-types/et-9/drifts: unavailable "
        "(Not found (404): Event type not found)\n"
        "\n"
        "2 drifts in 1 project, 1 untriaged.\n"
        "1 read failed; the list above is incomplete.\n"
    )


def test_drifts_dismiss_sample(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.drift_action("prod", "drift-1")
    assert main(["drifts", "dismiss", "drift-1", "--project", "prod", "--yes"]) == 0
    assert capsys.readouterr().out == (
        "tripl drifts dismiss - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        "prod: drift drift-1 (user_id, missing_field) is now false_positive.\n"
    )


def test_annotate_samples(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both answers, because the difference between them is the whole point."""
    argv = ["annotate", "Deployed web 2026.09.25", "--project", "prod"]
    tripl_api.annotate("prod")
    assert main(argv) == 0
    assert capsys.readouterr().out == (
        "tripl annotate - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        "prod: annotated 'Deployed web 2026.09.25' at 2026-09-25T14:02:00Z (ann-1).\n"
    )
    tripl_api.annotate("prod", make_annotation(), status=200)
    assert main(argv) == 0
    assert capsys.readouterr().out == (
        "tripl annotate - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        "prod: annotation 'Deployed web 2026.09.25' already exists (ann-1, at "
        "2026-09-25T14:02:00Z); the API de-duplicated it and nothing new was created.\n"
    )


def test_dry_run_sample(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["scans", "run", "scan-1", "--project", "prod", "--dry-run"]) == 0
    assert capsys.readouterr().out == (
        "tripl scans run - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        "dry run: would send POST /projects/prod/scans/scan-1/run with body None\n"
        "Nothing was sent.\n"
    )


def test_events_list_sample(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Includes the truncation line, because that is the case worth pinning.

    A page that filled up and a catalog that ended print the same rows; the
    footer is the only thing that tells them apart, and it names both flags
    that would fetch the rest.
    """
    tripl_api.events(
        "prod",
        [
            make_event(last_seen_at=_SEEN, drift_count=2),
            make_event("evt-2", name="app.purchase.completed", status="draft"),
        ],
        total=412,
    )
    assert main(["events", "list", "--project", "prod", "--limit", "2"]) == 0
    assert capsys.readouterr().out == (
        "tripl events list - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        # No branch named means the live main plan. The API has no id for main
        # and spells it by omitting `?branch=`, so the output does too.
        "prod\n"
        "  evt-1  app.screen_view.viewed  live   seen 2026-07-31T19:00:00Z  2 drifts\n"
        "  evt-2  app.purchase.completed  draft  never seen\n"
        "\n"
        "2 of 412 events shown; raise --limit or pass --offset to read the rest.\n"
    )


def test_events_show_sample(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.event(
        "prod",
        "evt-1",
        make_event(
            last_seen_at=_SEEN,
            tags=["checkout"],
            field_values=[
                {"id": "fv-1", "field_definition_id": "fd-1", "value": "checkout"},
                {"id": "fv-2", "field_definition_id": "fd-2", "value": "${cart_value}"},
            ],
            meta_values=[{"id": "mv-1", "meta_field_definition_id": "mf-1", "value": "TRIPL-412"}],
        ),
    )
    tripl_api.fields(
        "prod", "et-1", [make_field(), make_field("fd-2", name="cart_value", field_type="number")]
    )
    assert main(["events", "show", "evt-1", "--project", "prod"]) == 0
    assert capsys.readouterr().out == (
        "tripl events show - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        "prod\n"
        "  evt-1  app.screen_view.viewed\n"
        "    status       live\n"
        "    reviewed     yes\n"
        "    event type   app.screen_view (et-1)\n"
        "    tags         checkout\n"
        "    last seen    2026-07-31T19:00:00Z\n"
        "    sunset       -\n"
        "    drifts       0\n"
        "    description  -\n"
        # The second read's whole purpose: field_definition_id -> name.
        "    fields\n"
        "      screen_name  checkout\n"
        "      cart_value   ${cart_value}\n"
        # And its limit, stated rather than hidden: there is no meta-field
        # builder in tripl_cli.api, so these keep their definition ids.
        "    meta (by definition id)\n"
        "      mf-1  TRIPL-412\n"
    )


def test_plan_fields_sample_on_a_branch(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The one sample that names a branch, because the scope line only appears then."""
    tripl_api.branches("prod", [make_branch()])
    tripl_api.event_types("prod", [make_event_type()])
    tripl_api.fields(
        "prod",
        "et-1",
        [
            make_field(sensitivity="pii", enum_options=["checkout", "cart"]),
            make_field("fd-2", name="cart_value", field_type="number", is_required=False),
        ],
    )
    assert (
        main(
            [
                "plan",
                "fields",
                "app.screen_view",
                "--project",
                "prod",
                "--branch",
                "checkout-redesign",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == (
        "tripl plan fields - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        "prod (branch 'checkout-redesign')\n"
        "  fd-1  screen_name  string  required  pii   enum: checkout|cart\n"
        "  fd-2  cart_value   number  optional  none\n"
        "\n"
        "2 fields.\n"
    )


def test_plan_branches_sample(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Note the footer: ``2 branches``, not ``2 branchs``."""
    tripl_api.branches(
        "prod",
        [
            make_branch(
                "b-0001", name="main", kind="main", status="merged", ahead=None, behind_base=None
            ),
            make_branch(behind_base=True),
        ],
    )
    assert main(["plan", "branches", "--project", "prod"]) == 0
    assert capsys.readouterr().out == (
        "tripl plan branches - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        "prod\n"
        "  b-0001  main               main     merged  -\n"
        "  b-9f21  checkout-redesign  working  draft   3 ahead  behind base\n"
        "\n"
        "2 branches.\n"
    )


def test_plan_search_sample(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Search is capped by ``--limit`` alone: the route has no offset parameter.

    The body is what a CURRENT instance sends, so the warning below is the
    route's own ``truncated`` rather than the CLI's page-fullness guess.
    One row against ``--limit 1`` renders identically either
    way, which is precisely why the canonical sample has to say which rung it
    means: the documentation quotes this output under a paragraph that credits
    the route. The instance that predates the key keeps its own test in
    ``test_plan_cmd.py``.
    """
    tripl_api.search("prod", [make_search_result()], semantic_used=True, truncated=True)
    assert main(["plan", "search", "purchase", "--project", "prod", "--limit", "1"]) == 0
    assert capsys.readouterr().out == (
        "tripl plan search - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        "prod\n"
        "  event  evt-1  app.screen_view.viewed  Screen View  0.92\n"
        "\n"
        "1 search result shown — the most this page holds, "
        "and more may have matched; raise --limit.\n"
    )


def test_plan_properties_sample(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.variables("prod", [make_variable(open_drift_count=1)], total=3)
    assert main(["plan", "properties", "--project", "prod", "--limit", "1"]) == 0
    assert capsys.readouterr().out == (
        "tripl plan properties - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        "prod\n"
        "  var-1  cart_value  number  12 events  1 open drift\n"
        "\n"
        "1 of 3 properties shown; raise --limit or pass --offset to read the rest.\n"
    )
    # The former name still works, and says which command ran.
    assert main(["plan", "variables", "--project", "prod", "--limit", "1"]) == 0
    assert capsys.readouterr().out.startswith("tripl plan properties - ")


def test_an_empty_plan_read_says_so_rather_than_printing_nothing(
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``(no event types)`` plus ``0 event types.`` — never a blank body.

    A command that printed nothing at all would be indistinguishable from one
    that crashed before its output, which is the ambiguity the whole ASCII
    contract exists to remove.
    """
    tripl_api.event_types("prod", [])
    assert main(["plan", "types", "--project", "prod"]) == 0
    assert capsys.readouterr().out == (
        "tripl plan types - http://tripl.test (from $TRIPL_BASE_URL)\n"
        "\n"
        "prod\n"
        "  (no event types)\n"
        "\n"
        "0 event types.\n"
    )


def test_install_dry_run_sample(
    install_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The provisioning plan, byte for byte.

    Everything except the directory is fixed: the path is a tmp_path, so it is
    interpolated rather than pinned, and every other byte - including the two
    `+ cd ... && docker compose ...` lines an operator is invited to paste - is
    the contract.
    """
    assert (
        main(
            [
                "install",
                "--app-url",
                "https://tripl.example.com",
                "--dir",
                str(install_dir),
                "--dry-run",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == (
        f"tripl install - {install_dir}\n"
        "\n"
        "app url  https://tripl.example.com\n"
        "image    ghcr.io/tripl-io/tripl:latest\n"
        "\n"
        "files\n"
        "  compose.yaml                  create  0644\n"
        "  infra/rabbitmq/rabbitmq.conf  create  0644\n"
        "  .env                          create  0600\n"
        "  generated into .env, values never printed: ENCRYPTION_KEY, SECRET_KEY, "
        "POSTGRES_PASSWORD, RABBITMQ_PASSWORD\n"
        "\n"
        "commands\n"
        f"  + cd {install_dir} && docker compose pull\n"
        f"  + cd {install_dir} && docker compose up -d\n"
        "  then poll https://tripl.example.com/health for up to 300s\n"
        "\n"
        "dry run: nothing was written and nothing was run.\n"
    )


def test_upgrade_dry_run_sample(
    install_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Note the env assignment sits INSIDE the `cd`, so the line pastes correctly."""
    install_dir.mkdir(parents=True)
    (install_dir / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (install_dir / ".env").write_text(
        "APP_BASE_URL=https://tripl.example.com\nTRIPL_VERSION=1.4.0\n", encoding="utf-8"
    )

    assert main(["upgrade", "--to", "1.5.0", "--dir", str(install_dir), "--dry-run"]) == 0
    assert capsys.readouterr().out == (
        f"tripl upgrade - {install_dir}\n"
        "\n"
        "from  1.4.0\n"
        "to    1.5.0\n"
        "\n"
        "commands\n"
        f"  + cd {install_dir} && TRIPL_VERSION=1.5.0 docker compose pull\n"
        f"  + cd {install_dir} && docker compose up -d\n"
        "  the 1.5.0 pin is written to .env between the two, after the pull succeeds\n"
        "  then poll /health for up to 300s\n"
        "\n"
        "dry run: nothing was written and nothing was run.\n"
    )


_CHECK_LIST_CONFIG = """\
project: prod
sources: ["app/**"]
event_types:
  event:
    calls: [{function: "analytics.logEvent", positional: [name, properties]}]
"""
# One call of each kind `check --list` explains, ASCII through and through, so
# any non-ASCII byte in its output is the tool's own text.
_CHECK_LIST_SOURCE = """\
object Events { object Name { const val Open = "open" }; const val PROMO = "promo_%s_shown" }
class Screen {
    fun logEvent(event: String, params: Bundle) { analytics.logEvent(event, params) }
    fun onAction(action: UiAction) { analytics.logEvent(action.event) }
    fun refresh(flag: Boolean) {
        val log = if (flag) Events.Name.Open else "closed"
        analytics.logEvent(log, "not a dictionary", extra)
    }
    fun promo(id: String) { analytics.logEvent(Events.PROMO.format(id), mapOf("id" to id)) }
    fun show() {
        analytics.logEvent()
        logEvent("home_shown", bundle)
        wrap(Events.Name.Open)
    }
}
"""


def test_every_sample_is_ascii(
    tripl_api: FakeInstance,
    configured_env: None,
    install_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No unicode anywhere in the new commands' human output."""
    tripl_api.scan_run("prod", "scan-1")
    install_dir.mkdir(parents=True)
    (install_dir / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (install_dir / ".env").write_text("TRIPL_VERSION=1.4.0\n", encoding="utf-8")
    checkout = tmp_path / "checkout"
    (checkout / ".tripl").mkdir(parents=True)
    (checkout / "app").mkdir()
    (checkout / ".tripl" / "check.yml").write_text(_CHECK_LIST_CONFIG, encoding="utf-8")
    (checkout / "app" / "Screen.kt").write_text(_CHECK_LIST_SOURCE, encoding="utf-8")
    # `check --list` with no check config, from a checkout with more source
    # files than the (lowered) cap: the discovery text and its note.
    discovery = tmp_path / "discovery"
    (discovery / ".git").mkdir(parents=True)
    (discovery / "web").mkdir()
    for name in ("a.ts", "b.ts"):
        (discovery / "web" / name).write_text('analytics.track("Opened");\n', encoding="utf-8")
    monkeypatch.setattr(inventory_module, "MAX_DISCOVERY_FILES", 1)
    monkeypatch.chdir(discovery)
    for argv in (
        ["scans", "list"],
        ["scans", "jobs", "scan-1", "--project", "prod"],
        ["scans", "run", "scan-1", "--project", "prod"],
        ["drifts", "list"],
        ["events", "list", "--project", "prod"],
        ["events", "show", "evt-1", "--project", "prod"],
        ["plan", "types", "--project", "prod"],
        ["plan", "fields", "app.screen_view", "--project", "prod"],
        ["plan", "variables", "--project", "prod"],
        ["plan", "branches", "--project", "prod"],
        # `${cart_value}` and a quoted branch name are the two places a
        # non-ASCII character would plausibly arrive from author-written text.
        ["plan", "search", "purchase", "--project", "prod"],
        [
            "install",
            "--app-url",
            "https://tripl.example.com",
            "--dir",
            str(install_dir),
            "--dry-run",
        ],
        ["upgrade", "--to", "1.5.0", "--dir", str(install_dir), "--dry-run"],
        ["check", "--list", "--check-config", str(checkout / ".tripl" / "check.yml")],
        ["check", "--list"],
    ):
        main(argv)
        out = capsys.readouterr().out
        assert out.isascii(), f"{argv} emitted non-ASCII output"
    # Not vacuous for the listing: its fixture reaches every section, and the
    # section of calls that look like tracking has rows, not just its heading.
    assert main(["check", "--list", "--check-config", str(checkout / ".tripl" / "check.yml")]) == 0
    listing = capsys.readouterr().out
    for section in ("Why (call sites", "Functions that forward", "Calls that look like tracking"):
        assert section in listing
    assert "  1  wrap      constant  app/Screen.kt:13:9  wrap(Events.Name.Open)\n" in listing
    # Nor for discovery: it stopped at the cap, and said so.
    assert main(["check", "--list"]) == 0
    listing = capsys.readouterr().out
    for note in ("No check config found: ", "Discovery stopped at 1 source file; "):
        assert note in listing


def test_check_text_sample_with_suppression() -> None:
    """A suppressed finding is counted, not listed; marker problems follow the tally."""

    def result(path: str, line: int, name: str, waiver: Suppression | None) -> CheckResult:
        item = CheckItem(
            origin=Origin(path, line, 9, line, 40, "analytics.track"),
            event_type="event",
            name=name,
            fields={},
            properties=None,
            complete=False,
        )
        finding = CheckFinding(
            "unknown_event", "error", f"no event '{name}' in the plan", suppression=waiver
        )
        return CheckResult(item=item, status="error", findings=(finding,))

    waiver = Suppression("app/src/Debug.kt", 6, 8, (("unknown_event", None),), "debug menu only")
    used = Marker(
        "app/src/Debug.kt",
        6,
        8,
        "tripl-check-ignore[unknown_event] -- debug menu only",
        waiver.targets,
        waiver.reason,
    )
    reasonless = Marker(
        "app/src/Debug.kt",
        9,
        8,
        "tripl-check-ignore[unknown_event]",
        waiver.targets,
        None,
        "no_reason",
    )
    unused = Marker(
        "app/src/Old.kt",
        3,
        4,
        "tripl-check-ignore[unknown_event] -- fixed",
        waiver.targets,
        "fixed",
    )
    report = CheckReport(
        run=Run(
            instance=Instance("http://tripl.test", "$TRIPL_BASE_URL", "$TRIPL_API_KEY", "prod"),
            generated_at=_SEEN,
            duration_ms=10,
            requests=1,
        ),
        project="prod",
        mode=MODE_STATIC,
        strict=False,
        results=(
            result("app/src/Checkout.kt", 42, "checkout_startd", None),
            result("app/src/Debug.kt", 7, "debug_tap", waiver),
            result("app/src/Debug.kt", 10, "debug_long_press", None),
        ),
        files_scanned=3,
        markers=(
            MarkerState(used, True),
            MarkerState(reasonless, False),
            MarkerState(unused, False),
        ),
    )
    assert render_text(report) == (
        "app/src/Checkout.kt:42:9  error  event  checkout_startd\n"
        "    error  unknown_event: no event 'checkout_startd' in the plan\n"
        "app/src/Debug.kt:10:9  error  event  debug_long_press\n"
        "    error  unknown_event: no event 'debug_long_press' in the plan\n"
        "\n"
        "3 call sites in 3 files: 0 ok, 0 warnings, 3 errors; 1 finding suppressed.\n"
        'Suppression ignored: app/src/Debug.kt:9 tripl-check-ignore needs "-- <reason>".\n'
        "Unused suppression: app/src/Old.kt:3 tripl-check-ignore[unknown_event] matched no finding."
    )
    assert report.exit_code == 1


def test_check_text_sample_with_a_gate() -> None:
    """Under a gate the listing shows what the change brought in; the rest is counted."""

    def result(path: str, line: int, name: str, status: str = "error") -> CheckResult:
        item = CheckItem(
            origin=Origin(path, line, 9, line, 40, "analytics.track"),
            event_type="event",
            name=name,
            fields={},
            properties=None,
            complete=False,
        )
        code = "unknown_event" if status == "error" else "unknown_field"
        finding = CheckFinding(code, status, f"no event '{name}' in the plan")
        return CheckResult(item=item, status=status, findings=(finding,))

    reasonless = Marker(
        "app/src/Debug.kt",
        9,
        8,
        "tripl-check-ignore[unknown_event]",
        (("unknown_event", None),),
        None,
        "no_reason",
    )
    unused = Marker(
        "app/src/Checkout.kt",
        3,
        4,
        "tripl-check-ignore[unknown_event] -- fixed",
        (("unknown_event", None),),
        "fixed",
    )
    results = (
        result("app/src/Checkout.kt", 42, "checkout_startd"),
        result("app/src/Home.kt", 7, "home_shown"),
        result("app/src/Home.kt", 9, "home_left", "warning"),
        result("app/src/Cart.kt", 12, "cart_opened"),
    )
    report = CheckReport(
        run=Run(
            instance=Instance("http://tripl.test", "$TRIPL_BASE_URL", "$TRIPL_API_KEY", "prod"),
            generated_at=_SEEN,
            duration_ms=10,
            requests=3,
        ),
        project="prod",
        mode=MODE_STATIC,
        strict=False,
        results=apply_gate(results, (True, False, False, False), {3: (True,)}),
        branch_id="b-1",
        branch_name="checkout-v2",
        files_scanned=3,
        markers=(MarkerState(reasonless, False), MarkerState(unused, False)),
        gate=GateInfo(
            source=GATE_REV,
            files=5,
            paths=frozenset({"app/src/Checkout.kt"}),
            rev="1a2b3c4d5e6f7a8b",
            base="1a2b3c4d5e6f7a8b9c0d",
            plan_branch=PlanBranchComparison(compared=2, caused=1),
        ),
    )
    assert render_text(report) == (
        "app/src/Checkout.kt:42:9  error  event  checkout_startd\n"
        "    error  unknown_event: no event 'checkout_startd' in the plan\n"
        "app/src/Cart.kt:12:9  error  event  cart_opened\n"
        "    error  unknown_event: no event 'cart_opened' in the plan\n"
        "\n"
        "4 call sites in 3 files: 0 ok, 1 warning, 3 errors.\n"
        "Gate: changes since 1a2b3c4d5e6f (5 files, 1 call site new or changed). "
        "2 call sites fail the run; 1 error and 1 warning are not new in this change.\n"
        "Plan branch checkout-v2: 1 finding caused by the branch "
        "(2 call sites also checked against main).\n"
        "Unused suppression: app/src/Checkout.kt:3 tripl-check-ignore[unknown_event] "
        "matched no finding.\n"
        "Suppressions outside this change: 1 ignored (listed in --json and SARIF)."
    )
    assert report.exit_code == 1
