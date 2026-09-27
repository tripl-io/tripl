"""Subscriptions starter plan: sign-up, paywall, trial, renewals and churn."""

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
    description="Client platform the event was sent from.",
)
_PLAN_ID = TemplateField(
    "plan_id",
    "Plan ID",
    FT.string,
    is_required=True,
    description="Identifier of the subscription plan.",
)

TEMPLATE = ProjectTemplate(
    id="subscriptions",
    version=1,
    name="Subscriptions",
    description=(
        "Subscription product lifecycle: sign-up, paywall, free trial, paid "
        "subscription, renewals, cancellations and failed payments."
    ),
    branch_name="template/subscriptions",
    event_types=(
        TemplateEventType(
            "account",
            "Account",
            "Account creation and identity.",
            "#6366f1",
            (
                _PLATFORM,
                TemplateField(
                    "signup_method",
                    "Sign-up method",
                    FT.enum,
                    is_required=True,
                    enum_options=("email", "google", "apple", "facebook"),
                ),
                TemplateField("referral_source", "Referral source", FT.string),
            ),
        ),
        TemplateEventType(
            "paywall",
            "Paywall",
            "Screens that offer a paid plan.",
            "#f59e0b",
            (
                _PLATFORM,
                TemplateField(
                    "paywall_id",
                    "Paywall ID",
                    FT.string,
                    is_required=True,
                    description="Which paywall variant was shown.",
                ),
                TemplateField(
                    "placement",
                    "Placement",
                    FT.enum,
                    enum_options=("onboarding", "feature_gate", "settings", "promo"),
                ),
                TemplateField("plan_count", "Plans offered", FT.number),
            ),
        ),
        TemplateEventType(
            "trial",
            "Trial",
            "Free trial lifecycle.",
            "#0ea5e9",
            (
                _PLATFORM,
                _PLAN_ID,
                TemplateField("trial_length_days", "Trial length (days)", FT.number),
            ),
        ),
        TemplateEventType(
            "subscription",
            "Subscription",
            "Paid subscription lifecycle.",
            "#10b981",
            (
                _PLATFORM,
                _PLAN_ID,
                TemplateField(
                    "billing_period",
                    "Billing period",
                    FT.enum,
                    is_required=True,
                    enum_options=("monthly", "annual"),
                ),
                TemplateField("price", "Price", FT.number),
                TemplateField("currency", "Currency", FT.string),
                TemplateField(
                    "cancel_reason",
                    "Cancel reason",
                    FT.enum,
                    enum_options=(
                        "too_expensive",
                        "not_using",
                        "missing_features",
                        "switched_service",
                        "other",
                    ),
                ),
            ),
        ),
        TemplateEventType(
            "billing",
            "Billing",
            "Payment processing outcomes.",
            "#ef4444",
            (
                _PLATFORM,
                _PLAN_ID,
                TemplateField(
                    "failure_reason",
                    "Failure reason",
                    FT.enum,
                    enum_options=("card_declined", "insufficient_funds", "expired_card", "other"),
                ),
                TemplateField("retry_count", "Retry count", FT.number),
            ),
        ),
    ),
    variables=(
        TemplateVariable(
            "platform", VT.string, "Client platform.", allowed_values=("web", "ios", "android")
        ),
        TemplateVariable(
            "plan_id",
            VT.string,
            "Identifier of a subscription plan.",
            allowed_values=("basic", "standard", "premium"),
        ),
        TemplateVariable(
            "billing_period",
            VT.string,
            "How often the subscription is billed.",
            allowed_values=("monthly", "annual"),
        ),
        TemplateVariable(
            "currency", VT.string, "ISO 4217 currency code.", allowed_values=("USD", "EUR", "GBP")
        ),
    ),
    events=(
        TemplateEvent(
            "account",
            "sign_up_completed",
            "Sign-up completed",
            "A new account is created.",
            ("activation",),
            (("platform", "${platform}"), ("signup_method", "email")),
        ),
        TemplateEvent(
            "paywall",
            "paywall_viewed",
            "Paywall viewed",
            "A paywall offering the paid plans is shown.",
            ("monetization", "funnel"),
            (("platform", "${platform}"), ("paywall_id", "default")),
        ),
        TemplateEvent(
            "trial",
            "trial_started",
            "Trial started",
            "The user starts a free trial of a paid plan.",
            ("monetization", "funnel"),
            (("platform", "${platform}"), ("plan_id", "${plan_id}")),
        ),
        TemplateEvent(
            "subscription",
            "subscription_started",
            "Subscription started",
            "The first paid period begins, directly or after a trial.",
            ("monetization", "revenue"),
            (
                ("platform", "${platform}"),
                ("plan_id", "${plan_id}"),
                ("billing_period", "${billing_period}"),
                ("currency", "${currency}"),
            ),
        ),
        TemplateEvent(
            "subscription",
            "subscription_renewed",
            "Subscription renewed",
            "A subscription is charged for another period.",
            ("revenue",),
            (
                ("platform", "${platform}"),
                ("plan_id", "${plan_id}"),
                ("billing_period", "${billing_period}"),
                ("currency", "${currency}"),
            ),
        ),
        TemplateEvent(
            "subscription",
            "subscription_cancelled",
            "Subscription cancelled",
            "The user turns off auto-renewal or cancels immediately.",
            ("churn",),
            (
                ("platform", "${platform}"),
                ("plan_id", "${plan_id}"),
                ("billing_period", "${billing_period}"),
            ),
        ),
        TemplateEvent(
            "billing",
            "payment_failed",
            "Payment failed",
            "A charge for the subscription is declined.",
            ("churn", "revenue"),
            (("platform", "${platform}"), ("plan_id", "${plan_id}")),
        ),
    ),
    metric_suggestions=(
        TemplateMetricSuggestion(
            "trial_to_paid_conversion",
            "Trial-to-paid conversion",
            "Share of trials that turn into a paid subscription.",
            "event_composition",
            composition="ratio",
            numerator_event="subscription_started",
            denominator_event="trial_started",
        ),
        TemplateMetricSuggestion(
            "paywall_conversion",
            "Paywall conversion",
            "Trials started per paywall view.",
            "event_composition",
            composition="ratio",
            numerator_event="trial_started",
            denominator_event="paywall_viewed",
        ),
        TemplateMetricSuggestion(
            "cancellations",
            "Cancellations",
            "Number of cancelled subscriptions.",
            "event_composition",
            composition="single",
            numerator_event="subscription_cancelled",
        ),
        TemplateMetricSuggestion(
            "renewal_revenue",
            "Renewal revenue",
            "Sum of renewal charges, read from the billing table of a data source.",
            "fact",
            needs="data_source",
        ),
        TemplateMetricSuggestion(
            "payment_failure_rate",
            "Payment failure rate",
            "Failed payments per renewal.",
            "event_composition",
            composition="ratio",
            numerator_event="payment_failed",
            denominator_event="subscription_renewed",
        ),
    ),
    alert_suggestions=(
        TemplateAlertSuggestion(
            "subscription_volume_drop",
            "Volume drops on trial and subscription starts.",
            ("events", "metrics"),
        ),
        TemplateAlertSuggestion(
            "payment_failure_spike",
            "Spike in failed payments.",
            ("events",),
        ),
        TemplateAlertSuggestion(
            "billing_schema_drift",
            "Schema drift on subscription and billing events.",
            ("schema_drifts",),
        ),
    ),
)
