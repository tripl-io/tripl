"""Human output for ``install`` and ``upgrade``. ASCII only, TTY-identical.

Same rules as ``tripl_cli/render.py`` — no colour, no spinners, no unicode —
and the same reason: ``tripl install | tee provision.log`` and the terminal view
must be the same artifact. The column padder is imported from there rather than
re-implemented so the two families' tables cannot drift apart by a space.

``render_header`` itself does NOT fit these two commands: it prints
``tripl <cmd> - <base_url> (from <source>)``, and neither of these has a
configured instance URL — that is the whole point of them. The header here names
the DIRECTORY instead, which is what both commands actually act on.
"""

from __future__ import annotations

from pathlib import Path

from tripl_cli.install.files import ENV_NAME
from tripl_cli.install.health import HealthOutcome
from tripl_cli.install.plan import (
    APPEND,
    CREATE,
    KEPT,
    REPLACE,
    UNCHANGED,
    FileWrite,
    InstallPlan,
    SettingOutcome,
    UpgradePlan,
)
from tripl_cli.install.secrets import REQUIRED_SECRETS
from tripl_cli.install.shell import describe
from tripl_cli.model import JsonDict
from tripl_cli.render import columns

# What each action is called in the human table. Deliberately the same tokens
# the --json document carries, so a support conversation and a `jq` filter use
# one vocabulary.
_ACTION_LABELS = {
    CREATE: "create",
    REPLACE: "replace",
    APPEND: "append",
    UNCHANGED: "unchanged",
    KEPT: "kept",
}


def render_directory_header(command: str, directory: Path) -> str:
    return f"tripl {command} - {directory}"


def _file_row(write: FileWrite, root: Path) -> list[str]:
    try:
        name = str(write.path.relative_to(root))
    except ValueError:  # pragma: no cover - every planned write is under root
        name = str(write.path)
    label = _ACTION_LABELS[write.action]
    mode = f"{write.mode:04o}" if write.action in (CREATE, REPLACE) else ""
    note = f"({write.note})" if write.note else ""
    return [name, label, mode, note]


def render_files(writes: tuple[FileWrite, ...], root: Path) -> str:
    return "\n".join(f"  {line}" for line in columns([_file_row(w, root) for w in writes]))


def render_generated_names(writes: tuple[FileWrite, ...]) -> str:
    """Name the keys that were generated. NEVER a value, not once, not ever.

    A secret printed to a terminal is a secret in a scrollback buffer, in a
    ``tee`` log and in whatever the operator pastes into a ticket. There is a
    test that asserts none of the generated values appears in stdout, stderr or
    the --json document.
    """
    for write in writes:
        if not (write.secret and write.keys):
            continue
        if write.action == CREATE:
            # Only the SECRETS. The three settings are already on the two lines
            # above, with their values, and "values never printed" said of
            # APP_BASE_URL would be a plainly false sentence.
            names = [name for name in write.keys if name in REQUIRED_SECRETS]
            return f"generated into .env, values never printed: {', '.join(names)}"
        return f"appended to .env, values never printed: {', '.join(write.keys)}"
    return ""


def _kept_note(*settings: SettingOutcome | None) -> list[str]:
    """``(kept from .env)``, or nothing. A third cell only when there is one.

    ``columns`` rstrips, so an always-present empty cell would render the same
    bytes - but building the row honestly keeps the golden sample in
    tests/test_output_samples.py a two-column table, which is what it is.
    """
    kept = [setting.name for setting in settings if setting is not None and setting.kept]
    return [f"(kept from {ENV_NAME}: {', '.join(kept)})"] if kept else []


def render_install_plan(plan: InstallPlan) -> str:
    """The plan, printed identically whether or not it is about to be executed.

    Every value here is the EFFECTIVE one - what the stack will run with when
    the command finishes. On a re-run against an existing ``.env`` that is the
    file's value, not the flag's, and the row says so.
    """
    by_name = {setting.name: setting for setting in plan.settings}
    url, image, version = (
        by_name.get("APP_BASE_URL"),
        by_name.get("TRIPL_IMAGE"),
        by_name.get("TRIPL_VERSION"),
    )
    lines = [
        *columns(
            [
                ["app url", plan.app_base_url, *_kept_note(url)],
                ["image", f"{plan.image}:{plan.version}", *_kept_note(image, version)],
            ]
        ),
        "",
        "files",
        render_files(plan.writes, plan.directory),
    ]
    generated = render_generated_names(plan.writes)
    if generated:
        lines.append(f"  {generated}")
    if plan.start:
        lines.extend(["", "commands", *(f"  {describe(c)}" for c in plan.commands)])
        if plan.wait_seconds > 0:
            lines.append(f"  then poll {plan.app_base_url}/health for up to {plan.wait_seconds:g}s")
    else:
        lines.extend(["", "commands", "  (none: --no-start writes the files and starts nothing)"])
    return "\n".join(lines)


