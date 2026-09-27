"""Name similarity, convention inference and lint (GH #265, F12) — pure, no database.

Covers ``core.analyzers.name_similarity`` and the matching helpers in
``core.analyzers.duplicate_matching``: normalisation across spellings and
scripts, the lexical legs and their caps, the embedding blend, rule-aware
slot scoring, convention inference, lint codes with their suggestions, and
clustering / explosion detection.

Every name here is synthetic.
"""

from __future__ import annotations

import itertools
import random
import uuid

import pytest

from tripl.core.analyzers.duplicate_matching import (
    DUPLICATE_THRESHOLD,
    MIN_LEXICAL_FOR_EMBEDDING,
    CatalogName,
    NameIndex,
    cluster_duplicates,
    find_combinatorial_explosions,
    ordered_pair,
    scored_pairs,
)
from tripl.core.analyzers.name_rules import compile_rule, rule_separators
from tripl.core.analyzers.name_similarity import (
    Convention,
    combined_score,
    detect_case,
    infer_convention,
    lexical_score,
    lint_name,
    normalise,
    rule_aware_score,
    suggest,
    with_prefix,
)

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "tokens"),
    [
        ("Paywall Screen View", ("paywall", "screen", "view")),
        ("paywall_screen_view", ("paywall", "screen", "view")),
        ("paywall-screen-view", ("paywall", "screen", "view")),
        ("paywallScreenView", ("paywall", "screen", "view")),
        ("PaywallScreenView", ("paywall", "screen", "view")),
        ("shop:open:tap", ("shop", "open", "tap")),
        ("HTTPRequestSent", ("http", "request", "sent")),
        ("onboarding_step1", ("onboarding", "step", "1")),
        ("iOSPromoView", ("ios", "promo", "view")),
        ("  ", ()),
    ],
)
def test_normalise_splits_every_spelling(name: str, tokens: tuple[str, ...]) -> None:
    assert normalise(name) == tokens


def test_normalise_is_unicode_aware() -> None:
    # Cyrillic camel case splits on the case boundary and lowercases.
    assert normalise("ЭкранГлавная") == ("экран", "главная")
    assert normalise("экран_главная") == normalise("Экран Главная")
    # Compatibility forms fold (full-width letters) and German sharp s casefolds.
    assert normalise("Ｐａｙｗａｌｌ View") == ("paywall", "view")
    assert normalise("Straße Öffnen") == ("strasse", "öffnen")


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------


def test_identical_names_in_different_spellings_score_one() -> None:
    assert lexical_score("Paywall View", "paywall_view") == 1.0
    assert lexical_score("Paywall View", "View Paywall") == 1.0
    assert lexical_score("экран_главная", "Экран Главная") == 1.0


def test_filler_word_is_a_near_duplicate_not_an_identity() -> None:
    score = lexical_score("Paywall View", "Paywall Screen View")
    assert DUPLICATE_THRESHOLD <= score < 1.0


def test_swapped_filler_is_not_a_duplicate() -> None:
    # Filler ADDED is padding; filler SWAPPED names another thing on the screen.
    assert lexical_score("Paywall Screen View", "Paywall Button View") < DUPLICATE_THRESHOLD
    assert lexical_score("Paywall View", "Paywall Button View") >= DUPLICATE_THRESHOLD


def test_word_order_only_matches_when_a_verb_moved() -> None:
    assert lexical_score("View Paywall", "Paywall View") == 1.0
    assert lexical_score("home_to_profile", "profile_to_home") < MIN_LEXICAL_FOR_EMBEDDING
    assert lexical_score("Cart Item Tap", "Item Cart Tap") < MIN_LEXICAL_FOR_EMBEDDING
    # A noun-ish verb does not count as the moved verb.
    assert lexical_score("Search Home", "Home Search") < MIN_LEXICAL_FOR_EMBEDDING


def test_typo_and_plural_score_high() -> None:
    assert lexical_score("purchase_completed", "purchase_complete") >= DUPLICATE_THRESHOLD


def test_numeric_substitution_is_capped() -> None:
    assert lexical_score("Onboarding Step 1 View", "Onboarding Step 2 View") <= 0.5


def test_unrelated_names_score_low() -> None:
    assert lexical_score("Home Screen View", "Paywall View") < 0.5
    assert lexical_score("Settings Screen View", "Profile Screen View") < DUPLICATE_THRESHOLD


