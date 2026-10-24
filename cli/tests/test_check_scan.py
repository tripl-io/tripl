"""``tripl check`` static mode: tokenizer, call finder, enum index, presets.

Every fixture here is SYNTHETIC — made-up wrappers (``Analytics.shared``),
made-up enums (``AppEvents.Category``) — written to exercise the call SHAPES
real apps use: multi-line labelled wrapper calls, enum shorthand resolved from
another file, the Objective-C selector form, interpolated legacy names, names
returned by a function, and the Segment/Amplitude/Snowplow SDK calls in
TypeScript and Kotlin.

The last sections cover what ``tripl check --list`` adds on top of the same
scan: why a value was not read, the function scopes that refine a variable
into a parameter or a local, the forwarders, and the unmatched calls that look
like tracking.
"""

from __future__ import annotations

import inspect
import sys
import textwrap
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from types import FrameType
from typing import Any, SupportsIndex

import pytest

from tripl_cli.check import inventory as inventory_module
from tripl_cli.check import yamlish
from tripl_cli.check.calls import find_calls
from tripl_cli.check.config import CheckConfig, parse
from tripl_cli.check.inventory import Inventory, ListedCall, Location, inventory, summarize
from tripl_cli.check.lexer import STRING, Interp, Token, tokenize
from tripl_cli.check.model import REASON_CODES, CheckItem
from tripl_cli.check.scan import glob_regex, scan
from tripl_cli.check.scopes import FunctionScope, ScopeIndex
from tripl_cli.check.symbols import SymbolIndex, template_text, variable_name
from tripl_cli.check.values import EXPRESSION_MAX, Evaluator, Value
from tripl_cli.errors import TriplConfigError


def _repo(root: Path, files: dict[str, str]) -> Path:
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    return root


def _config(root: Path, text: str) -> CheckConfig:
    return parse(yamlish.loads(textwrap.dedent(text)), path=None, base=root)


def _summary(items: tuple[CheckItem, ...]) -> list[dict[str, Any]]:
    return [
        {
            "at": f"{item.origin.path}:{item.origin.line}",
            "type": item.event_type,
            "name": item.name,
            "fields": dict(item.fields),
            "properties": None if item.properties is None else dict(item.properties),
        }
        for item in items
    ]


# --- tokenizer -------------------------------------------------------------------
def test_swift_interpolation_keeps_parentheses_inside_strings_inside_it() -> None:
    [token] = [t for t in tokenize('let x = "a\\(f(")"))b"', "swift") if t.kind == STRING]
    assert token.parts == ("a", Interp('f(")")'), "b")


def test_kotlin_templates_become_plan_variable_tokens() -> None:
    source = 'val n = "promo_${sheet.id}_$kind"'
    [token] = [t for t in tokenize(source, "kotlin") if t.kind == STRING]
    assert token.parts == ("promo_", Interp("sheet.id"), "_", Interp("kind"))
    assert template_text(token.parts) == "promo_${id}_${kind}"


def test_typescript_template_literals_nest() -> None:
    [token] = [t for t in tokenize("f(`a${`b${c}`}d`)", "ts") if t.kind == STRING]
    assert token.parts[0] == "a" and token.parts[-1] == "d"
    assert isinstance(token.parts[1], Interp)


def test_objc_at_strings_and_escapes() -> None:
    [token] = [t for t in tokenize('x = @"say \\"hi\\"";', "objc") if t.kind == STRING]
    assert token.at is True
    assert token.parts == ('say "hi"',)


def test_comments_and_regex_literals_hide_no_calls_and_break_no_parens() -> None:
    source = textwrap.dedent(
        """
        // analytics.track("Commented Out")
        /* analytics.track("Also Commented") */
        const re = /\\)/g;
        analytics.track("Real", { a: 1 });
        """
    )
    found = find_calls(tokenize(source, "ts"), "ts", source)
    calls = [call for call in found if call.callee == "analytics.track"]
    assert len(calls) == 1
    assert calls[0].args[0].tokens[0].parts == ("Real",)


@pytest.mark.parametrize(
    ("expr", "name"),
    [
        ("id", "id"),
        ("sheet.id", "id"),
        ("type.rawValue", "type"),
        ("kind.value", "kind"),
        # Only rawValue / value are accessors; any other member IS the variable.
        ("sheet.title", "title"),
        ("item.name", "name"),
        ("self.id", "id"),
        ("name(for: x)", "name"),
        ("", "value"),
    ],
)
def test_variable_names_for_interpolations(expr: str, name: str) -> None:
    assert variable_name(expr) == name


def _callees(source: str, language: str) -> list[str]:
    source = textwrap.dedent(source)
    return [call.callee for call in find_calls(tokenize(source, language), language, source)]


@pytest.mark.parametrize(
    ("language", "source", "callees"),
    [
        # `->` opens a Kotlin `when` branch and a Kotlin/Java lambda body: calls.
        (
            "kotlin",
            """
            when (screen) {
                Screen.HOME -> analytics.track("home")
                else -> analytics.track("other")
            }
            """,
            ["when", "analytics.track", "analytics.track"],
        ),
        ("kotlin", 'items.forEach { item -> analytics.track("seen") }', ["analytics.track"]),
        (
            "java",
            'items.forEach(item -> analytics.track("seen"));',
            ["items.forEach", "analytics.track"],
        ),
        # In Swift `->` ends a signature, so what follows is a type, not a call.
        ("swift", "func make() -> Wrapper(x)", []),
        # Kotlin labelled return, and the TS/JS keywords that precede an expression.
        ("kotlin", 'items.forEach { return@forEach analytics.track("x") }', ["analytics.track"]),
        ("ts", 'export default analytics.track("x");', ["analytics.track"]),
    ],
)
def test_calls_after_arrows_labels_and_expression_keywords_are_found(
    language: str, source: str, callees: list[str]
) -> None:
    assert _callees(source, language) == callees


@pytest.mark.parametrize(
    "branch",
    ['await analytics.track("a")', '!analytics.track("a")', '(analytics.track("a"))'],
)
def test_a_ternary_branch_is_a_call_not_a_typed_declaration(branch: str) -> None:
    source = f'const sent = ready ? {branch} : analytics.track("b");'
    assert _callees(source, "ts") == ["analytics.track", "analytics.track"]


def test_case_name_accessors_are_language_aware(tmp_path: Path) -> None:
    """Kotlin/Java ``name`` and Swift ``description`` read the case's IDENTIFIER."""
    root = _repo(
        tmp_path,
        {
            "Tracking/Category.kt": """
                enum class Category(val value: String) { HOME("home_screen"), SETTINGS("settings") }
            """,
            "Tracking/Category.java": """
                enum Screen { MAIN("main_screen"); }
            """,
            "Tracking/Category.swift": """
                enum Tab: String { case home = "home_tab", feed }
            """,
            "Feature/Home.kt": """
                fun a() {
                    analytics.track(Category.HOME.name)
                    analytics.track(Category.HOME.value)
                    analytics.track(user.name)
                }
            """,
            "Feature/Home.java": """
                class H { void a() {
                    analytics.track(Screen.MAIN.name());
                    analytics.track(Screen.MAIN.value());
                } }
            """,
            "Feature/Home.swift": """
                func a() {
                    analytics.track(Tab.home.description)
                    analytics.track(Tab.home.rawValue)
                }
            """,
        },
    )
    config = _config(
        root,
        """
        project: demo
        enums: [{file: "Tracking/*"}]
        sources: ["Feature/**"]
        event_types:
          legacy:
            calls: [{function: "analytics.track", name_arg: 0}]
        """,
    )
    names = {(item.origin.path, item.origin.line): item.name for item in scan(config).items}
    assert names == {
        ("Feature/Home.java", 2): "MAIN",
        ("Feature/Home.java", 3): "main_screen",
        ("Feature/Home.kt", 2): "HOME",
        ("Feature/Home.kt", 3): "home_screen",
        # Not an enum case: a runtime value, never the word "user" or "name".
        ("Feature/Home.kt", 4): None,
        ("Feature/Home.swift", 2): "home",
        ("Feature/Home.swift", 3): "home_tab",
    }


def test_globs_understand_double_star_and_alternation() -> None:
    assert glob_regex("Sources/**/*.swift").fullmatch("Sources/A/B/c.swift")
    assert glob_regex("Sources/**/*.swift").fullmatch("Sources/c.swift")
    assert not glob_regex("Sources/*.swift").fullmatch("Sources/A/c.swift")
    assert glob_regex("src/**/*.{ts,tsx}").fullmatch("src/x/y.tsx")


# --- a project's own wrapper: Swift, multi-line, enum shorthand -----------------------
SWIFT_ENUMS = """
enum AppEvents {
    enum Category: String {
        case home
        case mapScreen = "map_screen"
    }
    enum Action: String {
        case open, close = "closed"
    }
}
"""

SWIFT_FEATURE = """
final class HomeView {
    var index = 0
    func didAppear() {
        Analytics.shared.log(
            category: .home,
            action: .open,
            label: "card",
            properties: ["source": "feed", "position": index]
        )
        Analytics.shared.log(category: AppEvents.Category.mapScreen.rawValue,
                             action: AppEvents.Action.close.rawValue,
                             label: dynamicLabel)
    }
}
"""

WRAPPER_CONFIG = """
project: demo
enums:
  - file: "Tracking/*.swift"
    languages: [swift]
sources: ["Feature/**"]
event_types:
  se:
    calls:
      - function: "Analytics.shared.log"
        args: {category: category, action: action, label: label, properties: properties}
      - objc_selector: "trackWithCategory:action:label:"
"""


def test_multi_line_wrapper_calls_resolve_enum_shorthand_from_another_file(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {"Tracking/AppEvents.swift": SWIFT_ENUMS, "Feature/HomeView.swift": SWIFT_FEATURE},
    )
    outcome = scan(_config(root, WRAPPER_CONFIG))
    assert _summary(outcome.items) == [
        {
            "at": "Feature/HomeView.swift:4",
            "type": "se",
            "name": None,
            "fields": {"category": "home", "action": "open", "label": "card"},
            "properties": {"source": "feed", "position": None},
        },
        {
            "at": "Feature/HomeView.swift:10",
            "type": "se",
            "name": None,
            "fields": {"category": "map_screen", "action": "closed", "label": None},
            "properties": None,
        },
    ]
    # Only the scanned sources are counted; the enum file is read for its values.
    assert outcome.files == 1
    assert outcome.items[1].dynamic == ("label",)
    assert outcome.items[0].origin.column == 9


def test_shorthand_shared_by_two_enums_is_disambiguated_by_the_field(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "Tracking/Events.swift": """
                enum Category: String { case home = "home_category" }
                enum Action: String { case home = "go_home" }
            """,
            "Feature/Home.swift": """
                func tap() {
                    Analytics.shared.log(category: .home, action: .home, label: nil)
                }
            """,
        },
    )
    [item] = scan(_config(root, WRAPPER_CONFIG)).items
    # `label: nil` is "no label", not "a label unknown until runtime".
    assert dict(item.fields) == {"category": "home_category", "action": "go_home"}


def test_a_reference_resolves_on_the_longest_suffix_it_shares_with_a_constant() -> None:
    symbols = SymbolIndex()
    symbols.add(("Screen", "Open"), "screen_open", member=True)
    symbols.add(("Tab", "Open"), "tab_open", member=True)
    symbols.add(("Events", "Screen", "Open"), "events_open", member=True)
    symbols.add(("Other", "Screen", "Open"), "other_open", member=True)
    symbols.add(("Open",), "open", member=False)
    symbols.add_case(("Screen", "Open"))
    # Three constants end in `Screen.Open`, one in `Events.Screen.Open`.
    assert symbols.resolve(["Events", "Screen", "Open"]) == "events_open"
    assert symbols.resolve(["Screen", "Open"]) is None
    assert symbols.resolve(["Events", "Tab", "Open"]) == "tab_open"
    assert symbols.resolve(["Other", "Open"]) is None
    # A bare name reads the file-scope constants only, Swift's `.Open` every member.
    assert symbols.resolve(["Open"]) == "open"
    assert symbols.resolve(["Open"], shorthand=True) is None
    assert symbols.resolve(["Open"], shorthand=True, hint="tab") == "tab_open"
    # A case-name accessor reads the identifier of a case it knows.
    assert symbols.resolve(["Screen", "Open", "name"], language="kotlin") == "Open"
    assert symbols.resolve(["Tab", "Open", "name"], language="kotlin") is None


