"""``tripl codegen`` — typed tracking code for Swift, Kotlin and TypeScript, from the plan.

One word, by the grammar in ``commands/__init__.py``: like ``tripl check`` it
acts on a checkout as a whole. It reads the SAME ``.tripl/check.yml`` — the
event types with a ``codegen:`` block — so one description of the team's
wrapper serves both checking calls and generating them.

The generated API is per event-type STYLE, never a function per event (see
``codegen/context.py``); the generated code calls the team's own wrapper (the
``transport``) or, without one, the shared ``TriplDestination`` seam. Output is
deterministic — sorted, no timestamps, a header naming project, branch and the
plan's content hash — so it can be committed and ``--check``ed in CI:

    tripl codegen            # write (and remove stale generated files)
    tripl codegen --check    # write nothing; exit 1 when the files on disk differ

The plan is read from ``GET …/plan/export?format=codegen_model`` — any project
member's key works — or, with ``--model FILE``, from a saved export (``tripl
export --format codegen_model``), which needs no instance at all.

Exit codes: 0 written or in sync; 1 drift under ``--check``, or the instance
failed; 2 a bad config, template or invocation.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from tripl_cli.api import plan_export
from tripl_cli.check.config import CODEGEN_LANGUAGES
from tripl_cli.codegen.files import SyncResult, sync
from tripl_cli.codegen.generate import GeneratedFile, generate
from tripl_cli.codegen.model import CodegenModel, parse_model
from tripl_cli.commands import add_json, add_timeout
from tripl_cli.commands._check_config import (
    add_check_config,
    add_project_flag,
    check_config_of,
    project_of,
)
from tripl_cli.commands._plan import MAIN, Branch, add_branch, begin, resolve_branch
from tripl_cli.config import Config
from tripl_cli.diagnostics.collect import instance_of
from tripl_cli.errors import EXIT_FAILURE, EXIT_OK, TriplConfigError
from tripl_cli.model import JsonDict, Run
from tripl_cli.render import render_header
from tripl_cli.report import codegen_document
from tripl_cli.runner import run_async

OFFLINE = "offline"


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "codegen",
        parents=[parent],
        help="generate typed tracking code (Swift, Kotlin, TypeScript) from the plan",
        description=(
            "Generate typed tracking code for every event type with a `codegen:` block in "
            ".tripl/check.yml: plan values become enums and events typed values, and the "
            "generated code calls the team's own wrapper. With --check, write nothing and "
            "exit 1 when the files on disk differ from what the plan generates."
        ),
    )
    parser.add_argument(
        "--lang",
        dest="languages",
        metavar="LANG",
        action="append",
        choices=CODEGEN_LANGUAGES,
        help="generate only this language (repeatable; default: each event type's own)",
    )
    parser.add_argument(
        "--out",
        dest="out",
        metavar="DIR",
        type=Path,
        help=(
            "write into DIR (DIR/<lang>/ when the run writes several languages) instead of "
            "the `codegen: {out: …}` directories"
        ),
    )
    add_check_config(parser)
    add_project_flag(parser)
    add_branch(parser)
    parser.add_argument(
        "--model",
        dest="model",
        metavar="FILE",
        type=Path,
        help="read a saved codegen_model export instead of asking the instance",
    )
    parser.add_argument(
        "--check",
        dest="check",
        action="store_true",
        help="write nothing; exit 1 when generated files are missing, differ or are stale",
    )
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run)


# --- shared with `tripl export` ---------------------------------------------------
def branch_document(branch: Branch) -> JsonDict | None:
    return None if branch.id is None else {"id": branch.id, "name": branch.name}


def fetch_export(
    config: Config,
    project: str,
    selector: str | None,
    export_format: str,
    *,
    timeout: float,
) -> tuple[JsonDict, Branch, Run]:
    """One export read (plus the branch listing when a branch was named)."""
    context = begin(config)

    async def body(client: httpx.AsyncClient) -> tuple[Any, Branch, int]:
        reader = context.reader(client)
        branch = await resolve_branch(reader, project, selector)
        payload = await reader.send(plan_export.export(project, export_format, branch=branch.id))
        return payload, branch, reader.requests

    payload, branch, requests = run_async(config, body, timeout=timeout)
    run = Run(
        instance=instance_of(config, context.base_url, "unknown"),
        generated_at=context.generated_at,
        duration_ms=int((time.monotonic() - context.started) * 1000),
        requests=requests,
    )
    return plan_export.document(payload), branch, run


def display_path(path: Path) -> str:
    """Relative to the working directory when under it, so output reads like `git status`."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def offline_run(config: Config, started: float) -> Run:
    return Run(
        # No instance was asked: naming the configured one would claim otherwise.
        instance=instance_of(config, OFFLINE, "unknown"),
        generated_at=datetime.now(UTC),
        duration_ms=int((time.monotonic() - started) * 1000),
        requests=0,
    )


