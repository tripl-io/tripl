"""``tripl upgrade --to X.Y.Z`` — the most dangerous thing this CLI does.

It moves a running stack to a new image tag, which applies Alembic migrations,
which are not reversible here. Everything below follows from that one fact:

* ``--to`` IS REQUIRED. There is no default and no "upgrade to latest"
  convenience. Refusing to guess which version you meant is the whole safety
  posture of this command.
* A DOWNGRADE IS REFUSED OUTRIGHT, with no override flag. Once ``alembic upgrade
  head`` has run, the old image does not know how to read the new schema, and
  the honest instruction is "restore your backup".
* AN UNORDERABLE PAIR (``latest``, ``sha-abc1234``, ``1.4``) says so and demands
  ``--allow-unordered-tag``, rather than inventing an ordering. NOT ``--yes``:
  every non-interactive caller must pass ``--yes`` or the backup prompt hangs
  it, so overloading that flag with "and ignore the ordering guard" turned the
  guard off on exactly the runs nobody is watching — the CI pipelines.
  Two decisions, two flags.
* THE BACKUP IS THE OPERATOR'S. This command PRINTS the ``pg_dump`` invocation
  and refuses to continue without an acknowledgement; it never runs it. A dump
  we invoked and then called "your backup" would be a promise we cannot keep —
  we cannot know there is disk space, and the dump does not contain
  ENCRYPTION_KEY, without which its encrypted columns are unreadable.

ORDER OF OPERATIONS, and why:

    pull  ->  write the pin  ->  up -d

The pull comes first so that a bad tag or an unreachable registry leaves ``.env``
untouched. The pin is written to ``.env`` BEFORE ``up -d`` rather than passed
inline, because an inline ``TRIPL_VERSION=x docker compose pull`` applies to the
pull only and the following ``up`` then starts ``${TRIPL_VERSION:-latest}`` —
the trap ``bin/release.sh`` records as tripl-jfm3.123.

It does NOT run ``alembic`` and does NOT run ``docker compose run --rm migrate``.
The ``migrate`` one-shot with ``condition: service_completed_successfully`` is
already the documented, race-free mechanism; a second CLI-owned migration path
would be a second thing to keep in sync with the first.
"""

from __future__ import annotations

import re
import sys
import time
from argparse import ArgumentParser, Namespace, _SubParsersAction
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from tripl_cli.commands import image_tag
from tripl_cli.commands._write import NOTHING_WRITTEN_OR_RUN, confirm
from tripl_cli.commands.install import (
    add_directory_flag,
    add_plan_flags,
    add_wait_flag,
    command_failure,
    emit,
    refuse_source_checkout,
    reject_instance_flags,
    require_docker,
    resolve_directory,
    run_commands,
)
from tripl_cli.config import Config
from tripl_cli.errors import EXIT_FAILURE, EXIT_OK, TriplConfigError
from tripl_cli.install import docker, files
from tripl_cli.install.health import HealthOutcome, wait_for_health
from tripl_cli.install.plan import (
    ORDER_DOWNGRADE,
    ORDER_SAME,
    ORDER_UNKNOWN,
    ORDER_UPGRADE,
    UpgradePlan,
)
from tripl_cli.install.render import (
    render_backup_gate,
    render_directory_header,
    render_health,
    render_health_timeout,
    render_upgrade_failure,
    render_upgrade_plan,
)
from tripl_cli.install.shell import Command, Runner, subprocess_runner
from tripl_cli.model import JsonDict, to_rfc3339
from tripl_cli.report import upgrade_document

# Strict three-part semver, digits only. `1.4`, `v1.4.0` and `sha-abc1234` all
# fail it deliberately: a partial match would be an invented ordering.
_SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# The override for the ordering guard, spelled out. Its name has to survive
# being read in a CI log by somebody who did not write the pipeline: `--yes`
# there says "this run is automated", and said nothing about downgrades.
FLAG_ALLOW_UNORDERED = "--allow-unordered-tag"