def test_rule_aware_score_compares_slot_by_slot() -> None:
    rule = compile_rule("{category}:{action}:{label}")
    assert rule is not None and rule.separators == (":",)
    # One slot with a different value is a different event under the rule.
    assert rule_aware_score("shop:tap:hat", "shop:tap:scarf", rule) == 0.0
    assert rule_aware_score("shop:tap:filter", "shop:tap:filters", rule) < DUPLICATE_THRESHOLD
    # A case-only difference in a value is still the same event.
    assert rule_aware_score("shop:tap:hat", "Shop:Tap:Hat", rule) == 1.0
    # A name that does not follow the rule falls back to the whole-name score.
    assert rule_aware_score("shop:tap", "shop:tap:hat", rule) == lexical_score(
        "shop:tap", "shop:tap:hat"
    )
    # Without the rule, a plural is a near-duplicate.
    assert lexical_score("shop:tap:filter", "shop:tap:filters") >= DUPLICATE_THRESHOLD


def test_rule_separators_are_the_literals_between_placeholders() -> None:
    assert rule_separators("{event.category}_{action}") == ("_",)
    assert rule_separators("{a}.{b}/{c}") == (".", "/")
    assert rule_separators("{a} - {b}") == (" - ",)
    assert rule_separators("{a}{b}") == ()
    assert rule_separators("static_name") == ()
    assert rule_separators(None) == ()


def test_rule_split_is_non_greedy_and_case_insensitive() -> None:
    rule = compile_rule("{screen}_{element}_{action}")
    assert rule is not None
    # A value holding the separator lands in the LAST slot, for every name alike.
    assert rule.split("shop_promo_card_tap") == ("shop", "promo", "card_tap")
    assert rule.slots == ("{screen}", "{element}", "{action}")
    fixed = compile_rule("app:{screen}_view")
    assert fixed is not None
    assert fixed.split("APP:home_VIEW") == ("home",)
    assert fixed.split("web:home_view") is None
    merged = compile_rule("{a}{b}:{c}")
    assert merged is not None and merged.slots == ("{a}{b}", "{c}")


def test_combined_score_blends_and_never_hides_a_lexical_match() -> None:
    assert combined_score(0.8, None) == 0.8
    assert combined_score(0.8, 1.0) == pytest.approx(0.9)
    # A weak cosine (name vs whole document) must not pull a lexical hit down.
    assert combined_score(0.95, 0.2) == 0.95
    # Out-of-range cosines are clamped.
    assert combined_score(0.5, 1.7) == pytest.approx(0.75)
    assert pytest.approx(0.76) == MIN_LEXICAL_FOR_EMBEDDING


# ---------------------------------------------------------------------------
# Convention inference and lint
# ---------------------------------------------------------------------------

TITLE_NAMES = [
    "Home Screen View",
    "Cart Screen View",
    "Profile Screen View",
    "Search Results View",
    "Order History View",
    "Settings Screen View",
]


def test_detect_case() -> None:
    assert detect_case("paywall_view") == "snake"
    assert detect_case("paywall-view") == "kebab"
    assert detect_case("paywallView") == "camel"
    assert detect_case("PaywallView") == "pascal"
    assert detect_case("Paywall View") == "space"
    assert detect_case("paywall") is None
    assert detect_case("shop:open_cart:tap") == "snake"


def test_infer_convention_title_case_verb_last() -> None:
    convention = infer_convention(TITLE_NAMES)
    assert convention.case == "space"
    assert convention.space_style == "title"
    assert convention.verb_position == "last"
    assert convention.separator is None
    assert convention.confidence == 1.0
    assert convention.sample_size == len(TITLE_NAMES)


def test_infer_convention_rule_shaped_names() -> None:
    convention = infer_convention(
        ["shop:open:tap", "shop:close:tap", "shop:item:view", "shop:cart:view", "shop:pay:tap"]
    )
    assert convention.separator == ":"
    # Single lowercase words say "lowercase"; spelled snake_case.
    assert convention.case == "snake"
    assert convention.prefix == "shop"


def test_infer_convention_is_silent_on_small_or_mixed_samples() -> None:
    assert infer_convention([]) == Convention()
    small = infer_convention(["Home View", "Cart View"])
    assert small.case == "mixed"
    assert lint_name("cart_view", small) == []
    mixed = infer_convention(
        ["home_view", "CartView", "Profile View", "search-view", "orderView", "Settings View"]
    )
    assert mixed.case == "mixed"


def test_lint_case_with_suggestion() -> None:
    convention = infer_convention(TITLE_NAMES)
    issues = lint_name("checkout_screen_view", convention)
    assert [issue["code"] for issue in issues] == ["case"]
    assert issues[0]["suggestion"] == "Checkout Screen View"
    assert suggest("checkout_screen_view", convention) == "Checkout Screen View"


