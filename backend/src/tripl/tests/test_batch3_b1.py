"""The two event-name builders, pinned against each other.

``plan_events`` mints ``Event.source_name`` and ``_build_event_name_from_row``
re-derives it once per metric row; the collector then looks the event up by that
string (``events_by_name``). Every divergence between the two is therefore the
same outage wearing a different hat: the row matches nothing, its volume leaves
the coverage numerator, and real traffic is filed as an unplanned identity.

Four such divergences are pinned here, one section each:

* group rules could only read columns that had a FieldDefinition,
  which is exactly the set a rule column is never in;
* with no name format the collector appended a ``col.path=``
  segment per JSON path and the planner appended none;
* a dotted placeholder the row did not carry killed the run
  instead of contributing an empty segment, the way a NULL column does; the same
  section pins the other edge of that seed, a path NO row carries, which renames
  every affected identity and must therefore be reported rather than swallowed;
* a format naming the ``event_type_column`` could never resolve,
  even though ``reserved_catalog_columns`` un-reserves that column for it.

Every assertion is pure Python over hand-built rows: no session, no Postgres, so
nothing here is blunted by the in-memory SQLite backend.
"""

import uuid

import pytest

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.cardinality import BreakdownAnalysis, CardinalityResult
from tripl.core.analyzers.event_plan import (
    absent_json_path_detail,
    plan_events,
    unnamed_skip_detail,
)
from tripl.core.name_template import NameFormatError
from tripl.models.scan_config import ScanConfig
from tripl.worker.tasks.metrics.metric_rows import _build_event_name_from_row
from tripl.worker.utils.reserved_columns import reserved_catalog_columns

# --------------------------------------------------------------------------
# group rules read the row, not just the catalog columns
# --------------------------------------------------------------------------

_EVENT_NAME_RULE = [
    {
        "name": "Home Screen View",
        "condition_logic": "all",
        "conditions": [{"field": "event_name", "pattern": "^Home Screen View$"}],
    }
]

_EVENT_TYPE_RULE = [
    {
        "name": "Checkout",
        "condition_logic": "all",
        "conditions": [{"field": "category", "pattern": "^checkout$"}],
    }
]


def _warehouse_event_name_analysis() -> BreakdownAnalysis:
    """The shipped demo's shape: an ``event_name`` COLUMN nobody gave a field to.

    ``reserved_catalog_columns`` returns every group-rule column, and
    ``catalog_sync`` forwards that set as ``skip_columns``, so the one column the
    rules are keyed on is by construction the one column with no FieldDefinition.
    """
    return BreakdownAnalysis(
        results={
            "screen_name": CardinalityResult(
                column=ColumnInfo("screen_name", "String"),
                count=1,
                is_low=True,
                sample_values=["Home"],
            ),
            "platform": CardinalityResult(
                column=ColumnInfo("platform", "String"),
                count=1,
                is_low=True,
                sample_values=["ios"],
            ),
        },
        rows=[("Home Screen View", "Home", "ios", 12)],
        reg_names=["event_name", "screen_name", "platform"],
        json_names=[],
    )


def test_the_collector_applies_a_group_rule_keyed_on_a_column_without_a_field_definition() -> None:
    """The direct pin: the rule column is in the ROW, it was just never read.

    The collector built the rule's value dict out of ``col_meta``, so a reserved
    column was invisible to it — and for the key literally named ``event_name``
    worse than invisible, because the identity pseudo-field then filled in the
    derived pipe-name. This row used to come back as
    ``"screen_name=Home | platform=ios"``.
    """
    derived = _build_event_name_from_row(
        ["Home Screen View", "Home", "ios", 12],
        {
            "screen_name": {"is_json": False, "is_low": True},
            "platform": {"is_json": False, "is_low": True},
        },
        {"event_name": 0, "screen_name": 1, "platform": 2},
        {},
        3,
        [],
        None,
        _EVENT_NAME_RULE,
    )

    assert derived == "Home Screen View"