def default_runner() -> Runner:
    """This module's own subprocess seam. See ``commands.install.default_runner``.

    A separate binding rather than a re-export: ``monkeypatch.setattr`` replaces
    an attribute on ONE module object, so a shared import would leave whichever
    command was not patched running real subprocesses.
    """
    return subprocess_runner


def register(
    subparsers: _SubParsersAction[ArgumentParser],
    parent: ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "upgrade",
        parents=[parent],
        help="move an installed tripl stack to a new image tag",
        description=(
            "Pull a new tag, move the TRIPL_VERSION pin in .env, and restart the stack. "
            "Prints the pg_dump command and requires an acknowledgement first - the "
            "migrations this applies are not reversible. Refuses a downgrade outright. A "
            "non-semver tag on either side has no ordering to check, so that move needs "
            f"{FLAG_ALLOW_UNORDERED}."
        ),
    )
    parser.add_argument(
        "--to",
        dest="target",
        metavar="TAG",
        required=True,
        type=image_tag("--to"),
        help="the image tag to move to, e.g. 1.5.0. Required; there is no default",
    )
    add_directory_flag(parser)
    add_wait_flag(parser)
    add_plan_flags(parser)
    parser.add_argument(
        FLAG_ALLOW_UNORDERED,
        dest="allow_unordered",
        action="store_true",
        help=(
            "proceed even though one of the two tags is not an X.Y.Z version, so this move "
            "cannot be checked for being a downgrade. Separate from --yes on purpose: --yes "
            "answers the backup prompt, which every non-interactive run must pass"
        ),
    )
    parser.set_defaults(handler=run_upgrade)


def compare(current: str, target: str) -> str:
    """How the two tags order, or ``unknown`` when they simply do not."""
    if current == target:
        return ORDER_SAME
    left, right = _SEMVER.fullmatch(current), _SEMVER.fullmatch(target)
    if left is None or right is None:
        return ORDER_UNKNOWN
    low = tuple(int(part) for part in left.groups())
    high = tuple(int(part) for part in right.groups())
    return ORDER_UPGRADE if high > low else ORDER_DOWNGRADE


def _unorderable(current: str, target: str) -> str:
    """The refusal, in one place, because ``--dry-run`` PREVIEWS the same sentence."""
    return (
        f"cannot tell whether {target!r} is newer than {current!r}: one of them is not an "
        "X.Y.Z version, so there is no ordering to check and a downgrade would go unnoticed. "
        f"Re-run with {FLAG_ALLOW_UNORDERED} if you are sure this is not one. --yes alone "
        "does not override this - it only answers the backup prompt."
    )


def upgrade_commands(directory: Path, target: str) -> tuple[Command, ...]:
    """``pull`` with the target in the ENVIRONMENT, then a plain ``up -d``.

    The environment overlay on the pull is what makes it fetch the new tag while
    ``.env`` still holds the old one — which is the whole point of pulling before
    the pin is rewritten. ``up -d`` carries no overlay because by then the pin is
    on disk (see the module docstring and tripl-jfm3.123).
    """
    return (
        Command(
            (docker.DOCKER, "compose", "pull"),
            cwd=directory,
            env={files.VERSION_KEY: target},
        ),
        Command((docker.DOCKER, "compose", "up", "-d"), cwd=directory),
    )


def require_installed(directory: Path) -> str:
    """Both files, or an error naming the command that creates them."""
    missing = [
        name for name in (files.COMPOSE_NAME, files.ENV_NAME) if not (directory / name).is_file()
    ]
    if missing:
        raise TriplConfigError(
            f"no tripl stack in {directory}: {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} missing. Run `tripl install` first, or "
            "point --dir at the directory you installed into."
        )
    return (directory / files.ENV_NAME).read_text(encoding="utf-8")


