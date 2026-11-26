"""Reproduce a missing or duplicate emission from completed synthetic journey runs."""

from __future__ import annotations

import argparse
import json
import shlex
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path

from tripl_cli.commands import add_json, bounded_text, never_connects
from tripl_cli.config import Config
from tripl_cli.errors import EXIT_CHECKS_FAILED, EXIT_OK, EXIT_UNAVAILABLE, TriplConfigError

_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Capture:
    build: str
    journey: str
    fixture: str
    allowlist: frozenset[str]
    runs: tuple[tuple[str, ...], ...]
    covered: bool
    counts: tuple[int, ...]

    def summary(self) -> dict[str, object]:
        stable = len(set(self.counts)) == 1
        return {"build": self.build, "count": self.counts[0] if stable else None}


def _read(path: Path, event: str) -> Capture | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TriplConfigError(f"cannot read capture {path}: {exc.strerror}") from None
    if len(raw) > _MAX_BYTES:
        raise TriplConfigError(f"capture {path} exceeds 1 MiB")
    try:
        data = json.loads(raw)
    except (UnicodeError, ValueError, RecursionError):
        raise TriplConfigError(f"invalid JSON capture: {path}") from None
    if not isinstance(data, dict) or set(data) != {
        "build",
        "journey",
        "fixture",
        "runs",
        "allowlist",
    }:
        raise TriplConfigError(f"{path} requires build, journey, fixture, allowlist and runs")
    for field in ("build", "journey", "fixture"):
        value = data[field]
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise TriplConfigError(f"invalid {field} in {path}")
    names = data["allowlist"]
    if (
        not isinstance(names, list)
        or not names
        or not all(isinstance(name, str) and name.strip() for name in names)
        or len(set(names)) != len(names)
    ):
        raise TriplConfigError(f"{path}: allowlist must contain distinct event names")
    allowed = set(names)
    runs = data["runs"]
    if not isinstance(runs, list) or not all(
        isinstance(run, list) and all(isinstance(name, str) and name in allowed for name in run)
        for run in runs
    ):
        raise TriplConfigError(f"{path}: runs must contain arrays of event names")
    return Capture(
        data["build"],
        data["journey"],
        data["fixture"],
        frozenset(allowed),
        tuple(tuple(run) for run in runs),
        event in allowed,
        tuple(run.count(event) for run in runs),
    )


def _first_difference(healthy: Capture, suspect: Capture) -> dict[str, object] | None:
    common = healthy.allowlist & suspect.allowlist
    sequences = []
    for capture in (healthy, suspect):
        projected = {tuple(name for name in run if name in common) for run in capture.runs}
        if len(projected) != 1:
            return None
        sequences.append(next(iter(projected)))
    for position, (before, after) in enumerate(zip_longest(*sequences), start=1):
        if before != after:
            return {"position": position, "healthy": before, "suspect": after}
    return None


def _compare(
    healthy: Capture | None,
    suspect: Capture | None,
    candidate: Capture | None,
    check_candidate: bool,
) -> tuple[str, str, int]:
    captures = [healthy, suspect] + ([candidate] if check_candidate else [])
    if any(c is None or len(c.counts) < 2 for c in captures):
        return (
            "inconclusive",
            "Require a capture with at least two completed attempts per build.",
            EXIT_UNAVAILABLE,
        )
    available = [c for c in captures if c is not None]
    if any(not c.covered for c in available):
        return "inconclusive", "Event is outside a capture allowlist.", EXIT_UNAVAILABLE
    if len({(c.journey, c.fixture) for c in available}) != 1:
        return "inconclusive", "Journey or fixture differs between captures.", EXIT_UNAVAILABLE
    if len({c.build for c in available}) != len(available):
        return "inconclusive", "Use distinct build identifiers.", EXIT_UNAVAILABLE
    if any(len(set(c.counts)) != 1 for c in available):
        return "inconclusive", "Event count varies between attempts.", EXIT_UNAVAILABLE
    baseline, observed = available[0].counts[0], available[1].counts[0]
    if baseline == 0:
        return "inconclusive", "Healthy build never emitted the selected event.", EXIT_UNAVAILABLE
    if baseline == observed and candidate and candidate.counts[0] != baseline:
        return (
            "still_failing",
            "Only the candidate differs from the healthy count.",
            EXIT_CHECKS_FAILED,
        )
    if baseline == observed:
        return "no_regression", "Suspect matches the healthy count.", EXIT_OK
    if candidate is None:
        return "reproduced", "Suspect differs from the healthy count.", EXIT_CHECKS_FAILED
    if candidate.counts[0] == baseline:
        return "verified", "Candidate restores the healthy count.", EXIT_OK
    return "still_failing", "Candidate still differs from the healthy count.", EXIT_CHECKS_FAILED


def run(args: argparse.Namespace, config: Config) -> int:
    captures = {
        "healthy": _read(args.healthy, args.event),
        "suspect": _read(args.suspect, args.event),
        "candidate": _read(args.candidate, args.event) if args.candidate else None,
    }
    verdict, reason, code = _compare(
        captures["healthy"], captures["suspect"], captures["candidate"], args.candidate is not None
    )
    baseline = captures["healthy"]
    argv = ["tripl", "reproduce", "--event", args.event]
    for role in captures:
        if path := getattr(args, role):
            argv.extend([f"--{role}", str(path)])
    command = shlex.join([*argv, "--json"])
    report = {
        "version": 1,
        "event": args.event,
        "journey": baseline.journey if baseline else None,
        "fixture": baseline.fixture if baseline else None,
        "verdict": verdict,
        "reason": reason,
        "command": command,
        **{role: c.summary() if c else None for role, c in captures.items()},
    }
    suspect = captures["suspect"]
    if baseline and suspect and verdict != "inconclusive":
        difference = _first_difference(baseline, suspect)
        if difference:
            report["first_difference"] = difference
    print(json.dumps(report) if args.as_json else f"{verdict}: {reason}\n{command}")
    return code


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], parent: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "reproduce", parents=[parent], help="compare a synthetic journey on specific builds"
    )
    parser.add_argument("--event", required=True, type=bounded_text("--event", 1, 200))
    for role in ("healthy", "suspect", "candidate"):
        parser.add_argument(
            f"--{role}",
            type=Path,
            required=role != "candidate",
            help=f"capture JSON from the {role} build",
        )
    add_json(parser)
    # It compares local capture files: no instance, so the connection settings
    # are not read and a malformed one cannot stop it.
    parser.set_defaults(handler=run, offline=never_connects)
