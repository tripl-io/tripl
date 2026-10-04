"""``tripl install`` end to end, with the subprocess seam and the clock replaced.

NOTHING HERE STARTS A CONTAINER. Every ``docker`` invocation goes to
``FakeRunner``, which records it; every HTTP call goes to respx. That is a
property of the design, not of the discipline in this file - the command
resolves its runner through ``default_runner()`` precisely so the fixture can
replace it.

The negatives come first, because they are the tests that matter: each one is a
mistake an operator makes on their first attempt, and each must cost an exit
code rather than a half-provisioned directory or a leaked secret.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
import respx

from tripl_cli.cli import main
from tripl_cli.commands import install as install_cmd
from tripl_cli.install import docker, files, health, secrets

from .conftest import FakeRunner

APP_URL = "https://tripl.example.com"
HEALTH_URL = f"{APP_URL}/health"
STATUS_URL = f"{APP_URL}/api/v1/auth/status"

DOCKER_PRESENT = {"docker": "/usr/bin/docker", "docker-compose": None}


@pytest.fixture(autouse=True)
def docker_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend Docker Engine + Compose V2 are installed. Never touches a daemon."""
    monkeypatch.setattr(docker, "which", lambda name: DOCKER_PRESENT.get(name))


def argv(directory: Path, *extra: str) -> list[str]:
    return ["install", "--app-url", APP_URL, "--dir", str(directory), *extra]


def listing(directory: Path) -> set[str]:
    if not directory.exists():
        return set()
    return {str(p.relative_to(directory)) for p in directory.rglob("*")}