@pytest.mark.parametrize(
    ("analysis", "field_names", "rules", "reserved", "event_type_column", "reg_index", "row"),
    [
        pytest.param(
            _warehouse_event_name_analysis(),
            ("screen_name", "platform"),
            _EVENT_NAME_RULE,
            {"event_name"},
            None,
            {"event_name": 0, "screen_name": 1, "platform": 2},
            ["Home Screen View", "Home", "ios", 12],
            id="rule-on-a-warehouse-event_name-column",
        ),
        pytest.param(
            BreakdownAnalysis(
                results={
                    "action": CardinalityResult(
                        column=ColumnInfo("action", "String"),
                        count=1,
                        is_low=True,
                        sample_values=["click"],
                    ),
                },
                rows=[("checkout", "click", 4)],
                reg_names=["category", "action"],
                json_names=[],
            ),
            ("action",),
            _EVENT_TYPE_RULE,
            {"category"},
            "category",
            {"category": 0, "action": 1},
            ["checkout", "click"],
            id="rule-on-the-event-type-column",
        ),
    ],
)
def test_the_planner_and_the_collector_derive_the_same_identity_for_the_same_row(
    analysis: BreakdownAnalysis,
    field_names: tuple[str, ...],
    rules: list[dict[str, object]],
    reserved: set[str],
    event_type_column: str | None,
    reg_index: dict[str, int],
    row: list[object],
) -> None:
    """The invariant the collector's ``events_by_name`` lookup rests on.

    Both reserved shapes are covered: a rule keyed on a plain warehouse column
    the catalog skipped, and a rule keyed on the event type column.
    """
    plan = plan_events(
        analysis,
        {name: uuid.uuid4() for name in field_names},
        event_type_column=event_type_column,
        event_group_rules=rules,
        reserved_columns=reserved,
    )
    assert len(plan.events) == 1
    assert plan.events_grouped == 1

    derived = _build_event_name_from_row(
        row,
        plan.col_meta,
        reg_index,
        {},
        len(reg_index),
        [],
        None,
        rules,
        event_type_column=event_type_column,
    )

    assert derived == plan.events[0].name
    # ...and that equality is not incidental: it IS the lookup the collector
    # does, and the branch it misses files the row as a shadow candidate.
    events_by_name = {event.name: event for event in plan.events}
    assert derived in events_by_name


def test_a_reserved_column_stays_out_of_the_catalog_even_though_the_rules_can_read_it() -> None:
    """Widening what the rules see must not widen what the scan tracks.

    The rule column has no FieldDefinition on purpose; making it a field value
    would put the rule's own pattern in the catalog.
    """
    plan = plan_events(
        _warehouse_event_name_analysis(),
        {"screen_name": uuid.uuid4(), "platform": uuid.uuid4()},
        event_group_rules=_EVENT_NAME_RULE,
        reserved_columns={"event_name"},
    )

    assert "event_name" not in plan.col_meta
    assert all(col_name != "event_name" for _, col_name, _ in plan.events[0].field_values)


# --------------------------------------------------------------------------
# with no name format, one segment per COLUMN in both builders
# --------------------------------------------------------------------------


def _payload_analysis() -> BreakdownAnalysis:
    """One JSON column, one declared passthrough path and one variable path."""
    return BreakdownAnalysis(
        results={
            "payload": CardinalityResult(
                column=ColumnInfo("payload", "JSON"),
                count=1,
                is_low=False,
                json_path_combos=[("action", "screen")],
            )
        },
        rows=[(["action", "screen"], "checkout", 7)],
        reg_names=[],
        json_names=["payload"],
        json_value_names=["payload.action"],
    )


