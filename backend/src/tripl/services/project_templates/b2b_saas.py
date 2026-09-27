"""B2B SaaS starter plan: sign-up, workspace activation, collaboration and billing."""

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

_WORKSPACE_ID = TemplateField(
    "workspace_id",
    "Workspace ID",
    FT.string,
    is_required=True,
    description="Identifier of the customer workspace (account).",
)

TEMPLATE = ProjectTemplate(
    id="b2b_saas",
    version=1,
    name="B2B SaaS",
    description=(
        "Team software sold per workspace: sign-up, workspace activation, "
        "invitations, feature adoption, upgrades and cancellations."
    ),
    branch_name="template/b2b-saas",
    event_types=(
        TemplateEventType(
            "account",
            "Account",
            "User account creation.",
            "#6366f1",
            (
                TemplateField(
                    "signup_method",
                    "Sign-up method",
                    FT.enum,
                    is_required=True,
                    enum_options=("email", "google", "microsoft", "sso"),
                ),
                TemplateField(
                    "company_size",
                    "Company size",
                    FT.enum,
                    enum_options=("1_10", "11_50", "51_200", "201_1000", "1000_plus"),
                ),
                TemplateField("platform", "Platform", FT.string),
            ),
        ),
        TemplateEventType(
            "workspace",
            "Workspace",
            "Workspace lifecycle.",
            "#0ea5e9",
            (
                _WORKSPACE_ID,
                TemplateField("plan_id", "Plan ID", FT.string),
            ),
        ),
        TemplateEventType(
            "collaboration",
            "Collaboration",
            "Inviting teammates into a workspace.",
            "#22c55e",
            (
                _WORKSPACE_ID,
                TemplateField(
                    "invitee_role",
                    "Invitee role",
                    FT.string,
                    is_required=True,
                    description="Role the invited user receives.",
                ),
                TemplateField(
                    "invite_channel",
                    "Invite channel",
                    FT.enum,
                    enum_options=("email", "link"),
                ),
            ),
        ),
        TemplateEventType(
            "product_usage",
            "Product usage",
            "Use of product features.",
            "#8b5cf6",
            (
                _WORKSPACE_ID,
                TemplateField(
                    "feature_name",
                    "Feature name",
                    FT.string,
                    is_required=True,
                    description="Stable key of the feature that was used.",
                ),
                TemplateField("user_role", "User role", FT.string),
            ),
        ),
        TemplateEventType(
            "billing",
            "Billing",
            "Plan changes and cancellations.",
            "#10b981",
            (
                _WORKSPACE_ID,
                TemplateField("plan_id", "Plan ID", FT.string, is_required=True),
                TemplateField("previous_plan_id", "Previous plan ID", FT.string),
                TemplateField("seats", "Seats", FT.number),
                TemplateField(
                    "billing_period",
                    "Billing period",
                    FT.enum,
                    enum_options=("monthly", "annual"),
                ),
                TemplateField(
                    "cancel_reason",
                    "Cancel reason",
                    FT.enum,
                    enum_options=(
                        "too_expensive",
                        "missing_features",
                        "switched_service",
                        "company_closed",
                        "other",
                    ),
                ),
            ),
        ),
    ),
    variables=(
        TemplateVariable("workspace_id", VT.string, "Identifier of a customer workspace."),
        TemplateVariable(
            "user_role",
            VT.string,
            "Role of a user inside a workspace.",
            allowed_values=("owner", "admin", "member", "guest"),
        ),
        TemplateVariable(
            "plan_id",
            VT.string,
            "Identifier of a pricing plan.",
            allowed_values=("free", "team", "business", "enterprise"),
        ),
        TemplateVariable("feature_name", VT.string, "Stable key of a product feature."),
    ),
    events=(
        TemplateEvent(
            "account",
            "sign_up_completed",
            "Sign-up completed",
            "A new user account is created.",
            ("activation", "funnel"),
            (("signup_method", "email"),),
        ),
        TemplateEvent(
            "workspace",
            "workspace_created",
            "Workspace created",
            "A user creates a new workspace.",
            ("activation", "funnel"),
            (("workspace_id", "${workspace_id}"), ("plan_id", "free")),
        ),
        TemplateEvent(
            "collaboration",
            "user_invited",
            "User invited",
            "A workspace member invites a teammate.",
            ("collaboration",),
            (("workspace_id", "${workspace_id}"), ("invitee_role", "${user_role}")),
        ),
        TemplateEvent(
            "collaboration",
            "invite_accepted",
            "Invite accepted",
            "An invited teammate joins the workspace.",
            ("collaboration",),
            (("workspace_id", "${workspace_id}"), ("invitee_role", "${user_role}")),
        ),
        TemplateEvent(
            "product_usage",
            "feature_used",
            "Feature used",
            "A user completes the core action of a feature.",
            ("engagement",),
            (
                ("workspace_id", "${workspace_id}"),
                ("feature_name", "${feature_name}"),
                ("user_role", "${user_role}"),
            ),
        ),
        TemplateEvent(
            "billing",
            "plan_upgraded",
            "Plan upgraded",
            "The workspace moves to a higher plan or adds seats.",
            ("revenue",),
            (("workspace_id", "${workspace_id}"), ("plan_id", "${plan_id}")),
        ),
        TemplateEvent(
            "billing",
            "subscription_cancelled",
            "Subscription cancelled",
            "The workspace cancels its paid plan.",
            ("churn",),
            (("workspace_id", "${workspace_id}"), ("plan_id", "${plan_id}")),
        ),
    ),
    metric_suggestions=(
        TemplateMetricSuggestion(
            "workspace_activation_rate",
            "Workspace activation rate",
            "Workspaces created per sign-up.",
            "event_composition",
            composition="ratio",
            numerator_event="workspace_created",
            denominator_event="sign_up_completed",
        ),
        TemplateMetricSuggestion(
            "invite_acceptance_rate",
            "Invite acceptance rate",
            "Accepted invites per invite sent.",
            "event_composition",
            composition="ratio",
            numerator_event="invite_accepted",
            denominator_event="user_invited",
        ),
        TemplateMetricSuggestion(
            "feature_usage",
            "Feature usage",
            "Number of feature uses.",
            "event_composition",
            composition="single",
            numerator_event="feature_used",
        ),
        TemplateMetricSuggestion(
            "upgrade_rate",
            "Upgrade rate",
            "Upgrades per workspace created.",
            "event_composition",
            composition="ratio",
            numerator_event="plan_upgraded",
            denominator_event="workspace_created",
        ),
        TemplateMetricSuggestion(
            "expansion_mrr",
            "Expansion MRR",
            "Monthly recurring revenue added by upgrades, from a SQL query on a data source.",
            "sql",
            needs="data_source",
        ),
    ),
    alert_suggestions=(
        TemplateAlertSuggestion(
            "activation_volume_drop",
            "Volume drops on sign-ups and workspace creation.",
            ("events", "metrics"),
        ),
        TemplateAlertSuggestion(
            "billing_schema_drift",
            "Schema drift on billing events.",
            ("schema_drifts",),
        ),
        TemplateAlertSuggestion(
            "source_freshness",
            "The event table stops receiving new rows.",
            ("source_freshness",),
        ),
    ),
)
