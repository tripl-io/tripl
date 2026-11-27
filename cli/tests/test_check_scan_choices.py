"""What the scan reads that depends on more than one value or on where it is written.

Conditionals over constants (``if`` / ``when`` / a ternary), locals set once to
a constant, Android's inline ``Bundle().apply { … }``, and the ``--list`` reason
for a constant that resolves to several values.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from tripl_cli.check import yamlish
from tripl_cli.check.calls import find_calls
from tripl_cli.check.config import CheckConfig, parse
from tripl_cli.check.inventory import inventory
from tripl_cli.check.lexer import tokenize
from tripl_cli.check.places import FilePlaces
from tripl_cli.check.scan import scan
from tripl_cli.check.symbols import SymbolIndex
from tripl_cli.check.values import Evaluator, Value

# Constants every snippet below may name, in each language's own syntax.
_CONSTANTS = {
    "kotlin": 'object Events {\n    const val A = "a"\n    const val B = "b"\n}\n',
    "java": 'class Events {\n static final String A = "a";\n static final String B = "b";\n}\n',
    "swift": 'enum Events {\n    static let a = "a"\n    static let b = "b"\n}\n',
    "ts": 'const Events = { A: "a", B: "b" };\n',
    "objc": 'NSString *const EventA = @"a";\nNSString *const EventB = @"b";\n',
}


def _read(language: str, body: str, *, index: str | None = None) -> Value:
    """The first argument of the first ``track(…)`` call in ``body``, read in its file.

    ``body`` follows the language's constants (or ``index``) in one file, as
    the scan reads it. Plain ``tripl check`` and ``--list`` must read the same.
    """
    source = (_CONSTANTS[language] if index is None else index) + textwrap.dedent(body)
    tokens = tokenize(source, language)
    symbols = SymbolIndex()
    symbols.index_tokens(tokens, language)
    places = FilePlaces(tokens, language, source)
    call = next(
        call
        for call in find_calls(tokens, language, source)
        if call.callee.endswith("track") or call.selector == "track:"
    )
    plain = Evaluator(symbols, language, places=places).evaluate(call.args[0].tokens)
    listed = Evaluator(symbols, language, explain=True, places=places).evaluate(call.args[0].tokens)
    assert (plain, plain.text, plain.choices) == (listed, listed.text, listed.choices)
    assert plain.reason == ""
    return listed


def _repo(root: Path, files: dict[str, str]) -> Path:
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    return root


def _config(root: Path, text: str) -> CheckConfig:
    return parse(yamlish.loads(textwrap.dedent(text)), path=None, base=root)


def _names(items: Any) -> list[tuple[str, str | None]]:
    return [(f"{item.origin.path}:{item.origin.line}", item.name) for item in items]


_LOG_CONFIG = """
project: demo
event_types:
  event:
    calls: [{function: "Tracker.log", positional: [name, properties]}]
