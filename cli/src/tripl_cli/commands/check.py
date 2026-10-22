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

One run, one set of verdicts, as many reports as a pipeline wants: ``--format``
picks what goes to stdout, and each ``--report FORMAT=PATH`` writes the same
result to a file — GitLab Code Quality and JUnit for a merge request, SARIF for
code scanning, the JSON document for scripts. A failed run writes none of them
and removes what an earlier run left at those paths, so a stale report can
never pass for this run's.

Exit codes: 0 clean; 1 when any item has an error (or, with ``--strict``, a
warning — ``--strict`` also reports values only known at runtime); 2 for a bad
config, a bad payload file, a bad invocation, or a config that matches no call
at all (``--allow-empty`` accepts that); 4 when the instance was unavailable —
unreachable, timed out, 429 or 5xx — so nothing was decided. An instance that
refuses the key is exit 1, with the reason on stderr.

``--check-config`` rather than ``--config``: ``--config`` is already the global
flag naming the CLI's own connection settings file, on every command.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path

import httpx

from tripl_cli.check import payloads as payloads_mod
from tripl_cli.check import scan as scan_mod
from tripl_cli.check.config import CheckConfig, repository_root
from tripl_cli.check.model import (
    MODE_PAYLOADS,
    MODE_STATIC,
    CheckItem,
    CheckReport,
    CheckResult,
    SkippedFile,
)
from tripl_cli.check.render import render_text, sarif_document
from tripl_cli.check.reports import codequality_document, junit_document, repo_paths
from tripl_cli.check.validate import validate
from tripl_cli.commands import add_json, add_timeout
from tripl_cli.commands._check_config import (
    add_check_config,
    add_project_flag,
    check_config_of,
    project_of,
)
from tripl_cli.commands._plan import Branch, add_branch, begin, resolve_branch
from tripl_cli.config import Config
from tripl_cli.diagnostics.collect import instance_of
from tripl_cli.errors import TriplConfigError, TriplError
from tripl_cli.model import Run
from tripl_cli.render import plural, render_header
from tripl_cli.report import check_document
from tripl_cli.runner import run_gate

