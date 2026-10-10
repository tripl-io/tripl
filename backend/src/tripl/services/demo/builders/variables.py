"""Variables builder: the templating layer.

Seeds the project variables, their *observed* value contexts (what the scan
pipeline would discover), one *authored* per-event override — a
``VariableEventValueOverride`` documenting the allowed values for a variable in
one event's context — and one OPEN ``VariableValueDrift``: a value observed
outside that documented list, so the variables drift UI (``list_value_drifts``,
the open-drift counts, the event drift badge) has a real row to show. It still
does NOT feed the firing alert rule's replay, but only because both paths
require a non-NULL ``scan_config_id`` (``None`` here): the live worker matches
``scan_config_id == config.id`` and the replay loader
``alerting_service._load_variable_value_drift_candidates`` — one of FIVE
candidate sources, not three — requires ``is_not(None)``.
All rows are reachable through the variables API.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.schema_drift import SCHEMA_DRIFT_STATUS_OPEN
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.models.variable_value import VariableValue, VariableValueKind
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.services.demo.scenario import DemoContext

# (name, source_name, variable_type, description, allowed_values)
#
# ``allowed_values`` is the DOCUMENTED value list the Variables table renders in
# its "Documented values" column and the coached "Variables & value drift"
# chapter tells the user to compare observed values against. It used to be unset
# on every demo variable, so the column read "—" and the chapter's instruction
# had nothing to point at. Only the closed-vocabulary variable
# gets one: ``user_id``/``session_id`` are unbounded identifiers, and documenting
# a list for them would be a lie the drift detector would then act on.
_VARIABLE_SPECS: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    ("user_id", "user_id", "string", "Unique identifier for the authenticated user.", ()),
    (
        "session_id",
        "session_id",
        "string",
        "Session identifier scoped to one app launch.",
        (),
    ),
    (
        "product_id",
        "product_id",
        "string",
        "Store product / SKU identifier.",
        ("prod_monthly", "prod_annual", "prod_lifetime"),
    ),
    # Twelve of the plan's events template ``${platform}`` in a field value, and
    # once, no variable answered to that token: the demo shipped
    # with ``_attach_template_warnings`` reporting an unknown variable on every
    # one of them, on every PATCH. Seeded rather than de-templated because the
    # demo exists to show templating WORKING — and the twelve values would all
    # have had to change together.
    (
        "platform",
        "platform",
        "string",
        "Client platform the event was sent from.",
        ("ios", "android", "web"),
    ),
)


# The one value observed OUTSIDE the documented override list below — the
# seeded drift row. Kept module-level so tests can assert against the contract.
DRIFT_OBSERVED_VALUES = ["prod_weekly"]
# Detected recently (well inside the 30-day read-time retention window) and
# derived from ctx.now, so two seeds with the same clock produce identical rows.
_DRIFT_DETECTED_AGO = timedelta(hours=6)


async def build_variables(session: AsyncSession, ctx: DemoContext) -> None:
    await _build_variables(session, ctx)
    await _build_observed_values(session, ctx)
    await _build_authored_override(session, ctx)
    await _build_value_drift(session, ctx)


async def _build_variables(session: AsyncSession, ctx: DemoContext) -> None:
    for name, source_name, variable_type, description, allowed_values in _VARIABLE_SPECS:
        var = Variable(
            project_id=ctx.project_id,
            branch_id=ctx.branch_id,
            name=name,
            source_name=source_name,
            variable_type=variable_type,
            description=description,
            allowed_values=list(allowed_values),
        )
        session.add(var)
        await session.flush()
        ctx.variable_ids[name] = var.id


async def _build_observed_values(session: AsyncSession, ctx: DemoContext) -> None:
    """Observed value contexts — the scan-discovered sample values per event/field."""
    observed = (
        # (variable, event, field_key, source_column, observed_count, values)
        (
            "user_id",
            "Home Screen View",
            "screen_view.screen_name",
            "user_id",
            14823,
            ["u_001", "u_002", "u_003", "u_004", "u_005"],
        ),
        (
            "product_id",
            "Purchase Completed",
            "purchase.product_id",
            "product_id",
            3241,
            ["prod_monthly", "prod_annual", "prod_lifetime"],
        ),
        (
            "session_id",
            "Paywall View",
            "screen_view.screen_name",
            "session_id",
            6102,
            ["sess_aaa", "sess_bbb", "sess_ccc"],
        ),
        (
            "platform",
            "Home Screen View",
            "screen_view.platform",
            "platform",
            14823,
            ["android", "ios", "web"],
        ),
    )
    for var_name, event_name, field_key, source_column, observed_count, values in observed:
        session.add(
            VariableValue(
                project_id=ctx.project_id,
                branch_id=ctx.branch_id,
                variable_id=ctx.variable_ids[var_name],
                event_id=ctx.event_ids[event_name],
                field_definition_id=ctx.field_ids[field_key],
                source_column=source_column,
                value_kind=VariableValueKind.low.value,
                observed_count=observed_count,
                values=values,
            )
        )
    await session.flush()


async def _build_authored_override(session: AsyncSession, ctx: DemoContext) -> None:
    """The authored property-list rows (user-owned; never scan-written).

    One documented per-event override (``product_id`` on Trial Started), and
    ``platform`` listed as required on Home Screen View with no override: the
    flagship event templates ``${platform}`` in a field value, so without the
    row its Properties card reports a property in use that is not listed.
    """
    session.add_all(
        [
            VariableEventValueOverride(
                project_id=ctx.project_id,
                branch_id=ctx.branch_id,
                variable_id=ctx.variable_ids["product_id"],
                event_id=ctx.event_ids["Trial Started"],
                values=["prod_monthly", "prod_annual"],
            ),
            VariableEventValueOverride(
                project_id=ctx.project_id,
                branch_id=ctx.branch_id,
                variable_id=ctx.variable_ids["platform"],
                event_id=ctx.event_ids["Home Screen View"],
                values=None,
                required=True,
            ),
        ]
    )
    await session.flush()


async def _build_value_drift(session: AsyncSession, ctx: DemoContext) -> None:
    """One OPEN value drift: ``prod_weekly`` seen on Trial Started's product_id.

    Mirrors what the scan upsert would write when it observes a value outside
    the documented override list above. ``scan_config_id`` stays NULL — the
    warehouse builder (and its ScanConfig) runs after this one, and the column
    is nullable by design (``SET NULL`` on scan deletion). That NULL is the ONLY
    thing keeping this row out of a firing. Every other predicate both loaders
    apply admits it — open status, ``detected_at`` six hours ago, the project
    matches, and ``product_id`` is not excluded from scans — and the demo firing
    rule carries ``include_variable_value_drifts=True`` (``builders/alerts.py``).
    The scan worker matches ``scan_config_id == config.id`` and a NULL never
    equals a config id; the replay twin
    ``alerting_service._load_variable_value_drift_candidates`` requires
    ``scan_config_id.is_not(None)`` for exactly that reason. Rule replay HAS read
    this family now — do not relax that clause on the assumption
    the replay is blind to it
    (``test_rule_replay_sources.py::test_a_value_drift_no_scan_can_reach_stays_out_of_the_replay``
    pins it).
    """
    session.add(
        VariableValueDrift(
            project_id=ctx.project_id,
            variable_id=ctx.variable_ids["product_id"],
            event_id=ctx.event_ids["Trial Started"],
            scan_config_id=None,
            observed_values=list(DRIFT_OBSERVED_VALUES),
            status=SCHEMA_DRIFT_STATUS_OPEN,
            detected_at=ctx.now - _DRIFT_DETECTED_AGO,
        )
    )
    await session.flush()
