"""A tag that was never published must cost one corrected re-run, not a hand edit.

The path this file pins: an operator pastes ``tripl install --version <tag>``
with a tag that does not exist. ``.env`` is written before ``docker compose
pull`` (compose cannot interpolate compose.yaml without it), the pull fails, and
then:

* a kept ``.env`` would keep the bad pin, because a re-run never rewrites it, so
  the corrected ``--version`` would be reported as NOT applied;
* the advice printed for a kept pin was ``tripl upgrade --to <tag>``, which
  refuses a downgrade outright and an unorderable pair without
  ``--allow-unordered-tag``.

So a pull that fails before ``up -d`` removes the ``.env`` this run created, and
the kept-pin advice follows the same ordering ``tripl upgrade`` enforces. Every
suggested tag is derived from the installed CLI, never typed.

NOTHING HERE STARTS A CONTAINER: every ``docker`` call goes to ``FakeRunner``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tripl_cli import __version__
from tripl_cli.cli import main
from tripl_cli.commands import install as install_cmd
from tripl_cli.install import docker, files, render
from tripl_cli.install.plan import FLAG_ALLOW_UNORDERED, SettingOutcome

from .conftest import FakeRunner

APP_URL = "https://tripl.example.com"
DOCKER_PRESENT = {"docker": "/usr/bin/docker", "docker-compose": None}


@pytest.fixture(autouse=True)
def docker_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend Docker Engine + Compose V2 are installed. Never touches a daemon."""
    monkeypatch.setattr(docker, "which", lambda name: DOCKER_PRESENT.get(name))


def argv(directory: Path, *extra: str) -> list[str]:
    return ["install", "--app-url", APP_URL, "--dir", str(directory), *extra]


# --- a failed pull on a fresh install ----------------------------------------


