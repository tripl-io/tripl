"""Subcommand registration.

Mirrors ``tripl_mcp.tools.register_all`` deliberately, so a contributor moving
between the two packages reads the same shape: one module per command, each
exposing ``register(subparsers, parent)``, and one place that lists them.

It also holds the argparse validators the commands share. They live here rather
than in a module of their own because they exist for one purpose only: a bad
``--days``, ``--timeout``, ``--until`` or ``--to`` fails at parse time with
``EXIT_USAGE``, before a socket is opened or a container is pulled.

...and, since tripl-3ixs, the three flags that are the same flag on every
command that carries them. ``--json``, ``--timeout`` and ``--project`` were
spelled out at each ``add_parser`` call, which by the sixth verb was five copies
of the timeout default and two private ``_add_timeout`` helpers in two command
modules. Thirteen verbs now take at least one of them, so they are defined once
and ``tests/test_contract.py`` walks the real parser to prove no verb got a
different default or a different range.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime

from tripl_cli.config import Config
from tripl_cli.errors import EXIT_USAGE, TriplConfigError
from tripl_cli.runner import REQUEST_TIMEOUT_SECONDS

# The bounds of ``--timeout``, as one pair rather than a literal per verb. 0.1 s
# is below any useful network round trip and 600 s is ten minutes, past which a
# per-REQUEST deadline is no longer bounding anything.
MIN_TIMEOUT_SECONDS = 0.1
MAX_TIMEOUT_SECONDS = 600.0

# What every command's `run` looks like. Note the divergence from
# tripl_mcp.runtime's module-global singleton: that exists because FastMCP owns
# the call stack and there is nowhere else to put the resolved config. A CLI
# owns its own main(), so a mutable global here would be a regression rather
# than consistency — the config is threaded through as an argument.
Handler = Callable[[argparse.Namespace, Config], int]


def register_all(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    """Attach every subcommand to ``subparsers``.

    ``parent`` carries the global connection flags and must be passed to every
    ``add_parser`` call as ``parents=[parent]`` so ``tripl doctor --url X`` works
    as well as ``tripl --url X doctor``.
    """
    # Imported here rather than at module scope: the command modules import
    # `Handler` and the validators from this one, so a top-level import would
    # close the cycle.
    from tripl_cli.commands import (
        annotate,
        doctor,
        drifts,
        events,
        install,
        plan,
        scans,
        status,
        upgrade,
        watch,
    )

    doctor.register(subparsers, parent)
    status.register(subparsers, parent)
    watch.register(subparsers, parent)
    # The grouped commands. A command acting on the instance as a whole is one
    # word; a command acting on a CLASS OF OBJECTS is `<plural-noun> <verb>`.
    scans.register(subparsers, parent)
    drifts.register(subparsers, parent)
    # The read groups (tripl-3ixs). `events` obeys the grammar above exactly.
    # `plan` bends it — its verbs name KINDS (`plan types`, `plan variables`)
    # rather than actions — and that is deliberate: the alternative was
    # `event-types list`, `variables list`, `branches list` and `search`, four
    # more top-level entries for four reads, which is the same objection that
    # rejected `list-scans`. One group per QUESTION an operator has, not one per
    # REST collection.
    events.register(subparsers, parent)
    plan.register(subparsers, parent)
    # One word each, by the same rule: they act on an instance as a whole — one
    # that does not exist yet, or one being moved to a new tag (tripl-ey6j.3).
    install.register(subparsers, parent)
    upgrade.register(subparsers, parent)
    # One word as well: it records one fact about the project ("we deployed") from
    # a CI step, rather than acting on a class of objects an operator browses.
    annotate.register(subparsers, parent)


def group_help(parser: argparse.ArgumentParser) -> Handler:
    """The handler a command GROUP installs, for when no verb was given.

    Help to stderr and ``EXIT_USAGE``, never 0 — the same rule ``main`` applies
    to a bare ``tripl``, for the same reason: a script that invoked a group with
    no verb has a bug.
    """

    def run(args: argparse.Namespace, config: Config) -> int:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    return run


def add_json(parser: argparse.ArgumentParser) -> None:
    """``--json`` on any command that emits ONE document.

    ``tripl watch`` deliberately does not use this: it emits JSON Lines, so its
    help says so in its own words. Every other command means the same thing by
    the flag, and now says it with the same sentence.
    """
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print one JSON document on stdout and every human line on stderr",
    )


def add_timeout(parser: argparse.ArgumentParser) -> None:
    """``--timeout SECONDS``, with the same default and the same range everywhere.

    Per REQUEST, not per run — ``watch`` polls for hours on a ten-second request
    deadline — which is why the help says so and why no command overrides the
    bounds.
    """
    parser.add_argument(
        "--timeout",
        dest="timeout",
        metavar="SECONDS",
        type=bounded_float("--timeout", MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS),
        default=REQUEST_TIMEOUT_SECONDS,
        help=f"per-request timeout in seconds (default: {REQUEST_TIMEOUT_SECONDS})",
    )


def add_project(parser: argparse.ArgumentParser, *, single: bool, verb: str = "list") -> None:
    """``--project SLUG``: repeatable where the command fans out, required where it does not.

    Both spellings are ``action="append"`` so ``require_single_project`` can
    tell "given once" from "given twice" and refuse the second — argparse's own
    ``required=True`` would accept the last of two silently, and for a command
    that names one object that is the wrong project acted on without a word.
    """
    parser.add_argument(
        "--project",
        dest="project",
        metavar="SLUG",
        action="append",
        help=("project slug (required)" if single else f"{verb} only this project (repeatable)"),
    )


def require_single_project(args: argparse.Namespace) -> str:
    """Exactly one ``--project SLUG``, for anything naming a single object.

    Explicit beats clever: the drift-action route carries a slug the CLI cannot
    infer from a drift id, and every ``events``/``plan`` route is per project
    with no instance-wide form at all. Lives beside the other argparse rules
    rather than in ``_write.py``, where it started: four READ verbs now need it,
    and "exactly one project" was never a write-safety rule (tripl-3ixs).
    """
    slugs: tuple[str, ...] = tuple(getattr(args, "project", None) or ())
    if len(slugs) == 1:
        return slugs[0]
    if not slugs:
        raise TriplConfigError("this command acts on one project; name it with --project <slug>.")
    raise TriplConfigError(
        "this command acts on one project, but --project was given "
        f"{len(slugs)} times: {', '.join(repr(slug) for slug in slugs)}."
    )


def bounded_int(flag: str, low: int, high: int) -> Callable[[str], int]:
    """An argparse ``type`` that refuses an out-of-range integer by name.

    argparse turns the raised error into its own usage exit (2), which is the
    same code ``TriplConfigError`` carries — a caller never has to tell "you
    typed it wrong" from "you configured it wrong".
    """

    def _parse(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{flag} must be an integer, got {raw!r}") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(
                f"{flag} must be between {low} and {high}, got {value}"
            )
        return value

    return _parse


def nonneg_int(flag: str) -> Callable[[str], int]:
    """An argparse ``type`` for a value the ROUTE bounds only from below.

    ``--offset`` is that value: both paged routes declare it ``minimum: 0`` with
    no maximum. It used to borrow ``LIMIT_MAX`` as a ceiling, which invented a
    cap the server does not have and left a project past 10,000 events unpageable
    from the CLI — refused at parse time, on an offset the API would have served.
    A local bound stricter than the contract is a bug wearing the shape of
    validation.
    """

    def _parse(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{flag} must be an integer, got {raw!r}") from None
        if value < 0:
            raise argparse.ArgumentTypeError(f"{flag} must be 0 or greater, got {value}")
        return value

    return _parse


def bounded_text(flag: str, low: int, high: int) -> Callable[[str], str]:
    """A string of bounded length, stripped, refused when it is blank.

    ``tripl plan search ''`` and a phrase pasted with 900 characters of log
    output both cost no request this way — the route would answer 422, and a
    422 reads as "the instance rejected me" rather than "you typed too much".
    """

    def _parse(raw: str) -> str:
        value = raw.strip()
        if not low <= len(value) <= high:
            raise argparse.ArgumentTypeError(
                f"{flag} must be {low} to {high} characters of non-whitespace, got {len(value)}"
            )
        return value

    return _parse


def bounded_float(flag: str, low: float, high: float) -> Callable[[str], float]:
    def _parse(raw: str) -> float:
        try:
            value = float(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{flag} must be a number, got {raw!r}") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(
                f"{flag} must be between {low} and {high}, got {value}"
            )
        return value

    return _parse


def image_tag(flag: str) -> Callable[[str], str]:
    """A Docker image tag, validated at parse time so a typo costs no network call.

    OCI's own grammar: ``[A-Za-z0-9_][A-Za-z0-9._-]{0,127}``. Checked here rather
    than left to the registry because ``docker compose pull`` with a malformed
    tag fails after opening a connection and printing a manifest error, which
    reads as "the release is missing" rather than "you typed a space".
    """

    def _parse(raw: str) -> str:
        value = raw.strip()
        if not _IMAGE_TAG.fullmatch(value):
            raise argparse.ArgumentTypeError(
                f"{flag} must be a docker image tag - a letter, digit or underscore "
                f"followed by up to 127 of [A-Za-z0-9._-] - got {raw!r}"
            )
        return value

    return _parse


_IMAGE_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")


def bounded_datetime(flag: str) -> Callable[[str], datetime]:
    """An RFC-3339 timestamp in, an AWARE UTC datetime out.

    A naive value is read as UTC rather than as the operator's local time: a
    snooze silently shifted by an offset is a drift that reappears at the wrong
    hour, and the API stores an aware datetime either way. Failing here rather
    than on the wire means a typo costs no request at all.
    """

    def _parse(raw: str) -> datetime:
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{flag} must be an RFC-3339 timestamp, e.g. 2026-08-04T00:00:00Z, got {raw!r}"
            ) from None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    return _parse