# What to do INSTEAD, per setting. `--force` is not on this list for any of them
# and never will be: it reaches compose.yaml and rabbitmq.conf only, because
# rewriting .env would destroy ENCRYPTION_KEY and with it every stored warehouse
# credential. Naming a flag that does not do the thing would be worse than
# saying nothing.
def _remedy(setting: SettingOutcome, directory: Path) -> str:
    if setting.name == "TRIPL_VERSION":
        return f"  To move the pin: tripl upgrade --to {setting.requested} --dir {directory}"
    return (
        f"  To change it: edit {directory / ENV_NAME}, then "
        f"`cd {directory} && docker compose up -d`."
    )


def render_kept_settings(plan: InstallPlan) -> str:
    """Name every flag value the existing ``.env`` overrode, and what to do about it.

    Silence here was the actual defect: a re-run with a new ``--app-url``
    printed the new URL everywhere, changed nothing, and exited 0.
    A warning rather than a refusal, because the rest of the
    re-run - converging compose.yaml, `pull`, `up -d` - is exactly what the
    operator asked for and still happens.
    """
    lines: list[str] = []
    for setting in plan.ignored_requests:
        lines.append(
            f"tripl: warning: {ENV_NAME} already sets {setting.name}={setting.effective}, and "
            f"`tripl install` never overwrites {ENV_NAME} (it holds ENCRYPTION_KEY), so the "
            f"requested {setting.requested} was NOT applied."
        )
        lines.append(_remedy(setting, plan.directory))
    return "\n".join(lines)


def render_upgrade_plan(plan: UpgradePlan) -> str:
    lines = [
        *columns(
            [
                ["from", plan.current],
                ["to", plan.target],
            ]
        ),
        "",
        "commands",
        *(f"  {describe(command)}" for command in plan.commands),
        f"  the {plan.target} pin is written to .env between the two, after the pull succeeds",
    ]
    if plan.wait_seconds > 0:
        lines.append(f"  then poll /health for up to {plan.wait_seconds:g}s")
    return "\n".join(lines)


def render_backup_gate(directory: Path, current: str) -> str:
    """The command the operator runs. We print it; we never run it.

    A ``pg_dump`` this CLI invoked and then called "your backup" would be a
    promise it cannot keep: it cannot know there is disk space, it cannot verify
    the dump, and the dump does NOT contain ENCRYPTION_KEY - without which every
    encrypted column in it is unreadable. Printing the command and refusing to
    proceed without an acknowledgement is the honest version.
    """
    return "\n".join(
        [
            f"backup   cd {directory} && docker compose exec -T postgres \\",
            f"           pg_dump -U tripl tripl | gzip > tripl-{current}.sql.gz",
            "         Take it now. This applies Alembic migrations, which are not reversible.",
            "         ENCRYPTION_KEY lives in .env, not in that dump - back it up separately.",
        ]
    )


def render_health(outcome: HealthOutcome, base_url: str) -> str:
    if outcome.skipped:
        # The REASON the outcome carries, never a guess. `upgrade` used to reach
        # this line with base_url="this instance" and print "not waiting for this
        # instance/health (--wait 0)." on a run where --wait was 300 and the real
        # problem was an .env with no APP_BASE_URL - a mangled sentence blaming
        # the wrong thing, twice over.
        target = f" for {base_url}/health" if base_url else ""
        return f"not waiting{target}: {outcome.skipped_reason}."
    if outcome.ok:
        return f"{base_url} is up after {outcome.waited_seconds:.0f}s."
    # The LAST probe's own words, not a summary of them: "HTTP 502" and
    # "ConnectError" send the reader to different logs, and a line that said
    # only "did not answer" would send them to both.
    reason = f" Last attempt: {outcome.last.error}" if outcome.last is not None else ""
    return (
        f"{base_url}/health did not answer within {outcome.waited_seconds:.0f}s "
        f"({outcome.attempts} attempts).{reason}"
    )


def render_health_timeout(directory: Path, base_url: str) -> str:
    """What to read next. The stack IS started - say so before the instructions."""
    return "\n".join(
        [
            "The stack was started; it just has not answered yet. A first run applies every",
            "Alembic revision before the app boots, which on a slow disk can outlast the",
            "default wait. Look here, in this order:",
            f"  cd {directory} && docker compose ps",
            f"  cd {directory} && docker compose logs migrate",
            f"  cd {directory} && docker compose logs app",
            f"  tripl doctor --url {base_url}",
        ]
    )


