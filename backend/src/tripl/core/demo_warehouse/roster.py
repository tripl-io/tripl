"""The demo catalog: every event type, field and event, authored once.

This is the ONE place the demo's catalog is written down. The seeder builds the
tracking plan from it (names, descriptions, statuses, tags, field values, meta
values), and the warehouse model (:mod:`.traffic`, :mod:`.rows`) emits rows from
it, so a plan event and the warehouse rows it should match cannot drift apart:
an event's ``name`` IS the warehouse ``event_name`` the scan reads, and its
``event_type`` IS the warehouse ``event_type``.

The product is a recipes and meal-planning app with a Premium subscription,
on iOS, Android and the web. The catalog is deliberately larger than what fires:
about a third of the events carry traffic, the rest are planned (draft, in
review, ready for development) or retired, which is what a real tracking plan
looks like and gives search something to find. Descriptions are written for
people (and for semantic search): they say what the user did, in the words
someone would search with.

Pure data. No I/O, no clock; :mod:`.calendar` owns time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

PLATFORMS: tuple[str, ...] = ("ios", "android", "web")
#: The default platform mix of an event, before its own tilt.
DEFAULT_PLATFORM_MIX: tuple[tuple[str, float], ...] = (
    ("ios", 0.45),
    ("android", 0.35),
    ("web", 0.20),
)
#: Countries and their share of rows. GB pays in GBP, which the plan's
#: ``currency`` enum does not list: that is the demo's enum-violation story.
COUNTRY_MIX: tuple[tuple[str, float], ...] = (
    ("US", 0.34),
    ("GB", 0.12),
    ("DE", 0.10),
    ("FR", 0.08),
    ("BR", 0.10),
    ("IN", 0.12),
    ("JP", 0.05),
    ("CA", 0.09),
)
CURRENCY_BY_COUNTRY: dict[str, str] = {
    "US": "USD",
    "CA": "USD",
    "BR": "USD",
    "IN": "USD",
    "JP": "USD",
    "GB": "GBP",
    "DE": "EUR",
    "FR": "EUR",
}
#: Store prices per product and currency, as the warehouse records them.
PRICES: dict[str, dict[str, str]] = {
    "prod_monthly": {"USD": "7.99", "EUR": "7.99", "GBP": "6.99"},
    "prod_annual": {"USD": "59.99", "EUR": "59.99", "GBP": "49.99"},
    "prod_weekly": {"USD": "2.99", "EUR": "2.99", "GBP": "2.49"},
}
#: The products the plan documents. ``prod_weekly`` is emitted by the warehouse
#: but not documented: the open variable-value drift on ``product_id``.
DOCUMENTED_PRODUCTS: tuple[str, ...] = ("prod_monthly", "prod_annual")
UNDOCUMENTED_PRODUCT = "prod_weekly"

# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

#: A property value domain: weighted choices, or an inclusive integer range.
type PropertyValue = str | int | bool


@dataclass(frozen=True)
class PropertyDef:
    """One key of the ``properties`` JSON document an event sends."""

    key: str
    choices: tuple[tuple[PropertyValue, float], ...] = ()
    int_range: tuple[int, int] | None = None


@dataclass(frozen=True)
class FieldDef:
    """A field of an event type, as the plan documents it."""

    name: str
    display_name: str
    field_type: str  # FieldDefinitionType value
    description: str
    is_required: bool = False
    enum_options: tuple[str, ...] | None = None


@dataclass(frozen=True)
class EventTypeDef:
    name: str
    display_name: str
    color: str
    description: str
    fields: tuple[FieldDef, ...]


@dataclass(frozen=True)
class EventDef:
    """One plan event and, when it fires, how the warehouse emits it.

    ``base_rate`` is the mean number of rows per hour before the daily and
    weekly shape (:mod:`.traffic`); 0 means the event never fires.
    ``field_values`` are what the plan's author typed, keyed by field name.
    """

    event_type: str
    name: str
    description: str
    status: str
    tags: tuple[str, ...]
    base_rate: float = 0.0
    screen_name: str | None = None
    button_id: str | None = None
    #: Weighted ``product_id`` values (purchase events).
    products: tuple[tuple[str, float], ...] = ()
    #: Whether rows carry ``amount`` and ``currency`` (a charge or a refund).
    priced: bool = False
    #: Platform mix override; ``None`` uses :data:`DEFAULT_PLATFORM_MIX`.
    platforms: tuple[tuple[str, float], ...] | None = None
    properties: tuple[PropertyDef, ...] = ()
    #: Fired only by app versions older than the newest two (a retired button).
    legacy_only: bool = False
    field_values: tuple[tuple[str, str], ...] = ()
    meta: tuple[tuple[str, str], ...] = ()
    metric_breakdown_columns: tuple[str, ...] = ()
    #: The event that replaces this one (deprecated events).
    successor: str | None = None


# ---------------------------------------------------------------------------
# Event types and fields
# ---------------------------------------------------------------------------

_PLATFORM_FIELD = FieldDef(
    "platform",
    "Platform",
    "enum",
    "Where the event happened: the iOS app, the Android app or the web app.",
    enum_options=PLATFORMS,
)


def _properties_field(description: str) -> FieldDef:
    return FieldDef("properties", "Properties", "json", description)


EVENT_TYPES: tuple[EventTypeDef, ...] = (
    EventTypeDef(
        "screen_view",
        "Screen View",
        "#6366f1",
        "A screen or page was shown to the user.",
        (
            FieldDef(
                "screen_name",
                "Screen Name",
                "string",
                "Stable identifier of the screen, the same on every platform.",
                is_required=True,
            ),
            _PLATFORM_FIELD,
            _properties_field("Context of the view; ``referrer`` says how the user got here."),
        ),
    ),
    EventTypeDef(
        "click",
        "Click",
        "#22c55e",
        "The user tapped or clicked a control.",
        (
            FieldDef("button_id", "Button ID", "string", "Stable identifier of the control."),
            FieldDef("screen_name", "Screen Name", "string", "The screen the control was on."),
            _PLATFORM_FIELD,
        ),
    ),
    EventTypeDef(
        "purchase",
        "Purchase",
        "#f59e0b",
        "Money and subscription state: checkouts, trials, charges, renewals and refunds.",
        (
            FieldDef(
                "product_id",
                "Product ID",
                "string",
                "The store product: the monthly or the annual Premium plan.",
            ),
            FieldDef("amount", "Amount", "number", "What the store charged, in ``currency``."),
            FieldDef(
                "currency",
                "Currency",
                "enum",
                "ISO currency of ``amount``.",
                enum_options=("USD", "EUR"),
            ),
            _PLATFORM_FIELD,
            _properties_field(
                "Payment details: ``payment_method`` and, for trials, ``trial_days``."
            ),
        ),
    ),
    EventTypeDef(
        "search",
        "Search",
        "#0ea5e9",
        "Finding recipes: typed and voice searches, suggestions and ingredient search.",
        (
            _PLATFORM_FIELD,
            _properties_field(
                "Search context: ``source`` (typed, voice, suggestion, recent), "
                "``results_count`` and ``query_length``."
            ),
        ),
    ),
    EventTypeDef(
        "content",
        "Content",
        "#a855f7",
        "What people do with recipes: save, cook, rate, time steps.",
        (
            _PLATFORM_FIELD,
            _properties_field("Recipe context: ``recipe_category`` and ``cook_time``."),
        ),
    ),
    EventTypeDef(
        "meal_plan",
        "Meal Plan",
        "#14b8a6",
        "The weekly meal planner and the shopping list built from it.",
        (
            _PLATFORM_FIELD,
            _properties_field("Planner context: ``day_of_week`` and ``planned_meals``."),
        ),
    ),
    EventTypeDef(
        "notification",
        "Notification",
        "#ec4899",
        "Push notifications, in-app messages and the permission prompt.",
        (
            _PLATFORM_FIELD,
            _properties_field("Message context: ``channel`` and ``campaign``."),
        ),
    ),
    EventTypeDef(
        "account",
        "Account",
        "#64748b",
        "Sign-up, sign-in and account management.",
        (
            _PLATFORM_FIELD,
            _properties_field("Sign-in context: ``method`` (email, apple, google)."),
        ),
    ),
    EventTypeDef(
        "app_lifecycle",
        "App Lifecycle",
        "#84cc16",
        "Installs, launches, updates and deep links.",
        (
            _PLATFORM_FIELD,
            _properties_field("Launch context: ``source`` (icon, push, deeplink, widget)."),
        ),
    ),
    EventTypeDef(
        "error",
        "Error",
        "#ef4444",
        "Failures the app reports: API errors, crashes, broken images.",
        (
            _PLATFORM_FIELD,
            _properties_field("Failure details: ``error_code`` and ``endpoint``."),
        ),
    ),
    EventTypeDef(
        "experiment",
        "Experiment",
        "#f97316",
        "A/B test exposures and feature flags.",
        (
            _PLATFORM_FIELD,
            _properties_field("Assignment: ``experiment`` and ``variant``."),
        ),
    ),
)

# ---------------------------------------------------------------------------
# Property domains, shared by the events of one type
# ---------------------------------------------------------------------------

_REFERRER = PropertyDef(
    "referrer",
    choices=(
        ("direct", 0.45),
        ("push", 0.15),
        ("deeplink", 0.1),
        ("search", 0.2),
        ("home_feed", 0.1),
    ),
)
_SEARCH_PROPERTIES = (
    PropertyDef(
        "source",
        choices=(("typed", 0.62), ("suggestion", 0.2), ("recent", 0.12), ("voice", 0.06)),
    ),
    PropertyDef("results_count", int_range=(0, 40)),
    PropertyDef("query_length", int_range=(2, 28)),
)
_RECIPE_PROPERTIES = (
    PropertyDef(
        "recipe_category",
        choices=(
            ("dinner", 0.42),
            ("lunch", 0.2),
            ("breakfast", 0.16),
            ("dessert", 0.12),
            ("snack", 0.1),
        ),
    ),
    PropertyDef(
        "cook_time",
        choices=(("under_15", 0.22), ("15_30", 0.41), ("30_60", 0.27), ("over_60", 0.1)),
    ),
)
_PLANNER_PROPERTIES = (
    PropertyDef(
        "day_of_week",
        choices=(
            ("mon", 0.18),
            ("tue", 0.15),
            ("wed", 0.15),
            ("thu", 0.14),
            ("fri", 0.12),
            ("sat", 0.12),
            ("sun", 0.14),
        ),
    ),
    PropertyDef("planned_meals", int_range=(1, 14)),
)
_MESSAGE_PROPERTIES = (
    PropertyDef("channel", choices=(("push", 0.7), ("in_app", 0.3))),
    PropertyDef(
        "campaign",
        choices=(
            ("dinner_reminder", 0.45),
            ("weekly_digest", 0.2),
            ("trial_ending", 0.15),
            ("promo_tuesday", 0.2),
        ),
    ),
)
_SIGN_IN_PROPERTIES = (
    PropertyDef("method", choices=(("email", 0.45), ("apple", 0.35), ("google", 0.2))),
)
_LAUNCH_PROPERTIES = (
    PropertyDef(
        "source",
        choices=(("icon", 0.72), ("push", 0.14), ("deeplink", 0.08), ("widget", 0.06)),
    ),
)
_ERROR_PROPERTIES = (
    PropertyDef(
        "error_code",
        choices=(("HTTP_500", 0.38), ("HTTP_503", 0.22), ("TIMEOUT", 0.28), ("NETWORK", 0.12)),
    ),
    PropertyDef(
        "endpoint",
        choices=(("/v2/recipes", 0.4), ("/v2/search", 0.3), ("/v2/plan", 0.18), ("/v2/sync", 0.12)),
    ),
)
_PAYMENT_PROPERTIES = (
    PropertyDef(
        "payment_method",
        choices=(("apple_pay", 0.38), ("google_pay", 0.27), ("card", 0.28), ("paypal", 0.07)),
    ),
)
_TRIAL_PROPERTIES = (*_PAYMENT_PROPERTIES, PropertyDef("trial_days", choices=((7, 1.0),)))
_EXPERIMENT_PROPERTIES = (
    PropertyDef(
        "experiment",
        choices=(("paywall_layout", 0.5), ("onboarding_length", 0.3), ("home_feed_ranking", 0.2)),
    ),
    PropertyDef("variant", choices=(("control", 0.5), ("b", 0.5))),
)

_PLANS = (("prod_monthly", 0.5), ("prod_annual", 0.4), ("prod_weekly", 0.1))
_TRIAL_PLANS = (("prod_monthly", 0.55), ("prod_annual", 0.35), ("prod_weekly", 0.1))
_MOBILE_ONLY = (("ios", 0.55), ("android", 0.45))
_WEB_HEAVY = (("ios", 0.25), ("android", 0.2), ("web", 0.55))

# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _screen(
    name: str,
    screen: str,
    description: str,
    status: str,
    tags: tuple[str, ...],
    base_rate: float = 0.0,
    **extra: object,
) -> EventDef:
    return EventDef(
        "screen_view",
        name,
        description,
        status,
        tags,
        base_rate,
        screen_name=screen,
        properties=(_REFERRER,),
        field_values=(("screen_name", screen), ("platform", "${platform}")),
        **extra,  # type: ignore[arg-type]
    )


def _click(
    name: str,
    button: str,
    screen: str,
    description: str,
    status: str,
    tags: tuple[str, ...],
    base_rate: float = 0.0,
    **extra: object,
) -> EventDef:
    return EventDef(
        "click",
        name,
        description,
        status,
        tags,
        base_rate,
        screen_name=screen,
        button_id=button,
        field_values=(
            ("button_id", button),
            ("screen_name", screen),
            ("platform", "${platform}"),
        ),
        **extra,  # type: ignore[arg-type]
    )


def _event(
    event_type: str,
    name: str,
    description: str,
    status: str,
    tags: tuple[str, ...],
    base_rate: float = 0.0,
    properties: tuple[PropertyDef, ...] = (),
    **extra: object,
) -> EventDef:
    return EventDef(
        event_type,
        name,
        description,
        status,
        tags,
        base_rate,
        properties=properties,
        field_values=(("platform", "${platform}"),),
        **extra,  # type: ignore[arg-type]
    )


def _purchase(
    name: str,
    description: str,
    status: str,
    tags: tuple[str, ...],
    base_rate: float = 0.0,
    *,
    products: tuple[tuple[str, float], ...] = _PLANS,
    priced: bool = False,
    properties: tuple[PropertyDef, ...] = _PAYMENT_PROPERTIES,
    **extra: object,
) -> EventDef:
    return EventDef(
        "purchase",
        name,
        description,
        status,
        tags,
        base_rate,
        products=products,
        priced=priced,
        properties=properties,
        field_values=(("product_id", "${product_id}"), ("platform", "${platform}")),
        **extra,  # type: ignore[arg-type]
    )


# Statuses: live (implemented, firing), implemented (built; the first traffic
# promotes it to live), in_review, ready_for_dev, draft, deprecated, archived.
EVENTS: tuple[EventDef, ...] = (
    # -- screen views ------------------------------------------------------
    _screen(
        "Home Screen View",
        "home",
        "User lands on the home feed: today's recipe picks, the meal-planner shortcut "
        "and recent searches. The first screen of almost every session.",
        "live",
        ("core", "onboarding"),
        150,
        meta=(("owner_team", "Growth"),),
        metric_breakdown_columns=("platform",),
    ),
    _screen(
        "Recipe Detail View",
        "recipe_detail",
        "A recipe page is opened: ingredients, step-by-step method, nutrition facts and "
        "reviews. The core content view of the app.",
        "live",
        ("core", "content"),
        110,
        meta=(("owner_team", "Content"),),
    ),
    _screen(
        "Search Results View",
        "search_results",
        "The list of recipes returned for a search query or a filter combination.",
        "live",
        ("search",),
        70,
        meta=(("owner_team", "Search"),),
    ),
    _screen(
        "Paywall View",
        "paywall",
        "The Premium upsell screen: plans, prices and the free-trial offer. Shown when a "
        "free user hits the saved-recipe limit or taps Upgrade.",
        "live",
        ("core", "checkout"),
        45,
        meta=(("owner_team", "Payments"),),
    ),
    _screen(
        "Meal Planner View",
        "meal_planner",
        "The weekly meal-planner calendar where recipes are placed on days.",
        "live",
        ("meal_plan",),
        34,
    ),
    _screen(
        "Onboarding Step 1 View",
        "onboarding_step1",
        "First onboarding screen: pick dietary preferences such as vegetarian, vegan, "
        "gluten-free or dairy-free.",
        "live",
        ("onboarding",),
        26,
    ),
    _screen(
        "Shopping List View",
        "shopping_list",
        "The grocery shopping list generated from the planned meals.",
        "live",
        ("meal_plan",),
        24,
    ),
    _screen(
        "Onboarding Step 2 View",
        "onboarding_step2",
        "Second onboarding screen: household size and how many dinners a week to plan.",
        "in_review",
        ("onboarding",),
        20,
    ),
    _screen(
        "Checkout Screen View",
        "checkout",
        "The payment sheet shown after a plan is picked, right before the store's purchase dialog.",
        "live",
        ("checkout",),
        16,
    ),
    _screen(
        "Onboarding Step 3 View",
        "onboarding_step3",
        "Last onboarding screen: the notification primer that explains dinner reminders "
        "before the system permission prompt.",
        "live",
        ("onboarding", "notifications"),
        16,
    ),
    _screen(
        "Profile Screen View",
        "profile",
        "User opens their profile: saved-recipe count, dietary preferences and subscription "
        "status.",
        "live",
        ("core",),
        15,
    ),
    _screen(
        "Cooking Mode View",
        "cooking_mode",
        "Hands-free cooking mode: one step per screen, large text, built-in timers.",
        "live",
        ("content",),
        14,
    ),
    _screen(
        "Collections View",
        "collections",
        "The user's folders of saved recipes, such as Weeknight dinners or Meal prep.",
        "live",
        ("content",),
        12,
    ),
    _screen(
        "Plan Comparison View",
        "plan_comparison",
        "Side-by-side comparison of the monthly and annual Premium plans, opened from the paywall.",
        "live",
        ("checkout",),
        8,
    ),
    _screen(
        "Notifications Inbox View",
        "notifications_inbox",
        "The in-app inbox of reminders and offers. Shipped in the last release; traffic "
        "promotes it from implemented to live.",
        "implemented",
        ("notifications",),
        4,
    ),
    _screen(
        "Settings Screen View",
        "settings",
        "App settings: measurement units, notifications, language and privacy.",
        "in_review",
        (),
    ),
    _screen(
        "Help Center View",
        "help_center",
        "Help articles and the contact-support entry point.",
        "ready_for_dev",
        ("support",),
    ),
    _screen(
        "Pantry View",
        "pantry",
        "Pantry inventory: ingredients the user already has at home, used to suggest "
        "recipes that need nothing extra.",
        "ready_for_dev",
        ("meal_plan",),
    ),
    _screen(
        "Nutrition Goals View",
        "nutrition_goals",
        "Daily calorie and macro targets (protein, carbs, fat) with progress rings.",
        "in_review",
        ("premium",),
    ),
    _screen(
        "Family Sharing View",
        "family_sharing",
        "Invite household members to share one Premium subscription and meal plan.",
        "draft",
        ("premium", "growth"),
    ),
    _screen(
        "Referral Program View",
        "referral",
        "Give-a-month, get-a-month referral page with the user's invite link.",
        "ready_for_dev",
        ("growth",),
    ),
    _screen(
        "Gift Subscription View",
        "gift_subscription",
        "Buy Premium as a gift for someone else: pick a plan and a delivery date.",
        "draft",
        ("checkout",),
    ),
    _screen(
        "Order History View",
        "order_history",
        "List of past purchases with store receipts and renewal dates.",
        "draft",
        ("checkout",),
    ),
    _screen(
        "Recipe Import View",
        "recipe_import",
        "Import a recipe from a website address into the user's collection.",
        "ready_for_dev",
        ("content",),
    ),
    _screen(
        "Weekly Recap View",
        "weekly_recap",
        "Sunday recap: meals cooked this week, money saved by planning, streaks.",
        "in_review",
        ("growth",),
    ),
    _screen(
        "Cuisine Explorer View",
        "cuisine_explorer",
        "Browse recipes by cuisine on a world map: Italian, Mexican, Thai, Indian.",
        "draft",
        ("content",),
    ),
    _screen(
        "Chef Profile View",
        "chef_profile",
        "A recipe author's page with their recipes and follower count.",
        "ready_for_dev",
        ("content",),
    ),
    _screen(
        "Offline Recipes View",
        "offline_recipes",
        "Recipes downloaded for offline cooking without a connection.",
        "draft",
        ("premium",),
    ),
    _screen(
        "Holiday Collection View",
        "holiday_collection",
        "The seasonal holiday landing page. Replaced by the seasonal row on the home feed.",
        "archived",
        ("content",),
    ),
    # -- clicks ------------------------------------------------------------
    _click(
        "Dismiss Paywall Click",
        "close_paywall",
        "paywall",
        "User closes the paywall without buying (the X or a swipe down).",
        "live",
        ("checkout",),
        30,
    ),
    _click(
        "Save Recipe Click",
        "save_recipe",
        "recipe_detail",
        "User taps the heart to save a recipe to a collection (bookmark, favourite).",
        "live",
        ("content",),
        26,
    ),
    _click(
        "Add to Meal Plan Click",
        "add_to_plan",
        "recipe_detail",
        "User adds a recipe to a day of the weekly meal planner.",
        "live",
        ("meal_plan",),
        18,
    ),
    _click(
        "Filter Applied Click",
        "apply_filter",
        "search_results",
        "User applies a search filter: diet, cook time, cuisine or calories.",
        "live",
        ("search",),
        16,
    ),
    _click(
        "Add to Shopping List Click",
        "add_to_list",
        "recipe_detail",
        "User sends a recipe's ingredients to the shopping list (grocery list).",
        "live",
        ("meal_plan",),
        15,
    ),
    _click(
        "Buy Button Click",
        "buy_now",
        "paywall",
        "User taps the primary Start Premium button on the paywall.",
        "live",
        ("core", "checkout"),
        14,
        meta=(("owner_team", "Payments"), ("jira", "PAY-1180")),
    ),
    _click(
        "Start Cooking Click",
        "start_cooking",
        "recipe_detail",
        "User starts hands-free cooking mode from a recipe.",
        "live",
        ("content",),
        12,
    ),
    _click(
        "Plan Toggle Click",
        "toggle_plan",
        "paywall",
        "User switches between the monthly and the annual plan on the paywall.",
        "live",
        ("checkout",),
        10,
    ),
    _click(
        "Video Play Click",
        "play_video",
        "recipe_detail",
        "User plays a recipe's step-by-step video.",
        "live",
        ("content",),
        10,
    ),
    _click(
        "Share Button Click",
        "share",
        "recipe_detail",
        "User shares a recipe link through the system share sheet (message, WhatsApp, e-mail).",
        "in_review",
        ("growth",),
        9,
    ),
    _click(
        "Skip Onboarding Click",
        "skip_onboarding",
        "onboarding_step1",
        "User skips the rest of onboarding and goes straight to the home feed.",
        "live",
        ("onboarding",),
        8,
    ),
    _click(
        "Upgrade Banner Click",
        "upgrade_banner",
        "home",
        "User taps the Go Premium banner on the home feed.",
        "live",
        ("checkout",),
        6,
    ),
    _click(
        "Legacy CTA Click",
        "old_cta",
        "paywall",
        "The old Upgrade button the paywall had before the buy_now redesign. Retired; "
        "only users on old app versions still send it.",
        "archived",
        ("checkout",),
        6,
        legacy_only=True,
        successor="Buy Button Click",
    ),
    _click(
        "Restore Purchase Click",
        "restore_purchase",
        "paywall",
        "User taps Restore purchases to recover a subscription bought on another device.",
        "in_review",
        ("checkout",),
    ),
    _click(
        "Contact Support Click",
        "contact_support",
        "help_center",
        "User opens the support chat from the help center.",
        "ready_for_dev",
        ("support",),
    ),
    _click(
        "Print Recipe Click",
        "print_recipe",
        "recipe_detail",
        "User prints a recipe or saves it as a PDF.",
        "ready_for_dev",
        ("content",),
    ),
    _click(
        "Rate App Prompt Click",
        "rate_app",
        "home",
        "User answers the in-app 'Enjoying the app?' rating prompt.",
        "draft",
        ("growth",),
    ),
    _click(
        "Unit Toggle Click",
        "toggle_units",
        "settings",
        "User switches between metric and US measurement units.",
        "draft",
        (),
    ),
    _click(
        "Servings Stepper Click",
        "servings_stepper",
        "recipe_detail",
        "User changes the number of servings; ingredient amounts rescale.",
        "in_review",
        ("content",),
    ),
    _click(
        "Dark Mode Toggle Click",
        "toggle_dark_mode",
        "settings",
        "User switches the app between light and dark appearance.",
        "draft",
        (),
    ),
    _click(
        "Invite Friend Click",
        "invite_friend",
        "referral",
        "User sends their referral invite link to a friend.",
        "ready_for_dev",
        ("growth",),
    ),
    # -- purchases ---------------------------------------------------------
    _purchase(
        "Checkout Started",
        "User reaches the payment sheet with a plan selected; the start of the purchase funnel.",
        "live",
        ("core", "checkout"),
        12,
    ),
    _purchase(
        "Trial Started",
        "User starts the 7-day free trial of Premium. No money is charged until the trial "
        "converts.",
        "live",
        ("checkout", "onboarding"),
        9,
        products=_TRIAL_PLANS,
        properties=_TRIAL_PROPERTIES,
    ),
    _purchase(
        "Purchase Completed",
        "User successfully completes an in-app purchase: the store charged the card and "
        "Premium is unlocked. Revenue.",
        "live",
        ("core", "checkout"),
        5,
        priced=True,
        meta=(("owner_team", "Payments"), ("jira", "PAY-1234")),
    ),
    _purchase(
        "Subscription Renewed",
        "A recurring subscription renewal was charged by the store at the end of the "
        "billing period.",
        "in_review",
        ("checkout",),
        3,
        priced=True,
    ),
    _purchase(
        "Trial Converted",
        "A free trial ended and turned into a paid subscription: the first charge.",
        "live",
        ("checkout",),
        3,
        priced=True,
    ),
    _purchase(
        "Coupon Redeemed",
        "User redeemed a coupon or promo code at checkout; the campaign is attached. "
        "Replaces Promo Applied.",
        "live",
        ("checkout", "growth"),
        2.5,
    ),
    _purchase(
        "Purchase Failed",
        "A purchase was attempted but the store or the bank declined the payment.",
        "live",
        ("checkout",),
        1.2,
    ),
    _purchase(
        "Promo Applied",
        "User redeemed a promotional code. Deprecated: replaced by Coupon Redeemed, which "
        "also records the campaign. Old app versions still send it.",
        "deprecated",
        ("checkout", "onboarding"),
        1,
        successor="Coupon Redeemed",
    ),
    _purchase(
        "Refund Processed",
        "A refund was issued: the user asked for their money back and the store returned "
        "the payment.",
        "in_review",
        (),
        priced=True,
    ),
    _purchase(
        "Subscription Cancelled",
        "User cancels their subscription. The cancel flow moved into the App Store and Play "
        "Store settings, so the app no longer sees it.",
        "implemented",
        ("checkout",),
    ),
    _purchase(
        "Billing Issue Detected",
        "The store reports a failed renewal (expired card, insufficient funds) and a grace "
        "period starts.",
        "in_review",
        ("checkout",),
    ),
    _purchase(
        "Gift Subscription Purchased",
        "User bought Premium as a gift for someone else.",
        "draft",
        ("checkout", "growth"),
        priced=True,
    ),
    _purchase(
        "Family Plan Upgraded",
        "User upgraded from an individual plan to the family plan.",
        "ready_for_dev",
        ("checkout", "premium"),
        priced=True,
    ),
    _purchase(
        "Plan Downgraded",
        "User switched from the annual to the monthly plan, effective at the next renewal.",
        "in_review",
        ("checkout",),
    ),
    _purchase(
        "Win-back Offer Accepted",
        "A lapsed subscriber accepted a discounted come-back offer.",
        "draft",
        ("checkout", "growth"),
    ),
    _purchase(
        "Price Increase Consent Given",
        "User agreed to the new subscription price the store asked them to confirm.",
        "ready_for_dev",
        ("checkout",),
    ),
    # -- search ------------------------------------------------------------
    _event(
        "search",
        "Search Performed",
        "User submits a recipe search, typed or spoken. The query length and the number "
        "of results are attached.",
        "live",
        ("core", "search"),
        60,
        _SEARCH_PROPERTIES,
        meta=(("owner_team", "Search"), ("jira", "SRCH-410")),
    ),
    _event(
        "search",
        "Search Suggestion Click",
        "User picks an autocomplete suggestion while typing a search.",
        "live",
        ("search",),
        22,
        _SEARCH_PROPERTIES,
    ),
    _event(
        "search",
        "Ingredient Search Performed",
        "User searches by the ingredients they have at home, such as 'chicken, rice, "
        "spinach' (what can I cook with...).",
        "live",
        ("search",),
        9,
        _SEARCH_PROPERTIES,
    ),
    _event(
        "search",
        "Search No Results",
        "A search returned zero recipes. The query is attached so the content team can "
        "fill the gap.",
        "live",
        ("search",),
        6,
        _SEARCH_PROPERTIES,
    ),
    _event(
        "search",
        "Voice Search Started",
        "User taps the microphone and starts a voice search.",
        "live",
        ("search",),
        3,
        _SEARCH_PROPERTIES,
    ),
    _event(
        "search",
        "Recent Search Click",
        "User re-runs one of their recent searches from the search screen.",
        "in_review",
        ("search",),
    ),
    _event(
        "search",
        "Search Filter Cleared",
        "User removes every filter from the search results.",
        "in_review",
        ("search",),
    ),
    _event(
        "search",
        "Barcode Scan Search",
        "User scans a product barcode in the store to find recipes that use it.",
        "draft",
        ("search",),
    ),
    _event(
        "search",
        "Saved Search Created",
        "User saves a search with its filters to get notified about new matching recipes.",
        "ready_for_dev",
        ("search",),
    ),
    _event(
        "search",
        "Search Results Sorted",
        "User changes the sort order of search results: popular, quickest, newest.",
        "in_review",
        ("search",),
    ),
    _event(
        "search",
        "Photo Search Performed",
        "User searches by taking a photo of the ingredients on the counter.",
        "draft",
        ("search", "premium"),
    ),
    # -- content -----------------------------------------------------------
    _event(
        "content",
        "Recipe Saved",
        "Server confirmed a recipe was added to one of the user's collections.",
        "live",
        ("content",),
        20,
        _RECIPE_PROPERTIES,
    ),
    _event(
        "content",
        "Step Timer Started",
        "User starts a step timer inside cooking mode ('simmer for 10 minutes').",
        "live",
        ("content",),
        9,
        _RECIPE_PROPERTIES,
    ),
    _event(
        "content",
        "Recipe Cooked",
        "User marks a recipe as cooked: finished cooking mode or tapped I made this.",
        "live",
        ("content",),
        8,
        _RECIPE_PROPERTIES,
    ),
    _event(
        "content",
        "Recipe Rated",
        "User rates a recipe from one to five stars after cooking it.",
        "live",
        ("content",),
        5,
        _RECIPE_PROPERTIES,
    ),
    _event(
        "content",
        "recipe_rated",
        "User rates a recipe with one to five stars. Added by the web team in its own "
        "snake_case naming before anyone searched the plan; the same moment as Recipe Rated.",
        "ready_for_dev",
        ("content",),
    ),
    _event(
        "content",
        "Recipe Video Completed",
        "User watched a recipe video to the end (full video view, completion rate).",
        "in_review",
        ("content",),
    ),
    _event(
        "content",
        "Collection Created",
        "User creates a new folder for saved recipes.",
        "in_review",
        ("content",),
    ),
    _event(
        "content",
        "Review Posted",
        "User writes a text review under a recipe.",
        "in_review",
        ("content",),
    ),
    _event(
        "content",
        "Photo Uploaded",
        "User uploads a photo of the dish they cooked.",
        "ready_for_dev",
        ("content",),
    ),
    _event(
        "content",
        "Recipe Printed",
        "Server rendered a printable PDF of a recipe.",
        "ready_for_dev",
        ("content",),
    ),
    _event(
        "content",
        "Ingredient Substituted",
        "User swaps an ingredient for a suggested alternative (butter for olive oil).",
        "draft",
        ("content",),
    ),
    _event(
        "content",
        "Recipe Scaled",
        "User changed the number of servings and the ingredient list was rescaled.",
        "in_review",
        ("content",),
    ),
    _event(
        "content",
        "Recipe Imported",
        "A recipe from a website was imported into the user's collection.",
        "ready_for_dev",
        ("content",),
    ),
    _event(
        "content",
        "Cooking Mode Exited Early",
        "User left cooking mode before the last step.",
        "draft",
        ("content",),
    ),
    _event(
        "content",
        "Recipe Reported",
        "User reported a recipe for wrong ingredients, a broken step or inappropriate content.",
        "in_review",
        ("content", "support"),
    ),
    _event(
        "content",
        "Cooking Tip Viewed",
        "User expanded a chef's tip inside a recipe step.",
        "draft",
        ("content",),
    ),
    # -- meal plan ---------------------------------------------------------
    _event(
        "meal_plan",
        "Shopping List Item Checked",
        "User ticks off an ingredient on the shopping list while in the store.",
        "live",
        ("meal_plan",),
        25,
        _PLANNER_PROPERTIES,
    ),
    _event(
        "meal_plan",
        "Meal Planned",
        "A recipe was placed on a day of the weekly meal plan.",
        "live",
        ("meal_plan", "core"),
        15,
        _PLANNER_PROPERTIES,
    ),
    _event(
        "meal_plan",
        "Meal Plan Generated",
        "The Premium auto-planner generated a whole week of dinners from the user's preferences.",
        "live",
        ("meal_plan", "premium"),
        4,
        _PLANNER_PROPERTIES,
    ),
    _event(
        "meal_plan",
        "Shopping List Shared",
        "User shares the shopping list with a household member.",
        "in_review",
        ("meal_plan", "growth"),
    ),
    _event(
        "meal_plan",
        "Meal Plan Cleared",
        "User empties the whole week of the meal plan.",
        "in_review",
        ("meal_plan",),
    ),
    _event(
        "meal_plan",
        "Shopping List Exported",
        "User exports the shopping list to another app or to the clipboard.",
        "draft",
        ("meal_plan",),
    ),
    _event(
        "meal_plan",
        "Grocery Delivery Started",
        "User sends the shopping list to a grocery delivery partner to order online.",
        "ready_for_dev",
        ("meal_plan", "growth"),
    ),
    _event(
        "meal_plan",
        "Leftovers Logged",
        "User records leftovers so the planner can schedule them for lunch.",
        "draft",
        ("meal_plan",),
    ),
    _event(
        "meal_plan",
        "Meal Swapped",
        "User swaps a planned meal for another suggestion.",
        "in_review",
        ("meal_plan",),
    ),
    _event(
        "meal_plan",
        "Pantry Item Added",
        "User adds an ingredient they have at home to the pantry.",
        "ready_for_dev",
        ("meal_plan",),
    ),
    # -- notifications -----------------------------------------------------
    _event(
        "notification",
        "Push Received",
        "A push notification was delivered to the device.",
        "live",
        ("notifications",),
        40,
        _MESSAGE_PROPERTIES,
        platforms=_MOBILE_ONLY,
    ),
    _event(
        "notification",
        "In-App Message Shown",
        "An in-app message or banner was shown to the user.",
        "live",
        ("notifications",),
        10,
        _MESSAGE_PROPERTIES,
    ),
    _event(
        "notification",
        "Push Opened",
        "User opened the app by tapping a push notification.",
        "live",
        ("notifications", "growth"),
        8,
        _MESSAGE_PROPERTIES,
        platforms=_MOBILE_ONLY,
    ),
    _event(
        "notification",
        "In-App Message Dismissed",
        "User closed an in-app message without acting on it.",
        "live",
        ("notifications",),
        6,
        _MESSAGE_PROPERTIES,
    ),
    _event(
        "notification",
        "Push Permission Granted",
        "User allowed notifications in the system permission prompt.",
        "live",
        ("notifications", "onboarding"),
        3,
        platforms=_MOBILE_ONLY,
    ),
    _event(
        "notification",
        "Push Permission Denied",
        "User declined notifications in the system permission prompt.",
        "live",
        ("notifications", "onboarding"),
        2,
        platforms=_MOBILE_ONLY,
    ),
    _event(
        "notification",
        "Email Opened",
        "User opened a reminder or marketing e-mail (tracking pixel).",
        "in_review",
        ("notifications",),
    ),
    _event(
        "notification",
        "Email Unsubscribed",
        "User unsubscribed from marketing e-mails.",
        "in_review",
        ("notifications",),
    ),
    _event(
        "notification",
        "Meal Reminder Sent",
        "The server sent the 5 pm 'What's for dinner?' reminder.",
        "in_review",
        ("notifications",),
    ),
    _event(
        "notification",
        "Trial Ending Reminder Sent",
        "The server reminded a trial user two days before the first charge.",
        "ready_for_dev",
        ("notifications", "checkout"),
    ),
    _event(
        "notification",
        "Weekly Digest Sent",
        "The Sunday e-mail with next week's recipe ideas was sent.",
        "draft",
        ("notifications",),
    ),
    _event(
        "notification",
        "SMS Opt-in Completed",
        "User agreed to receive text-message reminders.",
        "draft",
        ("notifications",),
    ),
    # -- account -----------------------------------------------------------
    _event(
        "account",
        "Login",
        "User signs in with e-mail, Apple or Google.",
        "live",
        ("account",),
        14,
        _SIGN_IN_PROPERTIES,
    ),
    _event(
        "account",
        "Sign Up Started",
        "User opens the create-account form to register (sign-up funnel, step one).",
        "live",
        ("account", "onboarding"),
        10,
        _SIGN_IN_PROPERTIES,
    ),
    _event(
        "account",
        "Sign Up Completed",
        "A new account was created (registration finished).",
        "live",
        ("core", "account", "onboarding"),
        7,
        _SIGN_IN_PROPERTIES,
        meta=(("owner_team", "Growth"), ("jira", "GRW-77")),
    ),
    _event(
        "account",
        "Email Verified",
        "User confirmed their e-mail address from the verification link.",
        "live",
        ("account",),
        5,
    ),
    _event(
        "account",
        "Logout",
        "User signs out of their account (log out) on this device.",
        "in_review",
        ("account",),
    ),
    _event(
        "account",
        "Password Reset Requested",
        "User asked for a password reset e-mail (forgot password).",
        "in_review",
        ("account", "support"),
    ),
    _event(
        "account",
        "Account Deleted",
        "User deleted their account and all their data (right to erasure).",
        "ready_for_dev",
        ("account", "privacy"),
    ),
    _event(
        "account",
        "Profile Updated",
        "User changed their name, photo or dietary preferences.",
        "in_review",
        ("account",),
    ),
    _event(
        "account",
        "Social Login Linked",
        "User linked an Apple or Google account to an existing e-mail account.",
        "draft",
        ("account",),
    ),
    _event(
        "account",
        "Two-Factor Enabled",
        "User turned on two-factor authentication.",
        "ready_for_dev",
        ("account", "privacy"),
    ),
    _event(
        "account",
        "Data Export Requested",
        "User asked for a copy of all their data (data portability request).",
        "in_review",
        ("account", "privacy"),
    ),
    _event(
        "account",
        "Marketing Consent Changed",
        "User changed whether they agree to marketing communication.",
        "draft",
        ("account", "privacy"),
    ),
    # -- app lifecycle -----------------------------------------------------
    _event(
        "app_lifecycle",
        "App Opened",
        "The app was launched or brought back to the foreground. The start of a session.",
        "live",
        ("core",),
        160,
        _LAUNCH_PROPERTIES,
        meta=(("owner_team", "Platform"),),
    ),
    _event(
        "app_lifecycle",
        "App Backgrounded",
        "The app went to the background (user switched apps or locked the phone).",
        "live",
        (),
        120,
    ),
    _event(
        "app_lifecycle",
        "App Installed",
        "First launch after the app was installed; the install source is attached.",
        "live",
        ("growth",),
        10,
        platforms=_MOBILE_ONLY,
    ),
    _event(
        "app_lifecycle",
        "App Updated",
        "First launch after the app updated to a new version. Spikes while a release rolls out.",
        "live",
        ("release",),
        4,
        platforms=_MOBILE_ONLY,
    ),
    _event(
        "app_lifecycle",
        "Deep Link Opened",
        "The app was opened from a link (a shared recipe, an e-mail, an ad).",
        "live",
        ("growth",),
        6,
        _LAUNCH_PROPERTIES,
    ),
    _event(
        "app_lifecycle",
        "Widget Tapped",
        "User opened the app from the home-screen 'Tonight's dinner' widget.",
        "in_review",
        (),
    ),
    _event(
        "app_lifecycle",
        "App Rated In Store",
        "User left a rating in the App Store or Google Play after the in-app prompt.",
        "draft",
        ("growth",),
    ),
    _event(
        "app_lifecycle",
        "Offline Mode Entered",
        "The app lost connectivity and switched to offline mode.",
        "in_review",
        ("reliability",),
    ),
    _event(
        "app_lifecycle",
        "Low Storage Warning",
        "The device is low on storage and offline recipes may be removed.",
        "draft",
        ("reliability",),
    ),
    # -- errors ------------------------------------------------------------
    _event(
        "error",
        "API Error",
        "A request to the backend failed with a server error or a timeout; the endpoint "
        "and the status are attached.",
        "live",
        ("reliability",),
        18,
        _ERROR_PROPERTIES,
        meta=(("owner_team", "Platform"), ("jira", "PLAT-902")),
    ),
    _event(
        "error",
        "Image Load Failed",
        "A recipe photo failed to load (CDN error, timeout).",
        "live",
        ("reliability",),
        5,
        _ERROR_PROPERTIES,
    ),
    _event(
        "error",
        "App Crash",
        "The app crashed; reported on the next launch.",
        "live",
        ("reliability",),
        1,
    ),
    _event(
        "error",
        "Payment Error",
        "The store returned an error during checkout (not a decline: a technical failure).",
        "in_review",
        ("checkout", "reliability"),
    ),
    _event(
        "error",
        "Sync Failed",
        "Offline changes to saved recipes or the shopping list failed to sync.",
        "in_review",
        ("reliability",),
    ),
    _event(
        "error",
        "Video Playback Error",
        "A recipe video failed to play: it stalled, would not load or showed a player error.",
        "draft",
        ("reliability",),
    ),
    _event(
        "error",
        "Search Timeout",
        "The search backend did not answer in time and the app showed a retry button.",
        "draft",
        ("search", "reliability"),
    ),
    _event(
        "error",
        "Login Failed",
        "A sign-in attempt failed (wrong password, expired token).",
        "ready_for_dev",
        ("account", "reliability"),
    ),
    # -- experiments -------------------------------------------------------
    _event(
        "experiment",
        "Experiment Exposure",
        "User was assigned to an A/B test variant and saw it. The experiment and the "
        "variant are attached.",
        "live",
        ("experiment",),
        20,
        _EXPERIMENT_PROPERTIES,
    ),
    _event(
        "experiment",
        "Feature Flag Evaluated",
        "The app evaluated a remote feature flag for the user.",
        "in_review",
        ("experiment",),
    ),
    _event(
        "experiment",
        "Holdout Group Assigned",
        "User was placed in the global holdout that sees no experiments.",
        "draft",
        ("experiment",),
    ),
    _event(
        "experiment",
        "Experiment Conversion Recorded",
        "A user in an A/B test reached the test's goal, such as starting a trial, recorded "
        "with the experiment and the variant so the lift report can compare them.",
        "in_review",
        ("experiment", "monetization"),
    ),
    _event(
        "experiment",
        "Paywall Variant Shown",
        "Which paywall layout the pricing test showed: the current screen or the new plan "
        "comparison layout.",
        "ready_for_dev",
        ("experiment", "paywall"),
    ),
    _event(
        "experiment",
        "Remote Config Fetched",
        "The app downloaded its remote configuration and feature flag values at launch.",
        "draft",
        ("experiment", "reliability"),
    ),
    # -- planned additions across the app ------------------------------------
    _event(
        "app_lifecycle",
        "App Uninstalled",
        "The app was removed from the device, as the store's uninstall report says. Feeds "
        "churn and retention analysis.",
        "draft",
        ("retention",),
    ),
    _event(
        "app_lifecycle",
        "Cold Start Measured",
        "Time from tapping the app icon to the first usable screen: launch speed and app "
        "performance.",
        "ready_for_dev",
        ("performance", "reliability"),
    ),
    _event(
        "error",
        "Checkout Validation Error",
        "The checkout form rejected the payment details: an expired card, a wrong postcode "
        "or a missing cardholder name.",
        "in_review",
        ("checkout", "reliability"),
    ),
    _event(
        "search",
        "Search Abandoned",
        "User typed a query and left without opening any result, a sign that search did not "
        "find what they wanted.",
        "draft",
        ("search", "discovery"),
    ),
    _purchase(
        "Subscription Paused",
        "Subscriber paused Premium for one to three months instead of cancelling. Billing "
        "stops and resumes on its own at the end of the pause.",
        "ready_for_dev",
        ("subscription", "retention"),
    ),
    _event(
        "notification",
        "Notification Settings Changed",
        "User turned push, e-mail or meal reminder notifications on or off in settings.",
        "in_review",
        ("notifications", "settings"),
    ),
    _event(
        "content",
        "Recipe Note Added",
        "User wrote a private note on a recipe: a tweak, a substitution or a reminder for "
        "next time.",
        "draft",
        ("content",),
    ),
)

# ---------------------------------------------------------------------------
# Events that fire but are not in the plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnplannedEventDef:
    """A name the warehouse emits that no plan event documents.

    They fire in short bursts on the calendar (:func:`.calendar.unplanned_bursts`),
    so a replay over the history surfaces them as shadow-event candidates.
    """

    event_type: str
    name: str
    rate_during_burst: float
    platforms: tuple[tuple[str, float], ...] = field(default=DEFAULT_PLATFORM_MIX)


UNPLANNED_EVENTS: tuple[UnplannedEventDef, ...] = (
    # A debug heartbeat a build left switched on.
    UnplannedEventDef("app_lifecycle", "app_heartbeat_v1", 40, _MOBILE_ONLY),
    # An old deep-link handler some marketing e-mails still hit.
    UnplannedEventDef("app_lifecycle", "legacy_deeplink_open", 15),
)

# ---------------------------------------------------------------------------
# Variables the plan documents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VariableDef:
    name: str
    description: str
    allowed_values: tuple[str, ...]


VARIABLES: tuple[VariableDef, ...] = (
    VariableDef("platform", "The platform the event came from.", PLATFORMS),
    VariableDef(
        "product_id",
        "The store product of a purchase. The warehouse also sends prod_weekly, which is "
        "not documented here.",
        DOCUMENTED_PRODUCTS,
    ),
    VariableDef("user_id", "Pseudonymous user identifier.", ()),
    VariableDef("session_id", "Identifier of one app session.", ()),
)

# ---------------------------------------------------------------------------
# Names other parts of the product depend on
# ---------------------------------------------------------------------------

#: Kept from the earlier demo: the coached chapters, the docs and the e2e specs
#: name these, so renaming one is a change to all of them.
KEPT_EVENT_NAMES: tuple[str, ...] = (
    "Home Screen View",
    "Paywall View",
    "Trial Started",
    "Buy Button Click",
    "Purchase Completed",
    "Subscription Cancelled",
    "Promo Applied",
    "Legacy CTA Click",
    "Refund Processed",
)
KEPT_EVENT_TYPES: tuple[str, ...] = ("screen_view", "click", "purchase")
#: The shadow-event candidate the reconcile chapter points at.
SHADOW_CANDIDATE_NAME = "app_heartbeat_v1"
#: Two plan events that describe the same moment (duplicate detection).
DUPLICATE_PAIRS: tuple[tuple[str, str], ...] = (("Recipe Rated", "recipe_rated"),)
#: The event whose ``amount`` / ``currency`` carry the schema-drift story.
SCHEMA_DRIFT_EVENT = "Purchase Completed"
#: The event whose ``product_id`` carries the variable-value drift (prod_weekly).
VARIABLE_DRIFT_EVENT = "Trial Started"
#: The dead event: planned, implemented, never in the warehouse window.
DEAD_EVENT = "Subscription Cancelled"

# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

_EVENTS_BY_NAME: dict[str, EventDef] = {event.name: event for event in EVENTS}
_TYPES_BY_NAME: dict[str, EventTypeDef] = {et.name: et for et in EVENT_TYPES}


def event_types() -> tuple[EventTypeDef, ...]:
    return EVENT_TYPES


def event_type(name: str) -> EventTypeDef:
    return _TYPES_BY_NAME[name]


def events() -> tuple[EventDef, ...]:
    """Every plan event, in plan order."""
    return EVENTS


def event(name: str) -> EventDef:
    return _EVENTS_BY_NAME[name]


def live_events() -> tuple[EventDef, ...]:
    """The plan events that fire (``base_rate > 0``)."""
    return tuple(event for event in EVENTS if event.base_rate > 0)


def events_of_type(event_type_name: str) -> tuple[EventDef, ...]:
    return tuple(event for event in EVENTS if event.event_type == event_type_name)


def variables() -> tuple[VariableDef, ...]:
    return VARIABLES


def unplanned_events() -> tuple[UnplannedEventDef, ...]:
    return UNPLANNED_EVENTS