def test_the_objc_selector_form_maps_each_part_to_its_field(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "Feature/SettingsController.m": """
                #import "Analytics.h"
                static NSString *const kCategorySettings = @"settings";

                @implementation SettingsController
                - (void)viewDidAppear:(BOOL)animated {
                    [super viewDidAppear:animated];
                    [[Analytics shared] trackWithCategory:kCategorySettings
                                                 action:@"open"
                                                  label:[self labelText]];
                }
                @end
            """
        },
    )
    [item] = scan(_config(root, WRAPPER_CONFIG)).items
    assert item.origin.line == 7
    assert item.origin.snippet == "trackWithCategory:action:label:"
    assert dict(item.fields) == {"category": "settings", "action": "open", "label": None}


# --- legacy flat events: interpolated names and names from a function ---------------------
def test_legacy_names_interpolate_and_come_from_functions(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "Feature/Paywall.swift": """
                struct Paywall {
                    let sheetId: String
                    func show() {
                        Analytics.shared.logEvent("promo_sheet_\\(sheetId)_shown",
                                                  parameters: ["plan": "annual"])
                        Analytics.shared.logEvent(eventName(for: .trial))
                    }
                    func eventName(for kind: Kind) -> String {
                        switch kind {
                        case .trial: return "trial_started"
                        case .renewal: return "renewal_started"
                        }
                    }
                }
            """
        },
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          legacy:
            calls: [{function: "Analytics.shared.logEvent", name_arg: 0}]
        """,
    )
    assert _summary(scan(config).items) == [
        {
            "at": "Feature/Paywall.swift:4",
            "type": "legacy",
            "name": "promo_sheet_${sheetId}_shown",
            "fields": {},
            "properties": {"plan": "annual"},
        },
        # One item per string the function can return.
        {
            "at": "Feature/Paywall.swift:6",
            "type": "legacy",
            "name": "trial_started",
            "fields": {},
            "properties": None,
        },
        {
            "at": "Feature/Paywall.swift:6",
            "type": "legacy",
            "name": "renewal_started",
            "fields": {},
            "properties": None,
        },
    ]


def test_a_call_with_nothing_knowable_is_reported_not_sent(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"Feature/A.swift": "func f() { Analytics.shared.logEvent(name) }\n"})
    config = _config(
        root,
        """
        project: demo
        event_types:
          legacy: {calls: [{function: "Analytics.shared.logEvent", name_arg: 0}]}
        """,
    )
    [item] = scan(config).items
    assert item.sendable is False
    assert item.dynamic == ("name",)


# --- SDK presets in TypeScript and Kotlin ----------------------------------------------
PRESET_CONFIG = """
project: demo
event_types:
  track: {preset: segment_track}
  amp: {preset: amplitude_log_event}
  se: {preset: snowplow_structured}
"""


def test_segment_amplitude_and_snowplow_presets_in_typescript(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "web/src/checkout.ts": """
                import { analytics } from "./segment";
                import { trackStructEvent } from "@snowplow/browser-tracker";

                export function onCheckout(total: number) {
                  analytics.track("Checkout Started", { total, currency: "EUR" });
                  amplitude.getInstance().logEvent(`Plan ${planId} Selected`, { plan: planId });
                  trackStructEvent({ category: "checkout", action: "submit",
                                     label: 'card', value: 3 });
                }
            """
        },
    )
    assert _summary(scan(_config(root, PRESET_CONFIG)).items) == [
        {
            "at": "web/src/checkout.ts:5",
            "type": "track",
            "name": "Checkout Started",
            "fields": {},
            "properties": {"total": None, "currency": "EUR"},
        },
        {
            "at": "web/src/checkout.ts:6",
            "type": "amp",
            "name": "Plan ${planId} Selected",
            "fields": {},
            "properties": {"plan": None},
        },
        {
            "at": "web/src/checkout.ts:7",
            "type": "se",
            "name": None,
            "fields": {"category": "checkout", "action": "submit", "label": "card", "value": "3"},
            "properties": None,
        },
    ]


def test_segment_amplitude_and_snowplow_presets_in_kotlin(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "android/Checkout.kt": """
                enum class EventCategory(val value: String) {
                    CHECKOUT("checkout"),
                    PROFILE("profile");
                }

                class Checkout(private val analytics: Analytics) {
                    fun submit(plan: String) {
                        analytics.track("Order Completed",
                            mapOf("plan" to plan, "currency" to "EUR"))
                        tracker.track(Structured(EventCategory.CHECKOUT.value, "submit")
                            .label("card").value(3.0))
                        amplitude.track(eventType = "Plan Selected",
                            eventProperties = mapOf("plan" to "annual"))
                    }
                }
            """
        },
    )
    assert _summary(scan(_config(root, PRESET_CONFIG)).items) == [
        {
            "at": "android/Checkout.kt:8",
            "type": "track",
            "name": "Order Completed",
            "fields": {},
            "properties": {"plan": None, "currency": "EUR"},
        },
        {
            "at": "android/Checkout.kt:10",
            "type": "se",
            "name": None,
            "fields": {"category": "checkout", "action": "submit", "label": "card", "value": "3.0"},
            "properties": None,
        },
        {
            "at": "android/Checkout.kt:12",
            "type": "amp",
            "name": "Plan Selected",
            "fields": {},
            "properties": {"plan": "annual"},
        },
    ]


def test_snowplow_screen_view_and_self_describing_in_swift(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "App/Screens.swift": """
                func appear() {
                    _ = tracker.track(ScreenView(name: "Settings", screenId: UUID()))
                    _ = tracker.track(SelfDescribing(
                        schema: "iglu:com.example/button_click/jsonschema/1-0-0",
                        payload: ["button": "save"]))
                }
            """
        },
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          page: {preset: snowplow_screen_view}
          custom: {preset: snowplow_self_describing}
        """,
    )
    summary = _summary(scan(config).items)
    assert [(row["type"], row["name"], row["fields"], row["properties"]) for row in summary] == [
        ("page", "Settings", {"id": None}, None),
        ("custom", "button_click", {}, {"button": "save"}),
    ]


