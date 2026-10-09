"""What lands on disk, and the rules that keep it from destroying anything.

Three files, and only three: ``compose.yaml``, ``infra/rabbitmq/rabbitmq.conf``
and ``.env``. There is deliberately NO data directory — Postgres lives in the
named volume ``pgdata18`` and uploaded event photos in the named volume
``photos`` (both in compose.yaml), so an operator who backs up the install
directory has backed up the configuration and none of the data.

``infra/rabbitmq/rabbitmq.conf`` is not optional and is the whole reason a
hand-copied ``compose.yaml`` is not enough: the compose file bind-mounts it, and
Docker's response to a missing bind-mount SOURCE is to create a DIRECTORY at
that path, after which RabbitMQ fails to start with an error that names neither
tripl nor the mount.

THE SAFETY RULES, in one place:

1. ``.env`` is never overwritten. It is created, or appended to with the
   operator's confirmation, or left alone. ``--force`` does not reach it, and
   there is no flag that does: losing ``ENCRYPTION_KEY`` permanently destroys
   every stored warehouse and alert-destination credential.
2. ``.env`` and ``.env.bak.*`` are created at 0600 BY ``os.open``. Never
   ``open()`` then ``chmod`` — that leaves a window, however short, in which
   the database password and SECRET_KEY are world-readable.
3. A ``compose.yaml`` that differs from ours is the operator's, not ours. It is
   reported and kept; ``--force`` replaces it.
4. The version pin is rewritten by copy-then-replace, preserving every other
   byte: comments, blank lines, key order and the trailing newline.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from importlib.resources import files as resource_files
from pathlib import Path

from tripl_cli.errors import TriplError
from tripl_cli.install.plan import (
    APPEND,
    CREATE,
    KEPT,
    REPLACE,
    UNCHANGED,
    FileWrite,
    SettingOutcome,
)
from tripl_cli.install.secrets import REQUIRED_SECRETS, REQUIRED_SETTINGS
from tripl_cli.model import to_rfc3339

# The published image, and the tag `tripl upgrade --to` moves. Same defaults
# compose.yaml carries in its own `${TRIPL_IMAGE:-...}` fallbacks; written out
# explicitly in the generated .env so `tripl upgrade` has a line to rewrite.
DEFAULT_IMAGE = "ghcr.io/tripl-io/tripl"
# The Enterprise image is private: pulling it needs `docker login ghcr.io` with
# the credentials that come with a subscription.
ENTERPRISE_IMAGE = "ghcr.io/tripl-io/tripl-enterprise"
# The image each `--edition` runs. `.env` records the image, not the edition.
EDITION_IMAGES = {"community": DEFAULT_IMAGE, "enterprise": ENTERPRISE_IMAGE}
DEFAULT_VERSION = "latest"

VERSION_KEY = "TRIPL_VERSION"

COMPOSE_NAME = "compose.yaml"
RABBITMQ_NAME = "rabbitmq.conf"
RABBITMQ_RELATIVE = Path("infra") / "rabbitmq" / RABBITMQ_NAME
ENV_NAME = ".env"

# 0600 for the file holding the database password, SECRET_KEY and the Fernet
# key; 0644 for the two that are already public in a git repository.
MODE_PRIVATE = 0o600
MODE_PUBLIC = 0o644

CONFIG_DOCS_URL = "https://docs.tripl.io/run/configuration"
TELEMETRY_DOCS_URL = "https://docs.tripl.io/run/telemetry"


def packaged(name: str) -> str:
    """Read one packaged asset out of the wheel, byte for byte.

    ``importlib.resources`` rather than ``Path(__file__).parent``: the latter is
    a lie in a zipimport or a frozen build, and this is the one place the CLI
    reads its own installed data.
    """
    return (resource_files("tripl_cli.install") / "files" / name).read_text(encoding="utf-8")


def looks_like_source_checkout(directory: Path) -> str | None:
    """Name the marker that says "do not scribble here", or None.

    ``tripl install`` writes its own ``compose.yaml``. Pointed at a clone of this
    repository it would land next to the real one, and with ``--force`` on top of
    it. Refusing costs an operator one ``--dir`` flag; not refusing costs them a
    diff they did not ask for in a tree they may not have committed.
    """
    for marker in (".git", Path("backend") / "src" / "tripl"):
        if (directory / marker).exists():
            return str(marker)
    return None


def world_readable_mode(path: Path) -> int | None:
    """The mode, when others can read this file. ``None`` when they cannot.

    Skipped on Windows, where ``st_mode`` carries no such meaning — the same
    carve-out ``config.py:_warn_if_world_readable`` makes, for the same reason.
    """
    if sys.platform == "win32":
        return None
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:  # pragma: no cover - the caller has just read the file
        return None
    return mode if mode & 0o077 else None


def parse_env(text: str) -> dict[str, str]:
    """``KEY=value`` lines to a mapping. Comments, blanks and junk ignored.

    Deliberately NOT a dotenv implementation: no quote stripping, no
    interpolation, no ``export``. The only two questions asked of the result are
    "is this key present and non-empty" and "what is the current
    ``TRIPL_VERSION``", and answering more than that would mean owning a parser
    that has to agree with Compose's.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        values[name.strip()] = value.strip()
    return values


