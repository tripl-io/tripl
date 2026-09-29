"""``fold_json_properties``: the union of an event's JSON keys (F23.4.1)."""

import json
import uuid

from tripl.core.analyzers._json_property_union import fold_json_properties
from tripl.core.analyzers.event_plan import PlannedEvent

FD = uuid.uuid4()


def _row(name: str, payload: dict, count: int | None) -> PlannedEvent:
    return PlannedEvent(
        name=name,
        field_values=((FD, "payload", json.dumps(payload, sort_keys=True)),),
        matched_rule_name=None,
        row_count=count,
    )


def test_every_row_of_an_identity_carries_the_union() -> None:
    rows = [
        _row("a", {"x": "${payload.x}"}, 1),
        _row("a", {"y": {"z": "${payload.y.z}"}}, 3),
        _row("b", {"w": "${payload.w}"}, 5),
    ]
    folded, presence = fold_json_properties(rows, ["payload"])

    union = {"x": "${payload.x}", "y": {"z": "${payload.y.z}"}}
    assert [json.loads(r.field_values[0][2]) for r in folded[:2]] == [union, union]
    assert json.loads(folded[2].field_values[0][2]) == {"w": "${payload.w}"}
    assert presence == {
        "a": {"payload.x": 0.25, "payload.y.z": 0.75},
        "b": {"payload.w": 1.0},
    }


def test_a_single_row_is_left_byte_for_byte() -> None:
    row = _row("a", {"b": "${payload.b}", "a": "kept"}, 2)
    folded, _ = fold_json_properties([row], ["payload"])
    assert folded == [row]


def test_the_busiest_row_wins_a_kept_literal() -> None:
    # Ascending by count, as ``_rows_most_frequent_last`` orders them.
    rows = [_row("a", {"env": "staging"}, 1), _row("a", {"env": "prod"}, 9)]
    folded, presence = fold_json_properties(rows, ["payload"])
    assert json.loads(folded[0].field_values[0][2]) == {"env": "prod"}
    assert presence == {"a": {}}


def test_a_row_without_keys_counts_as_absence() -> None:
    rows = [_row("a", {}, 3), _row("a", {"x": "${payload.x}"}, 1)]
    _, presence = fold_json_properties(rows, ["payload"])
    assert presence == {"a": {"payload.x": 0.25}}


def test_no_presence_without_counts() -> None:
    rows = [_row("a", {"x": "${payload.x}"}, None), _row("a", {"y": "${payload.y}"}, 2)]
    folded, presence = fold_json_properties(rows, ["payload"])
    assert json.loads(folded[1].field_values[0][2]) == {"x": "${payload.x}", "y": "${payload.y}"}
    assert presence == {}


def test_other_columns_are_untouched() -> None:
    row = PlannedEvent(
        name="a",
        field_values=((FD, "screen", "home"),),
        matched_rule_name=None,
        row_count=1,
    )
    assert fold_json_properties([row], ["payload"]) == ([row], {})


def test_folding_two_events_keeps_the_higher_measured_rate() -> None:
    from tripl.core.analyzers._event_generator_merge import _fold_presence

    assert _fold_presence(None, 0.4) == 0.4
    assert _fold_presence(0.4, None) == 0.4
    assert _fold_presence(0.4, 0.9) == 0.9
    assert _fold_presence(None, None) is None