def test_field_map_renames_preset_targets_to_the_plan_s_fields(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {"web/a.ts": 'trackStructEvent({ category: "c", action: "a" });\n'},
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          se:
            preset: snowplow_structured
            field_map: {category: se_category, action: se_action}
        """,
    )
    [item] = scan(config).items
    assert dict(item.fields) == {"se_category": "c", "se_action": "a"}


# --- config validation --------------------------------------------------------------
@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("event_types: {se: {calls: [{function: x, pattern: y}]}}", "exactly one of"),
        ("event_types: {se: {preset: mixpanel}}", "unknown preset 'mixpanel'"),
        ("event_types: {se: {}}", "say how this event type is tracked"),
        ("event_types: {se: {calls: [{function: x, sepc: 1}]}}", "unknown key(s) sepc"),
        ("event_types: {se: {calls: [{objc_selector: logEvent}]}}", "not a selector"),
        ("event_types: {se: {calls: [{pattern: '('}]}}", "not a valid regex"),
        ("event_types: {se: {calls: [{function: x, args: {a: 'b:c'}}]}}", "is not a target"),
        (
            "event_types: {se: {calls: [{function: x, languages: [cobol]}]}}",
            "'cobol' is not one of",
        ),
        ("projct: demo", "unknown key(s) projct"),
    ],
)
def test_config_mistakes_are_usage_errors_naming_the_key(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(TriplConfigError) as excinfo:
        parse(yamlish.loads(body), path=None, base=tmp_path)
    assert message in str(excinfo.value)
    assert excinfo.value.exit_code == 2


def test_the_yaml_subset_reads_the_documented_config_shape() -> None:
    document = yamlish.loads(
        textwrap.dedent(
            """
            project: my-app   # trailing comment
            branch: null
            sources: ["Sources/**", 'web/src/**']
            event_types:
              se:
                calls:
                  - function: "Analytics.shared.log"   # or pattern:
                    args: {category: category,
                           action: action}
                    positional: [category, action, label]
                  - objc_selector: "trackWithCategory:action:label:"
              page: {preset: snowplow_screen_view}
              legacy:
                calls:
                - {function: "Analytics.shared.logEvent", name_arg: 0}
            """
        )
    )
    assert document == {
        "project": "my-app",
        "branch": None,
        "sources": ["Sources/**", "web/src/**"],
        "event_types": {
            "se": {
                "calls": [
                    {
                        "function": "Analytics.shared.log",
                        "args": {"category": "category", "action": "action"},
                        "positional": ["category", "action", "label"],
                    },
                    {"objc_selector": "trackWithCategory:action:label:"},
                ]
            },
            "page": {"preset": "snowplow_screen_view"},
            "legacy": {"calls": [{"function": "Analytics.shared.logEvent", "name_arg": 0}]},
        },
    }


@pytest.mark.parametrize("text", ["a: &anchor 1", "a:\n\tb: 1", "a: 1\na: 2", "a: {b: 1"])
def test_the_yaml_subset_refuses_what_it_does_not_support(text: str) -> None:
    with pytest.raises(yamlish.YamlError):
        yamlish.loads(text)


def test_the_yaml_writer_writes_what_the_reader_reads_back() -> None:
    items = [
        {"function": "a.b", "args": {"x": None, "event.data": "name:iglu"}, "object_arg": 0},
        {"pattern": r"(?:[\w$.]*\.)?track", "receiver": "(?i)a|b # not a comment"},
        {"objc_selector": "track:", "positional": ["true", "Null", "1", "1.5", "~", "x y", ""]},
        {},
    ]
    text = yamlish.dump_sequence(items)
    assert yamlish.loads(text) == items
    # Plain only when the reader keeps the word the same string.
    assert text.splitlines()[:2] == [
        "- function: a.b",
        '  args: {x: null, event.data: "name:iglu"}',
    ]
    assert '["true", "Null", "1", "1.5", "~", "x y", ""]' in text


# --- why a value was not read (`tripl check --list`) ---------------------------------
_INDEX = """
object Events {
    object Name {
        const val Open = "open"
        const val Promo = "promo_%s_shown"
    }
}
"""


def _evaluated(language: str, source: str) -> Value:
    """The first argument of the ``track(…)`` call (or ``track:`` message) in ``source``.

    Read as ``tripl check --list`` reads it: a dynamic value says why.
    """
    symbols = SymbolIndex()
    symbols.index_tokens(tokenize(_INDEX, "kotlin"), "kotlin")
    tokens = tokenize(source, language)
    call = next(
        call
        for call in find_calls(tokens, language, source)
        if call.selector == "track:" or call.callee.endswith("track")
    )
    return Evaluator(symbols, language, explain=True).evaluate(call.args[0].tokens)


@pytest.mark.parametrize(
    ("language", "argument", "reason", "expression"),
    [
        ("kotlin", "event", "variable", "event"),
        ("kotlin", "action.event", "variable", "action.event"),
        ("kotlin", "_private", "variable", "_private"),
        ("ts", "$event", "variable", "$event"),
        ("kotlin", "HOME_SCREEN", "unknown_constant", "HOME_SCREEN"),
        ("kotlin", "_Foo", "unknown_constant", "_Foo"),
        ("kotlin", "Analytics.Event.Foo", "unknown_constant", "Analytics.Event.Foo"),
        ("swift", ".home", "unknown_constant", ".home"),
        # Apple's and Google's `k` prefix names a constant; `kind` is a variable.
        ("objc", "kFIREventSelectContent", "unknown_constant", "kFIREventSelectContent"),
        ("kotlin", "kind", "variable", "kind"),
        # A format is told from its shape, whatever the receiver; its template
        # shows when the format string reads as text.
        ("kotlin", '"%s_x".format(id)', "format", "format of %s_x"),
        ("kotlin", "Events.Name.Promo.format(id)", "format", "format of promo_%s_shown"),
        ("kotlin", "Unknown.Thing.format(id)", "format", "Unknown.Thing.format(id)"),
        ("java", 'String.format(Locale.US, "%s_x", x)', "format", "format of %s_x"),
        ("swift", 'String(format: "%@_x", id)', "format", "format of %@_x"),
        # `[…]` is a message only in Objective-C; in Swift and TypeScript, a literal.
        ("swift", '[stringWithFormat: "%@_x", 1]', "expression", '[stringWithFormat: "%@_x", 1]'),
        ("ts", '[c ? stringWithFormat : "%s"]', "expression", '[c ? stringWithFormat : "%s"]'),
        # A member call binds tighter than an operator: there `y` alone is
        # formatted, and only parentheses make the sum the receiver.
        ("kotlin", '"a" + null + y.format(z)', "concatenation", '"a" + null + y.format(z)'),
        ("kotlin", "name ?: y.format(z)", "conditional", "name ?: y.format(z)"),
        ("kotlin", "flag && y.format(z)", "expression", "flag && y.format(z)"),
        ("kotlin", '("a_" + id).format(z)', "format", "format of a_${id}"),
        ("kotlin", "getMapTypeLog()", "function", "getMapTypeLog()"),
        ("kotlin", 'if (flag) "a" else "b"', "conditional", 'if (flag) "a" else "b"'),
        (
            "kotlin",
            'when (x) { 1 -> "a" else -> "b" }',
            "conditional",
            'when (x) { 1 -> "a" else -> "b" }',
        ),
        ("ts", 'flag ? "a" : "b"', "conditional", 'flag ? "a" : "b"'),
        ("kotlin", 'name ?: "fallback"', "conditional", 'name ?: "fallback"'),
        ("swift", 'name ?? "fallback"', "conditional", 'name ?? "fallback"'),
        ("kotlin", "Event.A + Params.B", "concatenation", "Event.A + Params.B"),
        # A map builder the dictionary reader rejects is no function call.
        ("kotlin", "mapOf(K to v, )", "expression", "mapOf(K to v, )"),
        ("kotlin", "x.name().lowercase()", "expression", "x.name().lowercase()"),
        ("kotlin", "list[0]", "expression", "list[0]"),
        # The span the evaluator reads: casts and wrapping parentheses are gone.
        ("kotlin", "(event as String)", "variable", "event"),
    ],
)
def test_a_value_the_scan_cannot_read_says_why(
    language: str, argument: str, reason: str, expression: str
) -> None:
    value = _evaluated(language, f"track({argument})")
    assert value.kind == "dynamic"
    assert (value.reason, value.expression) == (reason, expression)


def test_an_objective_c_format_names_its_template() -> None:
    value = _evaluated("objc", '[tracker track:[NSString stringWithFormat:@"%@_x", y]];')
    assert (value.reason, value.expression) == ("format", "format of %@_x")


def test_a_plain_reference_keeps_its_path_and_a_long_expression_is_cut() -> None:
    assert _evaluated("kotlin", "track(action?.event)").path == ("action", "event")
    # Swift's `.home` names a case of the type the call expects, never a name in scope.
    assert _evaluated("swift", "track(.home)").path == ()
    assert _evaluated("kotlin", "track(getMapTypeLog())").path == ()
    chained = ".".join(f"step{number}()" for number in range(20))
    value = _evaluated("kotlin", f"track({chained})")
    assert value.reason == "expression"
    assert len(value.expression) == EXPRESSION_MAX
    assert value.expression.endswith("...")


def test_why_a_value_was_not_read_never_changes_what_it_is() -> None:
    """``reason``, ``expression`` and ``path`` describe a value; equality ignores them."""
    one = Value("dynamic", hint="event", reason="variable", expression="event", path=("event",))
    other = Value("dynamic", hint="event", reason="format", expression="x.format(y)")
    assert one == other
    assert hash(one) == hash(other)
    # Interpolated text is text, not a value the scan could not read.
    template = _evaluated("ts", "track(`${screen}_shown`)")
    assert (template.kind, template.text, template.reason) == ("text", "${screen}_shown", "")


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """How often ``Evaluator.evaluate`` reads each span from now on, by its tokens' text."""
    reads: Counter[str] = Counter()
    evaluate = Evaluator.evaluate

    def counted(self: Evaluator, tokens: Sequence[Token], *, field: str | None = None) -> Value:
        reads["".join(token.text for token in tokens)] += 1
        return evaluate(self, tokens, field=field)

    monkeypatch.setattr(Evaluator, "evaluate", counted)
    return reads


def test_a_concatenation_ending_in_a_format_reads_each_operand_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saying why ``"a" + (…) + null + y.format(z)`` is not read reads nothing again."""
    argument = "x"
    for _ in range(12):
        argument = f'"a" + ({argument}) + null + y.format(z)'
    reads = _count_reads(monkeypatch)
    value = _evaluated("kotlin", f"track({argument})")
    # Twice per level would be 4096 reads of the innermost operand.
    assert (value.reason, reads["(x)"]) == ("concatenation", 1)


@pytest.mark.parametrize(
    ("language", "opening", "inner", "closing", "reads"),
    [
        ("kotlin", "", "x", ".format(a)", 2),
        ("java", "String.format(Locale.US, ", '"%s"', ", y)", 2),
        ("swift", "String(format: ", '"%@"', ", y)", 2),
        ("objc", "[NSString stringWithFormat:", '@"%@"', ", y]", 2),
        # The format string `("a" + …)` is read, with its operands.
        ("kotlin", '("a" + ', "x", ").format(z)", 4),
    ],
)
def test_reading_a_format_string_never_nests(
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    opening: str,
    inner: str,
    closing: str,
    reads: int,
) -> None:
    """The formats inside a format string are not read in turn, however many there are."""
    argument = opening * 1000 + inner + closing * 1000
    counted = _count_reads(monkeypatch)
    evaluator = Evaluator(SymbolIndex(), language, explain=True)
    value = evaluator.evaluate(tokenize(argument, language))
    assert (value.kind, value.reason, sum(counted.values())) == ("dynamic", "format", reads)


def test_a_format_string_nested_past_the_stack_is_not_read() -> None:
    nested = '"a" + (' * 1000 + "x" + ")" * 1000
    evaluator = Evaluator(SymbolIndex(), "kotlin", explain=True)
    value = evaluator.evaluate(tokenize(f"({nested}).format(z)", "kotlin"))
    assert (value.kind, value.reason) == ("dynamic", "format")


def test_a_swift_literal_that_names_a_format_selector_is_read_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In Swift ``[stringWithFormat: x, 1]`` is a literal, not a message: nothing is read again."""
    argument = "x"
    for _ in range(12):
        argument = f"[stringWithFormat: {argument}, 1]"
    reads = _count_reads(monkeypatch)
    value = _evaluated("swift", f"track({argument})")
    # Reading each level's `x` as a format string would read it 13 times.
    assert (value.reason, reads["x"]) == ("expression", 1)


def test_only_an_explaining_evaluator_works_out_why_a_value_was_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain ``tripl check`` gives up on a value with its hint alone; ``--list`` explains it."""
    tokens = tokenize('String.format("%s_x", y)', "java")
    reads = _count_reads(monkeypatch)
    plain = Evaluator(SymbolIndex(), "java").evaluate(tokens)
    assert (plain.kind, plain.hint, plain.reason, plain.expression, plain.path) == (
        "dynamic",
        "format",
        "",
        "",
        (),
    )
    # The format string is read only for the `expression` `--list` shows.
    assert sum(reads.values()) == 1
    explained = Evaluator(SymbolIndex(), "java", explain=True).evaluate(tokens)
    assert (explained.reason, explained.expression) == ("format", "format of %s_x")
    assert sum(reads.values()) == 3
    assert explained == plain


@pytest.mark.parametrize(
    "argument",
    [
        # One per place the evaluator gives up: a name, a path, names that are
        # no plain path (an infix call), a character literal (a number it
        # cannot read) alone and negated, a concatenation with an operand that
        # is no text, a function the index does not read, and any other
        # expression.
        "event",
        "action.event",
        "a to b",
        "'a'",
        "-'a'",
        '"a" + null',
        "names()",
        "x.name().lowercase()",
    ],
)
def test_plain_check_gives_up_on_a_value_with_no_reason(
    monkeypatch: pytest.MonkeyPatch, argument: str
) -> None:
    explained = 0
    unread = Evaluator._unread

    def counted(self: Evaluator, *args: Any, **kwargs: Any) -> Value:
        nonlocal explained
        explained += 1
        return unread(self, *args, **kwargs)

    monkeypatch.setattr(Evaluator, "_unread", counted)
    tokens = tokenize(argument, "kotlin")
    plain = Evaluator(SymbolIndex(), "kotlin").evaluate(tokens)
    assert (plain.kind, plain.reason, plain.expression, plain.path) == ("dynamic", "", "", ())
    assert explained == 0
    # `--list` gets the very same value, and why.
    listed = Evaluator(SymbolIndex(), "kotlin", explain=True).evaluate(tokens)
    assert listed == plain and listed.reason


def test_plain_check_evaluates_each_argument_straight_from_scan_and_explains_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each frame between ``scan`` and the evaluator is stack a deeply nested argument loses."""
    root = _repo(tmp_path, {"web/a.ts": 'analytics.track("Signed Up", { plan: tier });\n'})
    config = _config(
        root,
        """
        project: demo
        event_types:
          track: {preset: segment_track}
        """,
    )
    reads: list[tuple[bool, list[str]]] = []
    evaluate = Evaluator.evaluate

    def traced(self: Evaluator, tokens: Sequence[Token], *, field: str | None = None) -> Value:
        callers = [frame.function for frame in inspect.stack(0)[1:]]
        reads.append((self.explain, callers[: callers.index("scan")]))
        return evaluate(self, tokens, field=field)

    monkeypatch.setattr(Evaluator, "evaluate", traced)
    [item] = scan(config).items
    assert (item.name, item.properties) == ("Signed Up", {"plan": None})
    assert reads[0] == (False, ["_evaluate", "collect_call"])
    assert [explain for explain, _ in reads] == [False] * len(reads)


def _deepest_evaluation(config: CheckConfig) -> int:
    """The most frames ``scan`` stacks while it evaluates an argument, its own frame included.

    Every one counts toward Python's recursion limit, the ones below the
    innermost ``evaluate`` too, and the deepest is where a nested argument
    runs out of it. A first scan goes unwatched: what runs once in a process
    (``re`` compiling a pattern the first time it is used) is not counted.
    """
    scan(config)
    deepest = 0

    def profile(frame: FrameType, event: str, arg: object) -> None:
        nonlocal deepest
        if event != "call":
            return
        frames, evaluating = 1, False
        while frame.f_code.co_name != "scan" and frame.f_back is not None:
            evaluating = evaluating or frame.f_code.co_qualname == "Evaluator.evaluate"
            frames += 1
            frame = frame.f_back
        if evaluating:
            deepest = max(deepest, frames)

    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        scan(config)
    finally:
        sys.setprofile(previous)
    return deepest


@pytest.mark.parametrize(
    ("path", "source", "leaf"),
    [
        ("src/A.kt", "fun go() {\n    Tracker.log(ARGUMENT)\n}\n", "x"),
        ("src/A.kt", "fun go() {\n    Tracker.log(ARGUMENT)\n}\n", "a.b"),
        (
            "src/A.java",
            "class A { void go() {\n    Tracker.log(ARGUMENT);\n} }\n",
            'String.format("%s", y)',
        ),
    ],
)
def test_plain_check_stacks_no_more_frames_than_before_list(
    tmp_path: Path, path: str, source: str, leaf: str
) -> None:
    """Plain ``tripl check`` reads an argument nested as deeply as it did before ``--list``.

    The recursion limit is left alone: a frame more at any level, or at the
    innermost value, is a level of nesting less.
    """
    argument = leaf
    for _ in range(20):
        argument = f'"a" + ({argument})'
    root = _repo(tmp_path, {path: source.replace("ARGUMENT", argument)})
    config = _config(
        root,
        """
        project: demo
        event_types:
          legacy: {calls: [{function: "Tracker.log", name_arg: 0}]}
        """,
    )
    # As on af5d1452, the last commit before `--list`, with Python 3.12, 3.13
    # and 3.14 alike: scan, map_call (now collect_call), _evaluate, evaluate,
    # then _concatenation and evaluate for each of the 20 levels, and the
    # innermost "a"'s _single, _string, template_text and its generator. No
    # leaf goes deeper; the path's _reference, variable_name, re.sub and
    # re._compile go exactly as deep.
    assert _deepest_evaluation(config) == 48


# --- target reasons ------------------------------------------------------------------
def _dynamic_reasons(found: Inventory) -> list[dict[str, str]]:
    return [dict(call.dynamic) for call in found.calls]


def test_a_dynamic_target_with_no_dynamic_value_says_why(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "web/a.ts": """
                analytics.track("Signed Up", "not a dictionary");
                analytics.track();
                trackStructEvent(props);
            """,
        },
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          track: {preset: segment_track}
          se: {preset: snowplow_structured}
        """,
    )
    found = inventory(config)
    assert _dynamic_reasons(found) == [
        # Properties given as text: a value, of the wrong shape.
        {"properties": "wrong_shape"},
        # No argument, and an object argument that is not a literal: nothing passed.
        {"name": "not_passed"},
        {"name": "not_passed"},
    ]
    # The object argument is kept, as the evaluator read it: it is why nothing was.
    objects = [call.read.unread_object for call in found.calls]
    assert objects[:2] == [None, None]
    assert objects[2] is not None
    assert (objects[2].kind, objects[2].reason, objects[2].expression) == (
        "dynamic",
        "variable",
        "props",
    )


def test_every_dynamic_target_has_exactly_one_reason(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "Feature/Home.swift": SWIFT_FEATURE,
            "Tracking/AppEvents.swift": SWIFT_ENUMS,
            "web/src/checkout.ts": """
                analytics.track(eventName, props);
                analytics.track("Plan " + plan, { plan });
                trackStructEvent({ category: "checkout", action: verb });
                analytics.track(flag ? "A" : "B", "props");
                analytics.track();
            """,
        },
    )
    config = _config(
        root,
        WRAPPER_CONFIG.replace('sources: ["Feature/**"]', 'sources: ["Feature/**", "web/**"]')
        + "  track: {preset: segment_track}\n  st: {preset: snowplow_structured}\n",
    )
    found = inventory(config)
    dynamic: Counter[str] = Counter()
    reasons: dict[str, Counter[str]] = {}
    for call in found.calls:
        for target in call.items[0].dynamic:
            dynamic[target] += 1
        # The items name a field by its bare name, `field:name` as `name`.
        for target, code in call.dynamic.items():
            reasons.setdefault(target.removeprefix("field:"), Counter())[code] += 1
    assert dynamic, "the fixture has no dynamic target at all"
    assert {target: sum(codes.values()) for target, codes in reasons.items()} == dict(dynamic)
    assert all(code in REASON_CODES for codes in reasons.values() for code in codes)


def test_a_field_named_like_a_reserved_target_is_not_that_target(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/Screens.kt": """
                fun showScreen(screenName: String) {
                    track("screen_view", screenName)
                }
                fun relay(event: String, screenName: String) {
                    track(event, screenName)
                }
                fun openBag(bag: Map<String, Any>) {
                    log("open", bag)
                }
            """,
        },
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          screen: {calls: [{function: "track", positional: [name, "field:name"]}]}
          bag: {calls: [{function: "log", positional: [name, "field:properties"]}]}
        """,
    )
    found = inventory(config)
    # Each its own reason: not the event name's wrong shape, nor properties not passed.
    assert _dynamic_reasons(found) == [
        {"field:name": "parameter"},
        {"name": "parameter", "field:name": "parameter"},
        {"field:properties": "parameter"},
    ]
    assert [call.items[0].dynamic for call in found.calls] == [
        ("name",),
        ("name", "name"),
        ("properties",),
    ]
    assert summarize(found).targets == {
        "screen": {"name": 2, "field:name": 2},
        "bag": {"name": 1, "field:properties": 1},
    }
    assert [
        (forwarder.function, [parameter.targets for parameter in forwarder.parameters])
        for forwarder in found.forwarders
    ] == [
        ("openBag", [("field:properties",)]),
        ("relay", [("name",), ("field:name",)]),
        ("showScreen", [("field:name",)]),
    ]