"""


# --- conditionals ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("language", "body", "choices"),
    [
        ("kotlin", 'track(if (flag) "x" else "y")', ("x", "y")),
        ("kotlin", "track(if (flag) Events.A else Events.B)", ("a", "b")),
        ("kotlin", 'track(if (a) "x" else if (b) { "y" } else Events.A)', ("x", "y", "a")),
        # Branches that agree are one value.
        ("kotlin", "track(if (flag) Events.A else Events.A)", ("a",)),
        (
            "kotlin",
            """
            track(when (source) {
                Source.PUSH -> "push"
                Source.LINK, Source.SHARE -> { Events.B }
                else -> "other"
            })
            """,
            ("push", "b", "other"),
        ),
        ("kotlin", 'track(when { a -> "x"; b -> "y"; else -> "z" })', ("x", "y", "z")),
        (
            "kotlin",
            """
            track(when (x) {
                1 -> if (y) "a" else "b"
                else -> "c"
            })
            """,
            ("a", "b", "c"),
        ),
        ("swift", 'track(if flag { "x" } else { Events.b })', ("x", "b")),
        ("swift", 'track(flag ? "x" : Events.a)', ("x", "a")),
        ("java", "track(flag ? Events.A : Events.B);", ("a", "b")),
        ("java", 'track(a ? "x" : b ? "y" : "z");', ("x", "y", "z")),
        ("java", 'track(a ? b ? "x" : "y" : "z");', ("x", "y", "z")),
        ("ts", 'track(flag ? Events.A : "z")', ("a", "z")),
        ("objc", 'track(flag ? EventA : @"z");', ("a", "z")),
    ],
)
def test_a_conditional_over_constants_is_read_as_its_choices(
    language: str, body: str, choices: tuple[str, ...]
) -> None:
    value = _read(language, body)
    if len(choices) == 1:
        assert (value.kind, value.text) == ("text", choices[0])
    else:
        assert (value.kind, value.choices) == ("choices", choices)


_MANY = "\n".join(f'    {number} -> "e{number}"' for number in range(51))


@pytest.mark.parametrize(
    ("language", "body"),
    [
        # A branch that is no constant, or no value at all.
        ("kotlin", 'track(if (flag) "x" else name)'),
        ("kotlin", 'track(if (flag) "x" else null)'),
        ("kotlin", 'track(if (flag) "x" else "x_${id}")'),
        ("kotlin", 'track(if (flag) "x")'),
        ("kotlin", 'track(if (flag) { log("x")\n "x" } else "y")'),
        ("kotlin", 'track(if (flag) { "x"\n "y" } else "z")'),
        ("kotlin", 'track(when (x) {\n 1 -> "a"\n else -> name\n})'),
        # An entry with no condition, or a result that runs into the next entry.
        ("kotlin", 'track(when (x) { -> "a" })'),
        ("kotlin", 'track(when (x) { 1 -> "a" 2 -> "b" })'),
        # More values than a call is checked for.
        ("kotlin", f"track(when (x) {{\n{_MANY}\n}})"),
        ("swift", 'track(if flag { "x" })'),
        ("swift", 'track(switch x { case .a: "a" default: "b" })'),
        ("ts", 'track(flag ? "x" : name)'),
        ("java", 'track(flag ? "x" : "y" + id);'),
        ("objc", '[tracker track:flag ?: @"z"];'),
    ],
)
def test_a_conditional_with_a_branch_that_is_no_constant_stays_dynamic(
    language: str, body: str
) -> None:
    value = _read(language, body)
    assert (value.kind, value.reason) == ("dynamic", "conditional")


@pytest.mark.parametrize(
    ("language", "body"),
    [
        ("ts", 'track((x: boolean) => x ? "lazy_a" : "lazy_b")'),
        ("ts", 'track(x => x ? "lazy_a" : "lazy_b")'),
        ("java", 'track(() -> flag ? "a" : "b");'),
        ("java", "track(x -> x ? Events.A : Events.B);"),
    ],
)
def test_a_lambda_whose_body_is_a_ternary_is_no_choice(language: str, body: str) -> None:
    """The argument is a function: its returns are not the value sent."""
    value = _read(language, body)
    assert (value.kind, value.reason) == ("dynamic", "conditional")


def test_a_lambda_in_a_dictionary_entry_is_no_choice() -> None:
    value = _read("ts", 'track({ name: () => x ? "a" : "b" })')
    assert value.kind == "entries"
    [(key, entry)] = value.entries
    assert (key, entry.kind, entry.reason) == ("name", "dynamic", "conditional")


def test_a_when_is_read_only_where_the_file_s_line_breaks_are_known() -> None:
    """An evaluator with no file cannot tell where an entry ends."""
    tokens = tokenize('when (x) {\n 1 -> "a"\n else -> "b"\n}', "kotlin")
    value = Evaluator(SymbolIndex(), "kotlin", explain=True).evaluate(tokens)
    assert (value.kind, value.reason) == ("dynamic", "conditional")


def test_each_choice_of_a_conditional_is_checked(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/Paywall.kt": """
                fun show(premium: Boolean) {
                    Tracker.log(if (premium) "paywall_pro" else "paywall")
                    Tracker.log(if (premium) "paywall_pro" else screen)
                }
            """
        },
    )
    items = scan(_config(root, _LOG_CONFIG)).items
    assert _names(items) == [
        ("app/Paywall.kt:2", "paywall_pro"),
        ("app/Paywall.kt:2", "paywall"),
        ("app/Paywall.kt:3", None),
    ]


# --- locals -----------------------------------------------------------------------------
def test_a_local_set_once_to_a_constant_is_read_in_plain_check(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/Screen.kt": """
                const val SHARED = "file_scope"
                fun show(premium: Boolean, flag: Boolean) {
                    val screen = "home"
                    Tracker.log(screen)
                    Tracker.log("${screen}_shown")
                    val event = if (premium) "pro" else "free"
                    Tracker.log(event)
                    val copy = screen
                    Tracker.log(copy)
                    val SHARED = "local_wins"
                    Tracker.log(SHARED)
                    val typed: String = "typed"
                    Tracker.log(typed)
                    val split = if (flag) "a"
                        else "b"
                    Tracker.log(split)
                }
            """,
            "ios/Screen.swift": """
                func show() {
                    let screen = "swift_home"
                    Tracker.log(screen)
                }
            """,
            "src/Screen.java": """
                class Screen {
                    void show() {
                        String screen = "java_home";
                        Tracker.log(screen);
                        final String other = "java_final";
                        Tracker.log(other);
                    }
                }
            """,
            "web/screen.ts": """
                export function show() {
                  const screen = "ts_home";
                  Tracker.log(screen);
                }
            """,
        },
    )
    config = _config(root, _LOG_CONFIG)
    assert _names(scan(config).items) == [
        ("app/Screen.kt:4", "home"),
        ("app/Screen.kt:5", "home_shown"),
        ("app/Screen.kt:7", "pro"),
        ("app/Screen.kt:7", "free"),
        ("app/Screen.kt:9", "home"),
        ("app/Screen.kt:11", "local_wins"),
        ("app/Screen.kt:13", "typed"),
        ("app/Screen.kt:16", "a"),
        ("app/Screen.kt:16", "b"),
        ("ios/Screen.swift:3", "swift_home"),
        ("src/Screen.java:4", "java_home"),
        ("src/Screen.java:6", "java_final"),
        ("web/screen.ts:3", "ts_home"),
    ]
    # `--list` reads the very same values.
    assert all(call.fully_read for call in inventory(config).calls)


def test_a_local_the_scan_cannot_be_sure_of_stays_dynamic(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/Screen.kt": """
                fun reassignable() {
                    var screen = "home"
                    Tracker.log(screen)
                }
                fun computed() {
                    val screen = compute()
                    Tracker.log(screen)
                }
                fun twice(flag: Boolean) {
                    if (flag) {
                        val screen = "a"
                    }
                    val screen = "b"
                    Tracker.log(screen)
                }
                fun lambda(names: List<String>) {
                    val screen = "home"
                    names.forEach { screen -> Tracker.log(screen) }
                }
                fun parameter(screen: String) {
                    val screen2 = "x"
                    Tracker.log(screen)
                }
                fun destructured(pair: Pair<String, String>) {
                    val screen = "home"
                    run {
                        val (screen, other) = pair
                        Tracker.log(screen)
                    }
                }
                fun template(id: String) {
                    val screen = "home_${id}"
                    Tracker.log(screen)
                }
                fun nested() {
                    run {
                        val screen = "home"
                    }
                    Tracker.log(screen)
                }
            """,
            "ios/Screen.swift": """
                func optional(value: String?) {
                    if let screen = value {
                        Tracker.log(screen)
                    }
                }
                func continued() {
                    let screen = "a"
                        + suffix
                    Tracker.log(screen)
                }
                func closure() {
                    let screen = "home"
                    names.forEach { screen in Tracker.log(screen) }
                }
            """,
            "src/Screen.java": """
                class Screen {
                    void reassigned(boolean flag) {
                        String screen = "a";
                        if (flag) screen = "b";
                        Tracker.log(screen);
                    }
                    void appended() {
                        String screen = "a";
                        screen += "_b";
                        Tracker.log(screen);
                    }
                }
            """,
            "web/screen.ts": """
                export function mutable() {
                  let screen = "home";
                  Tracker.log(screen);
                }
            """,
        },
    )
    items = scan(_config(root, _LOG_CONFIG)).items
    assert [name for _, name in _names(items)] == [None] * 14


def test_a_local_declared_without_an_initializer_stays_dynamic(tmp_path: Path) -> None:
    """The ``=`` of the statement after ``val x: T`` is no initializer of ``x``."""
    root = _repo(
        tmp_path,
        {
            "ios/V.swift": """
                func show(premium: Bool) {
                    let screen: String
                    analytics.mode = "debug"
                    screen = premium ? loadScreen() : "home"
                    Tracker.log(screen)
                }
            """,
            "app/V.kt": """
                fun show() {
                    val name: String
                    label = "x"
                    name = compute()
                    Tracker.log(name)
                }
                fun split() {
                    val typed: String
                        = "typed"
                    Tracker.log(typed)
                }
            """,
        },
    )
    config = _config(root, _LOG_CONFIG)
    assert _names(scan(config).items) == [
        ("app/V.kt:5", None),
        ("app/V.kt:10", "typed"),
        ("ios/V.swift:5", None),
    ]
    listed = {
        f"{call.origin.path}:{call.origin.line}": call.fully_read
        for call in inventory(config).calls
    }
    assert listed == {"app/V.kt:5": False, "app/V.kt:10": True, "ios/V.swift:5": False}


@pytest.mark.parametrize(
    ("language", "body"),
    [
        ("kotlin", "fun f() {\n    val a = 1\n    val b = 2\n    track(a + b)\n}\n"),
        ("swift", "func f() {\n    let a = 1\n    let b = 2\n    track(a + b)\n}\n"),
        ("ts", "function f() { const base = 10; const extra = 5; track(base + extra) }"),
        ("java", "class A { void f() { int a = 1; int b = 2; track(a + b); } }"),
        ("java", 'class A { void f() { int a = 1; int b = 2; track(a + b + "x"); } }'),
    ],
)
def test_locals_set_to_numbers_are_added_not_joined(language: str, body: str) -> None:
    value = _read(language, body, index="")
    assert (value.kind, value.reason) == ("dynamic", "concatenation")


@pytest.mark.parametrize(
    ("language", "body"),
    [
        (
            "kotlin",
            'fun f() {\n    val name = "a"\n'
            "    try { g() } catch (name: Exception) { track(name) }\n}\n",
        ),
        ("ts", 'function f() { const name = "a"; try { g() } catch (name) { track(name) } }'),
        (
            "swift",
            'func f() {\n    let name = "a"\n'
            "    if case let .some(name) = opt { track(name) }\n}\n",
        ),
        (
            "swift",
            'func f() {\n    let name = "a"\n'
            "    if case let Result.success(name) = r { track(name) }\n}\n",
        ),
    ],
)
def test_a_catch_or_pattern_name_hides_a_local(language: str, body: str) -> None:
    value = _read(language, body, index="")
    assert value.kind == "dynamic"


def test_a_local_inside_an_interpolation_is_read_where_its_string_is(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "web/screen.ts": """
                export function show(id: string) {
                  const screen = "home";
                  Tracker.log(`${screen}_${id}_shown`);
                }
            """
        },
    )
    [item] = scan(_config(root, _LOG_CONFIG)).items
    assert item.name == "home_${id}_shown"


def test_locals_are_read_only_so_many_deep() -> None:
    lines = ['    val v0 = "deep"'] + [f"    val v{n} = v{n - 1}" for n in range(1, 12)]
    body = "fun go() {\n" + "\n".join(lines) + "\n    track(v{last})\n}\n"
    assert _read("kotlin", body.replace("{last}", "7")).text == "deep"
    assert _read("kotlin", body.replace("{last}", "8")).kind == "dynamic"


# --- Bundle builders ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "argument",
    [
        'Bundle().apply { putString("screen", Events.A); putInt("count", 3) }',
        'Bundle().apply {\n    putString("screen", Events.A)\n    putInt("count", 3)\n}',
        'android.os.Bundle(2).apply { this.putString("screen", Events.A)\n putLong("count", 3L) }',
    ],
)
def test_an_inline_bundle_builder_is_read_as_a_dictionary(argument: str) -> None:
    value = _read("kotlin", f"track({argument})")
    assert value.kind == "entries"
    assert [(key, entry.text) for key, entry in value.entries] == [("screen", "a"), ("count", "3")]


@pytest.mark.parametrize(
    "argument",
    [
        'Bundle().apply { putString("screen", x); if (flag) putInt("count", 3) }',
        'Bundle().apply { putString("screen", x); putAll(extra) }',
        'Bundle(extra).apply { putString("screen", x) }',
        'Bundle().apply { putString("screen", x).also { } }',
        'Bundle().apply { putString("screen") }',
        'Bundle().also { it.putString("screen", x) }',
    ],
)
def test_a_bundle_builder_that_may_hold_other_keys_is_not_read(argument: str) -> None:
    assert _read("kotlin", f"track({argument})").kind == "dynamic"


def test_bundle_properties_are_checked(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/Screen.kt": """
                fun show(id: String) {
                    Tracker.log("screen_view", Bundle().apply {
                        putString("screen", "home")
                        putString("id", id)
                    })
                }
            """
        },
    )
    [item] = scan(_config(root, _LOG_CONFIG)).items
    assert (item.name, item.properties) == ("screen_view", {"screen": "home", "id": None})


# --- ambiguous constants ------------------------------------------------------------------
_TWO_ENUMS = """
enum Home: String { case home = "home_tab" }
enum Screen: String { case home = "home_screen" }
"""


def test_a_constant_that_resolves_to_several_values_says_so_in_list(tmp_path: Path) -> None:
    assert (_read("swift", "track(.home)", index=_TWO_ENUMS).reason) == "ambiguous_constant"
    kotlin = 'object A { object Keys { const val HOME = "a" } }\n'
    kotlin += 'object B { object Keys { const val HOME = "b" } }\n'
    assert _read("kotlin", "track(Keys.HOME)", index=kotlin).reason == "ambiguous_constant"
    # The field it goes to picks one, as it does in plain check.
    symbols = SymbolIndex()
    symbols.index_tokens(tokenize(_TWO_ENUMS, "swift"), "swift")
    picked = Evaluator(symbols, "swift", explain=True).evaluate(
        tokenize(".home", "swift"), field="screen"
    )
    assert (picked.kind, picked.text) == ("text", "home_screen")
    # A constant nobody declares is still unknown.
    assert _read("kotlin", "track(Keys.AWAY)", index=kotlin).reason == "unknown_constant"
    root = _repo(
        tmp_path,
        {
            "app/A.kt": 'const val HOME = "a"\n',
            "app/B.kt": 'const val HOME = "b"\n',
            "app/Screen.kt": """
                class Screen {
                    fun show(kind: String) {
                        Tracker.log(HOME)
                    }
                    fun forward(HOME: String) {
                        Tracker.log(HOME)
                    }
                }
            """,
            "app/Twice.kt": """
                class Twice {
                    companion object { const val AWAY = "x" }
                    fun show() { Tracker.log(AWAY) }
                }
            """,
            "app/Again.kt": """
                class Twice {
                    companion object { const val AWAY = "y" }
                }
            """,
        },
    )
    config = _config(root, _LOG_CONFIG)
    # Plain check sends them as dynamic and reports nothing of its own.
    assert [item.name for item in scan(config).items] == [None, None, None]
    found = inventory(config)
    assert [call.dynamic for call in found.calls] == [
        {"name": "ambiguous_constant"},
        # A parameter hides the constants, ambiguous or not.
        {"name": "parameter"},
        # A member named inside its type, declared twice with different values.
        {"name": "ambiguous_constant"},
    ]
