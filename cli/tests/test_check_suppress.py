"""``tripl-check-ignore`` markers: where the scan reads one, what it says, which calls it covers.

Every source here is SYNTHETIC. The placement rules are pinned per comment
form and per language, because each language's lexer decides what is a
comment and what is a string, and a marker read inside a string would waive a
finding nobody chose to waive.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tripl_cli.check import suppress, yamlish
from tripl_cli.check.calls import find_calls
from tripl_cli.check.config import parse
from tripl_cli.check.lexer import LineIndex, tokenize
from tripl_cli.check.model import (
    MARKER_NO_CODES,
    MARKER_NO_REASON,
    MARKER_NOT_SUPPRESSIBLE,
    CheckFinding,
    Marker,
    Origin,
    Suppression,
)
from tripl_cli.check.scan import MAX_EXPANSION, _Collected, call_items, scan
from tripl_cli.check.suppress import attach, comments, markers
from tripl_cli.check.values import Value


def _source(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def _markers(text: str, language: str) -> list[Marker]:
    source = _source(text)
    tokens = tokenize(source, language)
    return [entry.marker for entry in markers("f", source, tokens, language, LineIndex(source))]


def _covered(text: str, language: str) -> dict[str, list[int]]:
    """Each call (callee, or receiver + selector) -> the lines of the markers covering it."""
    source = _source(text)
    tokens = tokenize(source, language)
    lines = LineIndex(source)
    calls = find_calls(tokens, language, source)
    covering = attach(markers("f", source, tokens, language, lines), calls, lines)
    named: dict[str, list[int]] = {}
    for index, call in enumerate(calls):
        name = call.callee + (f" {call.selector}" if call.selector else "")
        named[name] = [suppression.line for suppression in covering.get(index, ())]
    return named


# --- comment forms -------------------------------------------------------------
def test_a_trailing_line_comment_covers_the_call_on_its_line() -> None:
    covered = _covered(
        """
        tracker.track(name: "Cart")  // tripl-check-ignore[unknown_field:ab_variant] -- experiment
        tracker.other(name: "Next")
        """,
        "swift",
    )
    assert covered == {"tracker.track": [1], "tracker.other": []}


def test_an_own_line_marker_skips_blank_and_comment_lines_to_the_next_code_line() -> None:
    covered = _covered(
        """
        fun f() {
            // tripl-check-ignore[unknown_event] -- debug-only event, stripped from release builds

            /** Sent from the debug menu. */
            analytics.track("debug_tap", mapOf("screen" to "home"))
            analytics.next("after")
        }
        """,
        "kotlin",
    )
    assert covered["analytics.track"] == [2]
    assert covered["mapOf"] == [2]  # starts on the same line: covered too
    assert covered["analytics.next"] == []


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("swift", '/// tripl-check-ignore[unknown_event] -- a doc comment\nlog(name: "x")\n'),
        ("objc", '/* tripl-check-ignore[unknown_event] -- one line */\nlog(@"x");\n'),
        ("java", '/**\n * tripl-check-ignore[unknown_event] -- in a javadoc\n */\nlog("x");\n'),
        (
            "kotlin",
            '/* outer /* inner */\n   tripl-check-ignore[unknown_event] -- nested */\nlog("x")\n',
        ),
        ("swift", '/* a /* b */ c\n tripl-check-ignore[unknown_event] -- nested */\nlog("x")\n'),
    ],
)
def test_every_comment_form_reads_the_marker(language: str, text: str) -> None:
    [marker] = _markers(text, language)
    assert marker.targets == (("unknown_event", None),)
    assert marker.problem is None
    assert _covered(text, language)["log"] == [marker.line]


def test_the_reason_of_a_block_marker_ends_where_the_comment_does() -> None:
    [marker] = _markers("/* tripl-check-ignore[unknown_event] -- legacy screen */ x()\n", "ts")
    assert marker.reason == "legacy screen"
    assert marker.text == "tripl-check-ignore[unknown_event] -- legacy screen"


@pytest.mark.parametrize(
    "line",
    [
        'const el = <B onClick={() => track("a")}>Go</B> // tripl-check-ignore[unknown_event] -- r',
        'el = <A onClick={() => track("a")}>x</A>{/* tripl-check-ignore[unknown_event] -- r */}',
    ],
)
def test_a_marker_after_a_jsx_closing_tag_is_read(line: str) -> None:
    # `</` once lexed as a regex that ran to the comment's first slash, so the
    # marker vanished: neither applied nor listed.
    [marker] = _markers(line + "\n", "ts")
    assert (marker.line, marker.problem) == (1, None)
    assert _covered(line + "\n", "ts")["track"] == [1]


def test_a_jsx_comment_covers_the_element_on_the_next_line() -> None:
    covered = _covered(
        """
        return (
          <div>
            {/* tripl-check-ignore[value_not_allowed:plan] -- mapped by the proxy */}
            <Button onClick={() => track("buy", { plan: p })} />
          </div>
        )
        """,
        "ts",
    )
    assert covered["track"] == [3]


def test_an_objc_message_is_covered_from_the_line_above() -> None:
    covered = _covered(
        """
        /* tripl-check-ignore[deprecated_event] -- legacy screen, removed in 5.0 */
        [Tracker trackWithCategory:@"cart" action:@"old"];
        [Tracker trackScreen:@"cart"];
        """,
        "objc",
    )
    assert covered == {"Tracker trackWithCategory:action:": [1], "Tracker trackScreen:": []}


# --- spans -------------------------------------------------------------------------
def test_a_marker_on_any_line_of_a_multi_line_call_covers_it() -> None:
    for marker_line in (2, 3):
        lines = [
            "Analytics.shared.log(category: .home,",
            "                     action: .open,",
            '                     label: "card")',
        ]
        lines[marker_line - 1] += "  // tripl-check-ignore[unknown_event] -- on purpose"
        covered = _covered("\n".join(lines) + "\n", "swift")
        assert covered["Analytics.shared.log"] == [marker_line]


def test_a_marker_on_a_builder_chain_link_covers_the_call() -> None:
    covered = _covered(
        """
        tracker.track(Structured("cat", "act")
            .label("card")  // tripl-check-ignore[value_not_allowed:label] -- legacy label
            .value(3.0))
        """,
        "kotlin",
    )
    assert covered["Structured"] == [2]
    assert covered["tracker.track"] == [2]


def test_two_calls_on_one_line_and_nested_calls_are_all_covered() -> None:
    covered = _covered(
        'log("a"); log2(inner("b"))  // tripl-check-ignore[unknown_event] -- both\n', "ts"
    )
    assert covered == {"log": [1], "log2": [1], "inner": [1]}


def test_a_trailing_marker_does_not_reach_the_next_line() -> None:
    covered = _covered('setup()  // tripl-check-ignore[unknown_event] -- setup\nlog("a")\n', "ts")
    assert covered == {"setup": [1], "log": []}


# --- what is not a marker -----------------------------------------------------------
@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("ts", 'const s = "// tripl-check-ignore[unknown_event] -- in a string"\n'),
        ("ts", "const t = `/* tripl-check-ignore[unknown_event] -- in a template */`\n"),
        ("ts", "const r = /tripl-check-ignore[unknown_event] -- in a regex/\n"),
        ("swift", 'let r = #"// tripl-check-ignore[unknown_event] -- raw"#\n'),
        ("kotlin", 'val s = """\n// tripl-check-ignore[unknown_event] -- text block\n"""\n'),
        ("ts", "// TODO tripl-check-ignore[unknown_event] -- mid-sentence\n"),
        ("ts", "/* see tripl-check-ignore[unknown_event] -- mid-sentence */\n"),
        ("ts", "// tripl-check-ignored[unknown_event] -- another word\n"),
    ],
)
def test_text_that_only_mentions_the_keyword_is_not_a_marker(language: str, text: str) -> None:
    assert _markers(text, language) == []