def render_upgrade_failure(directory: Path, target: str, backup: Path | None) -> str:
    """Why the pin was deliberately NOT rolled back."""
    lines = [
        "The `migrate` one-shot runs `alembic upgrade head` before app and workers start,",
        "so this most likely means the schema upgrade failed. Read it:",
        f"  cd {directory} && docker compose logs migrate",
        "",
        f"TRIPL_VERSION is left at {target} on purpose. Alembic applies revisions one at a",
        "time, so the schema may be partly upgraded; starting the old image against a",
        "partly-new schema would be worse than leaving the stack stopped. Fix the cause and",
        f"re-run `tripl upgrade --to {target}`, or restore the backup you took above.",
    ]
    if backup is not None:
        lines.append(f"The previous .env is at {backup.name}.")
    return "\n".join(lines)


# --- first-run next steps ---------------------------------------------------
#
# `tripl install` automates NONE of "owner account, data source, first scan",
# and that is a finding rather than a shortcut:
#
#   * A DATA SOURCE IS UNREACHABLE BY CONSTRUCTION. `POST /data-sources` depends
#     on `deps.get_owner_user`, which raises 403 whenever
#     `request.state.api_key_scope` is set - and every valid API key sets it. No
#     key of any scope, held by any role, can create one. Same wall that already
#     killed `tripl scans replay` (commands/scans.py).
#   * AN OWNER ACCOUNT IS REACHABLE AND STILL WRONG. Registering would mean
#     accepting a password on an argv (visible to ps(1), kept in shell history -
#     the hazard cli.py already warns about for --api-key), and the session
#     cookie the reply sets is `Secure`, so over a plain-http first run httpx's
#     jar discards it and the "automated" flow breaks silently.
#   * THE FIRST SCAN NEEDS THE DATA SOURCE. Unreachable by construction.
#
# What is printed instead is better than a static blurb: it is read from the
# instance's real bootstrap state over the unauthenticated /auth/status, so
# every sentence is a true statement about THIS instance. Screens are named
# ("Settings -> Data sources"), never deep-linked: that is the wording the admin
# guide already uses and it cannot rot when a SPA route changes.

_OWNER_ONLY_NOTE = (
    "  Settings -> Data sources   connect ClickHouse, BigQuery, Databricks, Snowflake,\n"
    "                             Redshift, Greenplum or PostgreSQL.\n"
    "                             Owner-only, and only from a browser session: an API key\n"
    "                             cannot reach this endpoint whatever its scope.\n"
    "  Settings -> API keys       create a tk_r_ key for the CLI."
)


def render_next_steps(bootstrap: JsonDict | None, base_url: str, *, probed: bool = True) -> str:
    """Next steps that are TRUE of this instance, or an honest generic form.

    ``probed=False`` says no request was made at all, which is a different
    sentence from "the request failed". Under ``--wait 0`` this command opens no
    connection to the public URL - that is the whole point of the flag, whose
    documented remedy is for an origin that is not reachable yet - so claiming
    the state "could not be read" would invent a failed attempt.
    """
    if bootstrap is None and not probed:
        opening = (
            f"Nothing was asked of {base_url}: --wait 0 means this command made no\n"
            f"connection to it. Open {base_url} and sign in; if it has no accounts yet,\n"
            "the first one you create becomes the owner."
        )
    elif bootstrap is None:
        # No path literal here on purpose: the route has exactly one home
        # (api/auth.STATUS) and a second spelling in a message is how a docs
        # page ends up naming an endpoint that moved.
        opening = (
            "The bootstrap state of this instance could not be read, so the next step\n"
            f"below is the general one. Open {base_url} and sign in; if it has no accounts\n"
            "yet, the first one you create becomes the owner."
        )
    elif not bootstrap.get("has_users"):
        opening = (
            f"This instance has no accounts yet. Open {base_url} and create the\n"
            "first one - it becomes the owner."
        )
    elif bootstrap.get("registration_enabled"):
        opening = f"This instance already has accounts; sign in at {base_url}."
    else:
        opening = (
            f"This instance already has accounts and registration is closed; sign in at\n"
            f"{base_url}, or ask an owner for an invitation "
            "(Settings -> Members -> Invite a member)."
        )
    return "\n".join(
        [
            opening,
            "Then, signed in as that owner:",
            _OWNER_ONLY_NOTE,
            f"Then: tripl doctor --url {base_url}",
        ]
    )
