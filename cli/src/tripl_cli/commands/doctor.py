"""``tripl doctor`` — read-only diagnostics with a verdict and an exit code.

The command the 2026-07-28..31 incident needed: a scan config failing for days
behind a generic error, events frozen with no indication why, a stale ACCEPTED
schema drift that had silently deleted a field, and a retry backoff that looked
like a hang. All four are visible through read-only REST calls, and this is the
one invocation that surfaces them.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime

import httpx

from tripl_cli.commands import add_json, add_timeout, bounded_int
from tripl_cli.config import Config, require_base_url
from tripl_cli.diagnostics.checks import run_checks
from tripl_cli.diagnostics.collect import (
    DEFAULT_MAX_EVENT_TYPES,
    DoctorOptions,
    collect_doctor,
    instance_of,
)
from tripl_cli.model import Report, Snapshot, exit_code_for
from tripl_cli.render import render_check, render_footer, render_header
from tripl_cli.report import doctor_document
from tripl_cli.runner import run_async


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "doctor",
        parents=[parent],
        help="check a tripl instance and report what is broken",
        description=(
            "Read-only diagnostics for a running tripl instance. Exits 0 when every check "
            "passes (or only warns), 3 when a check fails, 2 on a usage or configuration "
            "error, and 1 only if the tool itself broke."
        ),
    )
    parser.add_argument(
        "--project",
        dest="project",
        metavar="SLUG",
        action="append",
        # Repeatable rather than comma-separated: a delimiter is a thing to
        # escape, and slugs are user-chosen. Also no TRIPL_PROJECT env var —
        # config.py's precedence machinery is instance-scoped and shared in
        # spirit with tripl-mcp.
        help="check only this project (repeatable). Required for a project-scoped API key.",
    )
    parser.add_argument(
        "--include-demo",
        dest="include_demo",
        action="store_true",
        help="also check demo projects, which are excluded by default",
    )
    add_json(parser)
    parser.add_argument(
        "--strict",
        dest="strict",
        action="store_true",
        help="exit 3 on warnings too (never on skipped checks)",
    )
    add_timeout(parser)
    parser.add_argument(
        "--max-event-types",
        dest="max_event_types",
        metavar="N",
        type=bounded_int("--max-event-types", 1, 10_000),
        default=DEFAULT_MAX_EVENT_TYPES,
        help=(
            "how many event types to check for schema drift, across the whole run "
            f"(default: {DEFAULT_MAX_EVENT_TYPES}); the rest are reported as unexamined"
        ),
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace, config: Config) -> int:
    # Namespace attributes are Any; the local annotations are what keep the rest
    # of this function honest under mypy strict.
    slugs: tuple[str, ...] = tuple(args.project or ())
    as_json: bool = bool(args.as_json)
    strict: bool = bool(args.strict)
    options = DoctorOptions(
        project_slugs=slugs,
        include_demo=bool(args.include_demo),
        max_event_types=int(args.max_event_types),
        timeout=float(args.timeout),
    )
    base_url = require_base_url(config)
    human = sys.stderr if as_json else sys.stdout

    print(render_header("doctor", base_url, config.sources.get("base_url", "unknown")), file=human)
    print(file=human)

    started = time.monotonic()
    generated_at = datetime.now(UTC)

    async def body(client: httpx.AsyncClient) -> Snapshot:
        return await collect_doctor(
            client, config, options, base_url=base_url, now=datetime.now(UTC)
        )

    snapshot = run_async(config, body, timeout=options.timeout)
    checks = run_checks(snapshot)
    report = Report(
        command="doctor",
        instance=instance_of(config, base_url, snapshot.api_key_scope),
        generated_at=generated_at,
        duration_ms=int((time.monotonic() - started) * 1000),
        requests=snapshot.requests,
        checks=tuple(checks),
    )
    exit_code = exit_code_for(report, strict=strict)

    for check in checks:
        print(render_check(check), file=human)
    print(file=human)
    print(render_footer(report, exit_code), file=human)

    if as_json:
        # Exactly one object on stdout, newline-terminated, and nothing else —
        # that is what makes `tripl doctor --json | jq` a promise rather than a
        # habit.
        json.dump(doctor_document(report, exit_code=exit_code), sys.stdout)
        sys.stdout.write("\n")
    return exit_code