def test_a_file_without_the_keyword_is_not_walked(monkeypatch: pytest.MonkeyPatch) -> None:
    def walked(*_args: object) -> object:
        raise AssertionError("the comment walk ran on a file without the keyword")

    monkeypatch.setattr(suppress, "comments", walked)
    source = "// an ordinary comment\nlog('a')\n"
    assert markers("f", source, tokenize(source, "ts"), "ts", LineIndex(source)) == []


def test_gaps_between_tokens_hold_only_whitespace_and_comments() -> None:
    sources = {
        "swift": 'let a = "x // y" /* c /* d */ e */ + #"raw"# // tail\nf(\\(g))\n',
        "kotlin": 'val s = "a ${b /* not a comment */} c" // c\n/* x /* y */ z */ f()\n',
        "ts": "const r = /a\\/b/g; // c\nconst t = `${x /* in */}` /* d */ ;\n<p>{/* e */}</p>\n",
        "objc": '[A b:@"c" /* d */ e:1]; // f\n',
        "java": 'String s = """\n  // no\n  """; /* c */ int x = 1; // y\n',
    }
    for language, source in sources.items():
        tokens = tokenize(source, language)
        found = list(comments(source, tokens, language))
        rest = list(source)
        for start, end, _ in found:
            rest[start:end] = [" "] * (end - start)
        for token in tokens:
            rest[token.offset : token.end] = [" "] * (token.end - token.offset)
        assert "".join(rest).strip() == "", language