def missing_keys(text: str, required: Sequence[str]) -> tuple[str, ...]:
    """Which required names the file does not define, or defines as empty."""
    present = parse_env(text)
    return tuple(name for name in required if not present.get(name))


# Characters that would end our KEY=value line and begin a line we did not
# write. NUL is in the set because it truncates the file for several readers.
_LINE_BREAKERS = ("\n", "\r", "\x00")


def env_line(name: str, value: str) -> str:
    """One ``KEY=value`` line, or a refusal. The renderer's own last line of defence.

    ``config.normalize_base_url`` already rejects control characters in
    ``--app-url``, and it is the only caller that takes a value from an operator
    today. This check exists anyway because "the caller validated it" is exactly
    the assumption that stops being true when a second caller appears: a value
    carrying a newline does not corrupt this file, it ADDS settings to it, and
    the file is the one Docker Compose interpolates into every container. A
    renderer that trusts its input for that is a renderer with a hole in it.
    """
    breaker = next((ch for ch in _LINE_BREAKERS if ch in value), None)
    if breaker is not None:
        raise TriplError(
            f"refusing to write {name} into {ENV_NAME}: the value contains {breaker!r}, "
            "which would end the line and turn the rest of it into settings of its own. "
            "Nothing was written."
        )
    return f"{name}={value}"


def render_env(
    *,
    app_base_url: str,
    image: str,
    version: str,
    generated_at: datetime,
    values: Mapping[str, str],
    telemetry: bool | None = None,
) -> str:
    """The generated ``.env``, in full.

    Deliberately NOT a copy of ``.env.example``. That file is the BACKEND
    DEVELOPMENT template: it is full of ``localhost`` URLs and ``PHOTO_*`` /
    ``VITE_*`` keys that compose.yaml never reads, and an operator who finds
    ``DATABASE_URL=...@localhost:5432`` in their production .env reasonably
    concludes it is live. It is not — the app container receives only the keys
    compose.yaml lists in its ``environment:`` map, which is precisely the set
    written here.

    ``generated_at`` is injected rather than read from the clock so the golden
    test in ``tests/test_install_files.py`` is byte-exact.
    """
    lines = [
        f"# tripl instance configuration, generated by `tripl install` on "
        f"{to_rfc3339(generated_at)}.",
        "#",
        "# Docker Compose reads this file to INTERPOLATE compose.yaml. The containers receive",
        "# only the variables compose.yaml lists in its environment map, so a variable added",
        "# here that compose.yaml does not mention reaches nothing. Every tunable is listed at",
        f"# {CONFIG_DOCS_URL}",
        "#",
        "# This file holds live secrets: mode 600, and it must stay out of version control.",
        "# Back up ENCRYPTION_KEY separately from the database - warehouse and alert-",
        "# destination credentials are encrypted with it and cannot be recovered without it.",
        "",
        "# Public origin of this instance. Drives CORS, and must be the exact origin a browser",
        "# reaches. The stack forces SESSION_COOKIE_SECURE=true, so this should be https.",
        env_line("APP_BASE_URL", app_base_url),
        "",
        "# Released image and tag. `tripl upgrade --to X.Y.Z` moves the tag.",
        env_line("TRIPL_IMAGE", image),
        env_line(VERSION_KEY, version),
        "",
        "# Generated secrets - do not edit by hand.",
        "# ENCRYPTION_KEY is a Fernet key: 32 random bytes, url-safe base64.",
    ]
    lines.extend(env_line(name, values[name]) for name in REQUIRED_SECRETS)
    lines.extend(
        [
            "",
            "# Anonymous usage telemetry: one small ping a day, no names, emails, hosts or",
            f"# data. What it holds: {TELEMETRY_DOCS_URL}",
            "# Unset, it is on in the community edition and off in enterprise.",
            env_line("TELEMETRY_ENABLED", "true" if telemetry else "false")
            if telemetry is not None
            else "# TELEMETRY_ENABLED=false",
        ]
    )
    return "\n".join(lines) + "\n"


