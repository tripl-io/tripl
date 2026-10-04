"""``tripl watch`` — follow mode for an operator sitting in front of an incident.

``doctor`` answers "what is broken" at a point in time. ``watch`` answers "what
is happening right now": scan jobs as they run with their replay chunk progress,
signals as they open, deliveries as they fail. Terminal output, no daemon.

It reaches NO verdict and therefore never exits 3, whatever it observes. Two
failure modes make any other answer wrong: `tripl watch --duration 300 | tee
incident.log` under `set -e` must not die at the exact moment something
interesting happens, and a CI gate built on watch would be a slower, flakier
`doctor --strict` with a worse sampling model - watch sees only what fell inside
its polls, so a green watch run proves strictly LESS than a green doctor run
while looking like it proves more.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections.abc import Sequence
from datetime import datetime

import httpx

from tripl_cli.commands import add_timeout, bounded_float
from tripl_cli.config import Config, require_base_url
from tripl_cli.diagnostics.collect import Reader, instance_of
from tripl_cli.errors import EXIT_OK
from tripl_cli.model import JsonDict
from tripl_cli.render import render_header
from tripl_cli.report import watch_line
from tripl_cli.runner import run_async
from tripl_cli.watch.loop import EventSink, default_clock, follow, prepare
from tripl_cli.watch.model import (
    MAX_DURATION_SECONDS,
    MAX_INTERVAL_SECONDS,
    MAX_STALL_SECONDS,
    MIN_DURATION_SECONDS,
    MIN_INTERVAL_SECONDS,
    MIN_STALL_SECONDS,
    SLOW_STREAM_MIN_SECONDS,
    WATCH_INTERVAL_SECONDS,
    WATCH_STALL_SECONDS,
    ProjectRef,
    WatchEvent,
    WatchOptions,
    WatchOutcome,
)
from tripl_cli.watch.render import ProjectBaseline, plural, render_line, render_preamble


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "watch",
        parents=[parent],
        help="follow scan jobs, signals and delivery failures as they happen",
        description=(
            "Poll a tripl instance and print what changes: replay chunk progress, jobs "
            "starting and finishing, signals opening and clearing, alert deliveries "
            "failing. Runs until Ctrl-C or --duration. Never reports a verdict and never "
            "exits 3 - use `tripl doctor` for that."
        ),
    )
    parser.add_argument(
        "--project",
        dest="project",
        metavar="SLUG",
        action="append",
        help="follow only this project (repeatable). Required for a project-scoped API key.",
    )
    parser.add_argument(
        "--include-demo",
        dest="include_demo",
        action="store_true",
        help="also follow demo projects, which are excluded by default",
    )
    parser.add_argument(
        "--scan",
        dest="scan",
        metavar="NAME_OR_ID",
        action="append",
        help=(
            "follow only these scan configs, matched exactly on name and then on id "
            "(repeatable). Narrows the JOB lines only - signals and deliveries are never "
            "filtered by it, because metric-scope signals carry no scan config and would "
            "vanish"
        ),
    )
    parser.add_argument(
        "--interval",
        dest="interval",
        metavar="SECONDS",
        type=bounded_float("--interval", MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS),
        default=WATCH_INTERVAL_SECONDS,
        help=f"seconds between polls (default: {WATCH_INTERVAL_SECONDS:g})",
    )
    parser.add_argument(
        "--duration",
        dest="duration",
        metavar="SECONDS",
        type=bounded_float("--duration", MIN_DURATION_SECONDS, MAX_DURATION_SECONDS),
        default=None,
        help="stop after this many seconds and exit 0 (default: run until Ctrl-C)",
    )
    parser.add_argument(
        "--stall-after",
        dest="stall_after",
        metavar="SECONDS",
        type=bounded_float("--stall-after", MIN_STALL_SECONDS, MAX_STALL_SECONDS),
        default=WATCH_STALL_SECONDS,
        help=(
            "report a running job whose progress has not moved for this long "
            f"(default: {WATCH_STALL_SECONDS:g})"
        ),
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print JSON Lines on stdout, one object per event, and every human line on stderr",
    )
    add_timeout(parser)
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace, config: Config) -> int:
    # Namespace attributes are Any; the local annotations are what keep the rest
    # of this function honest under mypy strict.
    as_json: bool = bool(args.as_json)
    duration: float | None = float(args.duration) if args.duration is not None else None
    options = WatchOptions(
        project_slugs=tuple(args.project or ()),
        include_demo=bool(args.include_demo),
        scan_selectors=tuple(args.scan or ()),
        interval=float(args.interval),
        duration=duration,
        stall_after=float(args.stall_after),
    )
    base_url = require_base_url(config)
    human = sys.stderr if as_json else sys.stdout
    clock = default_clock()
    seq = 0

    def write(event: WatchEvent) -> None:
        # A BrokenPipeError here is NOT caught: `tripl watch | head -20` is a
        # pipeline the docs recommend, and EventSink turns the broken pipe into a
        # clean end of run - one place, so the human and the JSON path cannot
        # drift on what happens when the reader walks away.
        nonlocal seq
        seq += 1
        if as_json:
            # Flushed per line, and that is a correctness requirement rather than
            # polish: block-buffered output piped to `jq` shows nothing for
            # minutes, which reads as "the tool is hung" in the exact situation
            # this tool exists for.
            json.dump(watch_line(event, seq=seq), sys.stdout)
            sys.stdout.write("\n")
            sys.stdout.flush()
        print(render_line(event), file=human, flush=True)

    sink = EventSink(write=write)

    def say(text: str = "") -> None:
        """The preamble's writer. Same broken-pipe rule as the event path.

        `tripl watch | head -1` breaks the pipe before the first event line, and
        the traceback would land where the header just was.
        """
        if sink.closed:
            return
        try:
            print(text, file=human, flush=True)
        except BrokenPipeError:
            sink.closed = True

    say(render_header("watch", base_url, config.sources.get("base_url", "unknown")))
    say()

    async def body(client: httpx.AsyncClient) -> WatchOutcome:
        # The loop lives INSIDE the one run_async body, so the TCP+TLS handshake
        # happens once instead of every interval for hours against an instance
        # that is by assumption already unwell - and `async with` closes the pool
        # on the exception path, which for watch includes the Ctrl-C.
        reader = Reader(client, base_url)
        preparation = await prepare(reader, options)
        fast = len(preparation.targets)
        slow = fast + 3 * len(preparation.projects)

        def on_ready(baselines: Sequence[ProjectBaseline], counts: JsonDict) -> None:
            say(
                render_preamble(
                    baselines,
                    observed_at=clock.now(),
                    interval=options.interval,
                    slow_interval=SLOW_STREAM_MIN_SECONDS,
                    fast_requests=fast,
                    slow_requests=slow,
                    config_count=len(preparation.targets),
                    jobs_limit=options.jobs_limit,
                    deliveries_limit=options.deliveries_limit,
                )
            )
            say()
            sink.emit(
                _started(
                    options,
                    preparation.projects,
                    counts,
                    fast=fast,
                    slow=slow,
                    config=config,
                    base_url=base_url,
                    moment=clock.now(),
                )
            )

        return await follow(reader, preparation, options, sink=sink, clock=clock, on_ready=on_ready)

    run_async(config, body, timeout=float(args.timeout))
    # Always 0 when the run completed. A failed job, a new signal and a failed
    # delivery all still exit 0; Ctrl-C is 130 and is decided by cli.py.
    return EXIT_OK


def _started(
    options: WatchOptions,
    projects: Sequence[ProjectRef],
    counts: JsonDict,
    *,
    fast: int,
    slow: int,
    config: Config,
    base_url: str,
    moment: datetime,
) -> WatchEvent:
    slugs = [project.slug for project in projects]
    return WatchEvent(
        event="watch.started",
        time=moment,
        message=(
            f"{plural(len(slugs), 'project')}, {plural(fast, 'scan config')}, "
            f"poll {options.interval:g}s."
        ),
        data={
            # api_key_scope is "unknown" on purpose: watch does not read
            # /auth/me, the same honest answer `status` gives.
            "instance": dataclasses.asdict(instance_of(config, base_url, "unknown")),
            "projects": slugs,
            "interval_seconds": options.interval,
            "duration_seconds": options.duration,
            "stall_after_seconds": options.stall_after,
            # The RESOLVED options, not the module defaults they came from: every
            # other key on this line reports what the run is actually doing, and
            # two that silently reported the default instead would be wrong the
            # day either window grows a flag.
            "jobs_limit": options.jobs_limit,
            "deliveries_limit": options.deliveries_limit,
            "slow_stream_min_seconds": SLOW_STREAM_MIN_SECONDS,
            "requests_per_fast_tick": fast,
            "requests_per_slow_tick": slow,
            "baseline": counts,
        },
    )