# --- function scopes -----------------------------------------------------------------
def _scope_at(language: str, source: str, marker: str = "log") -> FunctionScope | None:
    """The innermost function scope around the first ``marker(`` call in ``source``."""
    source = textwrap.dedent(source)
    tokens = tokenize(source, language)
    index = next(
        position
        for position, token in enumerate(tokens)
        if token.text == marker and tokens[position + 1].is_punct("(")
    )
    return ScopeIndex(tokens, language, source).innermost(index)


def test_a_jsx_closing_tag_in_attribute_braces_does_not_keep_the_function_open() -> None:
    # `</div>` once lexed as a regex running to the end of the line, taking the
    # `}` that closes the attribute with it, so the function never closed and
    # every call after it was read as inside it.
    source = """
        function App(page) {
          const el = <Route element={<div>Home</div>} />
          return el
        }
        log(page)
        """
    assert _scope_at("ts", source) is None
    tokens = tokenize(textwrap.dedent(source), "ts")
    assert "/regex/" not in [token.text for token in tokens]


@pytest.mark.parametrize(
    ("language", "source", "name", "parameters"),
    [
        ("kotlin", "fun track(event: String) {\n    log(event)\n}\n", "track", ["event"]),
        ("kotlin", "fun track(event: String) = log(event)\n", "track", ["event"]),
        # An expression body may start on the line after `=`.
        ("kotlin", "fun track(event: String): Unit =\n    log(event)\n", "track", ["event"]),
        (
            "kotlin",
            "fun <T> Analytics.track(event: T, params: Map<String, Any> = emptyMap()) {\n"
            "    log(event)\n}\n",
            "track",
            ["event", "params"],
        ),
        (
            "swift",
            "func track(_ event: String) async throws -> Bool {\n    log(event)\n}\n",
            "track",
            ["event"],
        ),
        ("swift", "init(event: String) {\n    log(event)\n}\n", "init", ["event"]),
        ("swift", "public init?(event: String) {\n    log(event)\n}\n", "init", ["event"]),
        (
            "java",
            "public void track(String event) throws IOException, IllegalStateException {\n"
            "    log(event);\n}\n",
            "track",
            ["event"],
        ),
        ("ts", "function track(event: string) {\n  log(event);\n}\n", "track", ["event"]),
        (
            "ts",
            "class A {\n  async track(event: string): Promise<void> {\n    log(event);\n  }\n}\n",
            "track",
            ["event"],
        ),
        # A TypeScript return type may hold an object type, a function type or `object`.
        (
            "ts",
            "export async function logPurchase(productId: string): Promise<{ ok: boolean }> {\n"
            "  await log(productId)\n}\n",
            "logPurchase",
            ["productId"],
        ),
        (
            "ts",
            "function register(event) {\n  return {\n    send(event: string): { sent: boolean } {\n"
            "      log(event)\n    },\n  }\n}\n",
            "send",
            ["event"],
        ),
        (
            "ts",
            "function trackScreen(screen: string): () => void {\n"
            "  log(screen)\n  return () => {}\n}\n",
            "trackScreen",
            ["screen"],
        ),
        (
            "ts",
            "function payload(event: string): object {\n  log(event)\n}\n",
            "payload",
            ["event"],
        ),
        (
            "ts",
            "function payload(event: string): Record<string, object> {\n  log(event)\n}\n",
            "payload",
            ["event"],
        ),
        (
            "ts",
            "function pick<T>(value: T): T extends { id: string } ? string : never {\n"
            "  log(value)\n}\n",
            "pick",
            ["value"],
        ),
        (
            "ts",
            "const isEvent = (value: unknown): value is { name: string } => {\n"
            "  log(value)\n  return true\n}\n",
            "isEvent",
            ["value"],
        ),
        # An object type, or a function type's parameters, of any length: a part
        # in brackets counts as one token of a return type.
        (
            "ts",
            "export async function load(screen: string): Promise<{ "
            + "; ".join(f"field{number}: string | null" for number in range(12))
            + " }> {\n  log(screen)\n}\n",
            "load",
            ["screen"],
        ),
        (
            "ts",
            "const load = async (screen: string): Promise<{ "
            + "; ".join(f"field{number}: string | null" for number in range(12))
            + " }> => {\n  log(screen)\n}\n",
            "load",
            ["screen"],
        ),
        (
            "kotlin",
            "fun handler(screen: String): ("
            + ", ".join(["Map<String, Any>"] * 8)
            + ") -> Unit {\n    log(screen)\n}\n",
            "handler",
            ["screen"],
        ),
        ("ts", "const track = (event: string) => {\n  log(event);\n};\n", "track", ["event"]),
        ("ts", "const track = async (event: string) => log(event);\n", "track", ["event"]),
        ("ts", "const track = event => log(event);\n", "track", ["event"]),
        ("objc", "static void track(NSString *event) {\n    log(event);\n}\n", "track", ["event"]),
    ],
)
def test_function_scopes_per_shape(
    language: str, source: str, name: str, parameters: list[str]
) -> None:
    scope = _scope_at(language, source)
    assert scope is not None
    assert (scope.name, [parameter.name for parameter in scope.parameters]) == (name, parameters)


@pytest.mark.parametrize(
    ("language", "source"),
    [
        # A lambda, a closure, a control-flow block and an anonymous function are no scopes.
        ("kotlin", "items.forEach { event -> log(event) }\n"),
        ("kotlin", "init {\n    log(event)\n}\n"),
        ("kotlin", "constructor(event: String) : this() {\n    log(event)\n}\n"),
        ("swift", "items.forEach { event in log(event) }\n"),
        ("ts", "if (ready) {\n  log(event);\n}\n"),
        ("java", "while (ready) {\n    log(event);\n}\n"),
        ("ts", "items.forEach(function (event) {\n  log(event);\n});\n"),
        # Objective-C methods split their parameters between selector parts.
        ("objc", "- (void)track:(NSString *)event {\n    log(event);\n}\n"),
        # A call at the end of a line is no method of the block that follows it.
        ("ts", "send(event)\nif (ready) {\n  log(event);\n}\n"),
        # A signature tail is short, a part in brackets counting as one token: a
        # run of them is no return type, and no body is found past it. What a
        # `<…>` holds counts token by token, so no tail runs on through nested
        # type arguments.
        ("ts", "function track(event: string): " + "(e) " * 40 + "{\n  log(event);\n}\n"),
        ("ts", "const track = (event: string): " + "(e) " * 40 + "=> {\n  log(event);\n}\n"),
        (
            "ts",
            "const track = (event: string): Promise<" + "E " * 40 + "> => {\n  log(event);\n}\n",
        ),
    ],
)
def test_what_is_no_function_scope(language: str, source: str) -> None:
    assert _scope_at(language, source) is None


@pytest.mark.parametrize(
    ("language", "source", "name", "parameters"),
    [
        ("kotlin", "abstract fun a(event: String)\nfun b() { log(event) }\n", "b", []),
        # TypeScript with no `;`: a `void` return type is no `void` operator, and
        # a name with a parameter list after it starts the next member.
        (
            "ts",
            "abstract class A {\n  abstract a(event: string): void\n  b() { log(event) }\n}\n",
            "b",
            [],
        ),
        (
            "ts",
            "class A {\n  send(event: string): void\n  send(event: string, props: object): void\n"
            "  send(event: string, props?: object) {\n    log(event)\n  }\n}\n",
            "send",
            ["event", "props"],
        ),
    ],
)
def test_a_body_less_function_does_not_claim_the_next_body(
    language: str, source: str, name: str, parameters: list[str]
) -> None:
    scope = _scope_at(language, source)
    assert scope is not None
    assert (scope.name, [parameter.name for parameter in scope.parameters]) == (name, parameters)


