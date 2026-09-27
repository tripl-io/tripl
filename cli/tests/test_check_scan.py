"""``tripl check`` static mode: tokenizer, call finder, enum index, presets.

Every fixture here is SYNTHETIC — made-up wrappers (``Analytics.shared``),
made-up enums (``AppEvents.Category``) — written to exercise the call SHAPES
real apps use: multi-line labelled wrapper calls, enum shorthand resolved from
another file, the Objective-C selector form, interpolated legacy names, names
returned by a function, and the Segment/Amplitude/Snowplow SDK calls in
TypeScript and Kotlin.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from tripl_cli.check import yamlish
from tripl_cli.check.calls import find_calls
from tripl_cli.check.config import CheckConfig, parse
from tripl_cli.check.lexer import STRING, Interp, tokenize
from tripl_cli.check.model import CheckItem
from tripl_cli.check.scan import glob_regex, scan
from tripl_cli.check.symbols import template_text, variable_name
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
