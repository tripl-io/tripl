"""Variables builder: the templating layer of the plan.

Authors the project variables the roster documents (``platform``,
``product_id``, ``user_id``, ``session_id``) with their documented values, one
per-event override — the products Trial Started is documented to sell — and
``platform`` listed as required on Home Screen View, the flagship event that
templates ``${platform}``.

Nothing observed is written here. The value contexts a variable shows, and the
open value drift on Trial Started's ``product_id`` (the warehouse also sells
``prod_weekly``, which the plan does not document), come from the catalog scan
reading the warehouse.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.demo_warehouse import DOCUMENTED_PRODUCTS, VARIABLE_DRIFT_EVENT, roster
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.services.demo.scenario import DemoContext

#: The event whose ``${platform}`` is listed as required.
PLATFORM_REQUIRED_EVENT = "Home Screen View"


async def build_variables(session: AsyncSession, ctx: DemoContext) -> None:
    for var_def in roster.variables():
        variable = Variable(
            project_id=ctx.project_id,
            branch_id=ctx.branch_id,
            name=var_def.name,
            source_name=var_def.name,
            variable_type="string",
            description=var_def.description,
            allowed_values=list(var_def.allowed_values),
        )
        session.add(variable)
        await session.flush()
        ctx.variable_ids[var_def.name] = variable.id

    session.add_all(
        [
            VariableEventValueOverride(
                project_id=ctx.project_id,
                branch_id=ctx.branch_id,
                variable_id=ctx.variable_ids["product_id"],
                event_id=ctx.event_ids[VARIABLE_DRIFT_EVENT],
                values=list(DOCUMENTED_PRODUCTS),
            ),
            VariableEventValueOverride(
                project_id=ctx.project_id,
                branch_id=ctx.branch_id,
                variable_id=ctx.variable_ids["platform"],
                event_id=ctx.event_ids[PLATFORM_REQUIRED_EVENT],
                values=None,
                required=True,
            ),
        ]
    )
    await session.flush()
