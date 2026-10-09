"""``tripl install`` — a fresh machine to a running instance, in one command.

It writes three files into a directory and runs ``docker compose pull`` and
``docker compose up -d`` in it, then polls ``/health`` until the instance
answers. It is IDEMPOTENT rather than one-shot: run it again and it converges —
files that already match are left alone, files that differ are reported and
kept, and the two compose commands run anyway, because "make the running stack
match what is on disk" is the useful meaning of a re-run.

WHAT IT DELIBERATELY DOES NOT DO:

* It does not create the owner account, connect a data source or run the first
  scan. Two of those three are unreachable with an API key at all and the third
  should not be automated — the reasoning, with the code that enforces it, is
  written out in full at the bottom of ``install/render.py``. It reads the
  instance's real bootstrap state over the unauthenticated ``/auth/status``
  instead and prints next steps that are true of THAT instance.
* It does not overwrite ``.env``, and no flag makes it. ``--force`` reaches
  ``compose.yaml`` and ``rabbitmq.conf`` only. Losing ``ENCRYPTION_KEY``
  permanently destroys every stored warehouse credential.
* It does not capture the output of ``pull``/``up -d``. See ``install/shell.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from tripl_cli.commands import add_json, bounded_float, image_tag
from tripl_cli.commands._write import NOTHING_WRITTEN, confirm
from tripl_cli.config import Config, normalize_base_url
from tripl_cli.errors import EXIT_FAILURE, EXIT_OK, TriplConfigError, TriplError
from tripl_cli.install import docker, files, secrets
from tripl_cli.install.health import HealthOutcome, read_bootstrap, wait_for_health
from tripl_cli.install.plan import APPEND, CREATE, FileWrite, InstallPlan
from tripl_cli.install.render import (
    render_directory_header,
    render_health,
    render_health_timeout,
    render_install_plan,
    render_kept_settings,
    render_next_steps,
)
from tripl_cli.install.shell import Command, CommandResult, Runner, describe, subprocess_runner
from tripl_cli.model import JsonDict, to_rfc3339
from tripl_cli.report import install_document

# ./tripl, not the current directory (too easy to scribble into a repository),
# not /opt/tripl (needs root), not an XDG data dir (the operator has to be able
# to `cd` there and run `docker compose logs`).
DEFAULT_DIRECTORY = Path("tripl")

DEFAULT_WAIT_SECONDS = 300.0
MAX_WAIT_SECONDS = 3600.0

FLAG_APP_URL = "--app-url"


def default_runner() -> Runner:
    """The subprocess seam, resolved at call time so a test can replace it.

    Same trick ``commands.watch`` uses for ``default_clock``: the command is the
    only place a runner is constructed, so patching this one binding is what
    keeps every test in this repository from ever starting a container.
    """
    return subprocess_runner


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "install",
        parents=[parent],
        help="provision a tripl stack in a directory and start it",
        description=(
            "Write compose.yaml, infra/rabbitmq/rabbitmq.conf and a generated 0600 .env into "
            "a directory, then run `docker compose pull` and `docker compose up -d` in it and "
            "wait for /health. Re-running converges: an existing .env is never overwritten, "
            "and a compose.yaml you have edited is reported and kept. Needs Docker Engine "
            "with the Compose V2 plugin. Database data lives in the named volume pgdata18 "
            "and uploaded event photos in the named volume photos, not in this directory."
        ),
    )
    parser.add_argument(
        FLAG_APP_URL,
        dest="app_url",
        metavar="URL",
        required=True,
        help=(
            "public origin of the instance being created, e.g. https://tripl.example.com. "
            "Becomes APP_BASE_URL, which drives CORS and cookies - there is no default "
            "because a wrong value is invisible until nobody can sign in"
        ),
    )
    add_directory_flag(parser)
    parser.add_argument(
        "--version",
        dest="version",
        metavar="TAG",
        type=image_tag("--version"),
        # None rather than DEFAULT_VERSION, so PROVENANCE survives: on a re-run
        # against an existing .env we only tell the operator their tag was not
        # applied when they actually asked for one. The same distinction
        # `reject_instance_flags` draws from `Config.sources`.
        default=None,
        help=(
            f"image tag to pin in .env (default: {files.DEFAULT_VERSION}). Pin a released "
            "tag in production so a re-run cannot move you"
        ),
    )
    parser.add_argument(
        "--edition",
        dest="edition",
        choices=sorted(files.EDITION_IMAGES),
        # None rather than "community", for the same provenance reason as
        # --version: only a typed edition is reported back when .env keeps
        # another image.
        default=None,
        help=(
            "which image .env pins as TRIPL_IMAGE (default: community). The enterprise "
            "image is private: run `docker login ghcr.io` with your subscription's "
            "credentials first"
        ),
    )
    parser.add_argument(
        "--telemetry",
        dest="telemetry",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "anonymous usage telemetry: one small ping a day, no names, emails, hosts or "
            "data. Neither flag leaves the edition's default (on in community, off in "
            "enterprise); either writes TELEMETRY_ENABLED into the new .env. Asked once, "
            "here: an existing .env is never rewritten"
        ),
    )
    add_wait_flag(parser)
    parser.add_argument(
        "--no-start",
        dest="no_start",
        action="store_true",
        help="write the files and run nothing, for staging a machine",
    )
    parser.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help=(
            "replace a compose.yaml or rabbitmq.conf that differs from the packaged one. "
            "NEVER touches .env"
        ),
    )
    add_plan_flags(parser)
    parser.set_defaults(handler=run_install)


def add_directory_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dir",
        dest="directory",
        metavar="PATH",
        type=Path,
        default=DEFAULT_DIRECTORY,
        help=f"where the stack lives (default: ./{DEFAULT_DIRECTORY}); always reported absolute",
    )


def add_wait_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--wait",
        dest="wait",
        metavar="SECONDS",
        type=bounded_float("--wait", 0.0, MAX_WAIT_SECONDS),
        default=DEFAULT_WAIT_SECONDS,
        help=(
            f"how long to poll /health before giving up, 0 to skip waiting "
            f"(default: {DEFAULT_WAIT_SECONDS:g})"
        ),
    )


def add_plan_flags(parser: argparse.ArgumentParser) -> None:
    """``--dry-run``/``--yes``/``--json``, spelled exactly as everywhere else."""
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="print the plan; write nothing, run nothing",
    )
    parser.add_argument(
        "--yes",
        dest="assume_yes",
        action="store_true",
        help="skip the confirmation prompt (required when stdin is not a terminal)",
    )
    add_json(parser)


def reject_instance_flags(config: Config, command: str) -> None:
    """``--url``/``--api-key`` are meaningless here; refuse rather than ignore.

    These two commands act on a DIRECTORY, not on a running instance, so a
    silently-ignored connection flag is exactly the failure ``_write.py`` argues
    against — the operator believes they targeted staging and did not.

    The test is on PROVENANCE, not on the value: ``Config.sources`` records
    ``"--url"`` only when the flag was given explicitly, so an ambient
    ``$TRIPL_BASE_URL`` exported for ``tripl doctor`` does not trip this.
    """
    given = [
        flag
        for key, flag in (("base_url", "--url"), ("api_key", "--api-key"))
        if config.sources.get(key) == flag
    ]
    if not given:
        return
    raise TriplConfigError(
        f"`tripl {command}` targets a directory, not a running instance, so "
        f"{' and '.join(given)} would be ignored. The public URL of the instance you are "
        f"creating is {FLAG_APP_URL}."
    )


def resolve_directory(raw: Path) -> Path:
    """Absolute, always — every message and every document names a real path."""
    return raw.expanduser().resolve()


def refuse_source_checkout(directory: Path, command: str) -> None:
    marker = files.looks_like_source_checkout(directory)
    if marker is None:
        return
    raise TriplConfigError(
        f"{directory} looks like a tripl source checkout ({marker} is there). "
        f"`tripl {command}` writes its own compose.yaml and would overwrite it. "
        "Pick an empty directory with --dir."
    )


def warn_if_world_readable(directory: Path) -> str | None:
    """Same shape as ``config.py:_warn_if_world_readable``. Warn, never chmod.

    Never chmod: the file is the operator's, they may have set that mode on
    purpose, and a tool that silently tightens permissions on files it did not
    create is a tool nobody trusts with ``--force``.

    A warning is the right answer only when this run adds nothing to the file.
    When it would append freshly generated secrets, see ``refuse_loose_append``.
    """
    path = directory / files.ENV_NAME
    if not path.exists():
        return None
    mode = files.world_readable_mode(path)
    if mode is None:
        return None
    return (
        f"tripl: warning: {path} is readable by other users (mode {mode:03o}) and holds "
        f"the database password and SECRET_KEY; chmod 600 {path}"
    )


def refuse_loose_append(appending: FileWrite | None) -> None:
    """Never write a NEW secret into a file other users can already read.

    Warning and appending anyway was the one option that is not defensible: the
    secret is disclosed the moment it lands, and no later ``chmod`` un-discloses
    it. Refusing rather than tightening the mode ourselves keeps rule 2 of
    ``install/files.py`` intact - the file is the operator's - and costs them one
    ``chmod`` they can see.
    """
    if appending is None or not appending.secret:
        return
    mode = files.world_readable_mode(appending.path)
    if mode is None:
        return
    raise TriplConfigError(
        f"{appending.path} is readable by other users (mode {mode:03o}), and this run would "
        f"append freshly generated secrets to it ({', '.join(appending.keys)}). Refusing: a "
        "secret written into a world-readable file is disclosed the moment it is written, and "
        f"no later chmod undoes that. Run `chmod 600 {appending.path}` and try again. Nothing "
        "was written."
    )


def compose_commands(directory: Path) -> tuple[Command, ...]:
    """``pull`` then ``up -d``, in the install directory, with NO ``-f``.

    No ``-f compose.yaml``: compose's own discovery then finds a
    ``compose.override.yaml`` the operator added for TLS or a different port. An
    explicit ``-f`` would silently ignore it, and an install that quietly drops
    the operator's override is a data-plane change nobody was told about.

    ``cwd`` rather than ``--project-directory`` for the same reason: it is what
    makes compose read ``<dir>/.env`` and derive the project name from the
    directory basename, identical to what the operator does by hand.
    """
    return (
        Command((docker.DOCKER, "compose", "pull"), cwd=directory),
        Command((docker.DOCKER, "compose", "up", "-d"), cwd=directory),
    )


def require_docker(runner: Runner) -> None:
    """Probe before writing anything. Echo the daemon's own stderr verbatim."""
    found = docker.detect(runner)
    if found.usable:
        return
    message = found.problem or "docker is not usable"
    if found.detail.strip():
        indented = "\n".join(f"  docker: {line}" for line in found.detail.strip().splitlines())
        message = f"{message}\n{indented}"
    raise TriplConfigError(message)


