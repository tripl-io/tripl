"""Capture allowlisted event names from repeated runs of a local team-owned test."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from tripl_cli.commands import add_json, bounded_text, never_connects
from tripl_cli.config import Config
from tripl_cli.errors import TriplConfigError


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], parent: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "journey-capture",
        parents=[parent],
        help="run a local journey and record allowlisted event names",
    )
    for name in ("build", "journey", "fixture"):
        parser.add_argument(f"--{name}", required=True, type=bounded_text(name, 1, 200))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--allow-event",
        action="append",
        type=bounded_text("event", 1, 200),
        required=True,
        metavar="NAME",
    )
    parser.add_argument("test_command", nargs=argparse.REMAINDER, help="-- PROGRAM [ARGS ...]")
    add_json(parser)
    # It runs a local command and writes a local file: no instance, so the
    # connection settings are not read and a malformed one cannot stop it.
    parser.set_defaults(handler=run, offline=never_connects)


def run(args: argparse.Namespace, config: Config) -> int:
    command = args.test_command
    if command[:1] != ["--"] or len(command) < 2:
        raise TriplConfigError("provide the local test command after --")
    output: Path = args.output
    if output.exists():
        raise TriplConfigError("capture already exists; choose a fresh output path")
    if any(
        not item.strip() for item in (args.build, args.fixture, args.journey, *args.allow_event)
    ):
        raise TriplConfigError("metadata and event names must contain visible text")
    runs = []
    try:
        with TemporaryDirectory(dir=output.parent) as tmp:
            for attempt in range(2):
                events = Path(tmp).resolve() / f"events-{attempt}.json"
                env = {**os.environ, "TRIPL_JOURNEY_EVENTS": str(events)}
                result = subprocess.run(
                    command[1:],
                    env=env,
                    timeout=300,
                    check=False,
                    stdout=sys.stderr if args.as_json else None,
                )
                if result.returncode:
                    raise TriplConfigError(f"journey attempt {attempt + 1} did not complete")
                with events.open("rb") as stream:
                    payload = stream.read(1_048_577)
                if len(payload) > 1_048_576:
                    raise TriplConfigError("journey event capture exceeded 1 MiB")
                names = json.loads(payload)
                if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                    raise TriplConfigError("journey must write a JSON array of event names")
                runs.append([name for name in names if name in args.allow_event])
            document = {key: getattr(args, key) for key in ("build", "journey", "fixture")}
            document["allowlist"] = sorted(set(args.allow_event))
            capture = Path(tmp) / "capture.json"
            serialized = json.dumps({**document, "runs": runs})
            if len(serialized) + 1 > 1_048_576:
                raise TriplConfigError("combined capture exceeded 1 MiB")
            capture.write_text(serialized + "\n", encoding="utf-8")
            os.link(capture, output)
    except (OSError, ValueError, RecursionError, subprocess.TimeoutExpired) as exc:
        raise TriplConfigError(f"could not capture completed journey: {exc}") from exc
    if args.as_json:
        print(serialized)
    return 0
