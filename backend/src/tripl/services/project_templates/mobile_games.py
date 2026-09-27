"""Mobile games starter plan: sessions, onboarding, levels, purchases and ads."""

from __future__ import annotations

from tripl.models.domain_enums import FieldDefinitionType as FT
from tripl.models.variable import VariableType as VT
from tripl.services.project_templates.model import (
    ProjectTemplate,
    TemplateAlertSuggestion,
    TemplateEvent,
    TemplateEventType,
    TemplateField,
    TemplateMetricSuggestion,
    TemplateVariable,
)

_PLATFORM = TemplateField(
    "platform",
    "Platform",
    FT.string,
    is_required=True,
    description="Device platform the event was sent from.",
)
_APP_VERSION = TemplateField(
    "app_version", "App version", FT.string, description="Build version of the game client."
)

TEMPLATE = ProjectTemplate(
    id="mobile_games",
    version=1,
    name="Mobile games",
    description=(
        "Free-to-play mobile game: sessions, tutorial, level progression, "
        "in-app purchases and ad monetization."
    ),
    branch_name="template/mobile-games",
    event_types=(
        TemplateEventType(
            "session",
            "Session",
            "App launches and play sessions.",
            "#6366f1",
            (
                _PLATFORM,
                _APP_VERSION,
                TemplateField("session_number", "Session number", FT.number),
            ),
        ),
        TemplateEventType(
            "onboarding",
            "Onboarding",
            "First-time user experience.",
            "#0ea5e9",
            (
                _PLATFORM,
                _APP_VERSION,
                TemplateField("duration_seconds", "Duration (s)", FT.number),
            ),
        ),
        TemplateEventType(
            "level",
            "Level",
            "Level progression: starts, wins and losses.",
            "#22c55e",
            (
                _PLATFORM,
                TemplateField(
                    "level_id",
                    "Level ID",
                    FT.string,
                    is_required=True,
                    description="Identifier of the level.",
                ),
                TemplateField("attempt", "Attempt", FT.number),
                TemplateField("duration_seconds", "Duration (s)", FT.number),
                TemplateField(
                    "failure_reason",
                    "Failure reason",
                    FT.enum,
                    enum_options=("out_of_moves", "out_of_time", "quit", "defeated"),
                ),
            ),
        ),
        TemplateEventType(
            "monetization",
            "Monetization",
            "In-app purchases.",
            "#f59e0b",
            (
                _PLATFORM,
                TemplateField(
                    "product_sku",
                    "Product SKU",
                    FT.string,
                    is_required=True,
                    description="Store identifier of the purchased item.",
                ),
                TemplateField("price", "Price", FT.number),
                TemplateField("currency", "Currency", FT.string, is_required=True),
                TemplateField(
                    "store",
                    "Store",
                    FT.enum,
                    enum_options=("app_store", "google_play"),
                ),
            ),
        ),
        TemplateEventType(
            "ads",
            "Ads",
            "Advertising shown inside the game.",
            "#ec4899",
            (
                _PLATFORM,
                TemplateField(
                    "ad_format",
                    "Ad format",
                    FT.enum,
                    is_required=True,
                    enum_options=("rewarded", "interstitial", "banner"),
                ),
                TemplateField("ad_network", "Ad network", FT.string),
                TemplateField(
                    "placement",
                    "Placement",
                    FT.enum,
                    enum_options=("level_end", "continue_offer", "main_menu", "shop"),
                ),
            ),
        ),
    ),
    variables=(
        TemplateVariable(
            "platform", VT.string, "Device platform.", allowed_values=("ios", "android")
        ),
        TemplateVariable("app_version", VT.string, "Build version of the game client."),
        TemplateVariable("level_id", VT.string, "Identifier of a level."),
        TemplateVariable("product_sku", VT.string, "Store identifier of an in-app product."),
        TemplateVariable(
            "currency", VT.string, "ISO 4217 currency code.", allowed_values=("USD", "EUR", "GBP")
        ),
    ),
    events=(
        TemplateEvent(
            "session",
            "session_started",
            "Session started",
            "The game is opened or brought to the foreground after a timeout.",
            ("engagement",),
            (("platform", "${platform}"), ("app_version", "${app_version}")),
        ),
        TemplateEvent(
            "onboarding",
            "tutorial_started",
            "Tutorial started",
            "The player starts the tutorial.",
            ("onboarding", "funnel"),
            (("platform", "${platform}"), ("app_version", "${app_version}")),
        ),
        TemplateEvent(
            "onboarding",
            "tutorial_completed",
            "Tutorial completed",
            "The player finishes the tutorial.",
            ("onboarding", "funnel"),
            (("platform", "${platform}"), ("app_version", "${app_version}")),
        ),
        TemplateEvent(
            "level",
            "level_started",
            "Level started",
            "The player starts or restarts a level.",
            ("progression",),
            (("platform", "${platform}"), ("level_id", "${level_id}")),
        ),
        TemplateEvent(
            "level",
            "level_completed",
            "Level completed",
            "The player wins a level.",
            ("progression",),
            (("platform", "${platform}"), ("level_id", "${level_id}")),
        ),
        TemplateEvent(
            "level",
            "level_failed",
            "Level failed",
            "The player loses or quits a level.",
            ("progression",),
            (("platform", "${platform}"), ("level_id", "${level_id}")),
        ),
        TemplateEvent(
            "monetization",
            "in_app_purchase_completed",
            "In-app purchase completed",
            "A store purchase is confirmed.",
            ("revenue",),
            (
                ("platform", "${platform}"),
                ("product_sku", "${product_sku}"),
                ("currency", "${currency}"),
            ),
        ),
        TemplateEvent(
            "ads",
            "ad_impression",
            "Ad impression",
            "An ad is displayed to the player.",
            ("revenue", "ads"),
            (("platform", "${platform}"), ("ad_format", "rewarded")),
        ),
    ),
    metric_suggestions=(
        TemplateMetricSuggestion(
            "tutorial_completion_rate",
            "Tutorial completion rate",
            "Share of started tutorials that are completed.",
            "event_composition",
            composition="ratio",
            numerator_event="tutorial_completed",
            denominator_event="tutorial_started",
        ),
        TemplateMetricSuggestion(
            "level_win_rate",
            "Level win rate",
            "Levels completed per level started.",
            "event_composition",
            composition="ratio",
            numerator_event="level_completed",
            denominator_event="level_started",
        ),
        TemplateMetricSuggestion(
            "purchases",
            "Purchases",
            "Number of completed in-app purchases.",
            "event_composition",
            composition="single",
            numerator_event="in_app_purchase_completed",
        ),
        TemplateMetricSuggestion(
            "iap_revenue",
            "In-app purchase revenue",
            "Sum of purchase prices, read from the purchases table of a data source.",
            "fact",
            needs="data_source",
        ),
        TemplateMetricSuggestion(
            "ad_impressions",
            "Ad impressions",
            "Number of ads shown.",
            "event_composition",
            composition="single",
            numerator_event="ad_impression",
        ),
    ),
    alert_suggestions=(
        TemplateAlertSuggestion(
            "core_loop_volume_drop",
            "Volume drops on sessions and level starts.",
            ("events", "metrics"),
        ),
        TemplateAlertSuggestion(
            "monetization_volume_drop",
            "Volume drops on purchases and ad impressions.",
            ("events",),
        ),
        TemplateAlertSuggestion(
            "level_schema_drift",
            "Schema drift on level events after a client release.",
            ("schema_drifts",),
        ),
    ),
)
