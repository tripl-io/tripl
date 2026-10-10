from __future__ import annotations

from tripl.alert_templates import (
    ALERT_MESSAGE_FORMAT_PLAIN,
    AlertTemplateContext,
    DriftLineFacts,
    alert_direction_arrow,
    alert_direction_label,
    alert_scope_label,
    build_drift_line,
    escape_alert_value,
    format_metric_alert_value,
    format_percent_delta,
    get_default_items_template,
    get_default_message_template,
    has_baseline,
    normalize_message_template,
    render_alert_template,
)
from tripl.alerting_matching import SCOPE_METRIC
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.distribution_drift import DistributionDrift, mover_float
from tripl.models.domain_enums import MetricScopeType
from tripl.models.project import Project
from tripl.schemas.alerting import SimulatedRuleFiring

SCOPE_SCHEMA_DRIFT = MetricScopeType.schema.value
SCOPE_RELEASE_REGRESSION = MetricScopeType.release_regression.value

# The parenthetical that rides on ``${expected_count}`` for the one scope whose
# expectation is not a plain baseline. Byte-identical to
# ``worker.tasks.alerts_messages._ADOPTION_ADJUSTED_LABEL``, and pinned to it by
# ``tests/test_rule_replay_sources.py``, which renders ONE release regression through
# both renderers and asserts the two whole items are equal.
#
# It is spelled twice only because the leaf both renderers already share —
# ``alert_templates``, where ``NO_BASELINE_LABEL`` lives for exactly this reason
# — was being changed separately. Hoisting it there, beside a shared
# ``expected_basis(scope_type, expected_count)``, is the follow-up; until then
# the equality test is what stops the copy drifting.
_ADOPTION_ADJUSTED_LABEL = " (adoption-adjusted)"


def _drift_facts(firing: SimulatedRuleFiring) -> DriftLineFacts:
    """The simulated firing's half of the ``${drift_line}`` contract.

    Twin of ``worker.tasks.alerts_messages._drift_facts``, which fills the same
    fields off a delivered ``AlertDeliveryItem``. Keeping these two adapters the
    only production constructors is what stops the preview's wording and the
    send's from splitting again.

    ``window_from`` rides along like every other fact. It used to be the one
    field a firing could not supply — the replay loaded only stored anomaly
    rows, which record a bucket and no window — but a ``ReleaseRegression`` IS a
    stored row and it records both ends, and now the replay
    loads it. THREE hops carry it from that row to here and all three are
    load-bearing: ``_load_release_regression_candidates``'s
    ``DriftAlertCandidate(window_from=...)``, ``simulate_rule``'s
    ``SimulatedRuleFiring(window_from=...)``, and this line. With all three the
    rollout-overlap clause renders in the preview exactly as it does in the
    send; break any one and it drops out of the PREVIEW alone, silently,
    because ``alert_templates._format_window_span`` is written to return None
    rather than to fail. Every other family still leaves it None, which is what
    that drop-out branch is for — and what an item delivered before the column
    existed still relies on.
    """
    return DriftLineFacts(
        scope_type=firing.scope_type,
        drift_type=firing.drift_type,
        drift_field=firing.drift_field,
        sample_value=firing.sample_value,
        expected_count=firing.expected_count,
        percent_delta=firing.percent_delta,
        bucket=firing.bucket,
        window_from=firing.window_from,
        event_id=firing.event_id,
        event_type_id=firing.event_type_id,
    )


