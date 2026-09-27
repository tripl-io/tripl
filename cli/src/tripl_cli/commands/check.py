"""``tripl check`` — validate tracking calls in source code, or captured events, against the plan.

One word, by the grammar in ``commands/__init__.py``: it acts on a checkout (or
a capture file) as a whole, not on a class of objects an operator browses.

Two modes, one validator:

* **static** (default) — scan the files ``.tripl/check.yml`` names for the
  tracking calls it describes, per event type: the project's own wrapper
  functions first, SDK presets (Segment, Amplitude, Snowplow) as shorthands.
  Every call becomes an item with whatever is knowable at scan time; a value
  only known at runtime is sent as ``null``, which is never an error.
* **payloads** (``--payloads FILE``) — validate captured events, NDJSON or JSON.
  These are what the app really sent, so a missing required field IS an error.

Both batch their items to ``POST /projects/{slug}/plan/validate`` — a READ that
happens to be a POST, so any project member's key works, ``tk_r_`` included,
and there is no ``--dry-run`` because nothing is written.

Exit codes: 0 clean; 1 when any item has an error (or, with ``--strict``, a
warning — ``--strict`` also reports values only known at runtime); 2 for a bad
config, a bad payload file or a bad invocation. An instance that cannot be
reached or refuses the key is exit 1 as well, with the reason on stderr.

``--check-config`` rather than ``--config``: ``--config`` is already the global
flag naming the CLI's own connection settings file, on every command.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

from tripl_cli.check import payloads as payloads_mod
from tripl_cli.check import scan as scan_mod
from tripl_cli.check.config import CheckConfig, find_config, load
from tripl_cli.check.model import MODE_PAYLOADS, MODE_STATIC, CheckItem, CheckReport, CheckResult
from tripl_cli.check.render import render_text, sarif_document
from tripl_cli.check.validate import validate
from tripl_cli.commands import add_json, add_timeout
from tripl_cli.commands._plan import Branch, add_branch, begin, resolve_branch
from tripl_cli.config import Config
from tripl_cli.diagnostics.collect import instance_of
from tripl_cli.errors import TriplConfigError
from tripl_cli.model import Run
from tripl_cli.render import render_header
from tripl_cli.report import check_document
from tripl_cli.runner import run_async

FORMAT_TEXT = "text"
FORMAT_JSON = "json"
FORMAT_SARIF = "sarif"
FORMATS = (FORMAT_TEXT, FORMAT_JSON, FORMAT_SARIF)


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "check",
        parents=[parent],
        help="validate tracking calls in code, or captured events, against the plan",
        description=(
            "Scan source files for the tracking calls .tripl/check.yml describes and "
            "validate each against the tracking plan: unknown event types and events, "
            "unknown fields, values outside allowed values. With --payloads, validate "
            "captured events instead, where a missing required field is an error too. "
            "Exits 1 on any error (or warning, with --strict)."
        ),
    )
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
    parser.add_argument(
        "--payloads",
        dest="payloads",
        metavar="FILE",
        help="validate captured events (NDJSON, or a JSON array; - for stdin) instead of source",
    )
    parser.add_argument(
        "--project",
        dest="project",
        metavar="SLUG",
        action="append",
        help="project slug; overrides `project:` in the check config",
    )
    add_branch(parser)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 on warnings too, and report values that are only known at runtime",
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=FORMATS,
        default=None,
        help="output format (default: text); sarif is SARIF 2.1.0 for code scanning",
    )
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run)


def _format(args: argparse.Namespace) -> str:
    chosen: str | None = args.output_format
    if args.as_json:
        if chosen not in (None, FORMAT_JSON):
            raise TriplConfigError(f"--json and --format {chosen} ask for two outputs; pick one.")
        return FORMAT_JSON
    return chosen or FORMAT_TEXT


def _check_config(args: argparse.Namespace) -> CheckConfig | None:
    path: Path | None = args.check_config
    if path is not None:
        return load(path)
    found = find_config(Path.cwd())
    return load(found) if found is not None else None


def _project(args: argparse.Namespace, check_config: CheckConfig | None) -> str:
    slugs: list[str] = list(args.project or ())
    if len(slugs) > 1:
        raise TriplConfigError(
            f"--project was given {len(slugs)} times; tripl check validates one project."
        )
    if slugs:
        return slugs[0]
    if check_config is not None and check_config.project:
        return check_config.project
    raise TriplConfigError("name the project: `project:` in .tripl/check.yml, or --project <slug>.")


def run(args: argparse.Namespace, config: Config) -> int:
    output = _format(args)
    # Connection settings first: "no URL configured" is exit 2 before any file is read.
    context = begin(config)
    check_config = _check_config(args)
    project = _project(args, check_config)
    selector: str | None = args.branch
    if selector is None and check_config is not None:
        selector = check_config.branch
    files = 0
    items: tuple[CheckItem, ...]
    if args.payloads is not None:
        mode = MODE_PAYLOADS
        items = tuple(payloads_mod.load(args.payloads))
    else:
        if check_config is None:
            raise TriplConfigError(
                "no check config found: create .tripl/check.yml (see `tripl check --help` "
                "and the CLI docs), pass --check-config PATH, or validate captured events "
                "with --payloads FILE."
            )
        mode = MODE_STATIC
        outcome = scan_mod.scan(check_config)
        items, files = outcome.items, outcome.files

    async def body(client: httpx.AsyncClient) -> tuple[Branch, tuple[CheckResult, ...], int]:
        reader = context.reader(client)
        branch = await resolve_branch(reader, project, selector)
        results: tuple[CheckResult, ...] = ()
        if items:
            results = await validate(
                reader, project, items, branch_id=branch.id, strict=args.strict
            )
        return branch, results, reader.requests

    branch, results, requests = run_async(config, body, timeout=args.timeout)
    report = CheckReport(
        run=Run(
            instance=instance_of(config, context.base_url, "unknown"),
            generated_at=context.generated_at,
            duration_ms=int((time.monotonic() - context.started) * 1000),
            requests=requests,
        ),
        project=project,
        mode=mode,
        strict=args.strict,
        results=results,
        branch_id=branch.id,
        branch_name=branch.name,
        config_path=(
            str(check_config.path) if check_config is not None and check_config.path else None
        ),
        files_scanned=files,
    )
    human = sys.stdout if output == FORMAT_TEXT else sys.stderr
    source = config.sources.get("base_url", "unknown")
    print(render_header("check", context.base_url, source), file=human)
    print(file=human)
    if mode == MODE_STATIC and not items:
        print(
            "No tracking calls matched the check config; check `sources:` and the call specs.",
            file=human,
        )
    print(render_text(report), file=human)
    if output == FORMAT_JSON:
        json.dump(check_document(report), sys.stdout)
        sys.stdout.write("\n")
    elif output == FORMAT_SARIF:
        root = check_config.root if check_config is not None else None
        json.dump(sarif_document(report, root=root), sys.stdout, indent=2)
        sys.stdout.write("\n")
    return report.exit_code