@pytest.mark.parametrize(
    ("language", "source", "name"),
    [
        # A call on the line after an expression body is not in it ...
        ("kotlin", 'fun label(event: String) = "x_$event"\nval opened = log(event)\n', None),
        (
            "ts",
            "const relay = (event: string) => send(event)\n"
            'window.addEventListener("message", (event) => log(event.data))\n',
            None,
        ),
        # ... nor one past a comma at the arrow's own level.
        ("ts", "const relay = (event: string) => send(event), opened = log(event)\n", None),
        # A line that starts with `.`, `?.`, `?:`, `&&` or `||` goes on with it,
        # and so does the line after one that ends in an operator.
        ("kotlin", "fun label(event: String) = builder\n    .also { log(event) }\n", "label"),
        ("kotlin", "fun label(event: String) = builder\n    ?.also { log(event) }\n", "label"),
        ("ts", "const label = (event: string) => builder\n  ?.also(() => log(event))\n", "label"),
        ("kotlin", "fun pick(event: String?) = event\n    ?: log(event)\n", "pick"),
        ("kotlin", "fun logAnd(event: String) = enabled\n    && log(event)\n", "logAnd"),
        ("kotlin", "fun logOr(event: String) = disabled\n    || log(event)\n", "logOr"),
        ("ts", "const logAnd = (event: string) => enabled\n  && log(event)\n", "logAnd"),
        ("ts", "const logOr = (event: string) => disabled\n  || log(event)\n", "logOr"),
        ("kotlin", "fun logAnd(event: String) = enabled &&\n  log(event)\n", "logAnd"),
        # A brace-less Kotlin `if (…)` takes its branch from the next line, and
        # `else`, `catch` and `finally` go on; a TypeScript ternary may take a
        # line per branch.
        (
            "kotlin",
            "fun logIf(event: String, on: Boolean) =\n  if (on)\n    log(event)\n  else Unit\n",
            "logIf",
        ),
        (
            "kotlin",
            "fun pick(event: String, on: Boolean) = if (on) Unit\n    else log(event)\n",
            "pick",
        ),
        (
            "kotlin",
            "fun safe(event: String) = try { parse(event) }\n"
            "    catch (e: Exception) { log(event) }\n",
            "safe",
        ),
        (
            "kotlin",
            "fun safe(event: String) = try { parse(event) }\n    finally { log(event) }\n",
            "safe",
        ),
        (
            "ts",
            "export const logTap = (tap: string) =>\n  enabled\n    ? log(tap)\n    : undefined;\n",
            "logTap",
        ),
        # A line that starts with `+` goes on in TypeScript, and is a statement of
        # its own in Kotlin.
        ("ts", 'const label = (event: string) => "x_" + event\n  + log(event)\n', "label"),
        (
            "kotlin",
            'fun outer(event: String) {\n    fun label(event: String) = "x_$event"\n'
            "    +log(event)\n}\n",
            "outer",
        ),
    ],
)
def test_where_an_expression_body_ends(language: str, source: str, name: str | None) -> None:
    scope = _scope_at(language, source)
    assert (None if scope is None else scope.name) == name


def _bodiless(language: str, source: str) -> list[str]:
    """Each declaration with no body in ``source``: its line, up to its parameter list's end."""
    source = textwrap.dedent(source)
    scopes = ScopeIndex(tokenize(source, language), language, source)
    return [source[:end].rpartition("\n")[2].strip() for end in sorted(scopes.bodiless)]


@pytest.mark.parametrize(
    ("language", "source", "declarations"),
    [
        (
            "kotlin",
            """
            interface Tracker {
                fun <T : Any> trackEvent(event: T, props: Map<String, Any>)
                fun logScreen(screen: String): Unit
            }
            abstract class Base {
                abstract fun send(name: String)
            }
            fun relay(event: String) {
                trackEvent(event, props)
                return f(x)
                x = f(y)
                F(x)
                this.track(e)
            }
            """,
            [
                "fun <T : Any> trackEvent(event: T, props: Map<String, Any>)",
                "fun logScreen(screen: String)",
                "abstract fun send(name: String)",
            ],
        ),
        (
            "swift",
            """
            protocol Tracking {
                func track(_ event: String) async throws -> Bool
                init(screen: String)
                init?(coder: NSCoder)
            }
            func relay(event: String) {
                trackEvent(event)
                return f(x)
                x = f(y)
                _ = F(x)
                self.track(e)
                super.init(nibName: nil, bundle: nil)
            }
            """,
            ["func track(_ event: String)", "init(screen: String)", "init?(coder: NSCoder)"],
        ),
        (
            "java",
            """
            public interface AnalyticsTracker {
                void trackEvent(String name, Map<String, Object> props);
                List<String> names() throws IOException;
                String[] keys();
            }
            public abstract class Base {
                public abstract void logEvent(String name, Bundle params);
                public native int count(int x);
                void relay(String event) {
                    trackEvent(name);
                    return f(x);
                    x = f(y);
                    new F(x);
                    this.track(e);
                    assert valid(x);
                    Collections.<String>emptyList();
                    return count > limit(x);
                }
            }
            """,
            [
                "void trackEvent(String name, Map<String, Object> props)",
                "List<String> names()",
                "String[] keys()",
                "public abstract void logEvent(String name, Bundle params)",
                "public native int count(int x)",
            ],
        ),
        (
            "objc",
            """
            #import <Foundation/Foundation.h>

            void TrackEvent(NSString *name, NSDictionary *props);
            NSString *EventName(NSInteger code) NS_SWIFT_NAME(eventName(code:));
            FOUNDATION_EXPORT NSArray<NSString *> *EventNames(void);

            static void Relay(NSString *event) {
                TrackEvent(event, nil);
                return f(x);
                x = f(y);
                y = a * f(x);
                self.track(e);
            }
            """,
            [
                "void TrackEvent(NSString *name, NSDictionary *props)",
                "NSString *EventName(NSInteger code)",
                "FOUNDATION_EXPORT NSArray<NSString *> *EventNames(void)",
            ],
        ),
        # A prototype a `#define` holds, on its line or the next one a `\`
        # continues it to, is still a declaration, as is one whose return type
        # has a line of its own.
        (
            "objc",
            """
            #define DECLARE_SCREEN void TrackScreen(NSString *name);
            #define DECLARE_LOG(name) \\
                void name(NSString *event);
            #if DEBUG
            NSString *
            DebugName(NSInteger code);
            #endif
            """,
            [
                "#define DECLARE_SCREEN void TrackScreen(NSString *name)",
                "void name(NSString *event)",
                "DebugName(NSInteger code)",
            ],
        ),
        (
            "ts",
            """
            export interface Tracker {
              trackEvent(name: string, props: object);
              trackPage(page: string): void;
              flush(): void
            }
            abstract class Base {
              abstract send(event: string): void
              protected abstract name()
              log(event: string): void
              log(event: string, props: object): void
              log(event: string, props?: object) {}
            }
            declare function track(event: string): void;
            export function format(event: string): string
            export function format(event: string, id?: number) { return event }
            function relay(event: string) {
              trackEvent(name);
              return f(x);
              x = f(y);
              new F(x);
              this.track(e)
              cond ? track(a) : skip(b)
              const value = cond ? -track(a) : b
              const remove = async (id: string) => drop(id)
            }
            """,
            [
                "trackEvent(name: string, props: object)",
                "trackPage(page: string)",
                "flush()",
                "abstract send(event: string)",
                "protected abstract name()",
                "log(event: string)",
                "log(event: string, props: object)",
                "declare function track(event: string)",
                "export function format(event: string)",
            ],
        ),
    ],
)
def test_declarations_with_no_body_are_kept_and_calls_are_not(
    language: str, source: str, declarations: list[str]
) -> None:
    # Each is where its parameter list ends, as `find_calls` reports a call's end.
    assert _bodiless(language, source) == declarations


@pytest.mark.parametrize(
    "directive",
    [
        "#endif",
        "#else",
        "#if DEBUG",
        "#ifdef ANALYTICS_ENABLED",
        "#elif LEGACY",
        "#pragma clang diagnostic pop",
        "#import <Foundation/Foundation.h>",
        # A line that ends in a `\` goes on with the next one.
        "#define TRACKING_ENABLED \\\n    YES",
    ],
)
def test_a_preprocessor_line_ends_in_no_return_type(directive: str) -> None:
    source = (
        f"{directive}\nvoid TrackEvent(NSString *name);\n"
        f"{directive}\nNSString *EventName(NSInteger code);\n"
        f"{directive}\nNSArray<NSString *> *EventNames(void);\n"
        f"static void Relay(NSString *event) {{\n{directive}\n    TrackShown(event, nil);\n}}\n"
    )
    # Nor in a modifier: a prototype on the line after one is still a
    # declaration, whatever its return type, and a call is a call.
    assert _bodiless("objc", source) == [
        "void TrackEvent(NSString *name)",
        "NSString *EventName(NSInteger code)",
        "NSArray<NSString *> *EventNames(void)",
    ]


class _Read(str):
    """A source that counts the characters its slices and searches pass over."""

    read = 0

    def __getitem__(self, key: SupportsIndex | slice) -> str:
        part = super().__getitem__(key)
        self.read += len(part)
        return part

    def find(
        self, sub: str, start: SupportsIndex | None = None, end: SupportsIndex | None = None
    ) -> int:
        found = super().find(sub, start, end)
        low, high, _ = slice(start, end).indices(len(self))
        self.read += (found + len(sub) if found >= 0 else high) - low
        return found

    def rfind(
        self, sub: str, start: SupportsIndex | None = None, end: SupportsIndex | None = None
    ) -> int:
        found = super().rfind(sub, start, end)
        low, high, _ = slice(start, end).indices(len(self))
        self.read += high - (found if found >= 0 else low)
        return found


def _macro_read(lines: int) -> int:
    """How much of a ``#define`` of ``lines`` continued prototypes ``ScopeIndex`` reads."""
    prototypes = "".join(f"    NSString *Name{number}(int code); \\\n" for number in range(lines))
    text = f"#define DECLARE_ALL \\\n{prototypes}    NSString *NameLast(int code)\n"
    source = _Read(text)
    assert len(ScopeIndex(tokenize(text, "objc"), "objc", source).bodiless) == lines
    return source.read


def test_a_long_macro_is_not_read_again_for_each_line_it_continues() -> None:
    """Each prototype a ``\\``-continued ``#define`` holds is on the directive's line."""
    # Walking back to the `#define` from each of its lines would read the
    # macro once per line: twice the lines, four times the reading.
    assert _macro_read(400) < 3 * _macro_read(200)


@pytest.mark.parametrize("language", ["java", "objc"])
def test_any_number_of_type_parameter_lists_is_read_back(language: str) -> None:
    # More `<…>` lists before a declaration than Python's stack is deep.
    lists = "<A>" * 2000
    source = f"class A {{\n    {lists} List<T> f();\n    void go() {{ log(x); }}\n}}\n"
    assert _bodiless(language, source) == [f"{lists} List<T> f()"]
    scope = _scope_at(language, source)
    assert scope is not None and scope.name == "go"


@pytest.mark.parametrize(
    ("language", "source", "owner"),
    [
        (
            "swift",
            "struct ScreenView<T>: View, Equatable where T: Hashable {\n"
            "    init(screen: String) {\n        log(screen)\n    }\n}\n",
            "ScreenView",
        ),
        (
            "swift",
            "final class Home: UIViewController {\n"
            "    override init() {\n        log(x)\n    }\n}\n",
            "Home",
        ),
        # An extension's type is the last part of its dotted name.
        (
            "swift",
            "extension Analytics.Screen: CustomStringConvertible {\n"
            "    public init(name: String) {\n        log(name)\n    }\n}\n",
            "Screen",
        ),
        ("swift", "init(event: String) {\n    log(event)\n}\n", None),
        (
            "ts",
            "export class PageView<T extends { id: string }> extends Base<T> implements Tracked {\n"
            "  constructor(page: string) {\n    log(page)\n  }\n}\n",
            "PageView",
        ),
        (
            "ts",
            "const PageView = class extends Base {\n  constructor(page: string) {\n"
            "    log(page)\n  }\n}\n",
            None,
        ),
        # Only an `init` or a `constructor` is called by its type's name.
        ("ts", "class PageView {\n  track(page: string) {\n    log(page)\n  }\n}\n", None),
    ],
)
def test_an_initializer_belongs_to_the_type_around_it(
    language: str, source: str, owner: str | None
) -> None:
    scope = _scope_at(language, source)
    assert scope is not None and scope.owner == owner