def render_firing_item(
    firing: SimulatedRuleFiring,
    *,
    message_format: str,
    items_template: str,
    metric_unit: str | None = None,
) -> str:
    """Render a single simulated firing through the rule's items_template.

    Mirrors the worker's _build_item_template_context — sparkline/top_movers
    are left empty because preview is sync-friendly and shouldn't burn extra
    queries per firing. ``metric_unit`` is the firing metric's display unit
    (metric scope only) so percent metrics render the same numbers as the
    live send path.

    The two strings whose wording is not derivable from a shared formatter —
    ``${scope_label}`` and ``${drift_line}`` — are built by the shared
    ``alert_templates`` helpers rather than restated here. Both used to be a
    second copy and both had drifted from the send: a schema drift previewed as
    "drift: type_changed: amount — e.g. 9.99" and then arrived as
    "drift: type_changed amount sample=9.99".

    ``${expected_basis}`` is the third such string and was the last one still
    diverging — see the comment on it below.
    """
    scope_label = alert_scope_label(firing.scope_type)
    drift_line = build_drift_line(_drift_facts(firing))

    variables = {
        "scope_name": escape_alert_value(firing.scope_name, message_format),
        "scope_type": escape_alert_value(firing.scope_type, message_format),
        "scope_label": escape_alert_value(scope_label, message_format),
        "direction": escape_alert_value(firing.direction, message_format),
        "direction_label": escape_alert_value(
            alert_direction_label(firing.scope_type, firing.direction), message_format
        ),
        # Percent-unit catalog metrics render stored fractions ×100 with a "%"
        # suffix; otherwise the raw float flows to the shared stringifier
        # (integral values lose the decimal point, fractional ones keep one).
        # percent_delta stays a relative change.
        "actual_count": escape_alert_value(
            format_metric_alert_value(firing.actual_count, metric_unit), message_format
        ),
        "expected_count": escape_alert_value(
            format_metric_alert_value(firing.expected_count, metric_unit), message_format
        ),
        # Must be defined even where it is empty: render_alert_template leaves
        # an unknown ${var} in the output verbatim, so omitting it here would
        # print the literal "${expected_basis}" in every preview.
        #
        # It is no longer hard-coded to "". It was, behind a comment asserting
        # that the simulator "replays stored anomalies, which are never release
        # regressions (those are recomputed per scan, not stored as anomaly
        # rows)" — and ``ReleaseRegression`` is a table
        # (``models/release_regression.py``), recomputed in full per scan but
        # very much stored. The claim only looked true because the replay never
        # LOADED those rows; now that it does the empty string
        # would have been the family's last preview/send divergence, previewing
        # "expected=715.7" where the delivery says
        # "expected=715.7 (adoption-adjusted)" about the same firing.
        #
        # Same condition as the send's
        # (``alerts_messages._build_item_template_context``), and through the
        # same ``has_baseline`` so a signed expectation is QUALIFIED rather than
        # denied — the distinction drawn for every other reader of
        # "was there a baseline".
        "expected_basis": escape_alert_value(
            _ADOPTION_ADJUSTED_LABEL
            if firing.scope_type == SCOPE_RELEASE_REGRESSION and has_baseline(firing.expected_count)
            else "",
            message_format,
        ),
        "absolute_delta": escape_alert_value(
            format_metric_alert_value(firing.absolute_delta, metric_unit), message_format
        ),
        "percent_delta": escape_alert_value(f"{firing.percent_delta:.1f}", message_format),
        "percent_delta_label": escape_alert_value(
            format_percent_delta(firing.percent_delta, firing.expected_count), message_format
        ),
        "bucket": escape_alert_value(firing.bucket, message_format),
        "details_url": "",
        "monitoring_url": "",
        "details_line": "",
        "monitoring_line": "",
        "drift_field": escape_alert_value(firing.drift_field or "", message_format),
        "drift_type": escape_alert_value(firing.drift_type or "", message_format),
        "sample_value": escape_alert_value(firing.sample_value or "", message_format),
        "drift_line": escape_alert_value(drift_line, message_format),
        "sparkline": "",
        "top_movers": "",
        "sparkline_line": "",
        "top_movers_line": "",
        # Read from the stored attribution row at send time; a simulated firing
        # has no anomaly row behind it, so the preview leaves the line out.
        "attribution": "",
        "attribution_line": "",
        "direction_arrow": alert_direction_arrow(firing.scope_type, firing.direction),
        # The simulator has no delivery, so no incident to link to. The bare
        # escaped name is what ``format_alert_link`` returns for an empty URL,
        # so the preview shows exactly what a link-less format would render.
        "scope_link": escape_alert_value(firing.scope_name, message_format),
    }
    return render_alert_template(
        items_template,
        AlertTemplateContext(variables=variables, message_format=message_format),
    ).rstrip()


