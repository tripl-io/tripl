"""The plan-independent lint (``lint:`` in the check config): rules, config, and every surface.

Three rules need no plan: a name or key with a non-ASCII character, whitespace
or a quote; a name or key that does not match its configured pattern; and a
name or key read from a constant that holds the same value as another constant
of its object. Each is a fixed warning on the call, in a static run, a payload
run and ``--list``, and goes through markers, fingerprints and the gate like any
other finding.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tripl_cli.check.config import LintConfig, load, parse
from tripl_cli.check.lint import lint_items
from tripl_cli.check.model import (
    CODE_DUPLICATE_CONSTANT,
    CODE_NAME_CHARACTERS,
    CODE_NAME_PATTERN,
    LINT_CODES,
    SEVERITY_WARNING,
    CheckItem,
    Origin,
)
from tripl_cli.check.render import RULE_TEXT
from tripl_cli.check.scan import scan
from tripl_cli.cli import main
from tripl_cli.errors import TriplConfigError

from .conftest import FakeInstance
from .test_check_cmd import VALIDATE_URL, _respond, _write
from .test_check_gate_cmd import change, save


def _item(
    name: str | None = "checkout_started",
    *,
    fields: dict[str, str | None] | None = None,
    properties: dict[str, Any] | None = None,
) -> CheckItem:
    return CheckItem(
        origin=Origin("app/Screen.kt", 3, 9, snippet="analytics.logEvent"),
        event_type="event",
        name=name,
        fields=fields or {},
        properties=properties,
        complete=False,
    )


def _notes(item: CheckItem, lint: LintConfig | None = None) -> list[tuple[str, str | None, str]]:
    [linted] = lint_items([item], LintConfig() if lint is None else lint)
    assert all(note.severity == SEVERITY_WARNING for note in linted.notes)
    return [(note.code, note.field, note.message) for note in linted.notes]


# --- the character rule -------------------------------------------------------------
def test_a_cyrillic_letter_in_a_latin_name_is_named_by_its_code_point() -> None:
    # The second letter is CYRILLIC SMALL LETTER IE, which reads as a Latin e.
    assert _notes(_item("sеttings_opened")) == [
        (
            CODE_NAME_CHARACTERS,
            None,
            "the event name 's\\u0435ttings_opened' has a non-ASCII character "
            "U+0435 CYRILLIC SMALL LETTER IE",
        )
    ]


def test_quotes_and_whitespace_in_keys_are_flagged_on_their_key() -> None:
    item = _item(
        "it's_opened",
        fields={"screen name": "home"},
        properties={"it’s": 1, "plan": "annual"},
    )
    assert _notes(item) == [
        (CODE_NAME_CHARACTERS, None, 'the event name "it\'s_opened" has a quote U+0027 APOSTROPHE'),
        (
            CODE_NAME_CHARACTERS,
            "screen name",
            "the field key 'screen name' has whitespace U+0020 SPACE",
        ),
        (
            CODE_NAME_CHARACTERS,
            "it’s",
            "the property key 'it\\u2019s' has a non-ASCII character "
            "U+2019 RIGHT SINGLE QUOTATION MARK",
        ),
    ]


def test_a_clean_name_and_keys_get_nothing_and_the_item_is_the_same_object() -> None:
    item = _item(fields={"screen": "home"}, properties={"plan": "annual"})
    [linted] = lint_items([item], LintConfig(name_pattern=None))
    assert linted is item


def test_a_runtime_part_of_a_name_is_no_character_and_skips_name_pattern() -> None:
    lint = LintConfig(name_pattern=parse_pattern("[a-z_]+"))
    assert _notes(_item("promo_${sheet id}_shown"), lint) == []
    # The literal text around the runtime part is still read.
    [(code, _, message)] = _notes(_item("promo sheet_${id}_shown"), lint)
    assert (code, message) == (
        CODE_NAME_CHARACTERS,
        "the event name 'promo sheet_${id}_shown' has whitespace U+0020 SPACE",
    )


@pytest.mark.parametrize(
    ("char", "named"),
    [
        ("\t", "U+0009 CHARACTER TABULATION"),
        ("\n", "U+000A LINE FEED"),
        ("\r", "U+000D CARRIAGE RETURN"),
        ("\x85", "U+0085 NEXT LINE"),
    ],
)
def test_a_control_whitespace_character_is_named_by_its_alias(char: str, named: str) -> None:
    [(_, _, message)] = _notes(_item(f"a{char}b"))
    assert message.endswith(named)


def test_rules_switched_off_report_nothing() -> None:
    lint = LintConfig(characters=False, duplicates=False)
    assert _notes(_item("sеttings opened"), lint) == []


def test_every_lint_code_has_a_rule_text_for_sarif() -> None:
    assert set(LINT_CODES) <= set(RULE_TEXT)


def test_no_lint_section_leaves_every_item_alone() -> None:
    items = [_item("sеttings opened")]
    [same] = lint_items(items, None)
    assert same is items[0]


# --- the patterns -----------------------------------------------------------------
def parse_pattern(text: str) -> Any:
    lint = parse({"lint": {"name_pattern": text}}, path=None, base=Path.cwd()).lint
    assert lint is not None
    return lint.name_pattern


def test_name_pattern_and_key_pattern_must_match_the_whole_text() -> None:
    lint = parse(
        {"lint": {"name_pattern": "[a-z][a-z0-9_]*", "key_pattern": "[a-z_]+"}},
        path=None,
        base=Path.cwd(),
    ).lint
    assert lint is not None
    assert _notes(_item("checkout_started", properties={"plan": 1}), lint) == []
    # `checkout_started!` contains a match: only the whole name counts.
    assert _notes(_item("checkout_started!", fields={"Screen": "x"}), lint) == [
        (
            CODE_NAME_PATTERN,
            None,
            "the event name 'checkout_started!' does not match name_pattern '[a-z][a-z0-9_]*'",
        ),
        (
            CODE_NAME_PATTERN,
            "Screen",
            "the field key 'Screen' does not match key_pattern '[a-z_]+'",
        ),
    ]


# --- the config -----------------------------------------------------------------------
def test_a_bare_lint_section_turns_on_both_rules_with_no_setting(tmp_path: Path) -> None:
    assert parse({}, path=None, base=tmp_path).lint is None
    assert parse({"lint": None}, path=None, base=tmp_path).lint == LintConfig()
    assert parse({"lint": {}}, path=None, base=tmp_path).lint == LintConfig()
    lint = parse({"lint": {"duplicates": False}}, path=None, base=tmp_path).lint
    assert lint == LintConfig(duplicates=False)


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"names": True}, "lint: unknown key(s) names"),
        ({"name_pattern": "[a-z"}, "lint.name_pattern: '[a-z' is not a valid regex"),
        ({"key_pattern": ""}, "lint.key_pattern: expected a non-empty string"),
        ({"characters": "yes"}, "lint.characters: expected true or false, got 'yes'"),
        ("on", "lint: expected a mapping"),
    ],
)
def test_lint_config_mistakes_are_usage_errors(tmp_path: Path, section: Any, message: str) -> None:
    with pytest.raises(TriplConfigError, match=re.escape(message)):
        parse({"lint": section}, path=None, base=tmp_path)


# --- the duplicate rule, through the scan -------------------------------------------
KOTLIN_CONFIG = """
project: shop
sources: ["app/**"]
lint:
event_types:
  event:
    calls:
      - {function: "analytics.logEvent", positional: [name, properties]}