def _append_block(existing: str, pairs: Sequence[tuple[str, str]], generated_at: datetime) -> str:
    """The text appended to an incomplete ``.env``. Never reorders, never rewrites.

    A leading newline when the file does not end in one: that completes the
    operator's last line rather than joining our first key onto it, which is the
    only way "every pre-existing line is byte-identical" can be true.
    """
    prefix = "" if existing.endswith("\n") or not existing else "\n"
    lines = [
        "",
        f"# Appended by `tripl install` on {to_rfc3339(generated_at)}: required by",
        "# compose.yaml and missing from this file. Nothing above was changed.",
    ]
    lines.extend(env_line(name, value) for name, value in pairs)
    return prefix + "\n".join(lines) + "\n"


def plan_env(
    directory: Path,
    *,
    app_base_url: str,
    image: str,
    version: str,
    generated_at: datetime,
    values: Mapping[str, str],
    telemetry: bool | None = None,
) -> FileWrite:
    """Create it, append to it, or leave it entirely alone. Never overwrite."""
    path = directory / ENV_NAME
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return FileWrite(
            path=path,
            action=CREATE,
            mode=MODE_PRIVATE,
            content=render_env(
                app_base_url=app_base_url,
                image=image,
                version=version,
                generated_at=generated_at,
                values=values,
                telemetry=telemetry,
            ),
            keys=tuple(REQUIRED_SETTINGS) + tuple(REQUIRED_SECRETS),
            secret=True,
        )

    supplied = {
        "APP_BASE_URL": app_base_url,
        "TRIPL_IMAGE": image,
        VERSION_KEY: version,
        **values,
    }
    absent = missing_keys(existing, (*REQUIRED_SETTINGS, *REQUIRED_SECRETS))
    if not absent:
        return FileWrite(path=path, action=KEPT, mode=MODE_PRIVATE, note="complete")
    return FileWrite(
        path=path,
        action=APPEND,
        mode=MODE_PRIVATE,
        content=_append_block(existing, [(name, supplied[name]) for name in absent], generated_at),
        keys=absent,
        secret=True,
    )


def plan_settings(
    directory: Path,
    *,
    app_base_url: str,
    image: str,
    version: str,
    explicit: Sequence[str] = (),
) -> tuple[SettingOutcome, ...]:
    """What the three non-secret settings will BE, next to what was asked for.

    Read from the existing ``.env`` rather than from the flags, because that
    file is never overwritten (rule 1 in the module docstring) and so it — not
    the command line — decides what the stack runs with. Reporting the flag
    value on a re-run told an operator changing ``--app-url`` that it had worked
    when the only thing that changed was the sentence they were reading.

    ``explicit`` names the settings a FLAG asked for, so a default nobody typed
    is never reported back as a request that was refused.
    """
    requested = {"APP_BASE_URL": app_base_url, "TRIPL_IMAGE": image, VERSION_KEY: version}
    try:
        existing = parse_env((directory / ENV_NAME).read_text(encoding="utf-8"))
    except OSError:
        existing = {}
    return tuple(
        SettingOutcome(
            name=name,
            requested=value,
            # An empty value counts as absent, exactly as `missing_keys` treats
            # it - a `KEY=` line satisfies neither compose's `${KEY:?}` nor us.
            effective=existing.get(name) or value,
            kept=bool(existing.get(name)),
            explicit=name in explicit,
        )
        for name, value in requested.items()
    )


def _plan_asset(path: Path, asset: str, *, force: bool, generated_at: datetime) -> FileWrite:
    body = packaged(asset)
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return FileWrite(path=path, action=CREATE, mode=MODE_PUBLIC, content=body)
    if existing == body:
        return FileWrite(path=path, action=UNCHANGED, mode=MODE_PUBLIC)
    if force:
        # The file differs, so replacing it discards work somebody did by hand -
        # a TLS tweak, a changed port. `upgrade` has always kept a timestamped
        # copy of the .env before rewriting the pin; keeping one here makes
        # --force recoverable by the same move, and the name goes in the note so
        # the operator can diff it without being told where to look.
        backup = backup_path(path, generated_at)
        return FileWrite(
            path=path,
            action=REPLACE,
            mode=MODE_PUBLIC,
            content=body,
            note=f"yours kept as {backup.name}",
            backup=backup,
        )
    return FileWrite(
        path=path,
        action=KEPT,
        mode=MODE_PUBLIC,
        note="differs from the version this CLI ships",
    )


