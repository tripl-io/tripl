"""The write-safety rules, in one place, for the CLI's first mutating commands.

``doctor``, ``status`` and ``watch`` are all read-only and a ``tk_r_`` key
suffices for every one of them. ``scans run``, ``scans cancel``,
``drifts dismiss`` and ``drifts reopen`` change the instance, which is a new
category and needs rules rather than habits:

* THE SERVER IS THE AUTHORITY ON SCOPE. The ``tk_r_``/``tk_w_`` prefix is derived
  in ``api_key_service.py`` from the scope's first letter and says NOTHING about
  the backing user's role, so a local prefix check would be a fourth copy of a
  backend rule AND still wrong for a ``tk_w_`` key held by a viewer. The CLI
  sends the request and prints the client's 403 message, which already names all
  three possibilities.
* NEVER BLOCK ON A PROMPT IN A PIPELINE, AND NEVER AUTO-PROCEED IN ONE EITHER.
  A prompt that hangs a cron job holding a scan trigger is a defect; proceeding
  silently would make a piped invocation more dangerous than a typed one and
  make ``--yes`` meaningless. So: refuse, name ``--yes``, exit 2, send nothing.
* THE PROMPT GOES TO STDERR. ``input()`` writes its prompt to stdout, which would
  put prose inside the ``--json`` document contract.
* DECLINING EXITS 1. A script must never be able to read "the operator said no"
  as "the mutation happened".
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from tripl_cli.api.request import ApiRequest
from tripl_cli.errors import TriplConfigError, TriplError
from tripl_cli.model import JsonDict

# ``require_single_project`` used to live here. It moved to ``commands`` in
# four read verbs need it now, and "this command acts on one
# project" is an argument rule rather than a write-safety one — leaving it here
# would have had `tripl plan fields` importing the write-safety module.


def add_write_flags(parser: argparse.ArgumentParser, *, prompts: bool) -> None:
    """``--dry-run`` on every mutation; ``--yes`` only where there IS a prompt.

    No no-op ``--yes`` on ``scans run``: a flag that does nothing is a flag a
    script author will assume does something on the next command too.
    """
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="resolve everything and print the request, but send nothing",
    )
    if prompts:
        parser.add_argument(
            "--yes",
            dest="assume_yes",
            action="store_true",
            help="skip the confirmation prompt (required when stdin is not a terminal)",
        )


# What a refusal says did NOT happen. The default is the HTTP wording, true of
# the three verbs this module was written for; `install` and `upgrade` share the
# prompt and send no request at all, so they pass their own - the stakes there
# are what would have been written to disk, and a line reading "Nothing was
# sent." on a command that never opens a socket teaches an operator to discount
# the sentence everywhere else too.
NOTHING_SENT = "Nothing was sent."
# `install`, which has already probed Docker by the time it asks.
NOTHING_WRITTEN = "Nothing was written."
# `upgrade`, which asks before the pull, the pin rewrite and the restart.
NOTHING_WRITTEN_OR_RUN = "Nothing was written and nothing was run."


def confirm(question: str, *, assume_yes: bool, consequence: str = NOTHING_SENT) -> None:
    """Ask, or refuse to ask. Returns only when the mutation may proceed."""
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise TriplConfigError(
            f"{question} Refusing to prompt because stdin is not a terminal. "
            f"Re-run with --yes to confirm non-interactively. {consequence}"
        )
    # stderr, and no `input()`: its prompt goes to stdout, where --json puts the
    # one document a consumer parses.
    print(f"{question} [y/N] ", file=sys.stderr, end="")
    sys.stderr.flush()
    answer = sys.stdin.readline()
    if answer.strip().lower() not in ("y", "yes"):
        # EOF included. Exit 1, never 0: "declined" is not "done".
        raise TriplError(f"aborted. {consequence}")


def request_document(request: ApiRequest) -> JsonDict:
    """The printable projection of a request: method, path, params, body.

    NEVER headers and never the API key. This dict is what ``--dry-run`` prints
    and what every mutation document carries, and a credential that can be
    printed eventually is. ``None`` parameters are dropped here exactly as
    ``TriplClient.request`` drops them, so what is shown is what would go on the
    wire.
    """
    params: dict[str, Any] = {
        key: value for key, value in (request.params or {}).items() if value is not None
    }
    return {
        "method": request.method,
        "path": request.path,
        "params": params,
        "body": request.json_body,
    }
