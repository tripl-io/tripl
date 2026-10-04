"""``tripl upgrade`` — the refusals first, because refusing is most of the job.

The one thing this command must never do is leave an operator with a stack whose
image and schema disagree and no way back. Every test below is one clause of
that promise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
import respx

from tripl_cli.cli import main
from tripl_cli.commands import upgrade as upgrade_cmd
from tripl_cli.install import docker, files

from .conftest import FakeRunner

APP_URL = "https://tripl.example.com"
HEALTH_URL = f"{APP_URL}/health"

DOCKER_PRESENT = {"docker": "/usr/bin/docker", "docker-compose": None}


@pytest.fixture(autouse=True)
def docker_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker, "which", lambda name: DOCKER_PRESENT.get(name))


ENV_TEMPLATE = (
    "# hand-written header\n"
    f"APP_BASE_URL={APP_URL}\n"
    "\n"
    "TRIPL_IMAGE=ghcr.io/tripl-io/tripl\n"
    "TRIPL_VERSION={version}\n"
    "\n"
    "# a trailing comment\n"
    "ENCRYPTION_KEY=abc\n"
)


@pytest.fixture
def installed(install_dir: Path) -> Path:
    """A directory that looks like a finished `tripl install` at 1.4.0."""
    install_dir.mkdir(parents=True)
    (install_dir / "compose.yaml").write_text(files.packaged("compose.yaml"), encoding="utf-8")
    env = install_dir / ".env"
    env.write_text(ENV_TEMPLATE.format(version="1.4.0"), encoding="utf-8")
    env.chmod(0o600)
    return install_dir


def argv(directory: Path, target: str, *extra: str) -> list[str]:
    return ["upgrade", "--to", target, "--dir", str(directory), *extra]


def backups(directory: Path) -> list[Path]:
    return sorted(directory.glob(".env.bak.*"))


# --- refusals ----------------------------------------------------------------


def test_a_missing_stack_names_tripl_install(
    install_dir: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    install_dir.mkdir(parents=True)

    assert main(argv(install_dir, "1.5.0")) == 2

    assert "Run `tripl install` first" in capsys.readouterr().err
    assert fake_runner.calls == []


def test_the_same_version_is_a_no_op_that_touches_nothing(
    installed: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    before = (installed / ".env").read_bytes()

    assert main(argv(installed, "1.4.0")) == 0

    assert fake_runner.calls == []
    assert (installed / ".env").read_bytes() == before
    assert backups(installed) == []
    assert "already at 1.4.0; nothing to do." in capsys.readouterr().out


def test_a_downgrade_is_refused_outright(
    installed: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    """No override flag, on purpose: the honest instruction is "restore"."""
    before = (installed / ".env").read_bytes()

    assert main(argv(installed, "1.3.9")) == 2

    err = capsys.readouterr().err
    assert "downgrade refused: 1.4.0 -> 1.3.9" in err
    assert "not reversible" in err
    assert "no override flag" in err
    assert fake_runner.calls == []
    assert (installed / ".env").read_bytes() == before


def test_an_unorderable_tag_refuses_with_no_flags_at_all(
    installed: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv(installed, "latest")) == 2
    err = capsys.readouterr().err
    assert "cannot tell whether 'latest' is newer than '1.4.0'" in err
    assert upgrade_cmd.FLAG_ALLOW_UNORDERED in err
    assert fake_runner.calls == []


def test_yes_alone_does_not_disable_the_ordering_guard(
    installed: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    """The direction that matters, because automation ALWAYS passes --yes.

    A pipeline has to pass it or the backup prompt hangs it forever, so a
    `--yes` that also meant "and never mind that this may be a downgrade" turned
    the guard off on precisely the runs nobody is watching. Applying migrations
    backwards is the single most destructive thing this CLI can do.
    """
    before = (installed / ".env").read_bytes()

    assert main(argv(installed, "latest", "--yes")) == 2

    err = capsys.readouterr().err
    assert "cannot tell whether 'latest' is newer than '1.4.0'" in err
    assert "--yes alone does not override this" in err
    assert upgrade_cmd.FLAG_ALLOW_UNORDERED in err
    assert fake_runner.calls == []
    assert (installed / ".env").read_bytes() == before
    assert backups(installed) == []


def test_the_explicit_override_proceeds_and_still_warns(
    installed: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both flags, because they answer two different questions."""
    respx_mock.get(HEALTH_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))

    assert main(argv(installed, "latest", "--yes", upgrade_cmd.FLAG_ALLOW_UNORDERED)) == 0

    err = capsys.readouterr().err
    assert "could not be determined" in err
    assert "may be a downgrade" in err
    assert files.current_version((installed / ".env").read_text()) == "latest"