def render_firings_message(
    rule: AlertRule,
    firings: list[SimulatedRuleFiring],
    *,
    destination: AlertDestination,
    project: Project,
    metric_units: dict[str, str | None] | None = None,
    org_slug: str = "",
) -> tuple[list[str], str]:
    """Render per-firing items + the overall message text for the preview.

    ``metric_units`` maps metric-definition ids (metric-scope ``scope_ref``) to
    display units so percent metrics preview with the same ×100 values the
    live worker sends.
    """
    message_format = rule.message_format or ALERT_MESSAGE_FORMAT_PLAIN
    # Resolved per firing, as the send does: a drift has a default item of its own.
    custom_items_template = normalize_message_template(rule.items_template)
    rendered_items = [
        render_firing_item(
            firing,
            message_format=message_format,
            items_template=custom_items_template
            or get_default_items_template(message_format, firing.scope_type),
            metric_unit=(
                (metric_units or {}).get(firing.scope_ref)
                if firing.scope_type == SCOPE_METRIC
                else None
            ),
        )
        for firing in firings
    ]
    items_text = "\n".join(item for item in rendered_items if item)

    message_template = normalize_message_template(
        rule.message_template
    ) or get_default_message_template(message_format)
    overall_variables = {
        "project_name": escape_alert_value(project.name, message_format),
        "project_slug": escape_alert_value(project.slug, message_format),
        "org_slug": escape_alert_value(org_slug, message_format),
        "channel": escape_alert_value(destination.type, message_format),
        "destination_name": escape_alert_value(destination.name, message_format),
        "rule_name": escape_alert_value(rule.name, message_format),
        "scan_name": escape_alert_value("(replay)", message_format),
        "matched_count": escape_alert_value(len(firings), message_format),
        "items_count": escape_alert_value(len(firings), message_format),
        "items_text": items_text,
        # The simulator renders whatever the operator typed, so every variable
        # the validator ACCEPTS has to resolve here too — otherwise a template
        # saving cleanly prints "${headline}" back at them in the preview and
        # they cannot tell a typo from an unimplemented variable.
        "headline": escape_alert_value(f"{len(firings)} alerts", message_format),
        "window_label": escape_alert_value("(the digest window)", message_format),
        "ai_explanation_block": "",
    }
    rendered_message = render_alert_template(
        message_template,
        AlertTemplateContext(variables=overall_variables, message_format=message_format),
    ).rstrip()
    return rendered_items, rendered_message


def trim_alert_text(value: str | None, *, max_length: int = 500) -> str | None:
    """Cut alert text to ``max_length`` characters, ending in ``...`` when cut.

    The one generic trimmer: the live send (``worker.tasks.metrics.signals`` and
    ``lifecycle_alerts``) and the rule replay (``alerting_service``) both call it.
    """
    if value is None or len(value) <= max_length:
        return value
    return value[: max_length - 3] + "..."


def format_distribution_drift_sample(drift: DistributionDrift) -> str:
    """``psi=0.420; ios 10.0%->30.0%, ...``: a distribution drift's sample text.

    One function for both paths, the live send (``signals``) and the
    "Test rule" replay (``alerting_service``), so a preview cannot show a sample
    the real alert will not carry.
    """
    parts = [f"psi={drift.psi:.3f}"]
    mover_parts: list[str] = []
    for mover in (drift.top_movers or [])[:3]:
        value = str(mover.get("value", ""))
        baseline_share = mover_float(mover.get("baseline_share")) * 100
        current_share = mover_float(mover.get("current_share")) * 100
        mover_parts.append(f"{value} {baseline_share:.1f}%->{current_share:.1f}%")
    if mover_parts:
        parts.append(", ".join(mover_parts))
    return trim_alert_text("; ".join(parts)) or ""
