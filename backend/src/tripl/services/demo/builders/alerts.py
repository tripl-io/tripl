"""Alerts builder: the alerting a team sets up, with no network involved.

Authors three destinations — the demo-only local sink (``demo_sink``: renders
and records a delivery, sends nothing), a FLAKY twin of it whose first attempt
at every delivery fails with a labelled simulated error (so the failed-delivery
and Retry story is real), and a visibly-disabled Slack example carrying no
credentials — and the rules over them:

* ``Spike & drift watch (demo)`` on the local sink: every signal family, with
  one filter (experiment events are someone else's problem);
* ``Volume watch (flaky sink)`` on the flaky sink, for event and event-type
  volume signals (a rule has one destination, so the flaky sink needs a rule
  of its own; unfiltered, so whichever incident the warehouse ran last
  reaches it);
* ``Weekly release health (quiet)``: the whole app's volume falling, which a
  broken release would do, so the demo keeps a healthy monitor beside the
  firing ones. Not release regressions (the warehouse ships one with every
  release and its beta, so it would fire for days around each one) nor the
  total's spikes (most daily incidents are spikes, and a demo opens on two
  days of history, too short a baseline to absorb them); the firing rule
  takes both.

No delivery, rule state or inbox group is written here. The provisioning
pipeline's replay detects the warehouse's incidents and the worker's own
dispatch files the deliveries, exactly as a scheduled collection does.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_filter import AlertRuleFilter
from tripl.models.domain_enums import (
    AlertMessageFormat,
    AlertRuleFilterField,
    AlertRuleFilterOperator,
)
from tripl.services.demo.scenario import DemoContext

DEMO_SINK_NAME = "Local demo sink"
FLAKY_SINK_NAME = "Local demo sink (flaky)"
DISABLED_EXTERNAL_NAME = "Slack (disabled — connect a webhook to enable)"
FIRING_RULE_NAME = "Spike & drift watch (demo)"
FLAKY_RULE_NAME = "Volume watch (flaky sink)"
HEALTHY_RULE_NAME = "Weekly release health (quiet)"
#: The event type the firing rule leaves out.
FILTERED_OUT_EVENT_TYPE = "experiment"


async def build_alerts(session: AsyncSession, ctx: DemoContext) -> None:
    demo_sink = AlertDestination(
        project_id=ctx.project_id,
        type=AlertDestinationType.demo_sink.value,
        name=DEMO_SINK_NAME,
        enabled=True,
    )
    flaky_sink = AlertDestination(
        project_id=ctx.project_id,
        type=AlertDestinationType.demo_sink.value,
        name=FLAKY_SINK_NAME,
        enabled=True,
        simulate_first_attempt_failure=True,
    )
    disabled_external = AlertDestination(
        project_id=ctx.project_id,
        type=AlertDestinationType.slack.value,
        name=DISABLED_EXTERNAL_NAME,
        enabled=False,
    )
    session.add_all([demo_sink, flaky_sink, disabled_external])
    await session.flush()

    firing_rule = AlertRule(
        destination_id=demo_sink.id,
        name=FIRING_RULE_NAME,
        enabled=True,
        include_project_total=True,
        include_event_types=True,
        include_events=True,
        include_schema_drifts=True,
        include_distribution_drifts=True,
        include_variable_value_drifts=True,
        include_release_regressions=True,
        include_metrics=True,
        include_property_drifts=True,
        include_lifecycle=True,
        # "Data is late" alerts (#269): the demo warehouse is written every five
        # minutes, so this fires only when its writer stalls.
        include_source_freshness=True,
        notify_on_spike=True,
        notify_on_drop=True,
        message_format=AlertMessageFormat.plain.value,
    )
    flaky_rule = AlertRule(
        destination_id=flaky_sink.id,
        name=FLAKY_RULE_NAME,
        enabled=True,
        include_project_total=False,
        include_event_types=True,
        include_events=True,
        include_schema_drifts=False,
        include_distribution_drifts=False,
        include_variable_value_drifts=False,
        include_release_regressions=False,
        include_metrics=False,
        notify_on_spike=True,
        notify_on_drop=True,
        message_format=AlertMessageFormat.plain.value,
    )
    healthy_rule = AlertRule(
        destination_id=demo_sink.id,
        name=HEALTHY_RULE_NAME,
        enabled=True,
        include_project_total=True,
        include_event_types=False,
        include_events=False,
        include_release_regressions=False,
        notify_on_spike=False,
        notify_on_drop=True,
        message_format=AlertMessageFormat.plain.value,
    )
    session.add_all([firing_rule, flaky_rule, healthy_rule])
    await session.flush()
    ctx.rule_ids.update(
        {
            FIRING_RULE_NAME: firing_rule.id,
            FLAKY_RULE_NAME: flaky_rule.id,
            HEALTHY_RULE_NAME: healthy_rule.id,
        }
    )

    experiment_type_id = ctx.event_type_ids.get(FILTERED_OUT_EVENT_TYPE)
    if experiment_type_id is not None:
        session.add(
            AlertRuleFilter(
                rule_id=firing_rule.id,
                field=AlertRuleFilterField.event_type.value,
                operator=AlertRuleFilterOperator.not_in.value,
                values=[str(experiment_type_id)],
                position=0,
            )
        )
    await session.flush()
