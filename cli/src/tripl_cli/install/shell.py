"""The one seam between this CLI and the local machine.

Everything that runs a subprocess goes through ``Runner``, and nothing else in
the package imports ``subprocess`` at all. That is what lets the whole of
``install``/``upgrade`` be tested without a Docker daemon — the test suite
injects a ``FakeRunner`` that records every ``Command`` and answers with scripted
``CommandResult``s, so CI never starts a container.

THE OUTPUT RULE, which the rest of the package depends on:

    PROBE commands are CAPTURED. They are short, we parse them, and their stderr
    is echoed verbatim when they fail — "Cannot connect to the Docker daemon at
    unix:///var/run/docker.sock" is already the best sentence anyone will write
    about that failure and paraphrasing it loses the socket path.

    LONG-RUNNING commands (``pull``, ``up -d``) are INHERITED: ``stdout=None``,
    ``stderr=None``, straight to the operator's terminal, never captured. A
    wrapper that swallows compose's layer-pull progress and then reprints a
    summary is worse than useless — it turns a five-minute pull into an
    apparent hang and hides the one line that names the failing service.

The cost of that second rule is that compose's output cannot appear in the
``--json`` document. The document carries the ``argv``, the ``cwd`` and the
``returncode`` instead, and the docs say so.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Command:
    """One invocation, described rather than performed.

    ``env`` is an OVERLAY on the current environment, not a replacement: docker
    needs ``PATH``, ``HOME`` and ``DOCKER_HOST`` to work at all. It exists for
    exactly one thing — handing ``TRIPL_VERSION`` to ``docker compose pull``
    during an upgrade, where the shell environment beats the ``.env`` file that
    still holds the old pin.
    """

    argv: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandResult:
    """What came back. ``stdout``/``stderr`` are empty unless the call was captured."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# The bool is ``capture``. A positional parameter rather than a keyword so the
# type is writable in one line and a fake is trivially conformant.
Runner = Callable[[Command, bool], CommandResult]


def describe(command: Command) -> str:
    """The paste-able form of a command, as printed before it runs.

    ``+ cd /home/op/tripl && docker compose pull``

    ``shlex.quote`` on every word including the directory: an install path with
    a space in it would otherwise print an invocation that does something else
    when pasted, which is the exact promise this line exists to make.
    """
    rendered = " ".join(shlex.quote(word) for word in command.argv)
    if command.env:
        # INSIDE the `cd`, never before it: `VAR=x cd /dir && cmd` sets the
        # variable for `cd` and not for `cmd`, so a line printed that way would
        # not do what it says when pasted. This one does — a shell prefix is
        # exactly the one-process env overlay `subprocess_runner` performs.
        assignments = " ".join(
            f"{name}={shlex.quote(value)}" for name, value in sorted(command.env.items())
        )
        rendered = f"{assignments} {rendered}"
    if command.cwd is not None:
        rendered = f"cd {shlex.quote(str(command.cwd))} && {rendered}"
    return f"+ {rendered}"


def subprocess_runner(command: Command, capture: bool) -> CommandResult:
    """The real runner. The only ``subprocess`` call in the package."""
    environment = {**os.environ, **command.env} if command.env else None
    completed = subprocess.run(  # noqa: S603 - argv is a fixed tuple, never a shell string
        list(command.argv),
        cwd=str(command.cwd) if command.cwd is not None else None,
        env=environment,
        # capture_output would be `stdout=PIPE, stderr=PIPE`; None INHERITS the
        # parent's streams, which is what keeps compose's progress live.
        capture_output=capture,
        text=True,
        check=False,
    )
    return CommandResult(
        argv=command.argv,
        returncode=completed.returncode,
        stdout=completed.stdout or "" if capture else "",
        stderr=completed.stderr or "" if capture else "",
    )