def run_commands(
    commands: tuple[Command, ...], runner: Runner, *, human: TextIO
) -> tuple[list[int | None], CommandResult | None]:
    """Echo, then run INHERITED. Stops at the first failure and returns it.

    The exact invocation is printed before it runs so "the command above is safe
    to re-run by hand" is a true statement rather than a comforting one.
    """
    codes: list[int | None] = []
    for command in commands:
        print(describe(command), file=human)
        result = runner(command, False)
        codes.append(result.returncode)
        if not result.ok:
            return codes, result
    return codes, None


def command_failure(result: CommandResult) -> TriplError:
    return TriplError(
        f"`{' '.join(result.argv)}` failed (exit {result.returncode}). Its output is "
        "above; the command above is safe to re-run by hand."
    )


def emit(document: JsonDict) -> None:
    json.dump(document, sys.stdout)
    sys.stdout.write("\n")


def run_install(args: argparse.Namespace, config: Config) -> int:
    as_json: bool = bool(args.as_json)
    dry_run: bool = bool(args.dry_run)
    start: bool = not bool(args.no_start)
    wait_seconds: float = float(args.wait)
    human: TextIO = sys.stderr if as_json else sys.stdout
    started = time.monotonic()
    generated_at = datetime.now(UTC)
    runner = default_runner()

    reject_instance_flags(config, "install")
    directory = resolve_directory(args.directory)
    refuse_source_checkout(directory, "install")
    requested_url = normalize_base_url(str(args.app_url), FLAG_APP_URL)
    requested_version = files.DEFAULT_VERSION if args.version is None else str(args.version)
    requested_image = (
        files.DEFAULT_IMAGE if args.edition is None else files.EDITION_IMAGES[str(args.edition)]
    )

    values = secrets.generate(secrets.default_rng())
    writes = files.plan_writes(
        directory,
        app_base_url=requested_url,
        image=requested_image,
        version=requested_version,
        generated_at=generated_at,
        values=values,
        force=bool(args.force),
        telemetry=args.telemetry,
    )
    # What will be TRUE when this finishes, which on a re-run is the existing
    # .env's values - that file is never overwritten. Everything downstream (the
    # plan, the health poll, the next steps, the --json document) uses these, so
    # there is no path by which the command reports one URL and polls another.
    settings = files.plan_settings(
        directory,
        app_base_url=requested_url,
        image=requested_image,
        version=requested_version,
        # --app-url is required, so it is always a real request. --version and
        # --edition (TRIPL_IMAGE) are one only when they were typed.
        explicit=(
            "APP_BASE_URL",
            *((files.VERSION_KEY,) if args.version is not None else ()),
            *(("TRIPL_IMAGE",) if args.edition is not None else ()),
        ),
    )
    effective = {setting.name: setting.effective for setting in settings}
    app_base_url = effective["APP_BASE_URL"].rstrip("/")
    plan = InstallPlan(
        directory=directory,
        app_base_url=app_base_url,
        image=effective["TRIPL_IMAGE"],
        version=effective[files.VERSION_KEY],
        writes=writes,
        commands=compose_commands(directory) if start else (),
        wait_seconds=wait_seconds,
        start=start,
        dry_run=dry_run,
        secrets_generated=tuple(
            name for write in writes if write.secret for name in write.keys if name in values
        ),
        settings=settings,
    )

    print(render_directory_header("install", directory), file=human)
    print(file=human)
    print(render_install_plan(plan), file=human)
    # Before every other warning: "your flag did nothing" outranks "your tag is
    # not pinned", and an operator who reads only the first line must read this.
    kept = render_kept_settings(plan)
    if kept:
        print(kept, file=sys.stderr)
    if plan.version == files.DEFAULT_VERSION:
        # On stderr, so the plan on stdout stays a stable artifact. Not a
        # refusal: `latest` is the right default for an evaluation and the wrong
        # one for a production box, and only the operator knows which this is.
        print(
            f"tripl: note: TRIPL_VERSION={files.DEFAULT_VERSION} follows every release. In "
            "production pin a released tag (`--version 1.5.0`) so a re-run cannot move you "
            "onto an image you have not read the notes for.",
            file=sys.stderr,
        )
    if plan.image == files.ENTERPRISE_IMAGE:
        # Not checked here: the CLI holds no registry credentials, and a pull
        # without them fails in `docker compose pull` with Docker's own words.
        print(
            f"tripl: note: {files.ENTERPRISE_IMAGE} is private. Run `docker login ghcr.io` "
            "with the credentials that came with your subscription before the pull.",
            file=sys.stderr,
        )
    creating_env = any(
        write.action == CREATE and write.path.name == files.ENV_NAME for write in writes
    )
    telemetry_on = args.telemetry is True or (
        args.telemetry is None and plan.image != files.ENTERPRISE_IMAGE
    )
    if creating_env and telemetry_on:
        print(
            "tripl: note: anonymous usage telemetry is on: one small ping a day, no names, "
            f"emails, hosts or data ({files.TELEMETRY_DOCS_URL}). Turn it off with "
            "TELEMETRY_ENABLED=false in .env, or re-run with --no-telemetry before the "
            ".env exists.",
            file=sys.stderr,
        )
    if plan.insecure_scheme:
        # Loud, and not a refusal: an operator terminating TLS elsewhere or
        # evaluating on a laptop has a legitimate http:// origin.
        print(
            f"tripl: warning: {app_base_url} is plain HTTP. The stack forces "
            "SESSION_COOKIE_SECURE=true, so a browser will not store the session cookie "
            "over http - Safari refuses it even on localhost - and nobody will stay "
            "signed in. Put TLS in front of :8000 and re-run with an https URL.",
            file=sys.stderr,
        )

    if dry_run:
        print(file=human)
        print("dry run: nothing was written and nothing was run.", file=human)
        if as_json:
            emit(
                install_document(
                    plan,
                    generated_at=to_rfc3339(generated_at),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    results=[],
                    health=None,
                    bootstrap=None,
                    exit_code=EXIT_OK,
                )
            )
        return EXIT_OK

    appending = next((write for write in writes if write.action == APPEND), None)
    # Refuse before anything is probed or written; warn only when this run adds
    # nothing to a loose file.
    refuse_loose_append(appending)
    warning = warn_if_world_readable(directory)
    if warning is not None:
        print(warning, file=sys.stderr)
    if start:
        require_docker(runner)

    if appending is not None:
        confirm(
            f"Append {len(appending.keys)} generated values to {appending.path} "
            f"({', '.join(appending.keys)})? Nothing already in the file is changed.",
            assume_yes=bool(args.assume_yes),
            consequence=NOTHING_WRITTEN,
        )

    directory.mkdir(parents=True, exist_ok=True)
    files.apply(writes)

    codes: list[int | None] = []
    health: HealthOutcome | None = None
    bootstrap: JsonDict | None = None
    exit_code = EXIT_OK
    failure: CommandResult | None = None
    if start:
        print(file=human)
        codes, failure = run_commands(plan.commands, runner, human=human)
        if failure is not None:
            exit_code = EXIT_FAILURE
        else:
            health = wait_for_health(app_base_url, deadline_seconds=wait_seconds)
            print(file=human)
            print(render_health(health, app_base_url), file=human)
            if health.skipped:
                # NO CONNECTION AT ALL. `--wait 0` is what the docs prescribe
                # when the public URL is not reachable yet - a proxy still to be
                # brought up in front of :8000 - so reading /auth/status here
                # bought a ten-second stall against a URL the operator has just
                # told us not to try, and then printed a "could not be read"
                # line about an attempt nobody asked for.
                print(file=human)
                print(render_next_steps(None, app_base_url, probed=False), file=human)
            elif health.ok:
                fetched = read_bootstrap(app_base_url)
                bootstrap = fetched.value if fetched.ok else None
                print(file=human)
                print(render_next_steps(bootstrap, app_base_url), file=human)
            else:
                print(render_health_timeout(directory, app_base_url), file=human)
                exit_code = EXIT_FAILURE

    if as_json:
        emit(
            install_document(
                plan,
                generated_at=to_rfc3339(generated_at),
                duration_ms=int((time.monotonic() - started) * 1000),
                results=codes,
                health=health,
                bootstrap=bootstrap,
                exit_code=exit_code,
            )
        )
    if failure is not None:
        raise command_failure(failure)
    return exit_code