@pytest.mark.parametrize(
    ("header", "owner"),
    [
        # In TypeScript `object` is a type, not a Kotlin declaration ...
        ("export class Store<T extends object>", "Store"),
        ("export class Cache extends Map<string, object>", "Cache"),
        ("class Relay implements Handler<object>", "Relay"),
        ("class Pair<K, V = object>", "Pair"),
        ("abstract class Shape<T extends object = {}>", "Shape"),
        # ... and `internal` a name, not a Kotlin modifier.
        ("class Home extends internal.Screen", "Home"),
        # In `<…>` a TypeScript keyword may be part of a type.
        ("class Tuple<const T extends readonly string[]>", "Tuple"),
        ("class Factory<T extends new () => View>", "Factory"),
    ],
)
def test_a_typescript_class_header_holds_typescript_s_words(header: str, owner: str) -> None:
    scope = _scope_at(
        "ts", f"{header} {{\n  constructor(page: string) {{\n    log(page)\n  }}\n}}\n"
    )
    assert scope is not None and scope.owner == owner


def test_the_scope_index_finds_the_innermost_body_and_the_last_local_before_a_call() -> None:
    source = textwrap.dedent(
        """
        fun outer(event: String) {
            val name = "a"
            fun first() { log(name) }
            fun second() {
                val name = "b"
                log(name)
                val late = "c"
            }
            log(event)
        }
        """
    )
    tokens = tokenize(source, "kotlin")
    scopes = ScopeIndex(tokens, "kotlin", source)
    calls = [index for index, token in enumerate(tokens) if token.text == "log"]
    found: list[tuple[str, int | None]] = []
    for at in calls:
        scope = scopes.innermost(at)
        assert scope is not None
        local = scopes.local_declaration(scope, at, tokens[at + 2].text)
        line = None if local is None else source.count("\n", 0, tokens[local].offset) + 1
        found.append((scope.name, line))
    # The local of `outer` is not in `first`'s body; after `second`, the walk
    # goes out past both siblings to `outer`.
    assert found == [("first", None), ("second", 6), ("outer", None)]
    assert scopes.local_declaration(scopes.functions[2], calls[1], "late") is None


def test_parameters_carry_their_position_and_the_label_a_caller_writes() -> None:
    swift = _scope_at(
        "swift",
        'func track(_ event: String, for kind: Kind, label: String = "x") {\n    log(event)\n}\n',
    )
    assert swift is not None
    assert [(p.name, p.index, p.label) for p in swift.parameters] == [
        ("event", 0, None),
        ("kind", 1, "for"),
        ("label", 2, "label"),
    ]
    kotlin = _scope_at(
        "kotlin",
        "fun track(@Tag vararg events: String, crossinline done: () -> Unit,) {\n"
        "    log(events)\n}\n",
    )
    assert kotlin is not None
    assert [(p.name, p.index, p.label) for p in kotlin.parameters] == [
        ("events", 0, "events"),
        ("done", 1, "done"),
    ]
    ts = _scope_at("ts", "function track({ a }: P, name?: string, ...rest: R[]) {\n  log(a);\n}\n")
    assert ts is not None
    # A destructured part has no name but keeps its position.
    assert [(p.name, p.index, p.label) for p in ts.parameters] == [
        ("name", 1, None),
        ("rest", 2, None),
    ]


# --- what the listing reads, per call ------------------------------------------------
LOG_EVENT_CONFIG = """
project: demo
event_types:
  event:
    calls: [{function: "analytics.logEvent", positional: [name, properties]}]
"""


def _reads(call: ListedCall) -> dict[str, tuple[str, str | None, int | None]]:
    """``target -> (reason, function, line)`` for each value the call did not read."""
    return {
        target: (
            read.reason,
            read.declared.function if read.declared else None,
            read.declared.line if read.declared else None,
        )
        for target, read in call.read.targets()
        if read.value.kind == "dynamic"
    }


def test_a_variable_is_refined_from_the_function_the_call_sits_in(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/Screen.kt": """
                class Screen {
                    fun logEvent(event: String, params: Bundle) {
                        analytics.logEvent(event, params)
                    }
                    fun onAction(action: UiAction) {
                        analytics.logEvent(action.event)
                    }
                    fun refresh(flag: Boolean) {
                        val log = if (flag) Events.A else Events.B
                        analytics.logEvent(log)
                    }
                    fun setup() {
                        items.forEach { event -> analytics.logEvent(event) }
                    }
                }
            """,
            "app/Nested.kt": """
                fun outer(event: String) {
                    fun inner() {
                        analytics.logEvent(event)
                    }
                    fun other() {
                        val event = compute()
                        analytics.logEvent(event)
                    }
                }
                abstract class Base {
                    abstract fun a(event: String)
                    fun b() { analytics.logEvent(event) }
                }
            """,
        },
    )
    found = inventory(_config(root, LOG_EVENT_CONFIG))
    assert [_reads(call) for call in found.calls] == [
        # app/Nested.kt: an outer function's parameter captured by an inner one
        # stays a variable; a local of the innermost function is a local.
        {"name": ("variable", None, None)},
        {"name": ("local", "other", 6)},
        # A body-less abstract function does not reach into the next body.
        {"name": ("variable", None, None)},
        # app/Screen.kt
        {"name": ("parameter", "logEvent", 2), "properties": ("parameter", "logEvent", 2)},
        {"name": ("parameter_property", "onAction", 5)},
        {"name": ("local", "refresh", 9)},
        # A lambda's own parameter: lambdas are no scopes.
        {"name": ("variable", None, None)},
    ]
    forwarder = found.calls[3].read.name
    assert forwarder is not None and forwarder.declared is not None
    parameter = forwarder.declared.parameter
    assert parameter is not None and (parameter.name, parameter.index, parameter.label) == (
        "event",
        0,
        "event",
    )
    # Only a bare parameter makes a forwarder: `action.event` is set where the
    # action is built, so `onAction` is none.
    assert [forwarder.function for forwarder in found.forwarders] == ["logEvent"]


def test_swift_guard_let_keeps_a_parameter_a_parameter(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "App/Track.swift": """
                func track(event: String?) {
                    guard let event = event else { return }
                    Analytics.shared.logEvent(event)
                }
            """,
        },
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          legacy: {calls: [{function: "Analytics.shared.logEvent", name_arg: 0}]}
        """,
    )
    [call] = inventory(config).calls
    assert _reads(call) == {"name": ("parameter", "track", 1)}


def test_a_name_that_reads_as_a_constant_is_the_parameter_it_names(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/Screen.kt": """
                class Screen {
                    fun log(kEvent: String) {
                        analytics.logEvent(kEvent)
                    }
                    fun upper(EVENT: String) {
                        analytics.logEvent(EVENT)
                    }
                    fun property(kEvent: Event) {
                        analytics.logEvent(kEvent.name)
                    }
                    fun local() {
                        val SCREEN = pick()
                        analytics.logEvent(SCREEN)
                    }
                    fun constant(screen: String) {
                        analytics.logEvent(HOME_SCREEN)
                    }
                }
            """,
            "ios/Show.swift": """
                func show(home: String) {
                    analytics.logEvent(.home)
                }
            """,
        },
    )
    found = inventory(_config(root, LOG_EVENT_CONFIG))
    assert [_reads(call) for call in found.calls] == [
        # A parameter hides a constant of its name, whatever its case.
        {"name": ("parameter", "log", 2)},
        {"name": ("parameter", "upper", 5)},
        # A bare name only: one of more segments stays a constant's.
        {"name": ("unknown_constant", None, None)},
        # Never a local, which may be one a block declared (the test below).
        {"name": ("unknown_constant", None, None)},
        # A constant no parameter is named like stays one.
        {"name": ("unknown_constant", None, None)},
        # A Swift shorthand names a case, never the parameter of its name.
        {"name": ("unknown_constant", None, None)},
    ]
    assert [forwarder.function for forwarder in found.forwarders] == ["log", "upper"]


def test_a_name_that_reads_as_a_constant_is_no_local_a_block_may_have_closed(
    tmp_path: Path,
) -> None:
    """Locals are found with no regard to blocks: the one named may be out of scope."""
    root = _repo(
        tmp_path,
        {
            "app/Report.kt": """
                fun report(kind: String) {
                    if (kind == "a") {
                        val EVENT = "a_open"
                        analytics.logEvent(EVENT)
                    }
                    analytics.logEvent(EVENT)
                }
            """,
            "ios/Report.swift": """
                func report(kind: String, value: String?) {
                    if let kEvent = value {
                        analytics.logEvent(kEvent)
                    }
                    analytics.logEvent(kEvent)
                }
            """,
            "web/report.ts": """
                import { EVENT } from "./gen/events"

                export function report(kind: string) {
                  if (kind === "a") {
                    const EVENT = "a_open"
                    analytics.logEvent(EVENT)
                  }
                  analytics.logEvent(EVENT)
                }
                export function each(names: string[]) {
                  names.forEach((n) => { const kName = n; analytics.logEvent(kName) })
                  analytics.logEvent(kName)
                }
            """,
        },
    )
    found = inventory(_config(root, LOG_EVENT_CONFIG))
    # After each block, and after the lambda, the name is a constant the scan
    # could not resolve, not the local; inside, nothing tells the two apart.
    assert [(call.origin.path, call.origin.line) for call in found.calls] == [
        ("app/Report.kt", 4),
        ("app/Report.kt", 6),
        ("ios/Report.swift", 3),
        ("ios/Report.swift", 5),
        ("web/report.ts", 6),
        ("web/report.ts", 8),
        ("web/report.ts", 11),
        ("web/report.ts", 12),
    ]
    assert [_reads(call) for call in found.calls] == [
        {"name": ("unknown_constant", None, None)}
    ] * len(found.calls)


def test_unmapped_arguments_and_fields_read_from_default_labels(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/A.kt": """
                fun a() {
                    tracker.send(key = "opened", exclude = listOf("x"), params = mapOf("a" to 1))
                    analytics.logEvent("closed", bundle, extra)
                }
            """,
        },
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          event:
            calls:
              - {function: "tracker.send"}
              - {function: "analytics.logEvent", positional: [name, properties]}
        """,
    )
    send, log = inventory(config).calls
    # No `args:`: every label goes to the field of its name (`params` to properties).
    assert send.default_label_fields == (("key", "key"), ("exclude", "exclude"))
    assert send.read.unmapped == ()
    assert log.default_label_fields == ()
    assert log.read.unmapped == ("#2",)