def read_model_file(path: Path) -> JsonDict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TriplConfigError(f"--model {path} does not exist.") from None
    except (OSError, ValueError) as exc:
        raise TriplConfigError(f"--model {path} is not a readable JSON export: {exc}") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("event_types"), list):
        raise TriplConfigError(
            f"--model {path} is not a codegen_model export (`tripl export --format codegen_model`)."
        )
    return payload


# --- the command ---------------------------------------------------------------------
def run(args: argparse.Namespace, config: Config) -> int:
    started = time.monotonic()
    check_config = check_config_of(args)
    if check_config is None:
        raise TriplConfigError(
            "no check config found: create .tripl/check.yml with a `codegen:` block per "
            "event type (see `tripl codegen --help` and the CLI docs), or pass --check-config."
        )
    project = project_of(args, check_config, "codegen")
    selector: str | None = args.branch if args.branch is not None else check_config.branch
    if args.model is not None:
        if args.branch is not None:
            raise TriplConfigError(
                "--model is a saved export of one branch already; drop --branch."
            )
        payload, branch, run_facts = read_model_file(args.model), MAIN, offline_run(config, started)
        base_url, source = run_facts.instance.base_url, "--model"
    else:
        payload, branch, run_facts = fetch_export(
            config, project, selector, plan_export.FORMAT_CODEGEN_MODEL, timeout=args.timeout
        )
        base_url = run_facts.instance.base_url
        source = config.sources.get("base_url", "unknown")
    model: CodegenModel = parse_model(payload)
    if model.branch is None and branch.name is not None:
        model = replace(model, branch=branch.name)
    files = generate(check_config, model, project, languages=args.languages, out=args.out)
    result = sync(files, check=args.check, project=project)
    exit_code = EXIT_FAILURE if args.check and result.drifted else EXIT_OK

    human = sys.stderr if args.as_json else sys.stdout
    print(render_header("codegen", base_url, source), file=human)
    print(file=human)
    rows = _rows(files, result)
    for row in rows:
        print(f"  {row['status']:<9}  {row['path']}", file=human)
    print(file=human)
    print(_summary(rows, check=args.check, model=model), file=human)
    if args.as_json:
        document = codegen_document(
            run_facts,
            project=project,
            branch=branch_document(branch),
            revision=model.revision,
            check=args.check,
            files=rows,
            exit_code=exit_code,
        )
        json.dump(document, sys.stdout)
        sys.stdout.write("\n")
    return exit_code


def _rows(files: Sequence[GeneratedFile], result: SyncResult) -> list[JsonDict]:
    status: dict[Path, str] = {}
    for path in result.created:
        status[path] = "created"
    for path in result.changed:
        status[path] = "changed"
    for path in result.unchanged:
        status[path] = "unchanged"
    rows: list[JsonDict] = [
        {
            "path": display_path(item.path),
            "language": item.language,
            "event_type": item.event_type,
            "status": status.get(item.path, "unchanged"),
        }
        for item in files
    ]
    rows.extend(
        {"path": display_path(path), "language": None, "event_type": None, "status": "stale"}
        for path in result.stale
    )
    return rows


def _summary(rows: list[JsonDict], *, check: bool, model: CodegenModel) -> str:
    counts = {
        key: sum(row["status"] == key for row in rows)
        for key in ("created", "changed", "unchanged", "stale")
    }
    plan = f"revision {model.revision or 'unknown'} of {model.branch or 'main'}"
    if check:
        drift = counts["created"] + counts["changed"] + counts["stale"]
        if drift:
            return (
                f"{drift} of {len(rows)} generated files are out of date with the plan "
                f"({plan}); run `tripl codegen` and commit the result."
            )
        return f"{len(rows)} generated files are in sync with the plan ({plan})."
    return (
        f"{len(rows)} files from the plan ({plan}): {counts['created']} created, "
        f"{counts['changed']} changed, {counts['unchanged']} unchanged, "
        f"{counts['stale']} stale removed."
    )
