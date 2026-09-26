"""Argument parsing and the ``tripl`` entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from tripl_cli import __version__
from tripl_cli.commands import Handler, register_all
from tripl_cli.config import ENV_API_KEY, ENV_BASE_URL, resolve
from tripl_cli.errors import EXIT_USAGE, TriplError

# 128 + SIGINT, the shell convention for "killed by Ctrl-C".
EXIT_INTERRUPTED = 130


# Both spellings of the instance-URL flag. `tripl annotate` takes only the second:
# its own `--url` is the link a chart annotation opens (see build_global_parser).
INSTANCE_URL_FLAGS: tuple[str, ...] = ("--url", "--base-url")


def build_global_parser(
    *, instance_url_flags: Sequence[str] = INSTANCE_URL_FLAGS
) -> argparse.ArgumentParser:
    """The connection flags, as a parent shared by the root and every command.

    Every option here uses ``default=argparse.SUPPRESS``, and that is not style:
    ``_SubParsersAction.__call__`` parses the subcommand into a FRESH namespace
    and then copies every key back over the parent's, so an ordinary
    ``default=None`` on the subparser silently erases a value given BEFORE the
    subcommand name (``tripl --url X doctor``). With SUPPRESS the attribute is
    simply absent when unset and nothing gets clobbered — hence the
    ``getattr(args, ..., None)`` reads in ``main``.

    ``instance_url_flags`` exists for ``tripl annotate`` alone, whose ``--url``
    is the release link the annotation carries — the name the API field, the
    issue and every CI example use. That command builds its own parent with
    ``--base-url`` only, rather than passing ``conflict_handler="resolve"``:
    argparse shares action OBJECTS between a parent and its children, and
    resolving the conflict removes ``--url`` from that shared action — from every
    other command at once. ``tripl --url X annotate`` still works, because the
    root parser keeps both spellings.
    """
    parser = argparse.ArgumentParser(add_help=False)
    connection = parser.add_argument_group("connection")
    connection.add_argument(
        *instance_url_flags,
        dest="url",
        metavar="URL",
        default=argparse.SUPPRESS,
        # Two spellings, one dest, one help entry: --base-url is accepted
        # because the env var and every doc say BASE_URL, so users type it by
        # analogy. One option with an alias rather than two options, so there is
        # nothing to drift.
        help=f"tripl instance URL, e.g. https://tripl.example.com (env: {ENV_BASE_URL})",
    )
    connection.add_argument(
        "--api-key",
        dest="api_key",
        metavar="KEY",
        default=argparse.SUPPRESS,
        help=(
            f"tripl API key. Prefer {ENV_API_KEY} or the config file: a key on the "
            "command line is visible to other users via ps(1) and lands in shell history"
        ),
    )
    connection.add_argument(
        "--config",
        dest="config",
        metavar="PATH",
        type=Path,
        default=argparse.SUPPRESS,
        help="config file to read instead of the default location",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    globals_parser = build_global_parser()
    parser = argparse.ArgumentParser(
        prog="tripl",
        description=f"tripl {__version__} — operator CLI for a running tripl instance",
        parents=[globals_parser],
    )
    # Top level only: `tripl doctor --version` would be a confusing thing to
    # answer. Reads the installed distribution's metadata, so the number can
    # never disagree with the packaged one.
    parser.add_argument("--version", action="version", version=f"tripl {__version__}")
    # NOT required=True: with no commands registered yet that produces an
    # incomprehensible argparse error. main() handles the empty case instead.
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    register_all(subparsers, globals_parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse, resolve configuration, dispatch. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    # getattr rather than args.handler: the attribute only exists once a
    # subcommand's set_defaults has run, and it is Any off a Namespace, so the
    # annotation is what keeps `return handler(...)` honest under mypy strict.
    handler: Handler | None = getattr(args, "handler", None)
    if handler is None:
        # Bare `tripl`. Help to STDERR and a usage exit code, not 0: a script
        # that invokes the CLI with no subcommand has a bug, and exiting 0 would
        # let it look successful.
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        # Unconditional, and safe to be: resolution touches the filesystem but
        # never the network, and never fails on merely ABSENT values — only on a
        # malformed file or a --config that does not exist. Each command asks
        # for what it needs with require_base_url / require_api_key.
        config = resolve(
            base_url=getattr(args, "url", None),
            api_key=getattr(args, "api_key", None),
            config_path=getattr(args, "config", None),
        )
        return handler(args, config)
    except TriplError as exc:
        # Every expected failure is a message, never a traceback. A future
        # --debug that re-raises for the traceback goes here.
        print(f"tripl: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        # Reached without a real SIGINT by `tripl watch`, whose loop catches the
        # interrupt only long enough to print its footer and then RE-RAISES so
        # this stays the only place 130 is decided (tripl-ey6j.4).
        print(file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    sys.exit(main())