# --- calls that look like tracking but match no call spec ------------------------------
def test_unmatched_calls_that_look_like_tracking(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/Events.kt": """
                object Events {
                    object Name {
                        const val Open = "open"
                        const val Closed = "closed"
                    }
                }
            """,
            "app/Screen.kt": """
                fun screen(parent: View) {
                    wrap(Events.Name.Open)
                    PageTrackerScope(type = "home")
                    analyticsHub.logOpen(screenId)
                    other.wrap(Events.Name.Closed)
                    analytics.logEvent(Events.Name.Closed.format(id))
                    analytics.logEvent(eventName(Events.Name.Open))
                    trackLater(analytics.logEvent("x"))
                    trackShownRows()
                    parent.requestDisallowInterceptTouchEvent(true)
                    SingleLiveEvent(x)
                    segments.add(Events.Name.Open)
                    analyticsHub.getConfig(x)
                    putString(Events.Name.Open)
                    EventObserver(x)
                    _navigate.value = Event(Destination.Detail(id))
                    Analytics.Event("opened")
                }
                fun <T> trackEvent(event: T) { println(event) }
            """,
            # Collections take an event constant too.
            "app/Sinks.kt": """
                fun sinks(names: MutableList<String>, stack: ArrayDeque<String>) {
                    mutableSetOf(Events.Name.Open)
                    hashSetOf(Events.Name.Open)
                    linkedSetOf(Events.Name.Open)
                    sortedSetOf(Events.Name.Open)
                    arrayListOf(Events.Name.Open)
                    listOfNotNull(Events.Name.Open)
                    setOfNotNull(Events.Name.Open)
                    mapOf(Events.Name.Open)
                    mutableMapOf(Events.Name.Open)
                    hashMapOf(Events.Name.Open)
                    List.of(Events.Name.Open)
                    Set.of(Events.Name.Open)
                    Map.of(Events.Name.Open, 1)
                    EnumSet.of(Events.Name.Open)
                    ImmutableList.of(Events.Name.Open)
                    Arrays.asList(Events.Name.Open)
                    names.addAll(Events.Name.Open)
                    stack.push(Events.Name.Open)
                }
            """,
            # The DOM's event plumbing: listeners, handlers, the events dispatched.
            "web/input.ts": """
                window.addEventListener("scroll", f);
                el.dispatchEvent(e);
                bus.onTrackEvent(p);
                handleEvent(e);
                emitter.addTrackingListener(cb);
                el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
                new Event("change");
                new CustomEvent("cart:updated", { detail });
                new UIEvent("resize");
                new KeyboardEvent("keydown", { key: "Enter" });
                new FocusEvent("focus");
                new WheelEvent("wheel");
                new InputEvent("input");
                new PointerEvent("pointerdown");
                new TouchEvent("touchstart");
                new DragEvent("drop");
                new CompositionEvent("compositionend");
                new ClipboardEvent("paste");
                new AnimationEvent("animationend");
                new TransitionEvent("transitionend");
                new MessageEvent("message", { data });
                new ErrorEvent("error");
                new ProgressEvent("progress");
                new PopStateEvent("popstate");
                new HashChangeEvent("hashchange");
                new StorageEvent("storage");
                new SubmitEvent("submit");
                new BeforeUnloadEvent("beforeunload");
                new PageTransitionEvent("pageshow");
            """,
        },
    )
    found = inventory(_config(root, LOG_EVENT_CONFIG))
    assert [
        (group.method, group.signal, group.calls, dict(group.receivers))
        for group in found.unmatched
    ] == [
        # An event-name constant passed to something no spec matches comes first.
        ("wrap", "constant", 2, {"": 1, "other": 1}),
        # Then by calls and method: the name says so (only a bare `Event(…)`
        # is the DOM's or a one-shot holder); a tracking vendor's verb.
        ("Event", "name", 1, {"Analytics": 1}),
        ("PageTrackerScope", "name", 1, {"": 1}),
        ("logOpen", "receiver", 1, {"analyticsHub": 1}),
    ]
    first = found.unmatched[0]
    assert (first.first.path, first.first.line, first.first.column) == ("app/Screen.kt", 2, 5)
    assert first.example == "wrap(Events.Name.Open)"


def test_an_assertion_or_a_hook_given_an_event_constant_is_not_tracking(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/Events.kt": 'object Events { object Name { const val Open = "open" } }\n',
            "app/ScreenTest.kt": """
                fun opens(screen: Screen) {
                    expect(Events.Name.Open)
                    expect(screen.name).toBe(Events.Name.Open)
                    assertEquals(Events.Name.Open, screen.name)
                    assertThat(screen.name).isEqualTo(Events.Name.Open)
                    screen.name.shouldBe(Events.Name.Open)
                    useState(Events.Name.Open)
                    verify(tracker).send(eq(Events.Name.Open))
                    assertThat(screen.name, equalTo(Events.Name.Open))
                    screen.name.equals(Events.Name.Open)
                    relay(Events.Name.Open)
                }
            """,
        },
    )
    found = inventory(_config(root, LOG_EVENT_CONFIG))
    # They compare or keep the constant; only the call that passes it on is listed.
    assert [(group.method, group.signal) for group in found.unmatched] == [("relay", "constant")]


def test_unmatched_groups_sort_constants_first_then_by_calls(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/Events.kt": 'object Events { object Name { const val Open = "open" } }\n',
            "app/A.kt": """
                fun a() {
                    trackClose(x)
                    trackOpen(x)
                    trackOpen(y)
                    trackShown(x)
                    trackShown(y)
                    trackShown(z)
                    zap(Events.Name.Open)
                }
            """,
        },
    )
    found = inventory(_config(root, LOG_EVENT_CONFIG))
    assert [(group.method, group.signal, group.calls) for group in found.unmatched] == [
        # The constant group first, with the fewest calls; then the most calls
        # first, whatever their method's name.
        ("zap", "constant", 1),
        ("trackShown", "name", 3),
        ("trackOpen", "name", 2),
        ("trackClose", "name", 1),
    ]


def test_a_declaration_with_no_body_is_neither_a_call_nor_a_caller(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/Tracker.kt": """
                interface Tracker {
                    fun <T : Any> trackEvent(event: T, props: Map<String, Any>)
                }
                fun close(tracker: Tracker) {
                    tracker.trackEvent("close", emptyMap())
                }
            """,
            "ios/Track.h": "void TrackEvent(NSString *name, NSDictionary *props);\n",
            "ios/Track.m": """
                void TrackEvent(NSString *name, NSDictionary *props) {
                    track(name, props);
                }
                void Opened(void) {
                    TrackEvent(@"opened", nil);
                }
            """,
            "src/AnalyticsTracker.java": """
                public interface AnalyticsTracker {
                    void trackEvent(String name, Map<String, Object> props);
                }
            """,
            "src/Base.java": """
                public abstract class Base {
                    public abstract void logEvent(String name, Bundle params);
                    void opened() {
                        logEvent("opened", null);
                    }
                }
            """,
            "src/Main.java": """
                class Main {
                    void open(AnalyticsTracker tracker) {
                        tracker.trackEvent("open", null);
                    }
                }
            """,
            "src/Tracker.java": """
                public class Tracker extends Base implements AnalyticsTracker {
                    public void trackEvent(String name, Map<String, Object> props) {
                        analytics.track(name, props);
                    }
                    public void logEvent(String name, Bundle params) {
                        analytics.track(name, params);
                    }
                }
            """,
            "web/tracker.ts": """
                export interface Tracker {
                  trackEvent(name: string, props: object);
                }
                tracker.trackEvent("shown", {});
            """,
        },
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          event: {calls: [{function: "track", positional: [name, properties]}]}
        """,
    )
    found = inventory(config)
    # An interface or abstract method, a prototype: only the calls next to them are listed.
    assert [
        (group.method, group.calls, group.first.path, group.first.line) for group in found.unmatched
    ] == [
        ("trackEvent", 3, "app/Tracker.kt", 5),
        ("TrackEvent", 1, "ios/Track.m", 5),
        ("logEvent", 1, "src/Base.java", 4),
    ]
    # ... and only those calls are counted as the forwarders' callers.
    assert [
        (
            forwarder.function,
            forwarder.location.path,
            forwarder.callers.same_file,
            forwarder.callers.other_files,
            forwarder.callers.first_unmatched,
        )
        for forwarder in found.forwarders
    ] == [
        ("TrackEvent", "ios/Track.m", 1, 0, Location("ios/Track.m", 5, 5)),
        ("trackEvent", "src/Tracker.java", 0, 3, Location("app/Tracker.kt", 5, 5)),
        ("logEvent", "src/Tracker.java", 0, 1, Location("src/Base.java", 4, 9)),
    ]


def test_a_call_on_the_line_after_a_preprocessor_line_is_a_call(tmp_path: Path) -> None:
    screen = _repo(
        tmp_path / "screen",
        {
            "ios/Screen.m": """
                #import "Screen.h"

                @implementation Screen
                - (void)viewDidLoad {
                    [super viewDidLoad];
                #if DEBUG
                    [self debugSetup];
                #endif
                    TrackScreen(@"home", nil);
                #ifdef ANALYTICS_ENABLED
                    TrackEvent(@"open", nil);
                #endif
                    TrackEvent(@"close", nil);
                }
                @end
            """,
        },
    )
    found = inventory(_config(screen, "project: demo\n"))
    # Neither `endif` nor `ANALYTICS_ENABLED` is a return type: these are calls ...
    assert [
        (group.method, group.calls, group.first.line, group.first.column)
        for group in found.unmatched
    ] == [("TrackEvent", 2, 11, 5), ("TrackScreen", 1, 9, 5)]
    home = _repo(
        tmp_path / "home",
        {
            "ios/Home.m": """
                #import "Track.h"

                void LogScreen(NSString *screen) {
                    TrackScreen(screen, nil);
                }

                @implementation Home
                - (void)viewDidAppear:(BOOL)animated {
                #ifdef ANALYTICS_ENABLED
                    LogScreen(@"home");
                #endif
                }
                @end
            """,
        },
    )
    config = _config(
        home,
        """
        project: demo
        event_types:
          screen: {calls: [{function: "TrackScreen", positional: [name, properties]}]}
        """,
    )
    [forwarder] = inventory(config).forwarders
    # ... and one is its forwarder's caller.
    assert (forwarder.function, forwarder.callers.same_file, forwarder.callers.first_unmatched) == (
        "LogScreen",
        1,
        Location("ios/Home.m", 10, 5),
    )


def test_a_call_in_a_builder_chain_link_is_listed_with_its_matched_call(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/Track.kt": """
                fun f(x: String) {
                    tracker.track(Structured("cat", "act").label(trackLabel(x)).value(3))
                }
            """,
        },
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          se: {preset: snowplow_structured}
        """,
    )
    found = inventory(config)
    [call] = found.calls
    label = call.read.fields["label"]
    assert (label.reason, label.value.expression) == ("function", "trackLabel(x)")
    # The link's argument is the matched call's label, not a call of its own.
    assert found.unmatched == ()