def test_with_no_name_format_the_collector_derives_the_planners_name() -> None:
    """A JSON-column scan with no format matched none of its own events.

    The collector appended a ``col.path=`` segment per path and the planner
    appended none, so the derived identity never equalled the stored
    ``source_name`` and the config's whole volume went to the shadow inbox.
    """
    analysis = _payload_analysis()
    plan = plan_events(analysis, {"payload": uuid.uuid4()})
    assert len(plan.events) == 1

    derived = _build_event_name_from_row(
        analysis.rows[0],
        plan.col_meta,
        {},
        {"payload": 0},
        0,
        list(analysis.json_value_names),
        None,
    )

    assert derived == plan.events[0].name
    assert "payload.action=" not in derived
    assert "payload.screen=" not in derived


def test_a_name_format_still_resolves_every_json_path_in_both_builders() -> None:
    """The companion that stops the fix being "delete the path kwargs".

    A format is the one thing that can consume a per-path key, so the format
    branch must keep every one of them.
    """
    fmt = "{payload.action}/{payload.screen}"
    analysis = _payload_analysis()
    plan = plan_events(analysis, {"payload": uuid.uuid4()}, event_name_format=fmt)

    derived = _build_event_name_from_row(
        analysis.rows[0],
        plan.col_meta,
        {},
        {"payload": 0},
        0,
        list(analysis.json_value_names),
        fmt,
    )

    assert derived == plan.events[0].name
    assert derived == "checkout/${payload.screen}"


# --------------------------------------------------------------------------
# a JSON path the row does not carry is an empty segment
# --------------------------------------------------------------------------


def _split_paths_analysis() -> BreakdownAnalysis:
    """``GROUP BY ALL`` over ``JSONAllPaths``: the two path sets are two rows.

    Row layout is ``BaseAdapter.get_full_breakdown``'s: regular columns, then
    one path list per JSON column, then the kept value columns, then ``_cnt``.
    """
    return BreakdownAnalysis(
        results={
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=2,
                is_low=True,
                sample_values=["/home", "/about"],
            ),
            "event": CardinalityResult(
                column=ColumnInfo("event", "JSON"),
                count=2,
                is_low=True,
                json_path_combos=[("category",), ("other",)],
            ),
        },
        rows=[
            ("/home", ("category",), '"checkout"', 10),
            ("/about", ("other",), "null", 4),
        ],
        reg_names=["screen"],
        json_names=["event"],
        json_value_names=["event.category"],
    )


def test_a_dotted_placeholder_the_row_does_not_carry_is_an_empty_segment() -> None:
    """The row that omits the key is a group of its own, not a broken scan.

    It used to raise ``NameFormatError`` and take the whole run with it, even
    though a NULL regular column in the same position renders as ``""``.
    """
    plan = plan_events(
        _split_paths_analysis(),
        {"screen": uuid.uuid4(), "event": uuid.uuid4()},
        event_name_format="{screen} / {event.category}",
    )

    assert sorted(event.name for event in plan.events) == ["/about / ", "/home / checkout"]
    assert plan.events_unnamed == 0


def test_the_collector_tolerates_a_dotted_key_the_row_does_not_carry() -> None:
    """The collector half — it must agree or the newly plannable row is shadowed."""
    plan = plan_events(
        _split_paths_analysis(),
        {"screen": uuid.uuid4(), "event": uuid.uuid4()},
        event_name_format="{screen} / {event.category}",
    )

    derived = _build_event_name_from_row(
        ["/about", ("other",), "null", 4],
        plan.col_meta,
        {"screen": 0},
        {"event": 0},
        1,
        ["event.category"],
        "{screen} / {event.category}",
    )

    assert derived == "/about / "
    assert derived in {event.name for event in plan.events}


def test_a_dotted_placeholder_on_a_column_the_plan_lost_still_fails_loudly() -> None:
    """The narrowness pin: the seed must not disarm the drift guard.

    A base column with no FieldDefinition — deleted, or reserved away — is the
    failure the earlier diagnosability fixes exist to make loud, and it stays loud.
    """
    with pytest.raises(NameFormatError) as excinfo:
        plan_events(
            _split_paths_analysis(),
            {"screen": uuid.uuid4()},
            event_name_format="{screen} / {event.category}",
        )

    assert "event.category" in str(excinfo.value)
    assert str(excinfo.value).startswith("Scan failed")