def plan_writes(
    directory: Path,
    *,
    app_base_url: str,
    image: str,
    version: str,
    generated_at: datetime,
    values: Mapping[str, str],
    force: bool = False,
    telemetry: bool | None = None,
) -> tuple[FileWrite, ...]:
    """The three files, in the order they are reported and applied."""
    return (
        _plan_asset(directory / COMPOSE_NAME, COMPOSE_NAME, force=force, generated_at=generated_at),
        _plan_asset(
            directory / RABBITMQ_RELATIVE, RABBITMQ_NAME, force=force, generated_at=generated_at
        ),
        plan_env(
            directory,
            app_base_url=app_base_url,
            image=image,
            version=version,
            generated_at=generated_at,
            values=values,
            telemetry=telemetry,
        ),
    )


def _create(path: Path, body: str, mode: int) -> None:
    """Exclusive create with the mode ON THE OPEN. See rule 2 in the docstring."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(body)


def _replace_atomically(path: Path, body: str, mode: int) -> None:
    """Write beside the target, then rename over it.

    ``os.replace`` is atomic within a directory, so a reader (compose, say)
    never sees a half-written file and an interrupted run never leaves a
    truncated one.
    """
    temporary = path.with_name(f"{path.name}.tripl-{os.getpid()}.tmp")
    try:
        _create(temporary, body, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _append(path: Path, body: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(body)


def apply(writes: Iterable[FileWrite]) -> None:
    """Perform every mutating write. The no-op actions open nothing at all.

    Every ``OSError`` becomes a ``TriplError`` naming the path. A traceback out
    of ``os.open`` would tell an operator whose ``/srv`` is read-only that the
    tool crashed rather than that their directory is not writable — and the
    exclusive create can also lose a race with a second ``tripl install``, which
    is a real thing to say rather than a ``FileExistsError`` to decode.
    """
    for write in writes:
        if not write.mutates:
            continue
        try:
            write.path.parent.mkdir(parents=True, exist_ok=True)
            if write.action == CREATE:
                _create(write.path, write.content, write.mode)
            elif write.action == REPLACE:
                if write.backup is not None:
                    # O_EXCL, like the .env backup: a second --force in the same
                    # second must not overwrite the only copy of what it already
                    # replaced. Before the replace, so a failure here leaves the
                    # operator's file exactly where it was.
                    _create(write.backup, write.path.read_text(encoding="utf-8"), write.mode)
                _replace_atomically(write.path, write.content, write.mode)
            else:
                _append(write.path, write.content)
        except OSError as exc:
            raise TriplError(f"could not write {write.path}: {exc}") from exc


def backup_path(path: Path, stamp: datetime) -> Path:
    """``<name>.bak.20260801T120000Z`` — sortable, unambiguous, no colons."""
    return path.with_name(f"{path.name}.bak.{stamp.strftime('%Y%m%dT%H%M%SZ')}")


def backup_name(stamp: datetime) -> str:
    """``.env.bak.20260801T120000Z``, the one this repo's messages name by hand."""
    return backup_path(Path(ENV_NAME), stamp).name


def current_version(env_text: str) -> str | None:
    """The ``TRIPL_VERSION=`` pin, or None when the file does not carry one."""
    return parse_env(env_text).get(VERSION_KEY) or None


def rewrite_version(env_path: Path, target: str, *, stamp: datetime) -> Path:
    """Move the pin, preserving every other byte. Returns the backup's path.

    The backup is taken FIRST and at 0600, because the next statement replaces
    the only copy of the operator's ENCRYPTION_KEY. If ``TRIPL_VERSION`` is
    absent the line is appended rather than the file rewritten — the operator
    may be pinning through their own ``compose.override.yaml``, and we still owe
    them every other byte unchanged.
    """
    existing = env_path.read_text(encoding="utf-8")
    backup = env_path.with_name(backup_name(stamp))
    try:
        # O_EXCL, so a backup from a second upgrade in the same second is a
        # refusal rather than an overwrite. Clobbering the previous file would
        # destroy the only remaining copy of the pin we are about to move away
        # from - the safe direction here is always "stop".
        _create(backup, existing, MODE_PRIVATE)
    except OSError as exc:
        raise TriplError(
            f"could not write {backup}: {exc}. Nothing was changed - {env_path} still "
            "holds the current pin."
        ) from exc

    lines = existing.splitlines(keepends=True)
    replaced = False
    for index, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == VERSION_KEY:
            ending = "\n" if line.endswith("\n") else ""
            lines[index] = env_line(VERSION_KEY, target) + ending
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(env_line(VERSION_KEY, target) + "\n")
    _replace_atomically(env_path, "".join(lines), MODE_PRIVATE)
    return backup