def test_a_call_in_a_chain_link_the_spec_does_not_map_is_a_call_of_its_own(
    tmp_path: Path,
) -> None:
    root = _repo(
        tmp_path,
        {
            "web/checkout.ts": """
                export function checkout(order: Order) {
                  segment.track("checkout_started").then(() => trackPurchase(order))
                }
            """,
            "app/Open.kt": """
                fun show(id: String) {
                    analytics.logEvent("open", null).addOnSuccessListener(trackDelivered(id))
                    Structured("cat", "act").also(trackShown(id)).label(trackLabel(id))
                    Builder("built").label(trackDropped(id))
                    Structured("c", "a").also(Structured(trackInner(id), "y")).label(trackLabel(id))
                }
            """,
        },
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          event:
            calls:
              - {function: "segment.track", positional: [name]}
              - {function: "analytics.logEvent", positional: [name, properties]}
              - {function: "Builder", positional: [name], chain: {label: null}}
          se: {preset: snowplow_structured}
        """,
    )
    found = inventory(config)
    # `.then(…)`, `.addOnSuccessListener(…)` and `.also(…)` are no part of the
    # call, before a link the spec reads or after one; `.label(…)` is, mapped
    # to a field or to null. A matched call in such a link is one of its own,
    # and what it reads is listed with it.
    assert [(group.method, group.first) for group in found.unmatched] == [
        ("trackDelivered", Location("app/Open.kt", 2, 59)),
        ("trackPurchase", Location("web/checkout.ts", 2, 48)),
        ("trackShown", Location("app/Open.kt", 3, 35)),
    ]
    label = found.calls[1].read.fields["label"]
    assert (label.reason, label.value.expression) == ("function", "trackLabel(id)")


def test_an_unmatched_example_is_one_line_cut_at_the_limit(tmp_path: Path) -> None:
    arguments = ",\n    ".join(f"Events.Name.Open{number}" for number in range(12))
    root = _repo(
        tmp_path,
        {
            "app/Events.kt": 'object Events { object Name { const val Open0 = "open" } }\n',
            "app/A.kt": f"fun a() {{\n  relay({arguments})\n}}\n",
        },
    )
    [group] = inventory(_config(root, LOG_EVENT_CONFIG)).unmatched
    assert "\n" not in group.example and "  " not in group.example
    assert len(group.example) == inventory_module.EXAMPLE_MAX
    assert group.example.startswith("relay(Events.Name.Open0, Events.Name.Open1,")
    assert group.example.endswith("...")


# --- forwarders ----------------------------------------------------------------------
def test_forwarders_count_their_callers_by_name(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/Home.kt": """
                class Home {
                    fun <T> zeta(event: T) {
                        analytics.track(event)
                    }
                    fun gamma(name: String) {
                        analytics.track(name)
                    }
                    fun beta(name: String) {
                        analytics.track(name)
                    }
                    fun alpha(name: String) {
                        analytics.track(name)
                    }
                    fun report(name: String) {
                        analytics.track(name)
                    }
                    fun show() {
                        zeta("home_shown")
                        zeta("home_closed")
                        gamma("home_gamma")
                        beta("home_beta")
                        report("home_reported")
                    }
                }
            """,
            "app/Other.kt": """
                fun other() {
                    gamma("other_shown")
                }
            """,
            "ios/ScreenView.swift": """
                struct ScreenView {
                    init(event: String) {
                        analytics.track(event)
                    }
                }
                func show() {
                    _ = ScreenView(event: "home")
                }
            """,
        },
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          event:
            calls:
              - {function: "analytics.track", name_arg: 0}
              - {function: "report", name_arg: 0}
        """,
    )
    found = inventory(config)
    summary = [
        (
            forwarder.function,
            forwarder.location.path,
            forwarder.location.line,
            forwarder.calls,
            [(p.name, p.index, p.label, p.targets) for p in forwarder.parameters],
            (
                forwarder.callers.same_file,
                forwarder.callers.same_file_matched,
                forwarder.callers.other_files,
                forwarder.callers.other_files_matched,
            ),
        )
        for forwarder in found.forwarders
    ]
    event, name = [("event", 0, "event", ("name",))], [("name", 0, "name", ("name",))]
    assert summary == [
        # Unmatched callers in its own file first: its calls want a call spec. A
        # generic function's declaration, a call to `find_calls`, is none of them.
        ("zeta", "app/Home.kt", 2, 1, event, (2, 0, 0, 0)),
        # As many there: then unmatched callers elsewhere.
        ("gamma", "app/Home.kt", 5, 1, name, (1, 0, 1, 0)),
        # A Swift initializer is called by its type's name.
        ("ScreenView", "ios/ScreenView.swift", 2, 1, event, (1, 0, 0, 0)),
        ("beta", "app/Home.kt", 8, 1, name, (1, 0, 0, 0)),
        # None unmatched, every caller matched or none at all: by function name.
        ("alpha", "app/Home.kt", 11, 1, name, (0, 0, 0, 0)),
        ("report", "app/Home.kt", 14, 1, name, (1, 1, 0, 0)),
    ]
    assert [
        None if first is None else (first.path, first.line)
        for first in (forwarder.callers.first_unmatched for forwarder in found.forwarders)
    ] == [
        ("app/Home.kt", 18),
        ("app/Home.kt", 20),
        ("ios/ScreenView.swift", 7),
        ("app/Home.kt", 21),
        None,
        None,
    ]


def test_a_forwarder_found_only_elsewhere_points_at_its_first_caller_there(
    tmp_path: Path,
) -> None:
    root = _repo(
        tmp_path,
        {
            "app/A.kt": "fun relay(event: String) = analytics.track(event)\n",
            "app/B.kt": 'fun b() {\n    relay("b_shown")\n}\n',
        },
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          event: {calls: [{function: "analytics.track", name_arg: 0}]}
        """,
    )
    [forwarder] = inventory(config).forwarders
    callers = forwarder.callers
    assert (callers.same_file, callers.other_files) == (0, 1)
    assert callers.first_unmatched is not None
    assert (callers.first_unmatched.path, callers.first_unmatched.line) == ("app/B.kt", 2)


def test_a_forwarder_s_own_body_holds_none_of_its_callers(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/Analytics.kt": """
                class Analytics(private val firebaseAnalytics: FirebaseAnalytics) {
                    fun logEvent(event: String, params: Bundle) {
                        if (params.isEmpty) logEvent(event, defaults)
                        firebaseAnalytics.logEvent(event, params)
                    }
                    fun opened() {
                        logEvent("opened", bundle)
                    }
                }
            """,
        },
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          event:
            calls: [{function: "firebaseAnalytics.logEvent", positional: [name, properties]}]
        """,
    )
    [forwarder] = inventory(config).forwarders
    callers = forwarder.callers
    # The call it forwards to and its call of itself share its name, not its callers.
    assert (callers.same_file, callers.same_file_matched, callers.other_files) == (1, 0, 0)
    assert callers.first_unmatched == Location("app/Analytics.kt", 7, 9)


def test_each_forwarder_of_a_name_leaves_out_only_its_own_body(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/Analytics.kt": """
                class Analytics {
                    fun log(event: String) { fun log(event: String, n: Int) {
                        client.track(event, a)
                        log("deep", n)
                    }
                        client.track(event, b)
                        log("inner", 1)
                    }
                    fun log(event: String, flag: Boolean) {
                        if (flag) log("nested")
                        client.track(event, c)
                    }
                    fun closed() { log("after") }
                }
            """,
            "app/Relay.kt": """
                fun outer() {
                    fun relay(event: String) = client.track(event, a)
                    relay("next")
                }
            """,
        },
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          event: {calls: [{function: "client.track", positional: [name, properties]}]}
        """,
    )
    # Line 2 declares two `log`s, one inside the other: one forwarder, whose
    # bodies hold `log("deep", n)` (both of them) and `log("inner", 1)`. The
    # overload on line 9 holds `log("nested")`. `relay`'s body ends where
    # `relay("next")` starts.
    assert [
        (
            forwarder.location,
            forwarder.calls,
            forwarder.callers.same_file,
            forwarder.callers.first_unmatched,
        )
        for forwarder in inventory(config).forwarders
    ] == [
        (Location("app/Analytics.kt", 9), 1, 3, Location("app/Analytics.kt", 4, 9)),
        (Location("app/Analytics.kt", 2), 2, 2, Location("app/Analytics.kt", 10, 19)),
        (Location("app/Relay.kt", 2), 1, 1, Location("app/Relay.kt", 3, 5)),
    ]


def test_no_call_is_tested_against_each_overload_s_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Twenty overloads of one name, and twenty calls of it besides theirs."""
    overloads = "".join(
        f"    fun track(name: String, a{number}: Int) {{ client.track(name, props) }}\n"
        for number in range(20)
    )
    calls = "".join(f'        track("e{number}", {number})\n' for number in range(20))
    root = _repo(
        tmp_path,
        {"app/Tracker.kt": f"class Tracker {{\n{overloads}    fun all() {{\n{calls}    }}\n}}\n"},
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          event: {calls: [{function: "client.track", positional: [name, properties]}]}
        """,
    )
    asked = 0
    holds = FunctionScope.holds

    def counted(self: FunctionScope, index: int) -> bool:
        nonlocal asked
        asked += 1
        return holds(self, index)

    monkeypatch.setattr(FunctionScope, "holds", counted)
    found = inventory(config)
    # Each overload's own call of `track` is left out of its callers, and only that.
    assert {
        (forwarder.callers.same_file, forwarder.callers.same_file_matched)
        for forwarder in found.forwarders
    } == {(39, 19)}
    # Each matched call asks for the function it sits in. Asking each overload
    # about each of the 40 calls of `track` would be 800 questions more.
    assert asked <= 2 * len(found.calls)


def test_an_initializer_is_a_forwarder_under_its_type_s_name(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "App/Home.swift": """
                final class HomeViewController: UIViewController {
                    init() {
                        super.init(nibName: nil, bundle: nil)
                    }
                    func show() {
                        _ = ScreenView(screen: currentScreenName)
                    }
                }
            """,
            "App/ScreenView.swift": """
                struct ScreenView {
                    init(screen: String) {
                        analytics.track(screen)
                    }
                    init() {
                        self.init(screen: "home")
                    }
                }
            """,
            "web/main.ts": """
                export function boot() {
                  new PageView(location.pathname)
                }
            """,
            "web/page.ts": """
                export class PageView {
                  constructor(page: string) {
                    analytics.track(page)
                  }
                }
                export const Untyped = class {
                  constructor(page: string) {
                    analytics.track(page)
                  }
                }
            """,
        },
    )
    config = _config(
        root,
        """
        project: demo
        event_types:
          event: {calls: [{function: "analytics.track", name_arg: 0}]}
        """,
    )
    found = inventory(config)
    # The parameter is the initializer's own.
    assert [_reads(call) for call in found.calls] == [
        {"name": ("parameter", "init", 2)},
        {"name": ("parameter", "constructor", 2)},
        {"name": ("parameter", "constructor", 7)},
    ]
    # Its callers write its type's name, never `super.init` or `self.init`. A
    # class with no name has no callers to count: it is left out.
    assert [
        (
            forwarder.function,
            forwarder.location.path,
            forwarder.location.line,
            forwarder.callers.other_files,
            forwarder.callers.first_unmatched,
        )
        for forwarder in found.forwarders
    ] == [
        ("PageView", "web/page.ts", 2, 1, Location("web/main.ts", 2, 7)),
        ("ScreenView", "App/ScreenView.swift", 2, 1, Location("App/Home.swift", 6, 13)),
    ]
    assert [forwarder.callers.same_file for forwarder in found.forwarders] == [0, 0]


# --- the inventory as a whole ----------------------------------------------------------
def test_the_inventory_lists_exactly_the_items_scan_sends(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "Tracking/AppEvents.swift": SWIFT_ENUMS,
            "Feature/HomeView.swift": SWIFT_FEATURE,
            "Feature/Checkout.ts": """
                analytics.track("Checkout Started", { total, currency: "EUR" });
                amplitude.getInstance().logEvent(`Plan ${planId} Selected`, { plan: planId });
                trackStructEvent({ category: "checkout", action: "submit", label: 'card' });
            """,
        },
    )
    config = _config(
        root,
        WRAPPER_CONFIG
        + "  track: {preset: segment_track}\n"
        + "  amp: {preset: amplitude_log_event}\n"
        + "  st: {preset: snowplow_structured}\n",
    )
    found = inventory(config)
    outcome = scan(config)
    assert tuple(item for call in found.calls for item in call.items) == outcome.items
    assert found.files == outcome.files == 2
    assert len(found.calls) == outcome.calls
    assert [call.origin for call in found.calls] == [item.origin for item in outcome.items]
    # Files in sorted order: the TypeScript calls come first, then the Swift
    # wrapper, whose second call passes a label the scan cannot read.
    assert [call.fully_read for call in found.calls] == [True, True, True, True, False]
    assert [call.sent for call in found.calls] == [True] * 5


def test_an_inventory_needs_no_event_types(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"web/a.ts": 'analytics.track("Signed Up", { plan: "pro" });\n'})
    config = _config(root, "project: demo\n")
    with pytest.raises(TriplConfigError, match="names no event types"):
        scan(config)
    found = inventory(config)
    assert (found.calls, found.forwarders, found.files) == ((), (), 1)
    assert [(group.method, group.signal) for group in found.unmatched] == [("track", "name")]


def test_discovery_stops_at_its_file_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(
        tmp_path,
        {name: 'analytics.track("x");\n' for name in ("a.ts", "b.ts", "c.ts")} | {"notes.md": "x"},
    )
    config = _config(root, "project: demo\n")
    monkeypatch.setattr(inventory_module, "MAX_DISCOVERY_FILES", 2)
    capped = inventory(config, discovery=True)
    assert (capped.files, capped.truncated) == (2, True)
    monkeypatch.setattr(inventory_module, "MAX_DISCOVERY_FILES", 3)
    whole = inventory(config, discovery=True)
    assert (whole.files, whole.truncated) == (3, False)
    # With a config the user chose `sources:`: no cap.
    monkeypatch.setattr(inventory_module, "MAX_DISCOVERY_FILES", 1)
    assert (inventory(config).files, inventory(config).truncated) == (3, False)