def test_a_failed_pull_removes_the_env_this_run_created(
    install_dir: Path,
    fake_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_runner.codes["pull"] = 1

    assert main(argv(install_dir, "--version", "9.9.9")) == 1

    assert not (install_dir / ".env").exists()
    # The two public files stay: they hold nothing that pins a choice, and a
    # re-run reports them unchanged.
    assert (install_dir / "compose.yaml").exists()
    assert (install_dir / files.RABBITMQ_RELATIVE).exists()
    assert ("docker", "compose", "up", "-d") not in fake_runner.argvs
    err = capsys.readouterr().err
    assert "Nothing was started" in err
    assert "ghcr.io/tripl-io/tripl:9.9.9 is not a published tag" in err
    assert f"--version {files.example_tag()}" in err
    # Not the generic advice: `docker compose pull` by hand would now fail on
    # the missing .env.
    assert "safe to re-run by hand" not in err


def test_the_retry_with_a_corrected_version_is_applied(
    install_dir: Path,
    fake_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_runner.codes["pull"] = 1
    assert main(argv(install_dir, "--version", "9.9.9")) == 1
    capsys.readouterr()

    assert main(argv(install_dir, "--version", "0.3.1", "--no-start")) == 0

    on_disk = files.parse_env((install_dir / ".env").read_text(encoding="utf-8"))
    assert on_disk[files.VERSION_KEY] == "0.3.1"
    assert "was NOT applied" not in capsys.readouterr().err


def test_the_json_document_says_the_env_was_removed(
    install_dir: Path,
    fake_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_runner.codes["pull"] = 1

    assert main(argv(install_dir, "--version", "9.9.9", "--json")) == 1

    document = json.loads(capsys.readouterr().out)
    env = next(entry for entry in document["files"] if entry["path"] == files.ENV_NAME)
    assert env["action"] == "create"
    assert env["note"] == install_cmd.ENV_WITHDRAWN_NOTE
    assert document["exit_code"] == 1
    assert [entry["returncode"] for entry in document["commands"]] == [1, None]


def test_a_kept_env_is_never_removed_by_a_failed_pull(
    install_dir: Path,
    fake_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only a file THIS run wrote. One staged earlier may already be in use."""
    assert main(argv(install_dir, "--version", "9.9.9", "--no-start")) == 0
    env = install_dir / ".env"
    before = env.read_bytes()
    capsys.readouterr()
    fake_runner.codes["pull"] = 1

    assert main(argv(install_dir)) == 1

    assert env.read_bytes() == before
    err = capsys.readouterr().err
    assert "safe to re-run by hand" in err
    assert "has been removed" not in err


def test_a_failed_up_never_removes_the_env(
    install_dir: Path,
    fake_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """After `up -d` Postgres may have initialised with the generated password."""
    fake_runner.codes["up"] = 1

    assert main(argv(install_dir, "--version", "9.9.9")) == 1

    assert (install_dir / ".env").exists()
    assert "has been removed" not in capsys.readouterr().err


# --- the advice for a pin a kept .env holds ----------------------------------


def _kept_version(effective: str, requested: str) -> SettingOutcome:
    return SettingOutcome(
        name=files.VERSION_KEY,
        requested=requested,
        effective=effective,
        kept=True,
        explicit=True,
    )


def test_a_newer_tag_is_moved_with_a_plain_upgrade() -> None:
    advice = render._remedy(_kept_version("0.3.0", "0.3.1"), Path("/srv/tripl"))
    assert advice == "  To move the pin: tripl upgrade --to 0.3.1 --dir /srv/tripl"


def test_an_unorderable_pair_names_the_flag_upgrade_demands() -> None:
    """`latest` -> X.Y.Z is the first upgrade of every default install."""
    advice = render._remedy(_kept_version("latest", "0.3.1"), Path("/srv/tripl"))
    assert advice == (
        f"  To move the pin: tripl upgrade --to 0.3.1 --dir /srv/tripl {FLAG_ALLOW_UNORDERED}"
    )


def test_an_older_tag_is_never_sent_to_a_command_that_refuses_it() -> None:
    advice = render._remedy(_kept_version("1.4.0", "0.3.1"), Path("/srv/tripl"))
    assert "tripl upgrade --to" not in advice
    assert "will not move the pin back to 0.3.1" in advice
    assert "edit TRIPL_VERSION in /srv/tripl/.env" in advice
    assert "restoring a backup" in advice


def test_the_kept_pin_warning_on_a_real_re_run_carries_the_downgrade_advice(
    install_dir: Path,
    fake_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(argv(install_dir, "--version", "1.4.0", "--no-start")) == 0
    capsys.readouterr()

    assert main(argv(install_dir, "--version", "0.3.1", "--no-start")) == 0

    err = capsys.readouterr().err
    assert "the requested 0.3.1 was NOT applied" in err
    assert "tripl upgrade --to 0.3.1" not in err
    assert "will not move the pin back to 0.3.1" in err


# --- every suggested tag is derived, never typed ------------------------------


def test_the_example_tag_is_the_installed_version_when_it_is_a_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(files, "__version__", "0.3.1")
    assert files.example_tag() == "0.3.1"


@pytest.mark.parametrize("version", ["0.0.0+unknown", "0.4.0.dev1", "0.4.0rc1"])
def test_a_version_that_is_not_a_release_falls_back_to_the_placeholder(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    monkeypatch.setattr(files, "__version__", version)
    assert files.example_tag() == files.TAG_PLACEHOLDER


def test_the_latest_reminder_suggests_the_installed_version(
    install_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(argv(install_dir, "--dry-run")) == 0

    err = capsys.readouterr().err
    assert f"(`--version {files.example_tag()}`)" in err


def test_upgrade_help_suggests_the_installed_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["upgrade", "--help"])

    help_text = " ".join(capsys.readouterr().out.split())
    assert f"e.g. {files.example_tag()}." in help_text


def test_the_installed_cli_is_a_release_so_the_examples_are_real_tags() -> None:
    """bin/release.sh writes one X.Y.Z to the image, this CLI and tripl-mcp.

    If this fails, the examples above have fallen back to the placeholder in
    the environment CI tests in, which means the package metadata is not the
    pyproject's version.
    """
    assert files.example_tag() == __version__
