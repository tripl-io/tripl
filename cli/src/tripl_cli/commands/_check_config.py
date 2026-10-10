"""The ``.tripl/check.yml`` flags and lookup, shared by ``check``, ``codegen`` and ``export``.

All three read the same file and take the same ``--check-config`` and
``--project`` flags, so the flag help, the discovery rule and the "which
project" rule are written here once rather than once per command.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tripl_cli.check.config import CheckConfig, find_config, load
from tripl_cli.errors import TriplConfigError


def add_check_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--check-config",
        dest="check_config",
        metavar="PATH",
        type=Path,
        help=(
            "the check config (default: the nearest .tripl/check.yml, .yaml or .json "
            "at or above the current directory, up to the repository root)"
        ),
    )


def add_project_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project",
        dest="project",
        metavar="SLUG",
        action="append",
        help="project slug; overrides `project:` in the check config",
    )


def check_config_of(args: argparse.Namespace) -> CheckConfig | None:
    """``--check-config`` when given, else the nearest config file, else ``None``."""
    path: Path | None = args.check_config
    if path is not None:
        return load(path)
    found = find_config(Path.cwd())
    return load(found) if found is not None else None


def project_of(args: argparse.Namespace, check_config: CheckConfig | None, command: str) -> str:
    """One ``--project``, else ``project:`` from the check config. ``command`` names the verb."""
    slugs: list[str] = list(args.project or ())
    if len(slugs) > 1:
        raise TriplConfigError(
            f"--project was given {len(slugs)} times; tripl {command} reads one project."
        )
    if slugs:
        return slugs[0]
    if check_config is not None and check_config.project:
        return check_config.project
    raise TriplConfigError("name the project: `project:` in .tripl/check.yml, or --project <slug>.")
