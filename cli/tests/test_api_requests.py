"""The shared request layer, tested as pure functions.

No network and no ``main([...])`` here on purpose: everything in ``tripl_cli.api``
is a value or a total function over an already-decoded payload, which is exactly
what makes it safe for two distributions to share (tripl-ey6j.5).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tripl_cli.api import auth as auth_api
from tripl_cli.api import (
    branches,
    chart_annotations,
    data_sources,
    event_types,
    monitoring,
    projects,
    scans,
)
from tripl_cli.api import events as events_api
from tripl_cli.api import search as search_api
from tripl_cli.api import variables as variables_api
from tripl_cli.api.endpoints import ALL_TEMPLATES
from tripl_cli.api.request import ApiRequest

API_PACKAGE = Path(scans.__file__).parent


def _templated(path: str) -> str:
    """Undo the ``.format()`` a builder did, so the template is comparable."""
    for concrete, placeholder in (
        ("prod", "{slug}"),
        ("scan-1", "{scan_id}"),
        ("job-9", "{job_id}"),
        ("et-1", "{event_type_id}"),
        ("drift-1", "{drift_id}"),
        ("evt-1", "{event_id}"),
        ("var-1", "{variable_id}"),
        ("br-1", "{branch_id}"),
    ):
        path = path.replace(concrete, placeholder)
    return path


def _every_builder() -> list[ApiRequest]:
    return [
        projects.list_projects(),
        projects.get_project("prod"),
        auth_api.get_me(),
        auth_api.get_status(),
        data_sources.list_data_sources(),
        scans.list_configs("prod"),
        scans.get_config("prod", "scan-1"),
        scans.run("prod", "scan-1"),
        scans.replay_metrics("prod", "scan-1", {"time_from": "x"}),
        scans.list_jobs("prod", "scan-1"),
        scans.get_job("prod", "scan-1", "job-9"),
        scans.cancel_job("prod", "scan-1", "job-9"),
        event_types.list_event_types("prod"),
        event_types.get_event_type("prod", "et-1"),
        event_types.get_fields("prod", "et-1"),
        event_types.list_drifts("prod", "et-1"),
        event_types.apply_drift_action("prod", "drift-1", action="false_positive"),
        events_api.list_events("prod"),
        events_api.get_event("prod", "evt-1"),
        events_api.create_event("prod", {}),
        events_api.update_event("prod", "evt-1", {}),
        variables_api.list_variables("prod"),
        variables_api.get_values("prod", "var-1"),
        variables_api.get_event_overrides("prod", "var-1"),
        branches.list_branches("prod"),
        branches.get_branch("prod", "br-1"),
        branches.get_diff("prod", "br-1"),
        search_api.search_plan("prod", "checkout"),
        monitoring.get_monitors_summary("prod"),
        monitoring.list_signals("prod"),
        monitoring.list_alert_deliveries("prod"),
        monitoring.get_coverage("prod"),
        monitoring.list_dead_events("prod"),
        monitoring.list_shadow_events("prod"),
        chart_annotations.create_annotation(
            "prod", label="Deployed", at=datetime(2026, 9, 25, tzinfo=UTC)
        ),
    ]


def test_every_builder_returns_a_declared_template() -> None:
    """A builder cannot reach a path the contract test does not watch."""
    undeclared = [
        request.path
        for request in _every_builder()
        if _templated(request.path) not in ALL_TEMPLATES
    ]
    assert not undeclared, f"builders produce paths absent from ALL_TEMPLATES: {undeclared}"


def test_every_declared_template_has_a_builder() -> None:
    """And the reverse: a declared template nobody can build is dead weight."""
    built = {_templated(request.path) for request in _every_builder()}
    assert ALL_TEMPLATES - built == set()


def test_none_query_parameters_are_omitted() -> None:
    """``limit=None`` means "leave it off", not "send the default".

    That distinction is load-bearing: tripl-mcp takes the server's own default of
    50, doctor asks for 200 because it is measuring a streak, and watch asks for
    a smaller window because it repeats - three answers through one builder.
    """
    assert scans.list_jobs("prod", "scan-1").params == {"limit": None}
    assert scans.list_jobs("prod", "scan-1", limit=200).params == {"limit": 200}


def test_annotation_body_always_says_source_api_and_omits_unset_members() -> None:
    """``release`` is the worker's and ``manual`` the app's; a client sends ``api``."""
    at = datetime(2026, 9, 25, 14, 2, 30, 999, tzinfo=UTC)
    plain = chart_annotations.create_annotation("prod", label="Deployed", at=at)
    assert plain.method == "POST"
    assert plain.path == "/projects/prod/annotations"
    assert plain.json_body == {
        "label": "Deployed",
        "bucket": "2026-09-25T14:02:30Z",
        "source": "api",
    }

    full = chart_annotations.create_annotation(
        "prod",
        label="Deployed",
        at=at,
        url="https://example.com/r/1",
        description="web",
        scope_type="event_type",
        scope_ref="et-1",
    )
    assert full.json_body == {
        "label": "Deployed",
        "bucket": "2026-09-25T14:02:30Z",
        "source": "api",
        "url": "https://example.com/r/1",
        "description": "web",
        "scope_type": "event_type",
        "scope_ref": "et-1",
    }


def test_drift_action_body_omits_unset_members() -> None:
    plain = event_types.apply_drift_action("prod", "drift-1", action="false_positive")
    assert plain.json_body == {"action": "false_positive"}
    assert plain.method == "POST"
    assert plain.path == "/projects/prod/event-types/drifts/drift-1/actions"

    when = datetime(2026, 8, 4, tzinfo=UTC)
    full = event_types.apply_drift_action(
        "prod", "drift-1", action="snooze", note="known", snoozed_until=when
    )
    assert full.json_body == {
        "action": "snooze",
        "note": "known",
        "snoozed_until": "2026-08-04T00:00:00Z",
    }


def test_drift_items_reads_the_items_envelope() -> None:
    assert event_types.drift_items({"items": [{"id": "d1"}], "total": 1}) == [{"id": "d1"}]
    # A payload with no items is EMPTY, not an error - the caller must already
    # have established that the read succeeded (TRAP 3 lives one layer up).
    assert event_types.drift_items({"total": 0}) == []
    assert event_types.drift_items([]) == []
    assert event_types.drift_items(None) == []


def test_field_count_reads_a_null_field_definitions_as_zero() -> None:
    """The one derived fact both surfaces put in a table (tripl-i1dt).

    ``field_definitions`` is nullable on the wire, and the two spellings of this
    count - the CLI's table and the MCP's ``list_event_types`` - each had to
    remember the ``or []``. Now one does.
    """
    assert event_types.field_count({"field_definitions": [{"id": "f1"}, {"id": "f2"}]}) == 2
    assert event_types.field_count({"field_definitions": None}) == 0
    assert event_types.field_count({}) == 0


def test_semantic_used_reads_an_absent_flag_as_false_rather_than_unknown() -> None:
    """``SearchResponse.semantic_used`` is declared ``bool = False`` server-side.

    So a body without the key is the route SAYING the index did not answer, not
    the route declining to say. An agent handed ``null`` there would have no way
    to read the confidences it was also handed.
    """
    assert search_api.semantic_used({"items": [], "total": 0, "semantic_used": True}) is True
    assert search_api.semantic_used({"items": [], "total": 0, "semantic_used": False}) is False
    assert search_api.semantic_used({"items": [], "total": 0}) is False
    assert search_api.semantic_used(None) is False

    # Junk is not the route making the claim, so it reads as False too. Under
    # bool() the string "false" came out True, which inverts what every score in
    # the payload means: a low confidence is ordinary under a semantic match and
    # damning under a substring fallback (Copilot, PR #78).
    junk_values: tuple[object, ...] = ("false", "true", 1, 0, [], {}, "yes")
    for junk in junk_values:
        assert search_api.semantic_used({"items": [], "total": 0, "semantic_used": junk}) is False


def test_truncated_reads_an_absent_flag_as_false_rather_than_unknown() -> None:
    """The second envelope fact ``/search`` adds, read the same way (tripl-wkwv.3).

    ``total`` on this route is ``len(items)``, so it equals ``limit`` on any full
    page and cannot say whether ranked hits were dropped. ``truncated`` can, and
    the backend declares it ``bool = False`` — so a body without the key IS the
    route saying nothing was dropped, not the route declining to say. Junk takes
    the absent path for the same reason it does above: under ``bool()`` the
    string ``"false"`` would report a clipped answer on a complete one.
    """
    assert search_api.truncated({"items": [], "total": 0, "truncated": True}) is True
    assert search_api.truncated({"items": [], "total": 0, "truncated": False}) is False
    assert search_api.truncated({"items": [], "total": 0}) is False
    assert search_api.truncated(None) is False

    junk_values: tuple[object, ...] = ("false", "true", 1, 0, [], {}, "yes")
    for junk in junk_values:
        assert search_api.truncated({"items": [], "total": 0, "truncated": junk}) is False


def test_reported_truncated_separates_a_silent_route_from_one_saying_false() -> None:
    """The distinction ``truncated`` cannot make, for the consumer that needs it.

    tripl-wkwv.3. ``tripl plan search`` uses this as the top rung of a ladder
    whose bottom rung is "the page filled". Collapsed to a bool, an instance
    that predates the field would read as the route saying nothing was dropped,
    and the guess that exists for exactly that instance would never run. The MCP
    republishes the fact instead of deciding from it, so it keeps the bool.
    """
    assert search_api.reported_truncated({"items": [], "total": 0, "truncated": True}) is True
    assert search_api.reported_truncated({"items": [], "total": 0, "truncated": False}) is False
    assert search_api.reported_truncated({"items": [], "total": 0}) is None
    assert search_api.reported_truncated(None) is None

    # Junk is not the route making the claim either, so it falls through to the
    # guess rather than being believed.
    junk_values: tuple[object, ...] = ("false", "true", 1, 0, [], {}, "yes")
    for junk in junk_values:
        assert search_api.reported_truncated({"items": [], "total": 0, "truncated": junk}) is None


def test_is_untriaged_matrix() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    later = (now + timedelta(days=3)).isoformat()
    earlier = (now - timedelta(days=3)).isoformat()
    assert event_types.is_untriaged({"status": "open"}, now) is True
    assert event_types.is_untriaged({"status": "snoozed", "snoozed_until": later}, now) is False
    assert event_types.is_untriaged({"status": "snoozed", "snoozed_until": earlier}, now) is True
    # A snooze with no expiry is not a snooze that lasts forever.
    assert event_types.is_untriaged({"status": "snoozed", "snoozed_until": None}, now) is True
    assert event_types.is_untriaged({"status": "accepted"}, now) is False
    assert event_types.is_untriaged({"status": "false_positive"}, now) is False


def test_is_dispatchable_requires_interval_and_time_column() -> None:
    assert scans.is_dispatchable({"interval": "1h", "time_column": "t"}) is True
    assert scans.is_dispatchable({"interval": None, "time_column": "t"}) is False
    assert scans.is_dispatchable({"interval": "1h", "time_column": None}) is False
    assert scans.is_dispatchable({}) is False


def test_plan_drift_targets_spreads_the_budget_round_robin() -> None:
    """A big project must not consume the whole budget and starve a small one.

    Spending it in project order would give 6/0 here, and the starved project
    would appear with nothing to report and no statement that nobody looked.
    """
    big = [{"id": f"a{index}"} for index in range(10)]
    small = [{"id": f"b{index}"} for index in range(2)]
    targets, examined, totals = event_types.plan_drift_targets(
        {"big": big, "small": small}, budget=6
    )
    assert len(targets) == 6
    assert examined == {"big": 4, "small": 2}
    assert totals == {"big": 10, "small": 2}
    assert targets[0] == ("big", "a0")
    assert targets[1] == ("small", "b0")


def test_plan_drift_targets_reports_truncation_per_project() -> None:
    rows = {"one": [{"id": "x1"}, {"id": "x2"}], "two": [{"id": "y1"}]}
    targets, examined, totals = event_types.plan_drift_targets(rows, budget=2)
    assert len(targets) == 2
    assert examined == {"one": 1, "two": 1}
    assert totals == {"one": 2, "two": 1}
    # Every selected project is a key even at zero, so "not looked at" can be
    # said about it rather than inferred from an absence.
    empty_targets, empty_examined, _ = event_types.plan_drift_targets(
        {"one": [], "two": []}, budget=5
    )
    assert empty_targets == ()
    assert empty_examined == {"one": 0, "two": 0}


def test_resolve_selectors_prefers_exact_name_then_id_and_never_substring() -> None:
    named = {"scan-1": "prod events", "scan-2": "prod events extra"}
    assert scans.resolve_selectors(named, ["prod events"]) == (("scan-1",), ())
    assert scans.resolve_selectors(named, ["scan-2"]) == (("scan-2",), ())
    # A prefix is NOT a match: following the wrong config silently is worse than
    # refusing to start.
    assert scans.resolve_selectors(named, ["prod"]) == ((), ("prod",))
    assert scans.resolve_selectors(named, ["PROD EVENTS"]) == ((), ("PROD EVENTS",))
    # No selectors at all means everything, in listing order.
    assert scans.resolve_selectors(named, []) == (("scan-1", "scan-2"), ())


def test_resolve_selectors_returns_every_id_for_an_ambiguous_name() -> None:
    """Two configs, one name: the caller is handed both and must not pick one."""
    named = {"scan-1": "nightly", "scan-2": "nightly"}
    matched, unmatched = scans.resolve_selectors(named, ["nightly"])
    assert matched == ("scan-1", "scan-2")
    assert unmatched == ()


def test_config_names_falls_back_to_the_id_and_skips_rows_without_one() -> None:
    assert scans.config_names([{"id": "scan-1"}, {"name": "orphan"}]) == {"scan-1": "scan-1"}


def test_scan_config_summary_drops_base_query_and_adds_dispatchable() -> None:
    config = {
        "id": "scan-1",
        "name": "prod events",
        "interval": "1h",
        "time_column": "event_time",
        "base_query": "select * from analytics.events",
        "cardinality_threshold": 500,
        "json_value_paths": [],
    }
    summary = scans.scan_config_summary(config)
    assert "base_query" not in summary
    assert "cardinality_threshold" not in summary
    assert "json_value_paths" not in summary
    assert summary["dispatchable"] is True
    assert summary["name"] == "prod events"
    assert scans.scan_config_summary({"id": "s", "interval": None})["dispatchable"] is False


def test_the_jobs_limit_ceiling_is_the_constant_doctor_already_shares() -> None:
    """One 200 in the repository, not two."""
    from tripl_cli.model import JOBS_WINDOW

    assert scans.JOBS_LIMIT_MAX is JOBS_WINDOW


def test_the_shared_layer_will_build_accept_so_the_restriction_is_the_cli_s_own() -> None:
    """Half of the exclusion: ``accept`` is real, and THIS layer will build it.

    That is deliberate - the layer is shared with tripl-mcp and describes the API
    rather than one consumer's policy - and it is exactly why "the CLI cannot
    send accept" has to be enforced where the CLI CHOOSES the action. Asserting
    here that ``accept`` is absent from ``CLI_ALLOWED_DRIFT_ACTIONS`` would pin
    the constant's own contents, which no production code reads; the guarantee is
    asserted on the wire in
    ``test_drifts_cmd.py::test_no_reachable_dismiss_invocation_can_send_accept``.

    ``accept`` on a missing_field drift deletes the FieldDefinition - the damage
    doctor's schema_field_deleted_by_accept finding exists to report.
    """
    assert "accept" in event_types.DRIFT_ACTIONS
    built = event_types.apply_drift_action("prod", "drift-1", action="accept")
    assert built.json_body == {"action": "accept"}


def test_the_api_package_imports_nothing_consumer_specific() -> None:
    """No ``mcp``, no ``argparse``, no ``tripl_mcp`` anywhere under tripl_cli/api.

    An ``mcp`` import here would invert the dependency and drag the MCP SDK into
    every ``uvx tripl`` install; an argparse import would put CLI flags in a
    module tripl-mcp loads.
    """
    forbidden = {"mcp", "tripl_mcp", "argparse"}
    offenders: list[str] = []
    for path in sorted(API_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            roots: list[str] = []
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = [node.module.split(".")[0]]
            offenders.extend(f"{path.name}: {root}" for root in roots if root in forbidden)
    assert not offenders, f"tripl_cli.api imports consumer-specific modules: {offenders}"


def test_the_api_package_never_prints() -> None:
    """Rendering belongs to the consumer; a shared layer that prints has picked one."""
    offenders = [
        path.name
        for path in sorted(API_PACKAGE.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print"
    ]
    assert not offenders, f"tripl_cli.api prints: {offenders}"