# --- grammar --------------------------------------------------------------------------
def test_targets_and_fields_are_read_in_order() -> None:
    [marker] = _markers(
        "// tripl-check-ignore[unknown_event, unknown_field:ab variant ] -- two codes\n", "ts"
    )
    assert marker.targets == (("unknown_event", None), ("unknown_field", "ab variant"))
    assert marker.reason == "two codes"


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("// tripl-check-ignore[unknown_event]\n", MARKER_NO_REASON),
        ("// tripl-check-ignore[unknown_event] --\n", MARKER_NO_REASON),
        ("// tripl-check-ignore[unknown_event] -- ...!\n", MARKER_NO_REASON),
        ("// tripl-check-ignore[unknown_event] because -- late\n", MARKER_NO_REASON),
        ("// tripl-check-ignore -- no brackets\n", MARKER_NO_CODES),
        ("// tripl-check-ignore[] -- empty\n", MARKER_NO_CODES),
        ("// tripl-check-ignore[Unknown_Event] -- upper case\n", MARKER_NO_CODES),
        ("// tripl-check-ignore [unknown_event] -- a space\n", MARKER_NO_CODES),
        ("// tripl-check-ignore[no_verdict] -- never\n", MARKER_NOT_SUPPRESSIBLE),
        ("// tripl-check-ignore[unknown_event,no_verdict] -- never\n", MARKER_NOT_SUPPRESSIBLE),
    ],
)
def test_a_malformed_marker_is_read_and_suppresses_nothing(text: str, problem: str) -> None:
    source = text + 'log("a")\n'
    [marker] = _markers(source, "ts")
    assert marker.problem == problem
    assert marker.suppression() is None
    assert _covered(source, "ts") == {"log": []}


def test_code_field_narrows_the_waiver_to_that_field() -> None:
    waiver = Suppression("f", 1, 4, (("value_not_allowed", "plan"), ("unknown_event", None)), "r")
    assert waiver.covers(CheckFinding("value_not_allowed", "error", "x", field="plan"))
    assert not waiver.covers(CheckFinding("value_not_allowed", "error", "x", field="tier"))
    assert waiver.covers(CheckFinding("unknown_event", "error", "x"))
    assert waiver.covers(CheckFinding("unknown_event", "error", "x", field="any"))
    assert not waiver.covers(CheckFinding("unknown_field", "warning", "x", field="plan"))


def test_no_verdict_is_never_covered_whatever_the_marker_lists() -> None:
    waiver = Suppression("f", 1, 4, (("no_verdict", None),), "r")
    assert not waiver.covers(CheckFinding("no_verdict", "error", "x"))


# --- scan ------------------------------------------------------------------------------
def test_every_expanded_item_of_a_call_carries_its_suppressions() -> None:
    waiver = Suppression("f", 1, 4, (("unknown_event", None),), "r")
    collected = _Collected(
        name=Value("choices", choices=tuple(f"event_{n}" for n in range(MAX_EXPANSION + 10))),
        fields={},
    )
    items = call_items(collected, "legacy", Origin("f", 2, 1), suppressions=(waiver,))
    assert len(items) == MAX_EXPANSION
    assert all(item.suppressions == (waiver,) for item in items)
    # Outside equality: a waived call is still sent once with its identical twins.
    plain = call_items(collected, "legacy", Origin("f", 2, 1))
    assert items == plain


def test_the_scan_reports_every_marker_and_attaches_the_valid_ones(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text(
        _source(
            """
            // tripl-check-ignore[unknown_event] -- staged rollout
            track("one")
            track("two")  // tripl-check-ignore[unknown_event]
            // tripl-check-ignore[unknown_event] -- nothing below
            """
        ),
        encoding="utf-8",
    )
    config = parse(
        yamlish.loads(
            textwrap.dedent(
                """
                project: demo
                sources: ["src/**"]
                event_types:
                  legacy:
                    calls: [{function: "track", name_arg: 0}]
                """
            )
        ),
        path=None,
        base=tmp_path,
    )
    outcome = scan(config)
    assert [(m.path, m.line, m.problem) for m in outcome.markers] == [
        ("src/a.ts", 1, None),
        ("src/a.ts", 3, MARKER_NO_REASON),
        ("src/a.ts", 4, None),
    ]
    first, second = outcome.items
    assert [s.line for s in first.suppressions] == [1]
    assert second.suppressions == ()


def _scan_ts(root: Path, source: str) -> list[tuple[str | None, list[int]]]:
    """``source`` as ``src/a.ts``, scanned with ``track`` matched: each item's name and waivers."""
    (root / "src").mkdir()
    (root / "src" / "a.ts").write_text(_source(source), encoding="utf-8")
    config = parse(
        yamlish.loads(
            'project: demo\nsources: ["src/**"]\n'
            'event_types: {legacy: {calls: [{function: "track", name_arg: 0}]}}\n'
        ),
        path=None,
        base=root,
    )
    return [(item.name, [s.line for s in item.suppressions]) for item in scan(config).items]


def test_a_call_no_spec_matches_does_not_shift_the_waiver(tmp_path: Path) -> None:
    # Markers attach to the matched calls only, by their order among those:
    # `setup()` before the marker must not move it onto the next track.
    assert _scan_ts(
        tmp_path,
        """
        setup()
        // tripl-check-ignore[unknown_event] -- staged rollout
        track("one")
        track("two")
        """,
    ) == [("one", [2]), ("two", [])]


def test_a_call_no_spec_matches_on_the_marked_line_does_not_shift_it(tmp_path: Path) -> None:
    assert _scan_ts(
        tmp_path,
        """
        setup(); track("one")  // tripl-check-ignore[unknown_event] -- staged rollout
        track("two")
        """,
    ) == [("one", [1]), ("two", [])]
