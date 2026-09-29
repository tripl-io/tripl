"""``tripl events list`` and ``tripl events show``.

The behaviours worth pinning here are the ones an operator would only discover
in an incident: a page that stopped short says so, a failed read is exit 1 and
not an empty table, ``--branch`` resolves by name and refuses to guess, and no
verb reaches a write route.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tripl_cli.cli import main

from .conftest import FakeInstance, make_branch, make_event, make_field

_SEEN = datetime(2026, 7, 31, 19, 0, tzinfo=UTC)


def test_list_prints_one_row_per_event(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.events(
        "prod",
        [
            make_event(last_seen_at=_SEEN, drift_count=2),
            make_event("evt-2", name="app.purchase.completed", status="draft"),
        ],
    )
    assert main(["events", "list", "--project", "prod"]) == 0
    out = capsys.readouterr().out
    assert "evt-1  app.screen_view.viewed  live   seen 2026-07-31T19:00:00Z  2 drifts" in out
    assert "evt-2  app.purchase.completed  draft  never seen" in out
    assert out.endswith("2 events.\n")


def test_a_page_that_stopped_short_says_so_and_names_the_flags(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole reason the API's ``total`` is carried at all.

    A full page and a finished catalog produce the identical row count, and
    reading the second as the first is how an audit concludes a project has two
    events. The line names both flags because either one gets you the rest.
    """
    tripl_api.events("prod", [make_event(last_seen_at=_SEEN)], total=412)
    assert main(["events", "list", "--project", "prod", "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "1 of 412 events shown; raise --limit or pass --offset to read the rest." in out


def test_a_failed_read_is_exit_1_and_not_an_empty_table(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """TRAP 3, on a command with no verdict contract.

    ``events list`` reads ONE resource of ONE project, so a 403 leaves nothing
    to report beside — there is no partial answer, and printing ``0 events.`` at
    exit 0 would be the failure this repository exists to stop reproducing.
    """
    tripl_api.events("prod", None, status=403, payload={"detail": "Not authorized for project"})
    assert main(["events", "list", "--project", "prod"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "403" in captured.err


def test_list_requires_exactly_one_project(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["events", "list"]) == 2
    assert "name it with --project" in capsys.readouterr().err
    assert main(["events", "list", "--project", "prod", "--project", "mobile"]) == 2
    assert "--project was given 2 times" in capsys.readouterr().err


def test_a_bare_group_prints_help_on_stderr_and_exits_2(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["events"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "<verb>" in captured.err


def test_branch_is_resolved_by_name_and_sent_as_an_id(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--branch`` takes the name an operator has; the wire takes the UUID.

    Also pins that the parameter goes out at all: ``?branch=`` is a dependency
    rather than a declared query parameter, so it is absent from
    ``backend/openapi.json`` and the contract test cannot see it. This is the
    only thing watching it.
    """
    tripl_api.branches("prod", [make_branch("b-9f21", name="checkout-redesign")])
    route = tripl_api.events("prod", [make_event(last_seen_at=_SEEN)])
    assert (
        main(["events", "list", "--project", "prod", "--branch", "checkout-redesign", "--json"])
        == 0
    )
    request = route.calls.last.request
    assert request.url.params["branch"] == "b-9f21"
    document = json.loads(capsys.readouterr().out)
    assert document["branch"] == {"id": "b-9f21", "name": "checkout-redesign"}


def test_an_unknown_branch_exits_2_and_lists_the_candidates(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.branches("prod", [make_branch("b-9f21", name="checkout-redesign")])
    assert main(["events", "list", "--project", "prod", "--branch", "checkout"]) == 2
    err = capsys.readouterr().err
    assert "no branch matches 'checkout'" in err
    assert "checkout-redesign (b-9f21)" in err


def test_no_branch_means_main_and_sends_no_parameter(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Main is spelled by omission, on the wire and in the document alike.

    Sending ``branch=`` or ``branch=main`` would both be inventions: the
    dependency resolves main when the parameter is ABSENT, and any non-empty
    value must parse as a UUID.
    """
    route = tripl_api.events("prod", [make_event(last_seen_at=_SEEN)])
    assert main(["events", "list", "--project", "prod", "--json"]) == 0
    assert "branch" not in route.calls.last.request.url.params
    assert json.loads(capsys.readouterr().out)["branch"] is None


def test_status_is_repeatable_and_refuses_an_unknown_value(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    route = tripl_api.events("prod", [make_event(last_seen_at=_SEEN)])
    assert (
        main(["events", "list", "--project", "prod", "--status", "live", "--status", "draft"]) == 0
    )
    assert route.calls.last.request.url.params.get_list("status") == ["live", "draft"]
    capsys.readouterr()
    # argparse refuses an unknown choice itself, which is a SystemExit rather
    # than a returned code - the same shape every other bad-flag test takes.
    with pytest.raises(SystemExit) as exit_info:
        main(["events", "list", "--project", "prod", "--status", "shipped"])
    assert exit_info.value.code == 2


def test_reviewed_is_tri_state_and_is_absent_unless_asked_for(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Three states on the wire, and the third one is silence.

    The route reads an ABSENT ``reviewed`` as "either", so a client that always
    sends the parameter can never ask that question — and ``reviewed=false`` is
    the value a "drop the empties" filter written as ``if value`` swallows,
    which would turn ``--unreviewed`` into an unfiltered list nobody could tell
    from a correct one. ``ApiRequest`` drops ``None`` and only ``None``; this
    watches that it keeps doing so for a false.
    """
    route = tripl_api.events("prod", [make_event(last_seen_at=_SEEN)])
    assert main(["events", "list", "--project", "prod"]) == 0
    assert "reviewed" not in route.calls.last.request.url.params

    assert main(["events", "list", "--project", "prod", "--reviewed"]) == 0
    assert route.calls.last.request.url.params["reviewed"] == "true"

    assert main(["events", "list", "--project", "prod", "--unreviewed"]) == 0
    assert route.calls.last.request.url.params["reviewed"] == "false"

    capsys.readouterr()
    # Both at once is a contradiction, not a last-one-wins: argparse refuses it
    # itself, the same SystemExit every other bad-flag test takes.
    with pytest.raises(SystemExit) as exit_info:
        main(["events", "list", "--project", "prod", "--reviewed", "--unreviewed"])
    assert exit_info.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_field_value_is_absent_unless_asked_for(
    tripl_api: FakeInstance, configured_env: None
) -> None:
    """The other half of an event's content, filterable at last.

    ``--meta-value`` reached the wire from the first release and ``--field-value``
    did not, though the route has declared both since PR #78 and compares them
    the same way (a case-insensitive substring) - so "which events carry this
    screen name" was a question only the web app could ask (tripl-nhj0).

    Absence is asserted first for the same reason every filter here is: a
    parameter that appears without being asked for narrows an UNFILTERED listing,
    and an operator reading a short table has no way to tell that from a small
    catalog.
    """
    route = tripl_api.events("prod", [make_event(last_seen_at=_SEEN)])
    assert main(["events", "list", "--project", "prod"]) == 0
    assert "field_value" not in route.calls.last.request.url.params

    assert main(["events", "list", "--project", "prod", "--field-value", "checkout"]) == 0
    assert route.calls.last.request.url.params["field_value"] == "checkout"


def test_order_by_is_absent_unless_asked_for_and_refuses_an_unknown_value(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Omission is how the route's own default is asked for.

    Unlike ``--limit``, this flag has no client-side default: the route declares
    ``catalog`` and spelling that out here would pin today's answer, so an
    unasked-for ordering must leave the parameter off the wire entirely. The
    unknown value costs no request for the same reason every other bad flag
    does - the route declares a Literal, so ``--order-by newest`` would buy a
    422 to learn what argparse already knows.
    """
    route = tripl_api.events("prod", [make_event(last_seen_at=_SEEN)])
    assert main(["events", "list", "--project", "prod"]) == 0
    assert "order_by" not in route.calls.last.request.url.params

    assert main(["events", "list", "--project", "prod", "--order-by", "volume"]) == 0
    assert route.calls.last.request.url.params["order_by"] == "volume"

    calls_before = route.call_count
    capsys.readouterr()
    with pytest.raises(SystemExit) as exit_info:
        main(["events", "list", "--project", "prod", "--order-by", "newest"])
    assert exit_info.value.code == 2
    assert route.call_count == calls_before
    assert "invalid choice" in capsys.readouterr().err


def test_an_out_of_range_limit_costs_no_request(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    route = tripl_api.events("prod", [])
    with pytest.raises(SystemExit) as exit_info:
        main(["events", "list", "--project", "prod", "--limit", "10001"])
    assert exit_info.value.code == 2
    assert route.call_count == 0
    assert "--limit must be between 1 and 10000" in capsys.readouterr().err


def test_show_prints_field_values_under_their_field_names(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reason ``events show`` costs a second request.

    ``EventFieldValueResponse`` carries ``field_definition_id`` and nothing
    else, so without the fields read this block is a column of UUIDs — which is
    the part of the command worth having, rendered useless.
    """
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
        "prod",
        "et-1",
        [make_field(), make_field("fd-2", name="cart_value", field_type="number")],
    )
    assert main(["events", "show", "evt-1", "--project", "prod"]) == 0
    out = capsys.readouterr().out
    assert "screen_name  checkout" in out
    assert "cart_value   ${cart_value}" in out
    # Meta values keep their definition id: there is no meta-field builder in
    # the shared request layer, and inventing one here would be a path literal
    # outside tripl_cli/api.
    assert "meta (by definition id)" in out
    assert "mf-1  TRIPL-412" in out


def test_show_carries_the_event_verbatim_under_items(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """One ``items`` selector for the whole family, detail reads included."""
    tripl_api.event("prod", "evt-1", make_event(last_seen_at=_SEEN))
    assert main(["events", "show", "evt-1", "--project", "prod", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["kind"] == "event"
    assert [item["id"] for item in document["items"]] == ["evt-1"]
    # A detail read pages nothing, and the three nulls are what say so.
    assert (document["total"], document["offset"], document["limit"]) == (None, None, None)
    assert document["truncated"] is False


def test_json_puts_the_document_on_stdout_and_every_human_line_on_stderr(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.events("prod", [make_event(last_seen_at=_SEEN)])
    assert main(["events", "list", "--project", "prod", "--json"]) == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["command"] == "events list"
    assert document["items"][0]["name"] == "app.screen_view.viewed"
    assert "tripl events list -" in captured.err
    assert captured.out.count("\n") == 1


def test_the_document_carries_the_row_verbatim(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """No projection, deliberately.

    A CLI writes to a pipe, where a trimmed row is a field the operator has to
    fetch again. The MCP's ``EVENT_LIST_FIELDS`` exists because a model pays for
    every token; that is a different cost and it stays with its consumer
    (tripl-i1dt). If this ever starts trimming, it is a contract change.
    """
    row = make_event(
        last_seen_at=_SEEN,
        field_values=[{"id": "fv-1", "field_definition_id": "fd-1", "value": "checkout"}],
    )
    tripl_api.events("prod", [row])
    assert main(["events", "list", "--project", "prod", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == [row]


def test_show_lists_the_typed_properties(
    tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """F23: the event's property list, with type, requiredness and presence."""
    tripl_api.event(
        "prod",
        "evt-1",
        make_event(last_seen_at=_SEEN),
        properties=[
            {
                "name": "plan",
                "variable_type": "string",
                "required": True,
                "presence_rate": 0.97,
                "effective_values": ["free", "pro"],
            },
            {"name": "amount", "variable_type": "number", "required": False},
        ],
    )
    tripl_api.fields("prod", "et-1", [make_field()])
    assert main(["events", "show", "evt-1", "--project", "prod"]) == 0
    out = capsys.readouterr().out
    assert "properties:" in out
    assert "plan    string  required  97%  free, pro" in out
    assert "amount  number  optional  -    any" in out
    assert main(["events", "show", "evt-1", "--project", "prod", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert [p["name"] for p in document["items"][0]["properties"]] == ["plan", "amount"]
