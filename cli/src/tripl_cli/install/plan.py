"""What the command is about to do, as a VALUE.

``--dry-run`` prints this object and the executor consumes the same one, so the
plan an operator reviews and the plan that runs cannot diverge — the failure
mode of every "print what I would do" implemented as a second code path. The
tests assert on it directly for the same reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from tripl_cli.install.shell import Command

# What ``apply`` will do with one file. These strings are the ``action`` values
# in the --json document, so they are a contract: never renamed, never reused.
CREATE = "create"
REPLACE = "replace"
APPEND = "append"
UNCHANGED = "unchanged"
KEPT = "kept"

# The actions that actually open the file for writing.
MUTATING = frozenset({CREATE, REPLACE, APPEND})


@dataclass(frozen=True)
class FileWrite:
    """One file, and what will happen to it.

    ``content`` is the whole file for ``create``/``replace`` and only the
    appended block for ``append``; it is empty for the two no-op actions.
    ``secret`` marks the payload as unprintable — the renderers and the JSON
    document reach for ``keys`` instead, which holds NAMES only.
    """

    path: Path
    action: str
    mode: int
    content: str = ""
    note: str = ""
    keys: tuple[str, ...] = ()
    secret: bool = False
    # Where the file being REPLACED is copied first. Set for `replace` only;
    # `upgrade` has always kept a timestamped copy of the .env it rewrites, and
    # a `--force` that discarded an operator's edited compose.yaml without one
    # was the same command promising two different things.
    backup: Path | None = field(default=None)

    @property
    def mutates(self) -> bool:
        return self.action in MUTATING

    @property
    def sets_mode(self) -> bool:
        """True only where ``apply`` opens the file WITH a mode.

        ``append`` reuses the descriptor's existing permissions and the two
        no-op actions open nothing, so ``mode`` is meaningless on all three.
        Both the human table and the --json document ask this rather than
        printing ``mode`` unconditionally - see report._file_document.
        """
        return self.action in (CREATE, REPLACE)


@dataclass(frozen=True)
class SettingOutcome:
    """One ``.env`` setting: what the flags asked for, and what will be TRUE.

    They differ whenever a re-run names a value the existing ``.env`` already
    defines, because ``.env`` is never overwritten - it holds live secrets. The
    plan, the human output and the --json document all report ``effective``, so
    an operator who re-runs ``install`` to change the app URL is told it did not
    happen instead of being congratulated.
    """

    name: str
    requested: str
    effective: str
    # True when the existing file's value won.
    kept: bool
    # True when a FLAG asked for ``requested``. False for a default nobody
    # typed - ``--version`` left alone resolves to ``latest``, and telling an
    # operator that "the requested latest was NOT applied" on a re-run they
    # gave no tag to would be a warning about their own inaction.
    explicit: bool = False

    @property
    def applied(self) -> bool:
        """Would an operator reading ``requested`` back get it? Not if kept and different."""
        return self.effective == self.requested

    @property
    def ignored(self) -> bool:
        """Somebody asked for this value with a flag, and it did not take."""
        return self.explicit and not self.applied


@dataclass(frozen=True)
class InstallPlan:
    directory: Path
    # EFFECTIVE values throughout - what the stack will run with once this
    # command finishes, not what was typed. See SettingOutcome.
    app_base_url: str
    image: str
    version: str
    writes: tuple[FileWrite, ...]
    commands: tuple[Command, ...]
    wait_seconds: float
    # False under --no-start: write the files, run nothing. For an operator
    # staging a machine that is not meant to come up yet.
    start: bool
    dry_run: bool
    # NAMES of the secrets this run would generate. Never the values.
    secrets_generated: tuple[str, ...] = ()
    settings: tuple[SettingOutcome, ...] = ()

    @property
    def insecure_scheme(self) -> bool:
        return self.app_base_url.startswith("http://")

    @property
    def ignored_requests(self) -> tuple[SettingOutcome, ...]:
        """The flag values the existing ``.env`` overrode. Each one gets a warning."""
        return tuple(setting for setting in self.settings if setting.ignored)


# How the two versions compare. `unknown` is not a failure - it is what a tag
# like `latest` or `sha-abc1234` honestly produces, and it is why
# FLAG_ALLOW_UNORDERED is required in that case rather than an ordering being
# invented.
ORDER_SAME = "same"
ORDER_UPGRADE = "upgrade"
ORDER_DOWNGRADE = "downgrade"
ORDER_UNKNOWN = "unknown"

# The override for `unknown`, next to the ordering it overrides, because two
# commands name it: `tripl upgrade` defines the flag, and `tripl install` tells
# an operator to pass it when the pin a kept .env holds can only be moved with
# it. Not `--yes`: every non-interactive upgrade must pass `--yes` to answer the
# backup prompt, so a CI log showing `--yes` says "this run is automated" and
# nothing about downgrades.
FLAG_ALLOW_UNORDERED = "--allow-unordered-tag"

# A released tag: strict three-part semver, digits only. `1.4`, `v1.4.0` and
# `sha-abc1234` all fail it deliberately: a partial match would be an invented
# ordering.
RELEASE_TAG = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def compare(current: str, target: str) -> str:
    """How moving from ``current`` to ``target`` orders, or ``unknown`` when it does not."""
    if current == target:
        return ORDER_SAME
    left, right = RELEASE_TAG.fullmatch(current), RELEASE_TAG.fullmatch(target)
    if left is None or right is None:
        return ORDER_UNKNOWN
    low = tuple(int(part) for part in left.groups())
    high = tuple(int(part) for part in right.groups())
    return ORDER_UPGRADE if high > low else ORDER_DOWNGRADE


@dataclass(frozen=True)
class UpgradePlan:
    directory: Path
    current: str
    target: str
    ordering: str
    commands: tuple[Command, ...]
    wait_seconds: float
    dry_run: bool
    backup_command: str = ""
    env_backup: Path | None = field(default=None)