"""

KOTLIN_SOURCES = {
    "app/Events.kt": """
        package app

        const val TOP_A = "top"
        const val TOP_B = "top"

        object Events {
            const val SAVE_TAP = "save_tap"
            const val SHARE_TAP = "save_tap"
            const val UNUSED = "open_tap"
            const val OPEN_TAP = "open_tap"
        }

        object Other {
            const val SAVE = "save_tap"
        }

        object Keys {
            const val SCREEN = "screen"
            const val PLACE = "screen"
        }
    """,
    "app/Screen.kt": """
        package app

        class Screen {
            fun save() { analytics.logEvent(Events.SAVE_TAP) }
            fun share() { analytics.logEvent(Events.SHARE_TAP, mapOf(Keys.SCREEN to "x")) }
            fun open() { analytics.logEvent(Events.OPEN_TAP, mapOf(Keys.PLACE to "y")) }
            fun other() { analytics.logEvent(Other.SAVE) }
            fun top() { analytics.logEvent(TOP_A); analytics.logEvent(TOP_B) }
        }
    """,
}


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    _write(root, {".tripl/check.yml": KOTLIN_CONFIG, **KOTLIN_SOURCES})
    return root


def _scanned(root: Path) -> dict[tuple[int, str | None], list[tuple[str, str | None, str]]]:
    items = scan(load(root / ".tripl" / "check.yml")).items
    return {
        (item.origin.line, item.name): [(n.code, n.field, n.message) for n in item.notes]
        for item in items
    }


def test_two_constants_of_one_object_with_one_value_flag_every_call_using_one(
    shop: Path,
) -> None:
    found = _scanned(shop)
    assert found[(4, "save_tap")] == [
        (
            CODE_DUPLICATE_CONSTANT,
            None,
            "the event name is read from Events.SAVE_TAP = 'save_tap', the same value as "
            "Events.SHARE_TAP of Events; one of them probably names the wrong event",
        )
    ]
    assert found[(5, "save_tap")] == [
        (
            CODE_DUPLICATE_CONSTANT,
            None,
            "the event name is read from Events.SHARE_TAP = 'save_tap', the same value as "
            "Events.SAVE_TAP of Events; one of them probably names the wrong event",
        ),
        (
            CODE_DUPLICATE_CONSTANT,
            "screen",
            "the key 'screen' is read from Keys.SCREEN = 'screen', the same value as "
            "Keys.PLACE of Keys; one of them probably names the wrong key",
        ),
    ]
    # OPEN_TAP's twin is used by no call; its key's twin is.
    assert [code for code, _, _ in found[(6, "open_tap")]] == [CODE_DUPLICATE_CONSTANT]
    assert found[(6, "open_tap")][0][1] == "screen"
    # The same value in another object, and two file-scope constants: nothing.
    assert found[(7, "save_tap")] == []
    assert found[(8, "top")] == []


def test_without_a_lint_section_the_scan_adds_no_finding(shop: Path) -> None:
    _write(shop, {".tripl/check.yml": KOTLIN_CONFIG.replace("lint:\n", "")})
    assert all(notes == [] for notes in _scanned(shop).values())


TRACK_CONFIG = """
project: shop
sources: ["app/**"]
lint:
event_types:
  event:
    calls:
      - {function: "track", positional: [name, properties]}
      - {function: "log", positional: [name]}