def run_upgrade(args: Namespace, config: Config) -> int:
    as_json: bool = bool(args.as_json)
    dry_run: bool = bool(args.dry_run)
    assume_yes: bool = bool(args.assume_yes)
    allow_unordered: bool = bool(args.allow_unordered)
    wait_seconds: float = float(args.wait)
    target: str = str(args.target)
    human: TextIO = sys.stderr if as_json else sys.stdout
    started = time.monotonic()
    generated_at = datetime.now(UTC)
    runner = default_runner()

    reject_instance_flags(config, "upgrade")
    directory = resolve_directory(args.directory)
    refuse_source_checkout(directory, "upgrade")
    env_text = require_installed(directory)

    current = files.current_version(env_text)
    if current is None:
        raise TriplConfigError(
            f"{directory / files.ENV_NAME} does not set {files.VERSION_KEY}, so there is no "
            "pin to move. Add the line (or re-run `tripl install`) and try again."
        )

    ordering = compare(current, target)
    plan = UpgradePlan(
        directory=directory,
        current=current,
        target=target,
        ordering=ordering,
        commands=upgrade_commands(directory, target),
        wait_seconds=wait_seconds,
        dry_run=dry_run,
        backup_command=render_backup_gate(directory, current),
    )

    print(render_directory_header("upgrade", directory), file=human)
    print(file=human)

    if ordering == ORDER_SAME:
        # Nothing is run and .env is not touched. `already at X` has to be an
        # exit 0 so a converging provisioning script is not a failing one.
        print(f"already at {target}; nothing to do.", file=human)
        if as_json:
            emit(_document(plan, generated_at, started, [], None, EXIT_OK))
        return EXIT_OK

    if ordering == ORDER_DOWNGRADE:
        raise TriplConfigError(
            f"downgrade refused: {current} -> {target}. Alembic migrations are not "
            "reversible here, so the older image cannot read the schema the newer one "
            "wrote. Restore your backup instead. There is no override flag."
        )

    print(render_upgrade_plan(plan), file=human)
    print(file=human)

    # --dry-run FIRST, so the one move an operator is most unsure about is the
    # one they can preview. The refusal below used to come first, which made
    # `upgrade --to latest --dry-run` an exit 2 that printed nothing about what
    # the move would be - on a flag documented as "write nothing, run nothing".
    # The refusal is still stated here, as a preview.
    if dry_run:
        if ordering == ORDER_UNKNOWN and not allow_unordered:
            print(f"a real run would refuse here: {_unorderable(current, target)}", file=human)
        print("dry run: nothing was written and nothing was run.", file=human)
        if as_json:
            emit(_document(plan, generated_at, started, [], None, EXIT_OK))
        return EXIT_OK

    if ordering == ORDER_UNKNOWN and not allow_unordered:
        raise TriplConfigError(f"{_unorderable(current, target)} Nothing was changed.")

    if ordering == ORDER_UNKNOWN:
        print(
            f"tripl: warning: proceeding under {FLAG_ALLOW_UNORDERED}; the ordering of "
            f"{current!r} and {target!r} could not be determined, so this may be a downgrade.",
            file=sys.stderr,
        )

    print(plan.backup_command, file=human)
    print(file=human)
    confirm(
        f"Have you taken that backup of {directory.name}? "
        f"This upgrades {current} -> {target} and applies migrations that cannot be undone.",
        assume_yes=assume_yes,
        consequence=NOTHING_WRITTEN_OR_RUN,
    )

    require_docker(runner)

    # 1. Pull first, so a bad tag or a dead registry leaves .env untouched.
    print(file=human)
    pull_codes, failure = run_commands(plan.commands[:1], runner, human=human)
    if failure is not None:
        if as_json:
            emit(_document(plan, generated_at, started, pull_codes, None, EXIT_FAILURE))
        print(
            f"tripl: {files.ENV_NAME} was not touched - the pin is still {current} and the "
            "stack is still running the old image.",
            file=sys.stderr,
        )
        raise command_failure(failure)

    # 2. Move the pin, keeping a 0600 copy of the file that held the old one.
    backup = files.rewrite_version(directory / files.ENV_NAME, target, stamp=generated_at)
    pinned = replace(plan, env_backup=backup)
    print(
        f"{files.VERSION_KEY} is now {target} in {files.ENV_NAME} "
        f"(previous file kept as {backup.name}).",
        file=human,
    )

    # 3. Restart. The pin is on disk, so `up` starts the new tag.
    up_codes, failure = run_commands(pinned.commands[1:], runner, human=human)
    codes = [*pull_codes, *up_codes]
    if failure is not None:
        print(file=human)
        print(f"docker compose up -d failed (exit {failure.returncode}).", file=human)
        print(render_upgrade_failure(directory, target, backup), file=human)
        if as_json:
            emit(_document(pinned, generated_at, started, codes, None, EXIT_FAILURE))
        raise command_failure(failure)

    base_url = _app_base_url(env_text)
    exit_code = EXIT_OK
    health = (
        wait_for_health(base_url, deadline_seconds=wait_seconds)
        if base_url
        else HealthOutcome(
            ok=False,
            waited_seconds=0.0,
            attempts=0,
            skipped=True,
            skipped_reason=NO_ORIGIN_REASON,
        )
    )
    print(file=human)
    print(render_health(health, base_url), file=human)
    if health.ok:
        print(f"{target} is running.", file=human)
    elif health.skipped:
        # NOT "<tag> is running." - nothing here has contacted the instance, and
        # saying it is running was a claim this command had no evidence for.
        # Still exit 0: the pull, the pin and `up -d` all
        # succeeded, there is nothing to retry, and failing a converging
        # provisioning run for a check it never asked us to skip would be a red
        # pipeline no rerun can turn green. The unverified state is said in
        # words here and carried as health.skipped_reason in --json.
        print(
            f"{target} is pinned and the stack was restarted. Nothing has checked that it "
            "is serving.",
            file=human,
        )
        print(
            f"tripl: warning: this upgrade was not verified: {health.skipped_reason}.",
            file=sys.stderr,
        )
        if base_url:
            print(f"  Check it yourself: tripl doctor --url {base_url}", file=sys.stderr)
        else:
            print(
                f"  Add APP_BASE_URL to {directory / files.ENV_NAME} - compose.yaml "
                "interpolates it with ${APP_BASE_URL:?}, so the stack needs it anyway - "
                "then: tripl doctor --url <that origin>",
                file=sys.stderr,
            )
    else:
        print(render_health_timeout(directory, base_url), file=human)
        exit_code = EXIT_FAILURE
    if as_json:
        emit(_document(pinned, generated_at, started, codes, health, exit_code))
    return exit_code


# Why no probe was made when .env carries no APP_BASE_URL. Spelled out rather
# than reusing health.SKIPPED_NO_ORIGIN's short form, because this is the reason
# an operator has to act on.
NO_ORIGIN_REASON = f"{files.ENV_NAME} sets no APP_BASE_URL, so there is no origin to poll"


def _app_base_url(env_text: str) -> str:
    """The origin to poll, read from the .env we are upgrading.

    Read rather than asked for: the operator set it at install time, and asking
    again would be one more chance to type a different one — which is how a CORS
    mismatch gets introduced during an upgrade.
    """
    return files.parse_env(env_text).get("APP_BASE_URL", "").rstrip("/")


def _document(
    plan: UpgradePlan,
    generated_at: datetime,
    started: float,
    codes: list[int | None],
    health: HealthOutcome | None,
    exit_code: int,
) -> JsonDict:
    return upgrade_document(
        plan,
        generated_at=to_rfc3339(generated_at),
        duration_ms=int((time.monotonic() - started) * 1000),
        results=codes,
        health=health,
        exit_code=exit_code,
    )