FORMAT_TEXT = "text"
FORMAT_JSON = "json"
FORMAT_SARIF = "sarif"
FORMAT_CODEQUALITY = "codequality"
FORMAT_JUNIT = "junit"
FORMATS = (FORMAT_TEXT, FORMAT_JSON, FORMAT_SARIF, FORMAT_CODEQUALITY, FORMAT_JUNIT)


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
            "Exits 1 on any error (or warning, with --strict), 4 when the instance was "
            "unavailable."
        ),
    )
    add_check_config(parser)
    parser.add_argument(
        "--payloads",
        dest="payloads",
        metavar="FILE",
        help="validate captured events (NDJSON, or a JSON array; - for stdin) instead of source",
    )
    add_project_flag(parser)
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
        help=(
            "output format on stdout (default: text): json, sarif (SARIF 2.1.0, for code "
            "scanning), codequality (GitLab Code Quality) or junit (JUnit XML)"
        ),
    )
    parser.add_argument(
        "--report",
        dest="reports",
        metavar="FORMAT=PATH",
        action="append",
        help=(
            "also write the result as FORMAT to the file PATH; repeat for more reports "
            "from the same run, e.g. --report codequality=gl-code-quality-report.json"
        ),
    )
    parser.add_argument(
        "--allow-empty",
        dest="allow_empty",
        action="store_true",
        help=(
            "exit 0 when there is nothing to check: no call matched the check config, "
            "or the --payloads file holds no events"
        ),
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


def _report_targets(args: argparse.Namespace) -> list[tuple[str, Path]]:
    """Each ``--report FORMAT=PATH``, checked before anything is read or sent."""
    targets: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for spec in args.reports or ():
        report_format, separator, raw = spec.partition("=")
        if not separator or not report_format or not raw:
            raise TriplConfigError(
                f"--report {spec!r}: write it as FORMAT=PATH, "
                "for example codequality=gl-code-quality-report.json."
            )
        if report_format not in FORMATS:
            raise TriplConfigError(
                f"--report {spec!r}: {report_format!r} is not a format; "
                f"use one of {', '.join(FORMATS)}."
            )
        if raw == "-":
            raise TriplConfigError(
                f"--report {spec!r}: stdout is --format's; give --report a file path."
            )
        path = Path(raw)
        if path.resolve() in seen:
            raise TriplConfigError(f"--report names {raw} twice; each report needs its own file.")
        seen.add(path.resolve())
        targets.append((report_format, path))
    return targets


def run(args: argparse.Namespace, config: Config) -> int:
    targets = _report_targets(args)
    try:
        return _check(args, config, targets)
    except TriplError:
        _discard(targets)
        raise


def _check(args: argparse.Namespace, config: Config, targets: list[tuple[str, Path]]) -> int:
    output = _format(args)
    # Connection settings first: "no URL configured" is exit 2 before any file is read.
    context = begin(config)
    check_config = check_config_of(args)
    if args.payloads is None and check_config is None:
        # Before the project question: a missing config is the actual problem.
        raise TriplConfigError(
            "no check config found: create .tripl/check.yml (see `tripl check --help` "
            "and the CLI docs), pass --check-config PATH, or validate captured events "
            "with --payloads FILE."
        )
    project = project_of(args, check_config, "check")
    selector: str | None = args.branch
    if selector is None and check_config is not None:
        selector = check_config.branch
    files = 0
    skipped: tuple[SkippedFile, ...] = ()
    items: tuple[CheckItem, ...]
    if args.payloads is not None:
        mode = MODE_PAYLOADS
        items = tuple(payloads_mod.load(args.payloads, allow_empty=args.allow_empty))
    else:
        assert check_config is not None  # raised above when missing
        mode = MODE_STATIC
        outcome = scan_mod.scan(check_config)
        items, files, skipped = outcome.items, outcome.files, outcome.skipped
        if not items and not args.allow_empty:
            # A gate that checked nothing must not read as one that passed: a
            # renamed wrapper or a moved directory would otherwise turn CI green.
            raise TriplConfigError(_nothing_matched(check_config, files))

    async def body(client: httpx.AsyncClient) -> tuple[Branch, tuple[CheckResult, ...], int]:
        reader = context.reader(client)
        branch = await resolve_branch(reader, project, selector)
        results: tuple[CheckResult, ...] = ()
        if items:
            results = await validate(
                reader, project, items, branch_id=branch.id, strict=args.strict
            )
        return branch, results, reader.requests

    branch, results, requests = run_gate(config, body, timeout=args.timeout)
    scan_root = check_config.root if check_config is not None else None
    cwd = Path.cwd()
    repo_root = repository_root(scan_root if mode == MODE_STATIC and scan_root else cwd)
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
        repo_paths=repo_paths(
            (result.item.origin.path for result in results),
            mode=mode,
            scan_root=scan_root,
            repo_root=repo_root,
            cwd=cwd,
        ),
        skipped=skipped,
    )
    # Every file before anything is printed: a report that cannot be written
    # fails the run, and must not do so after stdout already told a success.
    for report_format, path in targets:
        _write(path, _render(report_format, report, scan_root))
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
    if output != FORMAT_TEXT:
        sys.stdout.write(_render(output, report, scan_root))
    return report.exit_code


def _nothing_matched(config: CheckConfig, files: int) -> str:
    if files == 0:
        where = f"no readable source file under {config.root} matches `sources:`"
    else:
        where = f"none of the {plural(files, 'file')} scanned has a call the call specs match"
    return (
        f"{where}, so nothing was checked. Fix `sources:`, `exclude:` or the call specs "
        "(a renamed wrapper, a moved directory), or pass --allow-empty if there is no "
        "tracking code to check yet."
    )


def _render(report_format: str, report: CheckReport, root: Path | None) -> str:
    """One format of the finished run, as the text that goes to stdout or a file."""
    if report_format == FORMAT_TEXT:
        return render_text(report) + "\n"
    if report_format == FORMAT_JSON:
        return json.dumps(check_document(report)) + "\n"
    if report_format == FORMAT_SARIF:
        return json.dumps(sarif_document(report, root=root), indent=2) + "\n"
    if report_format == FORMAT_CODEQUALITY:
        return json.dumps(codequality_document(report), indent=2) + "\n"
    return junit_document(report)


def _write(path: Path, text: str) -> None:
    """Write through a temporary file, so a reader never sees half a report."""
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise TriplError(f"cannot write the report {path}: {exc}") from exc


def _discard(targets: list[tuple[str, Path]]) -> None:
    for _report_format, path in targets:
        # A directory, or no permission: nothing this run wrote is there, and
        # the error that ended the run is the one worth reporting.
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