def healthy_instance(router: respx.MockRouter, *, has_users: bool = False) -> None:
    router.get(HEALTH_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    router.get(STATUS_URL).mock(
        return_value=httpx.Response(
            200, json={"has_users": has_users, "registration_enabled": not has_users}
        )
    )


# --- negatives ---------------------------------------------------------------


def test_no_docker_writes_nothing_and_runs_nothing(
    install_dir: Path,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(docker, "which", lambda name: None)

    assert main(argv(install_dir)) == 2

    assert fake_runner.calls == []
    assert listing(install_dir) == set()
    assert "docker was not found on PATH" in capsys.readouterr().err


def test_a_dead_daemon_echoes_dockers_own_words(
    install_dir: Path,
    fake_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_runner.codes["info"] = 1
    fake_runner.stderr["info"] = (
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    )

    assert main(argv(install_dir)) == 2

    err = capsys.readouterr().err
    assert "  docker: Cannot connect to the Docker daemon at unix:///var/run/docker.sock" in err
    assert listing(install_dir) == set()


def test_an_incomplete_env_on_a_non_tty_refuses_to_prompt(
    install_dir: Path,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A prompt that blocks in a provisioning script would hang it forever."""
    install_dir.mkdir(parents=True)
    env = install_dir / ".env"
    env.write_text("APP_BASE_URL=https://mine.example.com\n", encoding="utf-8")
    # 0600, or the world-readable refusal below fires first and this test would
    # be passing for the other reason.
    env.chmod(0o600)
    before = env.read_bytes()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert main(argv(install_dir)) == 2

    assert env.read_bytes() == before
    err = capsys.readouterr().err
    assert "--yes" in err
    # HTTP wording on a command that opens no socket. The stake here is the file.
    assert "Nothing was sent." not in err
    assert "Nothing was written." in err


def test_new_secrets_are_never_appended_to_a_world_readable_env(
    install_dir: Path,
    fake_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Warn-and-do-it-anyway is the one option that is not defensible.

    A secret written into a file other users can read is disclosed the moment it
    lands, and no later chmod undoes that. Refusing rather than tightening the
    mode ourselves keeps the file the operator's.
    """
    if sys.platform == "win32":  # pragma: no cover - st_mode carries no such meaning
        pytest.skip("file modes do not restrain this user")
    install_dir.mkdir(parents=True)
    env = install_dir / ".env"
    env.write_text("APP_BASE_URL=https://mine.example.com\n", encoding="utf-8")
    env.chmod(0o644)
    before = env.read_bytes()

    assert main(argv(install_dir, "--yes")) == 2

    assert env.read_bytes() == before
    assert files.parse_env(env.read_text()).get("ENCRYPTION_KEY") is None
    assert env.stat().st_mode & 0o777 == 0o644, "the mode must be the operator's to change"
    assert fake_runner.calls == [], "refused before the Docker probe, let alone the pull"
    err = capsys.readouterr().err
    assert "readable by other users (mode 644)" in err
    assert f"chmod 600 {env}" in err
    assert "ENCRYPTION_KEY" in err
    assert err.isascii()


@pytest.mark.parametrize("marker", [".git", "backend/src/tripl"])
def test_a_source_checkout_is_refused_before_anything_is_written(
    install_dir: Path,
    fake_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
    marker: str,
) -> None:
    (install_dir / marker).mkdir(parents=True)
    before = listing(install_dir)

    assert main(argv(install_dir)) == 2

    assert listing(install_dir) == before
    assert fake_runner.calls == []
    assert "source checkout" in capsys.readouterr().err


def test_a_bare_host_is_rejected_with_a_suggestion(
    install_dir: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["install", "--app-url", "tripl.example.com", "--dir", str(install_dir)]) == 2
    err = capsys.readouterr().err
    assert "--app-url" in err
    assert "https://tripl.example.com" in err
    assert listing(install_dir) == set()


def test_app_url_is_required(install_dir: Path) -> None:
    """argparse's own exit 2, before anything in this package runs."""
    with pytest.raises(SystemExit) as exit_info:
        main(["install", "--dir", str(install_dir)])
    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "flag,value", [("--url", "https://other.example"), ("--api-key", "tk_r_x")]
)
def test_an_explicit_instance_flag_is_refused_not_ignored(
    install_dir: Path,
    fake_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    value: str,
) -> None:
    assert main([*argv(install_dir), flag, value]) == 2
    err = capsys.readouterr().err
    assert flag in err
    assert "--app-url" in err
    assert fake_runner.calls == []


def test_an_ambient_base_url_env_var_does_not_trip_the_refusal(
    install_dir: Path,
    fake_runner: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
) -> None:
    """Provenance, not value: $TRIPL_BASE_URL is exported for `tripl doctor`."""
    monkeypatch.setenv("TRIPL_BASE_URL", "https://other.example")
    healthy_instance(respx_mock)

    assert main(argv(install_dir)) == 0


def test_health_never_arriving_names_every_log_to_read(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The stack WAS started. Say so first, then say where to look."""
    respx_mock.get(HEALTH_URL).mock(return_value=httpx.Response(502, text="bad gateway"))

    assert main(argv(install_dir, "--wait", "1")) == 1

    out = capsys.readouterr().out
    assert "docker compose ps" in out
    assert "docker compose logs migrate" in out
    assert "docker compose logs app" in out
    assert f"tripl doctor --url {APP_URL}" in out
    # Both compose commands DID run - this is not a refusal, it is a timeout.
    assert fake_runner.argvs[-2:] == [
        ("docker", "compose", "pull"),
        ("docker", "compose", "up", "-d"),
    ]


def test_a_failing_compose_command_exits_one_and_says_it_is_re_runnable(
    install_dir: Path,
    fake_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_runner.codes["pull"] = 1

    assert main(argv(install_dir)) == 1

    err = capsys.readouterr().err
    assert "docker compose pull` failed (exit 1)" in err
    assert "safe to re-run by hand" in err
    # The files were written before the pull, and `up -d` was never reached.
    assert (install_dir / ".env").exists()
    assert ("docker", "compose", "up", "-d") not in fake_runner.argvs


def test_a_failing_up_is_the_documented_exit_one_and_never_polls_health(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The branch the exit-code table documents, and the one nothing covered.

    It differs from a failing `pull` in the way that matters: the files are
    already on disk AND the pull succeeded, so an operator retrying by hand is
    retrying the last step rather than the whole command.
    """
    fake_runner.codes["up"] = 1

    assert main(argv(install_dir)) == 1

    assert fake_runner.argvs[-2:] == [
        ("docker", "compose", "pull"),
        ("docker", "compose", "up", "-d"),
    ]
    # The three files are on disk; a failed `up` never rolls them back.
    assert listing(install_dir) == {
        "compose.yaml",
        "infra",
        "infra/rabbitmq",
        "infra/rabbitmq/rabbitmq.conf",
        ".env",
    }
    # Nothing was polled: there is no instance to poll, and a /health timeout
    # would have blamed the wrong thing for five minutes.
    assert respx_mock.calls == []
    captured = capsys.readouterr()
    assert "docker compose up -d` failed (exit 1)" in captured.err
    assert "safe to re-run by hand" in captured.err
    assert "is up after" not in captured.out


def test_wait_zero_opens_no_connection_at_all(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--wait 0` is prescribed for a URL that is NOT reachable yet.

    It used to skip the /health poll and then read /auth/status anyway, which
    bought a ten-second stall against exactly the origin the operator had just
    said not to try - and then reported that the bootstrap state "could not be
    read", inventing a failed attempt.
    """
    respx_mock.get(HEALTH_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx_mock.get(STATUS_URL).mock(
        return_value=httpx.Response(200, json={"has_users": False, "registration_enabled": True})
    )

    assert main(argv(install_dir, "--wait", "0")) == 0

    assert respx_mock.calls == [], "--wait 0 must mean no connection"
    out = capsys.readouterr().out
    assert f"not waiting for {APP_URL}/health: --wait 0." in out
    assert f"Nothing was asked of {APP_URL}" in out
    assert "could not be read" not in out
    # The next steps are still printed - the operator still has to do them.
    assert "Settings -> Data sources" in out
    assert out.isascii()


def test_the_json_document_never_asserts_a_mode_it_did_not_set(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`"mode": "0600"` on an append was a machine-readable claim nothing checked.

    The command opens an appended file with O_APPEND and no mode at all, and
    never stats it. Reporting the permissions of the secrets file on that path
    was an assertion an operator could paste into a ticket.
    """
    respx_mock.get("https://mine.example.com/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx_mock.get("https://mine.example.com/api/v1/auth/status").mock(
        return_value=httpx.Response(200, json={"has_users": False, "registration_enabled": True})
    )
    install_dir.mkdir(parents=True)
    env = install_dir / ".env"
    env.write_text("APP_BASE_URL=https://mine.example.com\n", encoding="utf-8")
    env.chmod(0o600)

    assert main(argv(install_dir, "--yes", "--json")) == 0

    document = json.loads(capsys.readouterr().out)
    by_action = {entry["action"]: entry for entry in document["files"]}
    assert by_action["append"]["path"] == ".env"
    assert by_action["append"]["mode"] is None
    # ...and the two the command really does open with a mode still say so.
    assert by_action["create"]["mode"] == "0644"


def test_force_reports_the_copy_it_kept_of_the_operators_compose(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    healthy_instance(respx_mock)
    install_dir.mkdir(parents=True)
    compose = install_dir / "compose.yaml"
    compose.write_text("services: {}  # mine\n", encoding="utf-8")

    assert main(argv(install_dir, "--force", "--json")) == 0

    kept = sorted(install_dir.glob("compose.yaml.bak.*"))
    assert len(kept) == 1
    assert kept[0].read_text() == "services: {}  # mine\n"
    assert compose.read_text() == files.packaged("compose.yaml")
    document = json.loads(capsys.readouterr().out)
    replaced = next(entry for entry in document["files"] if entry["action"] == "replace")
    assert replaced["backup"] == kept[0].name
    assert kept[0].name in replaced["note"]


# --- positives and pins ------------------------------------------------------


def test_a_happy_install_writes_three_files_and_runs_two_commands(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    healthy_instance(respx_mock)

    assert main(argv(install_dir)) == 0

    assert listing(install_dir) == {
        "compose.yaml",
        "infra",
        "infra/rabbitmq",
        "infra/rabbitmq/rabbitmq.conf",
        ".env",
    }
    if sys.platform != "win32":
        assert (install_dir / ".env").stat().st_mode & 0o777 == 0o600
        assert (install_dir / "compose.yaml").stat().st_mode & 0o777 == 0o644

    # The probes are captured; the two long-running commands are NOT - their
    # output belongs on the operator's terminal, live.
    assert fake_runner.argvs == [
        ("docker", "compose", "version", "--short"),
        ("docker", "info", "--format", "{{.ServerVersion}}"),
        ("docker", "compose", "pull"),
        ("docker", "compose", "up", "-d"),
    ]
    assert fake_runner.captured == [True, True, False, False]
    assert [call.cwd for call in fake_runner.calls[2:]] == [install_dir, install_dir]
    # No -f: compose's own discovery must keep finding a compose.override.yaml.
    assert all("-f" not in call.argv for call in fake_runner.calls)

    out = capsys.readouterr().out
    assert "no accounts yet" in out
    assert "Settings -> Data sources" in out


def test_a_dry_run_writes_nothing_and_runs_nothing(
    install_dir: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv(install_dir, "--dry-run")) == 0

    assert listing(install_dir) == set()
    assert fake_runner.calls == []
    captured = capsys.readouterr()
    assert "dry run: nothing was written and nothing was run." in captured.out
    # The default tag follows every release, so say so - on stderr, where it
    # cannot disturb the plan an operator diffs.
    assert "In production pin a released tag" in captured.err


def test_an_explicit_version_gets_no_pin_reminder(
    install_dir: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv(install_dir, "--version", "1.5.0", "--dry-run")) == 0

    captured = capsys.readouterr()
    assert "ghcr.io/vladenisov/tripl:1.5.0" in captured.out
    assert "pin a released tag" not in captured.err


def test_no_start_writes_the_files_and_starts_nothing(
    install_dir: Path, fake_runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv(install_dir, "--no-start")) == 0

    assert (install_dir / ".env").exists()
    assert fake_runner.calls == []
    assert "--no-start" in capsys.readouterr().out


def test_re_running_on_a_provisioned_directory_changes_nothing_but_still_converges(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Idempotent, not one-shot: `pull` + `up -d` run again on purpose."""
    healthy_instance(respx_mock)
    assert main(argv(install_dir)) == 0
    before = {path: path.read_bytes() for path in install_dir.rglob("*") if path.is_file()}
    stamps = {path: path.stat().st_mtime_ns for path in before}
    capsys.readouterr()
    fake_runner.calls.clear()

    assert main(argv(install_dir)) == 0

    assert {path: path.read_bytes() for path in before} == before
    assert {path: path.stat().st_mtime_ns for path in before} == stamps
    assert ("docker", "compose", "up", "-d") in fake_runner.argvs
    out = capsys.readouterr().out
    assert "unchanged" in out
    assert "kept" in out


def test_an_incomplete_env_is_appended_to_under_yes_and_only_names_are_printed(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The file's APP_BASE_URL wins over --app-url, so it is that origin the
    # command polls. Mocking the flag's URL instead would test a poll that no
    # longer happens.
    respx_mock.get("https://mine.example.com/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx_mock.get("https://mine.example.com/api/v1/auth/status").mock(
        return_value=httpx.Response(200, json={"has_users": False, "registration_enabled": True})
    )
    install_dir.mkdir(parents=True)
    env = install_dir / ".env"
    original = "APP_BASE_URL=https://mine.example.com\nTRIPL_IMAGE=x\nTRIPL_VERSION=1.4.0\n"
    env.write_text(original, encoding="utf-8")
    env.chmod(0o600)

    assert main(argv(install_dir, "--yes")) == 0

    updated = env.read_text(encoding="utf-8")
    assert updated.startswith(original)
    parsed = files.parse_env(updated)
    out = capsys.readouterr().out
    for name in secrets.REQUIRED_SECRETS:
        assert name in out
        assert parsed[name] not in out
    if sys.platform != "win32":
        assert env.stat().st_mode & 0o777 == 0o600


def test_a_world_readable_env_gets_the_chmod_warning(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if sys.platform == "win32":  # pragma: no cover - POSIX-only meaning
        pytest.skip("st_mode carries no such meaning")
    healthy_instance(respx_mock)
    install_dir.mkdir(parents=True)
    env = install_dir / ".env"
    env.write_text(
        files.render_env(
            app_base_url=APP_URL,
            image=files.DEFAULT_IMAGE,
            version="latest",
            generated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            values=secrets.generate(),
        ),
        encoding="utf-8",
    )
    env.chmod(0o644)

    assert main(argv(install_dir)) == 0

    err = capsys.readouterr().err
    assert "is readable by other users (mode 644)" in err
    assert "SECRET_KEY" in err
    assert f"chmod 600 {env}" in err


def test_re_installing_with_a_changed_app_url_names_the_kept_value_not_the_requested_one(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`.env` holds live secrets and is never overwritten - so it wins. Say so.

    Reporting the flag value here told an operator re-running `install` to move
    the app URL that it had worked. It had not: the only thing that changed was
    the sentence they were reading.
    """
    respx_mock.get("https://old.example.com/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx_mock.get("https://old.example.com/api/v1/auth/status").mock(
        return_value=httpx.Response(200, json={"has_users": False, "registration_enabled": True})
    )
    assert (
        main(
            [
                "install",
                "--app-url",
                "https://old.example.com",
                "--dir",
                str(install_dir),
                "--version",
                "1.4.0",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "install",
                "--app-url",
                "https://new.example.com",
                "--dir",
                str(install_dir),
                "--version",
                "1.9.9",
                "--json",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    on_disk = files.parse_env((install_dir / ".env").read_text())
    assert on_disk["APP_BASE_URL"] == "https://old.example.com"
    assert on_disk["TRIPL_VERSION"] == "1.4.0"

    # The plan reports what will be TRUE, and marks it as the file's value.
    plan_text = captured.err
    assert "app url  https://old.example.com" in plan_text
    assert "(kept from .env: APP_BASE_URL)" in plan_text
    assert "ghcr.io/vladenisov/tripl:1.4.0" in plan_text
    assert "https://new.example.com\n" not in plan_text.replace(
        "requested https://new.example.com", ""
    )

    # ...and the refusal is stated, with the thing to do instead.
    assert "was NOT applied" in plan_text
    assert f"edit {install_dir / '.env'}" in plan_text
    assert "tripl upgrade --to 1.9.9" in plan_text

    document = json.loads(captured.out)
    assert document["app_base_url"] == "https://old.example.com"
    assert document["version"] == "1.4.0"
    by_name = {entry["name"]: entry for entry in document["settings"]}
    assert by_name["APP_BASE_URL"] == {
        "name": "APP_BASE_URL",
        "requested": "https://new.example.com",
        "effective": "https://old.example.com",
        "kept": True,
        "applied": False,
    }
    assert by_name["TRIPL_VERSION"]["applied"] is False
    assert (captured.out + captured.err).isascii()
    # The poll went to the origin that is really configured, not to the flag's.
    assert [str(call.request.url) for call in respx_mock.calls][-2:] == [
        "https://old.example.com/health",
        "https://old.example.com/api/v1/auth/status",
    ]


def test_a_re_run_that_omits_version_does_not_claim_a_tag_was_refused(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Provenance, not value: an unasked-for default is not a refused request."""
    healthy_instance(respx_mock)
    assert main(argv(install_dir, "--version", "1.4.0")) == 0
    capsys.readouterr()

    assert main(argv(install_dir)) == 0

    captured = capsys.readouterr()
    assert "TRIPL_VERSION" not in captured.err.split("kept from .env:")[-1].split("\n")[0]
    assert "was NOT applied" not in captured.err
    assert "ghcr.io/vladenisov/tripl:1.4.0" in captured.out
    # ...and the effective tag is not `latest`, so the pin reminder stays quiet.
    assert "pin a released tag" not in captured.err


def test_a_differing_compose_is_kept_and_force_replaces_it(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    healthy_instance(respx_mock)
    install_dir.mkdir(parents=True)
    compose = install_dir / "compose.yaml"
    compose.write_text("services: {}\n", encoding="utf-8")

    assert main(argv(install_dir)) == 0
    assert compose.read_text() == "services: {}\n"
    assert "differs from the version this CLI ships" in capsys.readouterr().out

    assert main(argv(install_dir, "--force")) == 0
    assert compose.read_text() == files.packaged("compose.yaml")


def test_a_plain_http_app_url_warns_about_the_secure_cookie(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    respx_mock.get("http://tripl.local/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx_mock.get("http://tripl.local/api/v1/auth/status").mock(
        return_value=httpx.Response(200, json={"has_users": False, "registration_enabled": True})
    )

    code = main(["install", "--app-url", "http://tripl.local", "--dir", str(install_dir)])

    assert code == 0
    assert "SESSION_COOKIE_SECURE=true" in capsys.readouterr().err


# --- the secret-leak guard ---------------------------------------------------


def test_no_generated_secret_reaches_stdout_stderr_or_the_json_document(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The rule that has no acceptable exception.

    A secret printed once is a secret in a scrollback buffer, in a `tee` log and
    in whatever gets pasted into a ticket. Known values are injected so this can
    be a substring search rather than a regex that hopes.
    """
    healthy_instance(respx_mock)
    known = {
        "ENCRYPTION_KEY": "KNOWN-ENCRYPTION-KEY-0000000000000000000000=",
        "SECRET_KEY": "KNOWN-SECRET-KEY-1111111111",
        "POSTGRES_PASSWORD": "deadbeef" * 6,
        "RABBITMQ_PASSWORD": "cafebabe" * 6,
    }
    monkeypatch.setattr(secrets, "generate", lambda rng=None: dict(known))

    assert main(argv(install_dir, "--json")) == 0

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    blob = captured.out + captured.err
    for name, value in known.items():
        assert value not in blob, f"{name} leaked into the output"
        assert value not in json.dumps(document), f"{name} leaked into the --json document"
        assert name in document["secrets_generated"]
    # ...and they really were written, so the test is not passing vacuously.
    assert files.parse_env((install_dir / ".env").read_text())["SECRET_KEY"] == known["SECRET_KEY"]


# --- the json document -------------------------------------------------------


def test_the_json_document_carries_argv_cwd_and_returncodes(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Compose's own output cannot be here - it was inherited, never captured."""
    healthy_instance(respx_mock)

    assert main(argv(install_dir, "--json")) == 0

    document = json.loads(capsys.readouterr().out)
    assert document["command"] == "install"
    assert document["directory"] == str(install_dir)
    assert document["app_base_url"] == APP_URL
    assert [entry["argv"] for entry in document["commands"]] == [
        ["docker", "compose", "pull"],
        ["docker", "compose", "up", "-d"],
    ]
    assert {entry["cwd"] for entry in document["commands"]} == {str(install_dir)}
    assert [entry["returncode"] for entry in document["commands"]] == [0, 0]
    assert [entry["action"] for entry in document["files"]] == ["create", "create", "create"]
    assert document["health"]["status"] == "ok"
    assert document["bootstrap"] == {"has_users": False, "registration_enabled": True}
    assert document["exit_code"] == 0
    # No instance block: there is no configured URL and no key at install time.
    assert "instance" not in document


def test_a_command_that_never_ran_carries_a_null_returncode(
    install_dir: Path,
    fake_runner: FakeRunner,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_runner.codes["pull"] = 1

    assert main(argv(install_dir, "--json")) == 1

    document = json.loads(capsys.readouterr().out)
    assert [entry["returncode"] for entry in document["commands"]] == [1, None]


# --- next steps --------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"has_users": False, "registration_enabled": True}, "no accounts yet"),
        ({"has_users": True, "registration_enabled": True}, "already has accounts; sign in"),
        ({"has_users": True, "registration_enabled": False}, "Invite a member"),
    ],
)
def test_next_steps_branch_on_the_real_bootstrap_state(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, bool],
    expected: str,
) -> None:
    respx_mock.get(HEALTH_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx_mock.get(STATUS_URL).mock(return_value=httpx.Response(200, json=payload))

    assert main(argv(install_dir)) == 0

    assert expected in capsys.readouterr().out


def test_an_unreadable_bootstrap_state_is_never_invented(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    respx_mock.get(HEALTH_URL).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx_mock.get(STATUS_URL).mock(return_value=httpx.Response(500, text="boom"))

    assert main(argv(install_dir)) == 0

    out = capsys.readouterr().out
    assert "bootstrap state of this" in out
    # The definite claim is absent; the conditional one ("if it has no accounts
    # yet") is what an unread state honestly supports.
    assert "This instance has no accounts yet." not in out


def test_neither_unauthenticated_probe_sends_an_authorization_header(
    install_dir: Path,
    fake_runner: FakeRunner,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with a key in the environment. These endpoints require none."""
    monkeypatch.setenv("TRIPL_API_KEY", "tk_w_secret")
    healthy_instance(respx_mock)

    assert main(argv(install_dir)) == 0

    sent = [call.request for call in respx_mock.calls]
    assert [str(request.url) for request in sent] == [HEALTH_URL, STATUS_URL]
    for request in sent:
        assert "authorization" not in request.headers


# --- the health poll itself --------------------------------------------------


async def test_the_poll_retries_until_health_answers(respx_mock: respx.MockRouter) -> None:
    """Virtual time: the loop sleeps, the clock advances, no test waits."""
    answers = [
        httpx.Response(503, text="starting"),
        httpx.Response(503, text="starting"),
        httpx.Response(200, json={"status": "ok"}),
    ]
    respx_mock.get(HEALTH_URL).mock(side_effect=answers)
    slept: list[float] = []
    ticks = iter([0.0, 0.0, 5.0, 10.0, 10.0])

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    outcome = await health._poll(
        APP_URL,
        1.0,
        deadline_seconds=60.0,
        sleep=sleep,
        monotonic=lambda: next(ticks),
    )

    assert outcome.ok
    assert outcome.attempts == 3
    assert slept == [health.POLL_INTERVAL_SECONDS, health.POLL_INTERVAL_SECONDS]


def test_waiting_can_be_skipped_entirely(install_dir: Path) -> None:
    outcome = health.wait_for_health(APP_URL, deadline_seconds=0)
    assert outcome.skipped
    assert outcome.status == "skipped"
    assert outcome.attempts == 0


# --- help --------------------------------------------------------------------


def test_help_is_ascii_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["install", "--help"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.isascii()


def test_the_default_runner_is_the_real_one() -> None:
    """Guards every test above: a seam patched to nothing would prove nothing."""
    from tripl_cli.install.shell import subprocess_runner

    assert install_cmd.default_runner() is subprocess_runner