def test_a_row_whose_whole_json_name_resolves_to_empty_is_not_planned() -> None:
    """An empty name is still a skipped row, reached now by a JSON path.

    Identical outcome to ``test_a_row_whose_name_resolves_to_empty_is_not_planned``
    in test_event_plan.py, which drives it with a NULL column instead.
    """
    analysis = _split_paths_analysis()
    analysis.rows = [("/about", ("other",), "null", 4)]

    plan = plan_events(
        analysis,
        {"screen": uuid.uuid4(), "event": uuid.uuid4()},
        event_name_format="{event.category}",
    )

    assert plan.events == []
    assert plan.events_unnamed == 1
    assert unnamed_skip_detail(1) in plan.details


def test_a_dotted_placeholder_no_row_carries_is_reported_not_swallowed() -> None:
    """The seed's blind spot, disclosed instead of silently re-minting identities.

    ``json_name_format_keys`` tests the BASE column only, so a path the producer
    renamed away — or a typo in the format — seeds ``""`` exactly like a quiet
    window and renders every name with an empty segment. The two are genuinely
    indistinguishable from one scan, so the run must not raise (that is the
    earlier outage) and must not stay silent either: an identity changing
    under the operator is not something to find out from a flat chart.

    Red on revert of the report: the names below are correct with or without it.
    """
    plan = plan_events(
        _split_paths_analysis(),
        {"screen": uuid.uuid4(), "event": uuid.uuid4()},
        # "catgeory": the typo, and the rename, look identical from here.
        event_name_format="{screen} / {event.catgeory}",
    )

    assert sorted(event.name for event in plan.events) == ["/about / ", "/home / "]
    assert plan.events_unnamed == 0
    assert absent_json_path_detail(["event.catgeory"]) in plan.details
    assert (
        absent_json_path_detail(["event.catgeory"])
        == "Event name format JSON path not present on any row, "
        "rendered as an empty segment: event.catgeory"
    )
    # Pluralised in the helper, for the same reason ``unnamed_skip_detail`` is:
    # this is copy an operator reads, and "1 paths" is a defect this repo has
    # already shipped once.
    assert absent_json_path_detail(["a.b", "c.d"]) == (
        "Event name format JSON paths not present on any row, rendered as empty segments: a.b, c.d"
    )


def test_a_path_one_row_carries_is_not_reported_absent() -> None:
    """The boundary: the rescued row must not look like a rename.

    One of the two rows carries ``event.category`` and the other does not, which
    is the ordinary ``GROUP BY ALL`` shape the seed exists for. Reporting that
    would put a line on every scan whose JSON payload has optional keys.
    """
    plan = plan_events(
        _split_paths_analysis(),
        {"screen": uuid.uuid4(), "event": uuid.uuid4()},
        event_name_format="{screen} / {event.category}",
    )

    assert plan.details == []


# --------------------------------------------------------------------------
# a name format may name the event type column
# --------------------------------------------------------------------------

_EVENT_TYPE_NAME_FORMAT = "{screen}:{action}"


def _single_type_analysis() -> BreakdownAnalysis:
    """The event type column IS in ``analysis.results`` — and still skipped."""
    return BreakdownAnalysis(
        results={
            "screen": CardinalityResult(
                column=ColumnInfo("screen", "String"),
                count=1,
                is_low=True,
                sample_values=["/home"],
            ),
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=1,
                is_low=True,
                sample_values=["click"],
            ),
        },
        rows=[("/home", "click", 5)],
        reg_names=["screen", "action"],
        json_names=[],
    )