"""


def _scan_notes(root: Path, sources: dict[str, str], config: str = TRACK_CONFIG) -> list[Any]:
    _write(root, {".tripl/check.yml": config, **sources})
    items = scan(load(root / ".tripl" / "check.yml")).items
    return [(item.origin.path, [(n.code, n.field) for n in item.notes]) for item in items]


def test_same_named_objects_in_two_files_are_two_objects(tmp_path: Path) -> None:
    # Two feature modules each declare their own `Events`, and each logs the
    # same event name once: no constant has a twin in its own object.
    sources = {
        "app/home/Events.kt": 'package app.home\nobject Events { const val SHOW = "opened" }\n',
        "app/map/Events.kt": 'package app.map\nobject Events { const val OPEN = "opened" }\n',
        "app/home/Home.kt": "package app.home\nfun h() { track(Events.SHOW) }\n",
        "app/map/Map.kt": "package app.map\nfun m() { track(Events.OPEN) }\n",
        "app/a/Keys.java": (
            'package a;\npublic final class Keys { public static final String SHOW = "x"; }\n'
        ),
        "app/b/Keys.java": (
            'package b;\npublic final class Keys { public static final String OPEN = "x"; }\n'
        ),
        "app/a/A.java": "package a;\nclass A { void f() { track(Keys.SHOW); } }\n",
        "app/b/B.java": "package b;\nclass B { void f() { track(Keys.OPEN); } }\n",
        "app/home/events.ts": "export const Ev = { SHOW: 'opened' } as const;\n",
        "app/map/events.ts": "export const Ev = { OPEN: 'opened' } as const;\n",
        "app/home/home.ts": "import { Ev } from './events';\ntrack(Ev.SHOW);\n",
        "app/map/map.ts": "import { Ev } from './events';\ntrack(Ev.OPEN);\n",
    }
    found = _scan_notes(tmp_path, sources)
    assert len(found) == 6
    assert all(notes == [] for _, notes in found)


def test_a_reference_that_may_name_several_constants_reads_none_of_them(tmp_path: Path) -> None:
    # `.home` can be Screen.home, Tab.home or Old.home: which one the call
    # reads is unknown, so Old.home's twin says nothing about it.
    sources = {
        "app/E.swift": (
            'enum Screen: String { case home = "home" }\n'
            'enum Tab: String { case home = "home" }\n'
            'enum Old { static let home = "home"; static let start = "home" }\n'
        ),
        "app/A.swift": "func f() { track(Old.start) }\nfunc g() { log(.home) }\n",
    }
    items = {item.origin.line: item for item in _scanned_items(tmp_path, sources)}
    assert items[2].constants == ()
    assert items[2].notes == ()
    # Old.start's own twin is used by no call.
    assert items[1].notes == ()


def _scanned_items(root: Path, sources: dict[str, str]) -> tuple[CheckItem, ...]:
    _write(root, {".tripl/check.yml": TRACK_CONFIG, **sources})
    return scan(load(root / ".tripl" / "check.yml")).items


def test_a_static_run_lints_no_field_key_the_check_config_names(tmp_path: Path) -> None:
    # `screenName` is written once, in check.yml: no call site can fix it, so
    # it is not reported on each of them. The code's own keys still are.
    config = TRACK_CONFIG.replace("lint:\n", 'lint:\n  key_pattern: "[a-z_]+"\n').replace(
        "positional: [name]}", 'positional: [name, "field:screenName"]}'
    )
    sources = {
        "app/E.kt": (
            'fun f() { log("open", "home"); log("close", "home") }\n'
            'fun g() { track("save", mapOf("Bad" to 1)) }\n'
        )
    }
    found = _scan_notes(tmp_path, sources, config)
    assert found == [
        ("app/E.kt", []),
        ("app/E.kt", []),
        ("app/E.kt", [(CODE_NAME_PATTERN, "Bad")]),
    ]


# --- tripl check ------------------------------------------------------------------------
SWIFT_CONFIG = """
project: demo
sources: ["Sources/**"]
lint:
  name_pattern: "[a-z][a-z0-9_]*"