def test_the_override_alone_still_hits_the_backup_gate(
    installed: Path,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Independent in both directions: the override is not a second --yes."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    before = (installed / ".env").read_bytes()

    assert main(argv(installed, "latest", upgrade_cmd.FLAG_ALLOW_UNORDERED)) == 2

    err = capsys.readouterr().err
    assert "Have you taken that backup" in err
    assert "--yes" in err
    assert (installed / ".env").read_bytes() == before
    assert fake_runner.calls == []


def test_an_unorderable_dry_run_previews_instead_of_refusing(
    installed: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--dry-run` is documented as "write nothing, run nothing" - not "exit 2".

    The refusal used to be raised before the dry-run branch, so the one move an
    operator is most unsure about was the one they could not preview.
    The refusal is still stated, as a preview of it.
    """
    before = (installed / ".env").read_bytes()

    assert main(argv(installed, "latest", "--dry-run")) == 0

    out = capsys.readouterr().out
    assert "from  1.4.0" in out
    assert "to    latest" in out
    assert "a real run would refuse here: cannot tell whether 'latest' is newer" in out
    assert upgrade_cmd.FLAG_ALLOW_UNORDERED in out
    assert "dry run: nothing was written and nothing was run." in out
    assert out.isascii()
    assert fake_runner.calls == []
    assert (installed / ".env").read_bytes() == before
    assert backups(installed) == []


def test_a_dry_run_under_the_override_previews_without_the_refusal(
    installed: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv(installed, "latest", "--dry-run", upgrade_cmd.FLAG_ALLOW_UNORDERED)) == 0

    out = capsys.readouterr().out
    assert "cannot tell whether" not in out
    assert "dry run: nothing was written and nothing was run." in out
    assert fake_runner.calls == []


def test_a_non_tty_without_yes_refuses_at_the_backup_gate(
    installed: Path,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A prompt here would hang a provisioning script forever."""
    before = (installed / ".env").read_bytes()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert main(argv(installed, "1.5.0")) == 2

    err = capsys.readouterr().err
    assert "--yes" in err
    # This command sends no request; it pulls, rewrites .env and restarts. The
    # HTTP wording would teach an operator to discount the sentence.
    assert "Nothing was sent." not in err
    assert "Nothing was written and nothing was run." in err
    assert fake_runner.calls == []
    assert (installed / ".env").read_bytes() == before
    assert backups(installed) == []


def test_no_docker_on_the_upgrade_path_leaves_the_pin_and_runs_nothing(
    installed: Path,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`require_docker` on THIS command was only ever reached on the success path.

    The autouse fixture forces Docker present, so nothing exercised the probe
    that has to stop an upgrade before the pull. It matters more
    here than on `install`: past this point the .env is rewritten.
    """
    monkeypatch.setattr(docker, "which", lambda name: None)
    before = (installed / ".env").read_bytes()

    assert main(argv(installed, "1.5.0", "--yes")) == 2

    assert "docker was not found on PATH" in capsys.readouterr().err
    assert fake_runner.calls == []
    assert (installed / ".env").read_bytes() == before
    assert backups(installed) == []


def test_a_dead_daemon_on_the_upgrade_path_echoes_dockers_own_words(
    installed: Path,
    fake_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_runner.codes["info"] = 1
    fake_runner.stderr["info"] = (
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    )
    before = (installed / ".env").read_bytes()

    assert main(argv(installed, "1.5.0", "--yes")) == 2

    err = capsys.readouterr().err
    assert "  docker: Cannot connect to the Docker daemon at unix:///var/run/docker.sock" in err
    # The two probes ran; the pull did not.
    assert ("docker", "compose", "pull") not in fake_runner.argvs
    assert (installed / ".env").read_bytes() == before
    assert backups(installed) == []


def test_no_app_base_url_reports_the_real_reason_and_never_claims_it_is_running(
    installed: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """It used to blame `--wait 0` on a run whose --wait was 300, and then say
    "<tag> is running." having contacted nothing at all."""
    env = installed / ".env"
    env.write_text("TRIPL_VERSION=1.4.0\n", encoding="utf-8")
    env.chmod(0o600)

    assert main(argv(installed, "1.5.0", "--yes")) == 0

    captured = capsys.readouterr()
    assert respx_mock.calls == [], "nothing was contacted, so nothing may be claimed about it"
    assert "--wait 0" not in captured.out + captured.err
    assert "1.5.0 is running." not in captured.out
    assert "Nothing has checked that it is serving." in captured.out
    assert "not waiting: .env sets no APP_BASE_URL, so there is no origin to poll." in captured.out
    assert "this upgrade was not verified" in captured.err
    assert "Add APP_BASE_URL to" in captured.err
    assert (captured.out + captured.err).isascii()
    # The upgrade itself succeeded, so exit 0 - but the pin really did move.
    assert files.current_version(env.read_text()) == "1.5.0"


@pytest.mark.parametrize(
    "env_body,extra,reason",
    [
        (None, ("--wait", "0"), "--wait 0"),
        ("TRIPL_VERSION=1.4.0\n", (), "APP_BASE_URL"),
    ],
    ids=["opted-out", "could-not-check"],
)
def test_the_json_document_distinguishes_opting_out_from_being_unable_to_check(
    installed: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
    env_body: str | None,
    extra: tuple[str, ...],
    reason: str,
) -> None:
    """Both are `health.status == "skipped"`; only `skipped_reason` tells them apart.

    One run per case: the .env backup is O_EXCL on a per-second name, so two
    upgrades of the same directory inside one test would collide by design.
    """
    if env_body is not None:
        (installed / ".env").write_text(env_body, encoding="utf-8")

    assert main(argv(installed, "1.5.0", "--yes", "--json", *extra)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document["health"]["status"] == "skipped"
    assert reason in document["health"]["skipped_reason"]
    assert respx_mock.calls == []


def test_the_backup_command_is_printed_and_never_run(
    installed: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    respx_mock.get(HEALTH_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))

    assert main(argv(installed, "1.5.0", "--yes")) == 0

    out = capsys.readouterr().out
    assert "pg_dump -U tripl tripl | gzip > tripl-1.4.0.sql.gz" in out
    assert "ENCRYPTION_KEY lives in .env, not in that dump" in out
    assert not any("pg_dump" in " ".join(call.argv) for call in fake_runner.calls)
    assert not any("exec" in call.argv for call in fake_runner.calls)


# --- the two failure paths, which differ in exactly one way ------------------


def test_a_failed_pull_leaves_the_env_byte_identical(
    installed: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pull comes first precisely so this is true."""
    before = (installed / ".env").read_bytes()
    fake_runner.codes["pull"] = 1

    assert main(argv(installed, "1.5.0", "--yes")) == 1

    assert (installed / ".env").read_bytes() == before
    assert backups(installed) == []
    assert ("docker", "compose", "up", "-d") not in fake_runner.argvs
    err = capsys.readouterr().err
    assert "the pin is still 1.4.0" in err


def test_a_failed_up_keeps_the_new_pin_and_explains_why(
    installed: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    """NEVER roll the pin back: the schema may already be partly upgraded."""
    original = (installed / ".env").read_bytes()
    fake_runner.codes["up"] = 1

    assert main(argv(installed, "1.5.0", "--yes")) == 1

    assert files.current_version((installed / ".env").read_text()) == "1.5.0"
    kept = backups(installed)
    assert len(kept) == 1
    assert kept[0].read_bytes() == original
    if sys.platform != "win32":
        assert kept[0].stat().st_mode & 0o777 == 0o600

    out = capsys.readouterr().out
    assert "docker compose logs migrate" in out
    assert "TRIPL_VERSION is left at 1.5.0 on purpose" in out
    assert f"The previous .env is at {kept[0].name}." in out


# --- the happy path ----------------------------------------------------------


def test_a_happy_upgrade_pulls_then_pins_then_restarts(
    installed: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    respx_mock.get(HEALTH_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    original = (installed / ".env").read_text()

    assert main(argv(installed, "1.5.0", "--yes")) == 0

    # The pull carries the target in its ENVIRONMENT (shell env beats .env in
    # compose v2); `up -d` does not, because by then the pin is on disk. An
    # inline prefix on the pull alone is a known pitfall.
    pull, up = fake_runner.calls[-2:]
    assert pull.argv == ("docker", "compose", "pull")
    assert dict(pull.env) == {"TRIPL_VERSION": "1.5.0"}
    assert up.argv == ("docker", "compose", "up", "-d")
    assert dict(up.env) == {}
    assert pull.cwd == up.cwd == installed
    assert fake_runner.captured[-2:] == [False, False]

    updated = (installed / ".env").read_text()
    assert updated == original.replace("TRIPL_VERSION=1.4.0", "TRIPL_VERSION=1.5.0")
    assert "1.5.0 is running." in capsys.readouterr().out


def test_a_dry_run_runs_nothing_and_leaves_the_pin(
    installed: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    before = (installed / ".env").read_bytes()

    assert main(argv(installed, "1.5.0", "--dry-run")) == 0

    assert fake_runner.calls == []
    assert (installed / ".env").read_bytes() == before
    assert backups(installed) == []
    assert "dry run: nothing was written and nothing was run." in capsys.readouterr().out


def test_an_env_with_no_pin_is_refused_rather_than_guessed(
    installed: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    (installed / ".env").write_text(f"APP_BASE_URL={APP_URL}\n", encoding="utf-8")

    assert main(argv(installed, "1.5.0")) == 2

    assert "does not set TRIPL_VERSION" in capsys.readouterr().err
    assert fake_runner.calls == []


def test_an_explicit_instance_flag_is_refused(
    installed: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*argv(installed, "1.5.0"), "--url", "https://other.example"]) == 2
    assert "targets a directory" in capsys.readouterr().err
    assert fake_runner.calls == []


# --- comparison --------------------------------------------------------------


@pytest.mark.parametrize(
    "current,target,expected",
    [
        ("1.4.0", "1.4.0", "same"),
        ("1.4.0", "1.5.0", "upgrade"),
        ("1.4.0", "1.3.9", "downgrade"),
        ("1.10.0", "1.9.0", "downgrade"),
        ("1.9.0", "1.10.0", "upgrade"),
        ("1.4.0", "latest", "unknown"),
        ("latest", "1.4.0", "unknown"),
        ("1.4", "1.5.0", "unknown"),
        ("1.4.0", "sha-abc1234", "unknown"),
    ],
)
def test_version_comparison(current: str, target: str, expected: str) -> None:
    """`1.10.0 > 1.9.0` is why this parses digits instead of comparing strings."""
    assert upgrade_cmd.compare(current, target) == expected


# --- the json document -------------------------------------------------------


def test_the_json_document_reports_ordering_and_the_env_backup(
    installed: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    respx_mock.get(HEALTH_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))

    assert main(argv(installed, "1.5.0", "--yes", "--json")) == 0

    document = json.loads(capsys.readouterr().out)
    assert document["command"] == "upgrade"
    assert document["current_version"] == "1.4.0"
    assert document["target_version"] == "1.5.0"
    assert document["ordering"] == "upgrade"
    assert document["env_backup"] == backups(installed)[0].name
    assert "pg_dump" in document["backup_command"]
    assert [entry["returncode"] for entry in document["commands"]] == [0, 0]
    assert document["commands"][0]["env"] == {"TRIPL_VERSION": "1.5.0"}
    assert document["health"]["status"] == "ok"


# --- help --------------------------------------------------------------------


def test_help_is_ascii_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["upgrade", "--help"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.isascii()


def test_to_is_required() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["upgrade"])
    assert exit_info.value.code == 2


def test_a_malformed_tag_fails_at_parse_time() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["upgrade", "--to", "not a tag"])
    assert exit_info.value.code == 2


def test_the_default_runner_is_the_real_one() -> None:
    from tripl_cli.install.shell import subprocess_runner

    assert upgrade_cmd.default_runner() is subprocess_runner