def test_lint_verb_order_with_suggestion() -> None:
    convention = infer_convention(TITLE_NAMES)
    issues = lint_name("View Wishlist", convention)
    assert [issue["code"] for issue in issues] == ["verb_order"]
    assert issues[0]["suggestion"] == "Wishlist View"


def test_verb_order_skips_noun_ish_verbs_and_participles() -> None:
    convention = infer_convention(TITLE_NAMES)
    # "Purchase", "Search", "Share" are as often nouns: never moved.
    assert lint_name("Purchase Wishlist", convention) == []
    assert lint_name("Search Results", convention) == []
    # A participle at either end describes a state: no reorder.
    assert lint_name("View Done", convention) == []
    assert lint_name("Tap Completed", convention) == []


def test_render_keeps_acronyms_and_mixed_case() -> None:
    convention = infer_convention(TITLE_NAMES)
    assert suggest("HTTPErrorView", convention) == "HTTP Error View"
    assert suggest("iOS Promo View", convention) == "iOS Promo View"
    assert suggest("ios_promo_view", convention) == "Ios Promo View"


def test_lint_separator_and_prefix() -> None:
    convention = infer_convention(
        ["shop:open:tap", "shop:close:tap", "shop:item:view", "shop:cart:view", "shop:pay:tap"]
    )
    codes = {issue["code"]: issue["suggestion"] for issue in lint_name("cart.open.tap", convention)}
    assert codes["separator"] == "cart:open:tap"
    assert codes["prefix"] == "shop:cart:open:tap"
    assert suggest("cart.open.tap", convention) == "shop:cart:open:tap"


def test_prefix_needs_a_full_sample_and_is_never_a_verb() -> None:
    assert infer_convention(["shop cart", "shop hat", "shop scarf"]).prefix is None
    verb_first = ["Open Cart", "Open Menu", "Open Settings", "Open Profile", "Open Inbox"]
    assert infer_convention(verb_first).prefix is None
    nouns = [
        "Shop Cart View",
        "Shop Hat View",
        "Shop Scarf View",
        "Shop Menu View",
        "Shop Bag View",
    ]
    assert infer_convention(nouns).prefix == "shop"


def test_lint_prefix_for_plain_names() -> None:
    base = infer_convention(TITLE_NAMES)
    convention = with_prefix(base, "shop")
    issues = lint_name("Basket View", convention)
    assert [issue["code"] for issue in issues] == ["prefix"]
    assert issues[0]["suggestion"] == "Shop Basket View"


def test_conforming_name_has_no_lint() -> None:
    convention = infer_convention(TITLE_NAMES)
    assert lint_name("Checkout Screen View", convention) == []
    assert suggest("Checkout Screen View", convention) == "Checkout Screen View"


def test_lint_keeps_non_ascii_names() -> None:
    convention = infer_convention(TITLE_NAMES)
    issues = lint_name("экран_оплаты_view", convention)
    assert issues and issues[0]["code"] == "case"
    assert issues[0]["suggestion"] == "Экран Оплаты View"


# ---------------------------------------------------------------------------
# Matching, clusters and explosions
# ---------------------------------------------------------------------------


def test_name_index_finds_same_type_first() -> None:
    page, click = uuid.uuid4(), uuid.uuid4()
    same = CatalogName(uuid.uuid4(), "Paywall View", page)
    other = CatalogName(uuid.uuid4(), "Paywall View", click)
    unrelated = CatalogName(uuid.uuid4(), "Home Screen View", page)
    index = NameIndex([other, unrelated, same])
    found = index.find("Paywall Screen View", page)
    assert [m.event_id for m in found] == [same.event_id, other.event_id]
    assert found[0].same_type and not found[1].same_type
    assert index.find("Paywall Screen View", page, exclude=[same.event_id])[0].event_id == (
        other.event_id
    )


def test_cluster_duplicates_links_same_type_and_respects_dismissals() -> None:
    page = uuid.uuid4()
    a = CatalogName(uuid.uuid4(), "Paywall View", page)
    b = CatalogName(uuid.uuid4(), "Paywall Screen View", page)
    c = CatalogName(uuid.uuid4(), "paywall_view", page)
    d = CatalogName(uuid.uuid4(), "Onboarding Step 1 View", page)
    e = CatalogName(uuid.uuid4(), "Onboarding Step 2 View", page)
    index = NameIndex([a, b, c, d, e])
    clusters = cluster_duplicates(index)
    assert len(clusters) == 1
    assert set(clusters[0].event_ids) == {a.event_id, b.event_id, c.event_id}
    assert clusters[0].score == 1.0

    # Dismissing one pair keeps the others linked through the third event.
    dismissed = frozenset({ordered_pair(a.event_id, c.event_id)})
    assert set(cluster_duplicates(index, dismissed=dismissed)[0].event_ids) == {
        a.event_id,
        b.event_id,
        c.event_id,
    }
    only_pair = NameIndex([a, b])
    assert (
        cluster_duplicates(only_pair, dismissed=frozenset({ordered_pair(a.event_id, b.event_id)}))
        == []
    )