def test_a_name_format_may_name_the_event_type_column() -> None:
    """``reserved_catalog_columns`` un-reserves it; the planner must then use it.

    Un-reserving the column was only ever half the rule: ``plan_column_meta``
    skips the event type column unconditionally, so the placeholder stayed
    unresolvable and every run died on "references unknown keys: screen".
    """
    config = ScanConfig(
        event_type_column="screen",
        time_column="time",
        event_name_format=_EVENT_TYPE_NAME_FORMAT,
    )
    reserved = reserved_catalog_columns(config)
    assert "screen" not in reserved, "the name-format column must not be reserved"

    plan = plan_events(
        _single_type_analysis(),
        {"action": uuid.uuid4()},
        event_type_column="screen",
        event_name_format=_EVENT_TYPE_NAME_FORMAT,
        reserved_columns=reserved,
    )

    assert [event.name for event in plan.events] == ["/home:click"]


def test_the_grouped_shape_names_a_group_column_absent_from_the_results() -> None:
    """``_process_breakdown(skip_column=...)`` leaves the column out of ``results``.

    So relaxing the skip in ``plan_column_meta`` would not have been enough on
    its own — this pins that the value is read off the ROW instead.
    """
    analysis = BreakdownAnalysis(
        results={
            "action": CardinalityResult(
                column=ColumnInfo("action", "String"),
                count=1,
                is_low=True,
                sample_values=["click"],
            ),
        },
        rows=[("/home", "click", 5)],
        reg_names=["screen", "action"],
        json_names=[],
    )

    plan = plan_events(
        analysis,
        {"action": uuid.uuid4()},
        event_type_column="screen",
        event_name_format=_EVENT_TYPE_NAME_FORMAT,
    )

    assert [event.name for event in plan.events] == ["/home:click"]


def test_the_event_type_column_is_still_not_a_field_value() -> None:
    """Only the name kwargs change: no new EventFieldValue, no snapshot bump."""
    plan = plan_events(
        _single_type_analysis(),
        {"action": uuid.uuid4()},
        event_type_column="screen",
        event_name_format=_EVENT_TYPE_NAME_FORMAT,
    )

    assert "screen" not in plan.col_meta
    assert all(col_name != "screen" for _, col_name, _ in plan.events[0].field_values)


def test_the_collector_names_the_event_type_column_the_planner_did() -> None:
    """The load-bearing half: a planner-only fix shadows 100% of the volume."""
    plan = plan_events(
        _single_type_analysis(),
        {"action": uuid.uuid4()},
        event_type_column="screen",
        event_name_format=_EVENT_TYPE_NAME_FORMAT,
    )

    derived = _build_event_name_from_row(
        ["/home", "click"],
        plan.col_meta,
        {"screen": 0, "action": 1},
        {},
        2,
        [],
        _EVENT_TYPE_NAME_FORMAT,
        event_type_column="screen",
    )

    assert derived == "/home:click"
    assert derived == plan.events[0].name


def test_the_collector_still_declines_a_row_with_no_col_meta() -> None:
    """The injection sits AFTER the ``not kwargs`` guard, and must stay there.

    ``plan_events`` returns no events at all when nothing matched a field
    definition, so a collector that minted ``"/home:click"`` here would derive an
    identity the planner never planned.
    """
    derived = _build_event_name_from_row(
        ["/home", "click"],
        {},
        {"screen": 0, "action": 1},
        {},
        2,
        [],
        _EVENT_TYPE_NAME_FORMAT,
        event_type_column="screen",
    )

    assert derived is None


def test_a_genuinely_missing_key_still_raises_the_curated_error() -> None:
    """None of the three new fallbacks may swallow a real drift.

    ``action`` is not a JSON path, not the event type column and not in
    ``col_meta``, so it is still the loud failure that was made self-diagnosing.
    """
    with pytest.raises(NameFormatError) as excinfo:
        _build_event_name_from_row(
            ["home"],
            {"screen_name": {"is_json": False, "is_low": True}},
            {"screen_name": 0},
            {},
            1,
            [],
            "{action}",
            event_type_column="screen_name",
        )

    assert "action" in str(excinfo.value)
    assert str(excinfo.value).startswith("Scan failed")
