"""Declarative demo scenario: recipe contract, context, and orchestrator.

The demo is a *versioned recipe*: an ordered set of focused builders that each
seed one slice of a project. ``DemoContext`` carries the identity
(``project_id``/``branch_id``/``slug``), the clock (``now``), the determinism
``seed``, and the stable semantic references (name -> id maps) that later builders
read instead of threading ORM objects between modules.

The orchestrator never commits — the caller (``demo_service.create_demo_project``)
owns the transaction boundary, so a mid-seed failure rolls the whole seed back.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

# Bumped to "2" for tripl-2su6.2: the scenario expanded (meta values, event-type
# relation/owner, authored variable override, event change history, figma spec,
# and a feature-branch journey), so demos seeded under recipe "1" are semantically
# older. Reset re-runs the current recipe, so re-seeding yields equivalent
# semantics with no duplicate rows (every id is minted fresh per run).
# Bumped to "4" for tripl-odrj.3: the feature branch now carries a REAL pending
# change (the main plan is deep-copied onto it and one event description is
# edited, so the branch diff / merge preview are non-empty) and one open
# variable-value drift is seeded on product_id (Trial Started).
#
# NOT bumped for tripl-wkwv.14 (events in the seeded audit trail), and that is a
# decision rather than an oversight: this string is what
# ``search_embeddings._parse_fixture`` matches the precomputed vector bundle's
# manifest against, while the bundle's filename is a SEPARATE constant
# (``search_embeddings_v4.npz``). Bumping without regenerating the bundle makes
# every demo fall back to lexical search — a headline demo feature lost to a
# version stamp, silently. Nothing else consumes the value: the banner displays
# it and does not compare it. So bump when the recipe gains something a reader
# can navigate to that an older demo lacks AND the fixture is regenerated with
# it; tripl-wkwv.14 adds rows to surfaces a recipe-4 demo already has, and
# indexes nothing new. Nor for F18's seeded planned promo, for the same reason.
DEMO_RECIPE_VERSION = "4"

# Default scenario seed. Fixed so the seeded metrics/anomalies/drift are
# reproducible for a given clock; overridable per DemoContext for tests.
DEMO_SEED = 20240711


@dataclass
class DemoContext:
    """Mutable seeding context shared across builders.

    Identity + clock + seed are set up front by ``demo_service``. The ``*_ids``
    maps and warehouse series are populated by earlier builders and consumed by
    later ones, keyed by *stable semantic labels* (never random uuids) so the
    scenario stays deterministic and each builder is independently testable.
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

    data_source_id: uuid.UUID | None = None
    scan_config_id: uuid.UUID | None = None
    fact_table_id: uuid.UUID | None = None

    # Warehouse series shared with the monitoring builder (so the real detector /
    # PSI run over exactly the stored counts).
    home_series: dict[datetime, int] = field(default_factory=dict)
    type_bucket_counts: dict[tuple[uuid.UUID, datetime], int] = field(default_factory=dict)
    # The bucket the warehouse builder injected the demo spike into: the NEWEST
    # stored hour, which is one hour before ``now`` (the hourly grid stops before
    # the still-open hour, so no series has a row at ``now``). Published so a
    # later builder can point at the spike without re-deriving where it landed —
    # ``None`` until the warehouse builder has run.
    spike_bucket: datetime | None = None
    # The weekly promo's past sends, ``(bucket, actual, usual)``, for the alerts
    # builder to record as anomalies people marked expected (#271).
    weekly_promo_points: list[tuple[datetime, int, int]] = field(default_factory=list)


Builder = Callable[[AsyncSession, DemoContext], Awaitable[None]]


@dataclass(frozen=True)
class DemoScenario:
    """A versioned demo recipe: an ordered tuple of builders."""

    recipe_version: str
    builders: tuple[Builder, ...]


def build_default_scenario() -> DemoScenario:
    """Assemble the current demo recipe.

    Builders are imported lazily so this module has no import-time dependency on
    the builder package (which imports ``DemoContext`` from here).
    """
    from tripl.services.demo.builders import (
        activity,
        alerts,
        audit,
        branches,
        catalog,
        governance,
        monitoring,
        plan,
        search,
        variables,
        warehouse,
    )

    return DemoScenario(
        recipe_version=DEMO_RECIPE_VERSION,
        builders=(
            # 1. Plan schema: event types, fields, meta fields, events, tags,
            #    field/meta values, one relation, one owner.
            plan.build_plan,
            # 2. Variables + observed values + one authored per-event override
            #    + one open variable-value drift outside that override's list.
            variables.build_variables,
            # 3. Activity: event change history + a figma spec (no stored bytes).
            activity.build_activity,
            # 4. Warehouse: DataSource, ScanConfig, EventMetric + breakdown series.
            warehouse.build_warehouse,
            # 5. Monitoring: anomaly settings + detector-derived anomalies, real
            #    PSI drift, schema drift.
            monitoring.build_monitoring,
            # 6. Governance: scan-job history, coverage, shadow/dead-event
            #    reconciliation — coherent with the seeded warehouse volume.
            governance.build_governance,
            # 7. Metrics catalog: fact table + four metric definitions + values.
            catalog.build_catalog,
            # 8. Alerting: a demo-only local ``demo_sink`` destination + a firing
            #    rule (over the seeded anomalies incl. the catalog metric) and a
            #    healthy rule, states, one locally-recorded delivery + items,
            #    inbox correlation group, and a spike annotation. Runs after
            #    catalog so it can reference the seeded metrics/anomalies. Emits
            #    zero network side effects.
            alerts.build_alerts,
            # 9. Feature-branch journey: revision + branch + comment, plus a
            #    deep copy of the main plan onto the branch with exactly one
            #    branch-side edit (a real pending change for diff/merge preview).
            branches.build_branches,
            # 10. Audit trail for everything the recipe authored, attributed to
            #     the demo's creator. Runs after every builder that creates an
            #     object it records (tripl-jfm3.60).
            audit.build_audit,
            # 11. Search reindex (last).
            search.build_search,
        ),
    )


async def seed_demo_content(session: AsyncSession, ctx: DemoContext) -> None:
    """Run the default scenario's builders in order (does not commit).

    Every seeded row is synthetic — the DataSource never receives a real query.
    Runs inside the caller's transaction so a failure rolls back cleanly.
    """
    scenario = build_default_scenario()
    for builder in scenario.builders:
        await builder(session, ctx)
