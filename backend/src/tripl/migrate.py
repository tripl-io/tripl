"""Bring the database schema up to date: the core's migrations, then each extension's.

``python -m tripl.migrate`` is what the ``migrate`` one-shot of ``compose.yaml``
runs. The core's step is exactly ``alembic upgrade head`` with the
``alembic.ini`` in the working directory, so a Community install does what it
always did.

An extension that owns tables migrates them itself: it declares an entry point
in the ``tripl.migrations`` group whose object is a callable taking no
arguments, and keeps its own Alembic history (a version table of its own), so
the core's single migration head is untouched. Those run after the core's, in
entry point name order, because their tables reference the core's.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path

from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)

MIGRATIONS_GROUP = "tripl.migrations"


def upgrade_core(config_path: Path = Path("alembic.ini")) -> None:
    """``alembic upgrade head`` for the core schema."""
    command.upgrade(Config(str(config_path)), "head")


def extension_upgrades() -> list[tuple[str, Callable[[], None]]]:
    """The installed extensions' migration callables, by entry point name."""
    found: list[tuple[str, Callable[[], None]]] = []
    for point in sorted(entry_points(group=MIGRATIONS_GROUP), key=lambda p: p.name):
        upgrade = point.load()
        if not callable(upgrade):
            raise TypeError(f"{point.value} in {MIGRATIONS_GROUP} is not callable")
        found.append((point.name, upgrade))
    return found


def main() -> None:
    upgrade_core()
    for name, upgrade in extension_upgrades():
        logger.info("migrate.extension name=%s", name)
        upgrade()


if __name__ == "__main__":
    main()
