"""``tripl scans`` — list configs, read job history, run one, cancel a job.

Grammar: a command acting on the instance as a whole is one word (``doctor``,
``status``, ``watch``); a command acting on a class of objects is
``<plural-noun> <verb>``. Flat spellings (``list-scans``) were rejected: five
more top-level entries, and no answer for the sixth.

``tripl scans replay`` is deliberately ABSENT. The route exists
(``POST /projects/{slug}/scans/{scan_id}/metrics/replay``) but carries
``deps.get_owner_user``, which rejects every request whose
``request.state.api_key_scope`` is set — and that is every valid API key, read or
write. A Bearer-token client cannot reach it, so the command would 403 every
time. ``scans cancel`` ships in its place: editor-gated, the natural pair to
``run``, and what the 2026-07-28..31 incident actually needed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime

import httpx

from tripl_cli.api import scans as scans_api
from tripl_cli.api.request import ApiRequest
from tripl_cli.commands import (
    add_json,
    add_project,
    add_timeout,
    bounded_int,
    group_help,
    require_single_project,
)
from tripl_cli.commands._resolve import resolve_one
from tripl_cli.commands._write import add_write_flags, confirm, request_document
from tripl_cli.config import Config, require_base_url
from tripl_cli.diagnostics.collect import (
    Reader,
    instance_of,
    raise_selection_failure,
    read_or_raise,
)
from tripl_cli.diagnostics.collect import select_projects as select
from tripl_cli.errors import EXIT_FAILURE, EXIT_OK
from tripl_cli.model import (
    JsonDict,
    MutationOutcome,
    ProjectScans,
    Run,
    ScanJobsSnapshot,
    ScansSnapshot,
    SectionError,
    as_dict,
    as_list,
    text_of,
)
from tripl_cli.render import (
    render_header,
    render_mutation,
    render_scan_configs,
    render_scan_jobs,
)
from tripl_cli.report import mutation_document, scan_jobs_document, scans_document
from tripl_cli.runner import gather_bounded, run_async

# The API's own default. Deliberately not doctor's 200: `scans jobs` answers
# "what has this config been doing lately", which a screenful covers, and the
# maximum is one flag away.
DEFAULT_JOBS_LIMIT = 50


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "scans",
        parents=[parent],
        help="list, inspect, run and cancel warehouse scans",
        description=(
            "Operate on a project's warehouse scan configurations. Reads need a tk_r_ key; "
            "`run` and `cancel` need a tk_w_ key backed by an editor or owner. A bounded "
            "metrics replay is not available here - that route is owner-session-only and "
            "cannot be reached with an API key at all."
        ),
    )
    verbs = parser.add_subparsers(dest="scans_command", metavar="<verb>")
    _register_list(verbs, parent)
    _register_jobs(verbs, parent)
    _register_run(verbs, parent)
    _register_cancel(verbs, parent)
    # Bare `tripl scans` prints this group's help and exits 2, the same rule
    # `tripl` with no subcommand follows: a script that invoked a group with no
    # verb has a bug, and exiting 0 would let it look successful.
    parser.set_defaults(handler=group_help(parser))


def _register_list(
    verbs: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = verbs.add_parser(
        "list",
        parents=[parent],
        help="list every project's scan configurations",
        description=(
            "List scan configurations with their schedule and whether the dispatcher would "
            "ever select them. base_query and the tuning knobs are omitted - read those in "
            "the tripl UI."
        ),
    )
    add_project(parser, single=False)
    parser.add_argument(
        "--include-demo",
        dest="include_demo",
        action="store_true",
        help="also list demo projects, which are excluded by default",
    )
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run_list)


def _register_jobs(
    verbs: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = verbs.add_parser(
        "jobs",
        parents=[parent],
        help="print one scan config's recent job history",
        description=(
            "Print a scan config's recent jobs, newest first - the command that gives you a "
            "job id to hand to `tripl scans cancel`."
        ),
    )
    parser.add_argument("scan", metavar="<scan>", help="scan config name or id (exact match)")
    add_project(parser, single=True)
    parser.add_argument(
        "--limit",
        dest="limit",
        metavar="N",
        type=bounded_int("--limit", 1, scans_api.JOBS_LIMIT_MAX),
        default=DEFAULT_JOBS_LIMIT,
        help=(
            f"how many jobs to ask for, 1..{scans_api.JOBS_LIMIT_MAX} "
            f"(default: {DEFAULT_JOBS_LIMIT})"
        ),
    )
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run_jobs)


def _register_run(
    verbs: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = verbs.add_parser(
        "run",
        parents=[parent],
        help="trigger a scan run now (needs a tk_w_ key)",
        description=(
            "Trigger a run of a stored scan configuration. Needs a tk_w_ key backed by an "
            "editor or owner. Does NOT prompt: it executes SQL an owner already authored on "
            "a schedule that already runs it. Exits 1 if the returned job is already failed - "
            "the API answers 201 even when the broker was unreachable."
        ),
    )
    parser.add_argument("scan", metavar="<scan>", help="scan config name or id (exact match)")
    add_project(parser, single=True)
    add_write_flags(parser, prompts=False)
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run_run)


def _register_cancel(
    verbs: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = verbs.add_parser(
        "cancel",
        parents=[parent],
        help="cancel an active scan job (needs a tk_w_ key)",
        description=(
            "Cancel a pending or running scan job. Needs a tk_w_ key backed by an editor or "
            "owner. Prompts, because the job stops mid-window; --yes skips the prompt "
            "and is REQUIRED when stdin is not a terminal. A job that already finished "
            "answers 409."
        ),
    )
    parser.add_argument("scan", metavar="<scan>", help="scan config name or id (exact match)")
    parser.add_argument("job_id", metavar="<job-id>", help="the job to cancel")
    add_project(parser, single=True)
    add_write_flags(parser, prompts=True)
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run_cancel)


async def _resolve_scan(reader: Reader, slug: str, selector: str) -> tuple[str, str]:
    """``<scan>`` -> (id, name). Never guesses, never picks one of two.

    ``commands._resolve.resolve_one`` runs ``api.scans.resolve_selectors``,
    which is watch's own matcher, so ``scans run 'prod events'`` and
    ``watch --scan 'prod events'`` cannot mean different configs — and
    ``--branch`` on the plan reads cannot mean a third kind of match.
    """
    listing = await reader.send(scans_api.list_configs(slug))
    return resolve_one(scans_api.config_names(as_list(listing)), selector, what="scan config")


def run_list(args: argparse.Namespace, config: Config) -> int:
    slugs: tuple[str, ...] = tuple(args.project or ())
    as_json: bool = bool(args.as_json)
    include_demo: bool = bool(args.include_demo)
    base_url = require_base_url(config)
    human = sys.stderr if as_json else sys.stdout
    started = time.monotonic()
    generated_at = datetime.now(UTC)

    async def body(client: httpx.AsyncClient) -> tuple[Reader, tuple[ProjectScans, ...]]:
        reader = Reader(client, base_url)
        # Same selection machinery as status and watch, so the three cannot
        # drift on WHICH projects they report about; the 403 advice comes free.
        selection = await select(reader, slugs=slugs, include_demo=include_demo, scope="unknown")
        raise_selection_failure(selection)
        projects = selection.projects
        found = [text_of(project, "slug") or "" for project in projects]
        listings = await gather_bounded(
            [reader.try_read_list(scans_api.list_configs(slug)) for slug in found]
        )
        rows: list[ProjectScans] = []
        for project, slug, fetched in zip(projects, found, listings, strict=True):
            errors: list[SectionError] = []
            configs: tuple[JsonDict, ...] = ()
            if fetched.ok and fetched.value is not None:
                configs = tuple(scans_api.scan_config_summary(item) for item in fetched.value)
            else:
                # NOT an empty list. A 403 on one project of six has to be a
                # printed line and a non-zero exit, never a shorter table.
                errors.append(
                    SectionError(
                        section="scans",
                        endpoint=scans_api.CONFIGS.format(slug=slug),
                        status_code=fetched.status_code,
                        message=fetched.error or "the tripl API did not answer",
                    )
                )
            rows.append(
                ProjectScans(
                    slug=slug,
                    name=text_of(project, "name") or slug,
                    is_demo=bool(project.get("is_demo")),
                    scans=configs,
                    errors=tuple(errors),
                )
            )
        return reader, tuple(rows)

    reader, projects = run_async(config, body, timeout=float(args.timeout))
    snapshot = ScansSnapshot(
        run=Run(
            instance=instance_of(config, base_url, "unknown"),
            generated_at=generated_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            requests=reader.requests,
        ),
        projects=projects,
    )

    print(
        render_header("scans list", base_url, config.sources.get("base_url", "unknown")), file=human
    )
    print(file=human)
    print(render_scan_configs(snapshot), file=human)
    if as_json:
        json.dump(scans_document(snapshot), sys.stdout)
        sys.stdout.write("\n")
    return EXIT_FAILURE if snapshot.failed else EXIT_OK


def run_jobs(args: argparse.Namespace, config: Config) -> int:
    slug = require_single_project(args)
    selector: str = str(args.scan)
    limit: int = int(args.limit)
    as_json: bool = bool(args.as_json)
    base_url = require_base_url(config)
    human = sys.stderr if as_json else sys.stdout
    started = time.monotonic()
    generated_at = datetime.now(UTC)

    async def body(client: httpx.AsyncClient) -> tuple[Reader, str, str, list[JsonDict]]:
        reader = Reader(client, base_url)
        scan_id, scan_name = await _resolve_scan(reader, slug, selector)
        # try_read_list + read_or_raise, not send + as_list: `as_list` coerces a
        # 200 carrying an object to [], which this command would then render as
        # "(no jobs)" at exit 0 - a non-list read printed as empty, which is the
        # one thing the Fetched guard exists to stop (TRAP 3).
        fetched = await reader.try_read_list(scans_api.list_jobs(slug, scan_id, limit=limit))
        return reader, scan_id, scan_name, read_or_raise(fetched)

    reader, scan_id, scan_name, jobs = run_async(config, body, timeout=float(args.timeout))
    snapshot = ScanJobsSnapshot(
        run=Run(
            instance=instance_of(config, base_url, "unknown"),
            generated_at=generated_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            requests=reader.requests,
        ),
        project=slug,
        scan_id=scan_id,
        scan_name=scan_name,
        limit=limit,
        jobs=tuple(jobs),
    )

    print(
        render_header("scans jobs", base_url, config.sources.get("base_url", "unknown")), file=human
    )
    print(file=human)
    print(render_scan_jobs(snapshot), file=human)
    if as_json:
        json.dump(scan_jobs_document(snapshot), sys.stdout)
        sys.stdout.write("\n")
    return EXIT_OK


def _emit_mutation(
    outcome: MutationOutcome, *, base_url: str, config: Config, as_json: bool
) -> None:
    human = sys.stderr if as_json else sys.stdout
    print(
        render_header(outcome.command, base_url, config.sources.get("base_url", "unknown")),
        file=human,
    )
    print(file=human)
    print(render_mutation(outcome), file=human)
    if as_json:
        json.dump(mutation_document(outcome), sys.stdout)
        sys.stdout.write("\n")


def run_run(args: argparse.Namespace, config: Config) -> int:
    slug = require_single_project(args)
    selector: str = str(args.scan)
    as_json: bool = bool(args.as_json)
    dry_run: bool = bool(args.dry_run)
    base_url = require_base_url(config)
    started = time.monotonic()
    generated_at = datetime.now(UTC)

    async def body(
        client: httpx.AsyncClient,
    ) -> tuple[Reader, str, str, ApiRequest, JsonDict | None]:
        reader = Reader(client, base_url)
        # Resolve first: that is most of what --dry-run is for, and it is also
        # what turns a typo into exit 2 before anything is triggered.
        scan_id, scan_name = await _resolve_scan(reader, slug, selector)
        request = scans_api.run(slug, scan_id)
        if dry_run:
            return reader, scan_id, scan_name, request, None
        # No prompt. `run` executes SQL an owner already authored on a schedule
        # that already runs it; the backend calls it editor-level and the MCP
        # marks it destructiveHint=False. A prompt on the most-scripted command
        # would be noise.
        return reader, scan_id, scan_name, request, as_dict(await reader.send(request))

    reader, scan_id, scan_name, request, result = run_async(
        config, body, timeout=float(args.timeout)
    )
    outcome = MutationOutcome(
        command="scans run",
        run=Run(
            instance=instance_of(config, base_url, "unknown"),
            generated_at=generated_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            requests=reader.requests,
        ),
        request=request_document(request),
        project=slug,
        dry_run=dry_run,
        scan_id=scan_id,
        scan_name=scan_name,
        result=result,
    )
    _emit_mutation(outcome, base_url=base_url, config=config, as_json=as_json)
    if result is not None and text_of(result, "status") == "failed":
        # A 201 is NOT success. `scan_service.trigger_scan` catches a broker
        # failure and returns the job with status="failed" and an error_message,
        # still 201 - so a script reading the status code alone would report a
        # scan that never started as started.
        print(
            f"tripl: the job was created but is already failed: "
            f"{text_of(result, 'error_message') or 'no error message'}",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    return EXIT_OK


def run_cancel(args: argparse.Namespace, config: Config) -> int:
    slug = require_single_project(args)
    selector: str = str(args.scan)
    job_id: str = str(args.job_id)
    as_json: bool = bool(args.as_json)
    dry_run: bool = bool(args.dry_run)
    assume_yes: bool = bool(args.assume_yes)
    base_url = require_base_url(config)
    started = time.monotonic()
    generated_at = datetime.now(UTC)

    async def body(
        client: httpx.AsyncClient,
    ) -> tuple[Reader, str, str, ApiRequest, JsonDict | None]:
        reader = Reader(client, base_url)
        scan_id, scan_name = await _resolve_scan(reader, slug, selector)
        request = scans_api.cancel_job(slug, scan_id, job_id)
        if dry_run:
            # Never prompts: nothing is going to be sent.
            return reader, scan_id, scan_name, request, None
        confirm(
            f"Cancel job {job_id} of {slug} {scan_name!r}? It stops at the next chunk "
            "boundary; metrics already written are kept.",
            assume_yes=assume_yes,
        )
        return reader, scan_id, scan_name, request, as_dict(await reader.send(request))

    reader, scan_id, scan_name, request, result = run_async(
        config, body, timeout=float(args.timeout)
    )
    outcome = MutationOutcome(
        command="scans cancel",
        run=Run(
            instance=instance_of(config, base_url, "unknown"),
            generated_at=generated_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            requests=reader.requests,
        ),
        request=request_document(request),
        project=slug,
        dry_run=dry_run,
        scan_id=scan_id,
        scan_name=scan_name,
        job_id=job_id,
        result=result,
    )
    _emit_mutation(outcome, base_url=base_url, config=config, as_json=as_json)
    return EXIT_OK
