"""Versioned, declarative demo scenario package.

Public surface consumed by ``demo_service`` (and, transitively, the projects
API): the recipe version, the seeding context, and the orchestrator. The builder
modules under ``demo.builders`` seed one focused slice each.
"""

from tripl.services.demo.scenario import (
    DEMO_RECIPE_VERSION,
    DEMO_SEED,
    DemoContext,
    DemoScenario,
    build_default_scenario,
    finish_authored_content,
    seed_authored_plan,
    seed_demo_content,
)

__all__ = [
    "DEMO_RECIPE_VERSION",
    "DEMO_SEED",
    "DemoContext",
    "DemoScenario",
    "build_default_scenario",
    "finish_authored_content",
    "seed_authored_plan",
    "seed_demo_content",
]
