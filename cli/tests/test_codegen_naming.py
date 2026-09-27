"""Plan strings -> identifiers (``tripl_cli.codegen.naming``), per language."""

from __future__ import annotations

import pytest

from tripl_cli.codegen import naming
from tripl_cli.codegen.languages import DIALECTS
from tripl_cli.codegen.naming import KOTLIN, SWIFT, TS, Namer
from tripl_cli.codegen.value_types import EnumRegistry


@pytest.mark.parametrize(
    ("raw", "words"),
    [
        ("weather_alert", ["weather", "alert"]),
        ("Home Screen View", ["Home", "Screen", "View"]),
        ("checkout:start", ["checkout", "start"]),
        ("HomeView", ["Home", "View"]),
        ("URLOpened", ["URL", "Opened"]),
        ("v2Checkout", ["v2", "Checkout"]),
        ("1st run", ["1st", "run"]),
        ("promo_sheet_${id}_shown", ["promo", "sheet", "id", "shown"]),
        ("  --  ", []),
        ("café", ["caf"]),
    ],
)
def test_words_split_on_punctuation_and_camel_case(raw: str, words: list[str]) -> None:
    assert naming.words(raw) == words


@pytest.mark.parametrize(
    ("raw", "lower", "upper", "snake"),
    [
        ("Home Screen View", "homeScreenView", "HomeScreenView", "HOME_SCREEN_VIEW"),
        ("checkout:start", "checkoutStart", "CheckoutStart", "CHECKOUT_START"),
        ("weather_alert", "weatherAlert", "WeatherAlert", "WEATHER_ALERT"),
        ("URLOpened", "urlOpened", "UrlOpened", "URL_OPENED"),
        ("!!!", "value", "Value", "VALUE"),
    ],
)
def test_the_three_conventions(raw: str, lower: str, upper: str, snake: str) -> None:
    assert naming.lower_camel(raw) == lower
    assert naming.upper_camel(raw) == upper
    assert naming.upper_snake(raw) == snake


def test_a_leading_digit_gets_an_underscore_in_every_language() -> None:
    assert naming.member("1st run", SWIFT) == "_1stRun"
    assert naming.member("1st run", TS) == "_1stRun"
    assert naming.member("1st run", KOTLIN, snake=True) == "_1ST_RUN"
    assert naming.type_name("3d touch", SWIFT) == "_3dTouch"


@pytest.mark.parametrize(
    ("raw", "language", "expected"),
    [
        ("default", SWIFT, "default_"),
        ("self", SWIFT, "self_"),
        ("default", TS, "default_"),
        ("class", KOTLIN, "class_"),
        ("object", KOTLIN, "object_"),
        # Contextual keywords are valid identifiers and stay as they are.
        ("open", SWIFT, "open"),
        ("type", TS, "type"),
        ("type", SWIFT, "type"),
        # Members the generated enums already have.
        ("rawValue", SWIFT, "rawValue_"),
        ("name", SWIFT, "name_"),
    ],
)
def test_reserved_words_get_a_trailing_underscore(raw: str, language: str, expected: str) -> None:
    assert naming.member(raw, language) == expected


def test_kotlin_enum_entries_never_collide_with_keywords() -> None:
    assert naming.member("object", KOTLIN, snake=True) == "OBJECT"


@pytest.mark.parametrize(
    ("raw", "language", "expected"),
    [
        ("string", SWIFT, "StringValue"),
        ("type", SWIFT, "TypeValue"),
        ("record", TS, "RecordValue"),
        ("unit", KOTLIN, "UnitValue"),
        ("category", SWIFT, "Category"),
    ],
)
def test_reserved_type_names_get_value(raw: str, language: str, expected: str) -> None:
    assert naming.type_name(raw, language) == expected


def test_collisions_are_numbered_in_the_order_names_are_taken() -> None:
    namer = Namer({"Taken"})
    assert namer.take("homeView") == "homeView"
    assert namer.take("homeView") == "homeView2"
    assert namer.take("homeView") == "homeView3"
    assert namer.take("Taken") == "Taken2"


def test_enum_cases_that_collide_keep_their_exact_raw_values() -> None:
    registry = EnumRegistry(DIALECTS[SWIFT], Namer())
    value_type = registry.enum("Screen", ["Home View", "home_view", "home-view"])
    assert value_type.enum is not None
    cases = [(case.ident, case.raw) for case in value_type.enum.cases]
    # Sorted by raw value first, so the numbering is deterministic.
    assert cases == [
        ("homeView", "Home View"),
        ("homeView2", "home-view"),
        ("homeView3", "home_view"),
    ]


def test_the_same_values_under_the_same_name_share_one_enum() -> None:
    registry = EnumRegistry(DIALECTS[TS], Namer())
    first = registry.enum("Platform", ["ios", "android"])
    again = registry.enum("Platform", ["android", "ios"])
    other = registry.enum("Platform", ["web"])
    assert first.enum is again.enum
    assert other.enum is not None and other.enum.name == "Platform2"
    assert [enum.name for enum in registry.enums] == ["Platform", "Platform2"]


def test_is_identifier_guards_type_name_overrides() -> None:
    assert naming.is_identifier("AppEvents", SWIFT)
    assert not naming.is_identifier("App Events", SWIFT)
    assert not naming.is_identifier("class", KOTLIN)
