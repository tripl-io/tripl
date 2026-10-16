"""Declarative demo scenario: recipe contract, context, and the authored builders.

The demo is a *versioned recipe*. Its governing rule: **the plan is authored;
observations are scanned.** The builders listed here author what a team writes
by hand — the tracking plan, variables, the warehouse connection, detection
settings, the metrics catalog, alerting, collaboration, branches and the audit
trail. Everything observed (volumes, anomalies, drift, regressions, shadow
events, metric values, alert deliveries) is left to tripl's own pipeline, which
:mod:`tripl.services.demo.pipeline` runs over the demo warehouse afterwards.

``DemoContext`` carries the identity (``project_id``/``branch_id``/``slug``),
the clock (``now``), the determinism ``seed`` and the stable semantic references
(name -> id maps) later builders and pipeline steps read instead of threading
ORM objects between modules. The builders never commit; the caller owns the
transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

# Bumped to "5": the demo reads a real warehouse. The plan comes from the shared
# roster (157 events instead of 18) and every observation from tripl's scan and
# metrics replay, so a recipe-4 demo is a different product: it gets the Reset
# banner. This string is also what ``search_embeddings._parse_fixture`` matches
# the precomputed vector bundle against, so the bundle must be regenerated for
# the same recipe (``scripts/generate_demo_search_embeddings.py``); until it
# is, demos fall back to lexical search.
DEMO_RECIPE_VERSION = "5"

# Default scenario seed: the few authored timestamps (the "created" stagger)
# derive from it. The warehouse does not: it runs on the global clock.
DEMO_SEED = 20240711


@dataclass
class DemoContext:
    """Mutable seeding context shared across builders and pipeline steps.

    Identity + clock + seed are set up front by ``demo_service``. The ``*_ids``
    maps are populated by earlier builders and consumed by later ones, keyed by
    *stable semantic labels* (never random uuids).
    """

    project_id: uuid.UUID
    branch_id: uuid.UUID
    slug: str
    now: datetime
    seed: int = DEMO_SEED
    # The demo creator is the only real user in the seed; ``None`` when unknown
    # (e.g. a direct builder test). Builders that reference a real user skip
    # rather than invent one when this is ``None``.
    created_by: uuid.UUID | None = None

    # Stable semantic references, populated as builders run.
    event_type_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    # Field key is "<event_type_name>.<field_name>", e.g. "screen_view.platform".
    field_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    meta_field_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    event_ids: dict[str, uuid.UUID] = field(default_factory=dict)  # event name -> id
    variable_ids: dict[str, uuid.UUID] = field(default_factory=dict)  # var name -> id
    metric_ids: dict[str, uuid.UUID] = field(default_factory=dict)  # metric name -> id
    rule_ids: dict[str, uuid.UUID] = field(default_factory=dict)  # alert rule name -> id

    data_source_id: uuid.UUID | None = None
    scan_config_id: uuid.UUID | None = None
    fact_table_id: uuid.UUID | None = None


Builder = Callable[[AsyncSession, DemoContext], Awaitable[None]]


@dataclass(frozen=True)
class DemoScenario:
    """A versioned demo recipe: the ordered authored builders.

    ``builders`` author the plan the scan then reads. ``finishing`` author what
    must see main AFTER the pipeline wrote to it: the catalog scan stamps every
    event's identity, adds the warehouse's columns as fields and creates the
    variables it finds, so a branch whose base snapshot was taken before that
    would open already behind main. A provisioning runs ``finishing`` after the
    observed stages; :func:`seed_demo_content` (the authored half alone) runs
    both back to back.
    """

    recipe_version: str
    builders: tuple[Builder, ...]
    finishing: tuple[Builder, ...] = ()


def build_default_scenario() -> DemoScenario:
    """Assemble the current recipe's authored half.

    Builders are imported lazily so this module has no import-time dependency on
    the builder package (which imports ``DemoContext`` from here).
    """
    from tripl.services.demo.builders import (
        activity,
        alerts,
        audit,
        branches,
        catalog,
        collaboration,
        monitoring,
        plan,
        variables,
        warehouse,
    )

    return DemoScenario(
        recipe_version=DEMO_RECIPE_VERSION,
        builders=(
            # 1. Plan schema from the roster: event types, fields, meta fields,
            #    every event with its tags, field/meta values and successor, one
            #    relation, the event-type owners.
            plan.build_plan,
            # 2. Variables, their documented values, one per-event override.
            variables.build_variables,
            # 3. The ``demo`` data source and its catalog scan config.
            warehouse.build_warehouse,
            # 4. Detection settings, a per-scope override, one field contract.
            monitoring.build_monitoring,
            # 5. Metrics catalog: the orders fact table and four definitions.
            catalog.build_catalog,
            # 6. Alerting: the local sink, its flaky twin, a disabled external
            #    destination, the firing rule (with filters) and a quiet rule.
            alerts.build_alerts,
            # 7. Event history, a figma spec, discussion comments.
            activity.build_activity,
            # 8. Collaboration: the dismissed near-duplicate, watches, docs.
            collaboration.build_collaboration,
        ),
        finishing=(
            # 9. Branches: one merged into main, one open and under review —
            #    snapshotted from main as the scan left it (see ``finishing``).
            branches.build_branches,
            # 10. Audit trail for everything above, attributed to the creator.
            #     Last: it records only objects that already exist.
            audit.build_audit,
        ),
    )


async def seed_demo_content(session: AsyncSession, ctx: DemoContext) -> None:
    """Run the recipe's whole authored half in order, with nothing observed between (no commit)."""
    await seed_authored_plan(session, ctx)
    await finish_authored_content(session, ctx)


async def seed_authored_plan(session: AsyncSession, ctx: DemoContext) -> None:
    """The builders the scan reads after: everything but ``finishing`` (no commit)."""
    for builder in build_default_scenario().builders:
        await builder(session, ctx)


async def finish_authored_content(session: AsyncSession, ctx: DemoContext) -> None:
    """The ``finishing`` builders, once main holds what the pipeline wrote (no commit)."""
    for builder in build_default_scenario().finishing:
        await builder(session, ctx)