event_types:
  legacy:
    calls: [{function: "Analytics.shared.logEvent", name_arg: 0, properties_arg: parameters}]
"""


@pytest.fixture
def validator(tripl_api: FakeInstance) -> Any:
    return tripl_api.handler(VALIDATE_URL, _respond, method="POST")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two buttons, two constants, one event the plan knows: no plan check sees it."""
    root = tmp_path / "repo"
    _write(
        root,
        {
            ".tripl/check.yml": SWIFT_CONFIG,
            "Sources/Events.swift": """
                enum Events {
                    static let save = "trial_started"
                    static let share = "promo_sheet_x_shown"
                }
            """,
            "Sources/Buttons.swift": """
                func save() {
                    Analytics.shared.logEvent(Events.save)
                }

                func share() {
                    Analytics.shared.logEvent(Events.share)
                }
            """,
        },
    )
    (root / ".git").mkdir()
    monkeypatch.chdir(root / "Sources")
    return root


DUPLICATED = (
    "enum Events {\n"
    '    static let save = "trial_started"\n'
    '    static let share = "trial_started"\n'
    "}\n"
)


def test_a_lint_finding_is_a_warning_that_fails_only_under_strict(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--strict"]) == 0
    capsys.readouterr()
    _write(repo, {"Sources/Events.swift": DUPLICATED})
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "Sources/Buttons.swift:2:5  warning  legacy  trial_started" in out
    assert (
        "    warning  duplicate_constant: the event name is read from Events.save = "
        "'trial_started', the same value as Events.share of Events; one of them probably "
        "names the wrong event" in out
    )
    assert out.rstrip().endswith("2 call sites in 2 files: 0 ok, 2 warnings, 0 errors.")
    assert main(["check", "--strict"]) == 1


def test_a_marker_suppresses_a_lint_finding(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = "// tripl-check-ignore[duplicate_constant] -- both buttons save on purpose"
    _write(
        repo,
        {
            "Sources/Events.swift": DUPLICATED,
            "Sources/Buttons.swift": (
                f"func save() {{\n    {marker}\n    Analytics.shared.logEvent(Events.save)\n}}\n"
                f"func share() {{\n    {marker}\n    Analytics.shared.logEvent(Events.share)\n}}\n"
            ),
        },
    )
    assert main(["check", "--strict"]) == 0
    assert "2 findings suppressed" in capsys.readouterr().out


def test_lint_findings_have_line_free_fingerprints(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, {"Sources/Events.swift": DUPLICATED})

    def fingerprints() -> list[str]:
        assert main(["check", "--json"]) == 0
        document = json.loads(capsys.readouterr().out)
        return [
            finding["fingerprint"]
            for result in document["results"]
            for finding in result["findings"]
            if finding["code"] == CODE_DUPLICATE_CONSTANT
        ]

    before = fingerprints()
    assert len(before) == 2
    buttons = repo / "Sources" / "Buttons.swift"
    buttons.write_text("// Moved down.\n\n" + buttons.read_text(encoding="utf-8"), "utf-8")
    assert fingerprints() == before


def test_a_constant_edited_into_a_duplicate_is_new_at_the_call_the_change_left_alone(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    patch = save(repo, change(repo, {"Sources/Events.swift": DUPLICATED}))
    assert main(["check", "--strict", "--new-from-patch", patch]) == 1
    out = capsys.readouterr().out
    # The save button's call and its constant are as they were; the finding is new anyway.
    assert "Sources/Buttons.swift:2:5  warning  legacy  trial_started" in out
    assert "Sources/Buttons.swift:6:5  warning  legacy  trial_started" in out


def test_a_lint_finding_the_base_had_is_not_new(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, {"Sources/Events.swift": DUPLICATED})
    buttons = (repo / "Sources" / "Buttons.swift").read_text(encoding="utf-8")
    patch = save(repo, change(repo, {"Sources/Buttons.swift": "// A note.\n" + buttons}))
    assert main(["check", "--strict", "--new-from-patch", patch]) == 0
    assert "0 call sites fail the run" in capsys.readouterr().out


def test_a_call_with_no_name_known_still_gets_its_key_linted(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        repo,
        {
            "Sources/Relay.swift": (
                "func relay(name: String) {\n"
                '    Analytics.shared.logEvent(name, parameters: ["it’s": 1])\n}\n'
            )
        },
    )
    assert main(["check", "--json"]) == 0
    [relayed] = [
        result
        for result in json.loads(capsys.readouterr().out)["results"]
        if result["location"]["path"] == "Sources/Relay.swift"
    ]
    assert relayed["status"] == "dynamic"
    assert [(f["code"], f["field"]) for f in relayed["findings"]] == [
        (CODE_NAME_CHARACTERS, "it’s")
    ]
    assert main(["check", "--strict"]) == 1


def test_payloads_get_the_character_and_pattern_rules_from_the_check_config(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    events = repo / "events.ndjson"
    events.write_text(
        json.dumps({"event_type": "legacy", "name": "trial_started"})
        + "\n"
        + json.dumps({"event_type": "legacy", "name": "trial_started", "properties": {"A b": 1}})
        + "\n",
        encoding="utf-8",
    )
    assert main(["check", "--payloads", str(events), "--json"]) == 0
    results = json.loads(capsys.readouterr().out)["results"]
    assert [r["status"] for r in results] == ["ok", "warning"]
    assert [(f["code"], f["field"]) for f in results[1]["findings"]] == [
        (CODE_NAME_CHARACTERS, "A b"),
    ]
    assert main(["check", "--payloads", str(events), "--strict"]) == 1


# --- tripl check --list -----------------------------------------------------------------
@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("tripl check --list sent a request")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)


def test_list_shows_the_lint_under_each_call_and_counts_it(
    repo: Path, offline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, {"Sources/Events.swift": DUPLICATED})
    assert main(["check", "--list"]) == 0
    out = capsys.readouterr().out
    assert (
        "Sources/Buttons.swift:2:5  legacy  Analytics.shared.logEvent  name=trial_started\n"
        "    warning  duplicate_constant: the event name is read from Events.save" in out
    )
    assert "\nLint, call sites per code: duplicate_constant 2.\n" in out
    assert main(["check", "--list", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["lint"] is True
    assert document["summary"]["lint"] == {"duplicate_constant": 2}
    first = document["calls"][0]["lint"]
    assert [(f["code"], f["severity"], f["field"]) for f in first] == [
        ("duplicate_constant", "warning", None)
    ]


def test_list_says_when_the_lint_found_nothing(
    repo: Path, offline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--list"]) == 0
    assert "\nLint: no findings.\n" in capsys.readouterr().out