def test_combinatorial_explosion_over_rule_slots() -> None:
    rule = compile_rule("{category}:{action}:{label}")
    names = [f"shop:tap:item_{k}" for k in range(60)] + ["shop:open:cart"]
    [explosion] = find_combinatorial_explosions(names, rule)
    assert explosion.slot == 2
    assert explosion.label == "{label}"
    assert explosion.count == 60
    assert explosion.pattern == "shop:tap:*"
    assert find_combinatorial_explosions(names[:50], rule) == []
    # Within the scan's cardinality threshold it explodes; above it the column
    # would already be a template.
    assert find_combinatorial_explosions(names, rule, cardinality_threshold=100)
    assert find_combinatorial_explosions(names, rule, cardinality_threshold=40) == []


def test_combinatorial_explosion_needs_a_naming_rule() -> None:
    names = [f"Promo {k} Shown" for k in range(55)]
    assert find_combinatorial_explosions(names, None) == []
    rule = compile_rule("{a} {b} {c}")
    [explosion] = find_combinatorial_explosions(names, rule)
    assert explosion.slot == 1
    assert explosion.count == 55


# ---------------------------------------------------------------------------
# False-positive guard: a rule-built catalog is (almost) never a duplicate
# ---------------------------------------------------------------------------

_CATEGORIES = ["shop", "profile", "feed", "cart"]
_ACTIONS = ["tap", "view", "open"]
_LABELS = [
    "filter", "filters", "banner", "banners", "item", "items", "hat", "hats", "scarf",
    "card", "cards", "promo card", "promo cards", "step 1", "step 2", "tab home",
    "tab profile", "button", "list", "grid", "header", "footer", "avatar", "avatars",
    "coupon", "coupons", "size chart", "color chart", "sort", "sorts", "promo screen",
    "promo button", "home to profile", "profile to home", "offer", "offers", "badge",
    "badges",
]  # fmt: skip


def _rule_catalog(separator: str, seed: int, size: int = 200) -> tuple[str, list[str]]:
    """``size`` distinct synthetic names of ``{category}<sep>{action}<sep>{label}``.

    Labels come in singular/plural and swapped-word pairs on purpose — the
    near-misses a whole-name comparison flags.
    """
    combos = list(itertools.product(_CATEGORIES, _ACTIONS, _LABELS))
    random.Random(seed).shuffle(combos)
    inner = "_" if separator == ":" else separator
    names = [
        separator.join((category, action, label.replace(" ", inner)))
        for category, action, label in combos[:size]
    ]
    return f"{{category}}{separator}{{action}}{separator}{{label}}", names


@pytest.mark.parametrize("separator", [":", "_", "-", " "])
def test_rule_catalog_false_positive_rate_stays_under_one_percent(separator: str) -> None:
    name_format, names = _rule_catalog(separator, seed=7)
    type_id = uuid.uuid4()
    entries = [CatalogName(uuid.uuid4(), name, type_id) for name in names]
    total = len(names) * (len(names) - 1) // 2

    ruled, truncated = scored_pairs(NameIndex(entries, formats_by_type={type_id: name_format}))
    assert not truncated
    flagged = [pair for pair, score in ruled if score >= DUPLICATE_THRESHOLD]
    assert len(flagged) / total < 0.01
    assert flagged == []

    # The same catalog WITHOUT the rule does flag plurals: the rule is what helps.
    unruled, _ = scored_pairs(NameIndex(entries))
    assert any(score >= DUPLICATE_THRESHOLD for _, score in unruled)


def test_scored_pairs_stops_at_the_pair_budget() -> None:
    type_id = uuid.uuid4()
    entries = [CatalogName(uuid.uuid4(), f"Paywall View {k}", type_id) for k in range(40)]
    pairs, truncated = scored_pairs(NameIndex(entries), max_pairs=10)
    assert truncated
    assert len(pairs) <= 10
    _, over_budget = scored_pairs(NameIndex(entries))
    assert not over_budget
