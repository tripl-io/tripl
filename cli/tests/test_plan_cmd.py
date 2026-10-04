"""``tripl plan types|fields|variables|branches|search``.

One document shape across five verbs, so most of what is worth pinning is the
shape itself plus the two places a verb legitimately differs: ``plan branches``
takes no ``--branch``, and ``plan search`` reports whether the semantic index
answered.
"""

from __future__ import annotations

import json

import pytest

from tripl_cli.cli import main

from .conftest import (
    FakeInstance,
    make_branch,
    make_event_type,
    make_field,
    make_search_result,
    make_variable,
)


def test_types_prints_a_field_count_per_event_type(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    event_type = make_event_type()
    event_type["field_definitions"] = [make_field(), make_field("fd-2", name="cart_value")]
    tripl_api.event_types("prod", [event_type])
    assert main(["plan", "types", "--project", "prod"]) == 0
    out = capsys.readouterr().out
    assert "et-1  app.screen_view  app.screen_view  2 fields" in out
    assert out.endswith("1 event type.\n")


def test_a_bare_array_route_reports_no_paging_at_all(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """``total``/``offset``/``limit`` null is a statement, not a gap.

    It says the route pages nothing, so the rows ARE the whole answer — which is
    the fact that lets a consumer skip the "is there more" question entirely.
    """
    tripl_api.event_types("prod", [make_event_type()])
    assert main(["plan", "types", "--project", "prod", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert (document["total"], document["offset"], document["limit"]) == (None, None, None)
    assert document["truncated"] is False
    assert document["kind"] == "event_type"


def test_fields_resolves_the_event_type_by_name(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.event_types("prod", [make_event_type()])
    tripl_api.fields(
        "prod",
        "et-1",
        [
            make_field(sensitivity="pii", enum_options=["checkout", "cart"]),
            make_field("fd-2", name="cart_value", field_type="number", is_required=False),
        ],
    )
    assert main(["plan", "fields", "app.screen_view", "--project", "prod"]) == 0
    out = capsys.readouterr().out
    assert "fd-1  screen_name  string  required  pii   enum: checkout|cart" in out
    assert "fd-2  cart_value   number  optional  none" in out
    assert out.endswith("2 fields.\n")


def test_an_unknown_event_type_exits_2_and_lists_the_candidates(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same refusal ``<scan>`` gives, from the same matcher.

    Never a substring and never case-insensitive: `plan fields app.screen` must
    not quietly answer about `app.screen_view`, because the fields it prints are
    what an author then writes values against.
    """
    tripl_api.event_types("prod", [make_event_type()])
    assert main(["plan", "fields", "app.screen", "--project", "prod"]) == 2
    err = capsys.readouterr().err
    assert "no event type matches 'app.screen'" in err
    assert "app.screen_view (et-1)" in err


def test_an_enum_of_numbers_does_not_break_the_table(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """``enum_options`` is ``array[Any]`` on the wire, not ``array[string]``."""
    tripl_api.event_types("prod", [make_event_type()])
    tripl_api.fields(
        "prod", "et-1", [make_field(name="tier", field_type="number", enum_options=[1, 2, 3])]
    )
    assert main(["plan", "fields", "app.screen_view", "--project", "prod"]) == 0
    assert "enum: 1|2|3" in capsys.readouterr().out


def test_variables_page_and_report_what_they_left_behind(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.variables("prod", [make_variable(open_drift_count=1)], total=3)
    assert main(["plan", "variables", "--project", "prod", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "var-1  cart_value  number  12 events  1 open drift" in out
    assert "1 of 3 properties shown; raise --limit or pass --offset to read the rest." in out


def test_branches_names_the_one_that_is_behind_its_base(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """``behind base`` is the column an operator acts on before a merge."""
    tripl_api.branches(
        "prod",
        [
            make_branch("b-0001", name="main", kind="main", status="merged", ahead=None),
            make_branch(behind_base=True),
        ],
    )
    assert main(["plan", "branches", "--project", "prod"]) == 0
    out = capsys.readouterr().out
    assert "b-0001  main               main     merged  -" in out
    assert "b-9f21  checkout-redesign  working  draft   3 ahead  behind base" in out
    # The pluraliser's sibilant rule. "2 branchs" shipped once.
    assert out.endswith("2 branches.\n")


def test_branches_takes_no_branch_flag(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A flag that quietly did nothing would read as one that did something.

    The branch listing is a property of the project, not of a revision, so the
    route has no ``?branch=`` to send and the flag must not exist.
    """
    with pytest.raises(SystemExit) as exit_info:
        main(["plan", "branches", "--project", "prod", "--branch", "anything"])
    assert exit_info.value.code == 2
    assert "unrecognized arguments: --branch" in capsys.readouterr().err


def test_search_reports_whether_the_semantic_index_answered(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """``semantic_used`` changes what a low confidence MEANS.

    A substring fallback scores differently from a semantic match, so a consumer
    ranking on ``confidence`` needs to know which one it got. It is the one
    route-level fact any of these verbs reports, and it rides in ``meta``.
    """
    # Both bodies below carry `truncated`, so a test about `semantic_used` does
    # not double as a second pin of the older instance that sends no such key —
    # that one is pinned on its own, further down.
    tripl_api.search("prod", [make_search_result()], semantic_used=True, truncated=True)
    assert main(["plan", "search", "purchase", "--project", "prod", "--limit", "1"]) == 0
    captured = capsys.readouterr()
    assert "event  evt-1  app.screen_view.viewed  Screen View  0.92" in captured.out
    # No "1 of 57": the route's total is computed AFTER the trim, so it can only
    # ever equal the page. The warning comes from the route's own `truncated`,
    # which says THAT hits were dropped and never how many — so the line does
    # not pretend to.
    assert (
        "1 search result shown — the most this page holds, and more may have matched; "
        "raise --limit." in captured.out
    )

    tripl_api.search("prod", [make_search_result()], semantic_used=True, truncated=False)
    assert main(["plan", "search", "purchase", "--project", "prod", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["meta"] == {"semantic_used": True}
    # No offset parameter exists on this route, so advising one would send the
    # operator after a flag that cannot be built.
    assert document["offset"] is None


def test_search_prefers_the_routes_truncation_flag_over_a_full_page(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A page that filled exactly is not a page that dropped rows.

    ``/search`` retrieves one row past its candidate window, so it KNOWS. The CLI
    was guessing from ``len(items) >= limit`` and printing "more may have
    matched" on a query whose matches land exactly on ``--limit`` — while
    ``tripl-mcp``'s ``search_plan``, reading the same key out of the same body,
    answered false. Two surfaces of one product, opposite answers about one
    response, and ``--limit`` raised for nothing.
    """
    tripl_api.search("prod", [make_search_result()], truncated=False)

    assert main(["plan", "search", "purchase", "--project", "prod", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert out.endswith("1 search result.\n")
    assert "more may have matched" not in out

    assert main(["plan", "search", "purchase", "--project", "prod", "--limit", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["truncated"] is False


def test_search_takes_the_routes_flag_even_where_the_page_did_not_fill(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The top rung is the route's ANSWER, not a veto over the guess.

    Seen only against a full page, "prefer ``truncated``" is indistinguishable
    from the weaker "let ``truncated: false`` silence the guess": the test above
    still passes if the ladder can only ever turn a True into a False. This pins
    the other direction — one row against ``--limit 5``, where page fullness
    says False and the route says True, and the route wins.

    Asserted on the JSON envelope and not on the printed warning, deliberately.
    Today's ``/search`` cannot send this body: it trims by slicing, so
    ``truncated: true`` implies the page filled exactly. What is pinned here is
    the precedence between two rungs of ``PlanRead.truncated``, which holds for
    whatever fills ``route_truncated`` next — not the wording of a line no real
    response can trigger, which is the trap ``FakeInstance.search`` refuses a
    ``total`` parameter to avoid.
    """
    tripl_api.search("prod", [make_search_result()], truncated=True)
    assert main(["plan", "search", "purchase", "--project", "prod", "--limit", "5", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["truncated"] is True


def test_search_still_guesses_against_an_instance_that_reports_no_truncation(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silence is not the route saying nothing was dropped.

    ``truncated`` is new and the CLI is installed separately from the instance it
    talks to, so an older body simply has no key. The page-fullness guess — the
    only signal that ever existed for this route — has to keep running there, or
    the fix would turn a warning that is sometimes spurious into a silence that
    is sometimes wrong.
    """
    tripl_api.search("prod", [make_search_result()])
    assert main(["plan", "search", "purchase", "--project", "prod", "--limit", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["truncated"] is True


def test_meta_is_present_and_empty_on_the_verbs_whose_route_reports_nothing(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A consumer must never have to test for the key before reading it."""
    assert main(["plan", "types", "--project", "prod", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["meta"] == {}


def test_a_blank_or_oversized_query_costs_no_request(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    route = tripl_api.search("prod", [])
    for query in ("   ", "x" * 501):
        with pytest.raises(SystemExit) as exit_info:
            main(["plan", "search", query, "--project", "prod"])
        assert exit_info.value.code == 2
    assert route.call_count == 0
    assert "<query> must be 1 to 500 characters" in capsys.readouterr().err


def test_search_refuses_an_entity_type_the_route_does_not_know(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["plan", "search", "purchase", "--project", "prod", "--type", "dashboard"])
    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_every_verb_sends_the_resolved_branch_id(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """One resolver, so five verbs cannot disagree about what a branch name means."""
    tripl_api.branches("prod", [make_branch()])
    routes = {
        "types": tripl_api.event_types("prod", [make_event_type()]),
        "variables": tripl_api.variables("prod", [make_variable()]),
        "search": tripl_api.search("prod", [make_search_result()]),
    }
    for verb, extra in (("types", []), ("variables", []), ("search", ["purchase"])):
        assert (
            main(["plan", verb, *extra, "--project", "prod", "--branch", "checkout-redesign"]) == 0
        )
        capsys.readouterr()
        assert routes[verb].calls.last.request.url.params["branch"] == "b-9f21"


def test_a_bare_group_prints_help_on_stderr_and_exits_2(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["plan"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "<verb>" in captured.err


def test_a_failed_read_is_exit_1_and_not_an_empty_table(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.event_types("prod", {"detail": "Not authorized"}, status=403)
    assert main(["plan", "types", "--project", "prod"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "403" in captured.err
