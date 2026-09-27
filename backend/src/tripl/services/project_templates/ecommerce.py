"""E-commerce starter plan: browse, cart, checkout and order events."""

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
_CURRENCY = TemplateField(
    "currency", "Currency", FT.string, description="ISO 4217 currency code of the amounts."
)
_PAYMENT_METHOD = TemplateField(
    "payment_method",
    "Payment method",
    FT.enum,
    enum_options=("card", "paypal", "apple_pay", "google_pay", "bank_transfer"),
    description="How the customer paid.",
)

TEMPLATE = ProjectTemplate(
    id="ecommerce",
    version=1,
    name="E-commerce",
    description=(
        "Online store funnel: product discovery, cart, checkout and orders, including refunds."
    ),
    branch_name="template/ecommerce",
    event_types=(
        TemplateEventType(
            "page_view",
            "Page view",
            "A storefront page or app screen was shown.",
            "#6366f1",
            (
                _PLATFORM,
                TemplateField(
                    "page_type",
                    "Page type",
                    FT.enum,
                    enum_options=("home", "category", "search", "product", "cart", "other"),
                    description="Kind of page that was shown.",
                ),
                TemplateField("page_path", "Page path", FT.string, description="URL path."),
            ),
        ),
        TemplateEventType(
            "product",
            "Product",
            "Interactions with a single product.",
            "#0ea5e9",
            (
                _PLATFORM,
                TemplateField(
                    "product_id",
                    "Product ID",
                    FT.string,
                    is_required=True,
                    description="Catalog identifier (SKU) of the product.",
                ),
                TemplateField("product_category", "Product category", FT.string),
                TemplateField("price", "Price", FT.number, description="Unit price."),
                _CURRENCY,
                TemplateField("quantity", "Quantity", FT.number),
            ),
        ),
        TemplateEventType(
            "cart",
            "Cart",
            "The shopping cart as a whole.",
            "#f59e0b",
            (
                _PLATFORM,
                TemplateField("cart_value", "Cart value", FT.number),
                TemplateField("item_count", "Item count", FT.number),
                _CURRENCY,
            ),
        ),
        TemplateEventType(
            "checkout",
            "Checkout",
            "Steps of the checkout flow before the order is placed.",
            "#8b5cf6",
            (
                _PLATFORM,
                TemplateField(
                    "checkout_step",
                    "Checkout step",
                    FT.enum,
                    is_required=True,
                    enum_options=("shipping", "payment", "review"),
                ),
                TemplateField("cart_value", "Cart value", FT.number),
                _CURRENCY,
                _PAYMENT_METHOD,
            ),
        ),
        TemplateEventType(
            "order",
            "Order",
            "A placed order and what happens to it afterwards.",
            "#10b981",
            (
                _PLATFORM,
                TemplateField(
                    "order_id",
                    "Order ID",
                    FT.string,
                    is_required=True,
                    description="Unique identifier of the order.",
                ),
                TemplateField("revenue", "Revenue", FT.number, description="Order total."),
                TemplateField("currency", "Currency", FT.string, is_required=True),
                _PAYMENT_METHOD,
                TemplateField(
                    "refund_reason",
                    "Refund reason",
                    FT.enum,
                    enum_options=("damaged", "wrong_item", "not_as_described", "other"),
                ),
            ),
        ),
    ),
    variables=(
        TemplateVariable("product_id", VT.string, "Catalog identifier (SKU) of a product."),
        TemplateVariable(
            "currency", VT.string, "ISO 4217 currency code.", allowed_values=("USD", "EUR", "GBP")
        ),
        TemplateVariable("order_id", VT.string, "Unique identifier of an order."),
        TemplateVariable(
            "platform", VT.string, "Client platform.", allowed_values=("web", "ios", "android")
        ),
    ),
    events=(
        TemplateEvent(
            "page_view",
            "page_viewed",
            "Page viewed",
            "Any storefront page or app screen is shown.",
            ("navigation",),
            (("platform", "${platform}"),),
        ),
        TemplateEvent(
            "page_view",
            "product_list_viewed",
            "Product list viewed",
            "A category or search results page with a list of products is shown.",
            ("navigation", "funnel"),
            (("platform", "${platform}"), ("page_type", "category")),
        ),
        TemplateEvent(
            "product",
            "product_viewed",
            "Product viewed",
            "The product detail page is shown.",
            ("funnel",),
            (
                ("platform", "${platform}"),
                ("product_id", "${product_id}"),
                ("currency", "${currency}"),
            ),
        ),
        TemplateEvent(
            "product",
            "product_added_to_cart",
            "Product added to cart",
            "The customer adds a product to the cart.",
            ("funnel", "cart"),
            (
                ("platform", "${platform}"),
                ("product_id", "${product_id}"),
                ("currency", "${currency}"),
            ),
        ),
        TemplateEvent(
            "cart",
            "cart_viewed",
            "Cart viewed",
            "The cart page or drawer is opened.",
            ("funnel", "cart"),
            (("platform", "${platform}"), ("currency", "${currency}")),
        ),
        TemplateEvent(
            "checkout",
            "checkout_started",
            "Checkout started",
            "The customer starts checkout from the cart.",
            ("funnel", "checkout"),
            (
                ("platform", "${platform}"),
                ("checkout_step", "shipping"),
                ("currency", "${currency}"),
            ),
        ),
        TemplateEvent(
            "checkout",
            "payment_info_entered",
            "Payment info entered",
            "The customer submits payment details.",
            ("funnel", "checkout"),
            (
                ("platform", "${platform}"),
                ("checkout_step", "payment"),
                ("currency", "${currency}"),
            ),
        ),
        TemplateEvent(
            "order",
            "order_completed",
            "Order completed",
            "The order is placed and payment is confirmed.",
            ("funnel", "revenue"),
            (
                ("platform", "${platform}"),
                ("order_id", "${order_id}"),
                ("currency", "${currency}"),
            ),
        ),
        TemplateEvent(
            "order",
            "order_refunded",
            "Order refunded",
            "An order is fully or partially refunded.",
            ("revenue",),
            (
                ("platform", "${platform}"),
                ("order_id", "${order_id}"),
                ("currency", "${currency}"),
            ),
        ),
    ),
    metric_suggestions=(
        TemplateMetricSuggestion(
            "checkout_conversion",
            "Checkout conversion",
            "Share of started checkouts that end in a completed order.",
            "event_composition",
            composition="ratio",
            numerator_event="order_completed",
            denominator_event="checkout_started",
        ),
        TemplateMetricSuggestion(
            "add_to_cart_rate",
            "Add-to-cart rate",
            "Products added to the cart per product view.",
            "event_composition",
            composition="ratio",
            numerator_event="product_added_to_cart",
            denominator_event="product_viewed",
        ),
        TemplateMetricSuggestion(
            "orders",
            "Orders",
            "Number of completed orders.",
            "event_composition",
            composition="single",
            numerator_event="order_completed",
        ),
        TemplateMetricSuggestion(
            "gross_revenue",
            "Gross revenue",
            "Sum of order revenue, read from the orders table of a data source.",
            "fact",
            needs="data_source",
        ),
        TemplateMetricSuggestion(
            "refund_rate",
            "Refund rate",
            "Refunded orders per completed order.",
            "event_composition",
            composition="ratio",
            numerator_event="order_refunded",
            denominator_event="order_completed",
        ),
    ),
    alert_suggestions=(
        TemplateAlertSuggestion(
            "core_funnel_volume_drop",
            "Volume drops on core funnel events (product views, checkouts, orders).",
            ("events", "metrics"),
        ),
        TemplateAlertSuggestion(
            "checkout_schema_drift",
            "Schema drift on checkout and order events (new, missing or retyped fields).",
            ("schema_drifts",),
        ),
        TemplateAlertSuggestion(
            "source_freshness",
            "The event table stops receiving new rows.",
            ("source_freshness",),
        ),
    ),
)
