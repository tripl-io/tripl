"""The three probes, and the three refusals they produce.

Every one of these is a NEGATIVE: they are written first and they are the tests
that matter, because each names a failure an operator will actually hit on their
first attempt and each must cost an exit code rather than a half-provisioned
directory. Nothing here starts a container - the runner is a recorder.
"""

from __future__ import annotations

import pytest

from tripl_cli.install import docker
from tripl_cli.install.shell import Command, CommandResult

from .conftest import FakeRunner


def no_docker(name: str) -> str | None:
    return None


def only_legacy(name: str) -> str | None:
    return "/usr/local/bin/docker-compose" if name == docker.LEGACY_COMPOSE else None


def both(name: str) -> str | None:
    return f"/usr/bin/{name}"


def only_engine(name: str) -> str | None:
    return "/usr/bin/docker" if name == docker.DOCKER else None


def test_no_docker_at_all_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe one. Not a single subprocess is spawned when the binary is absent."""
    monkeypatch.setattr(docker, "which", no_docker)
    runner = FakeRunner()

    found = docker.detect(runner)

    assert runner.calls == []
    assert not found.usable
    assert found.problem is not None
    assert "docker was not found on PATH" in found.problem
    assert docker.DOCS_PREREQUISITES in found.problem


def test_the_legacy_v1_binary_is_named_and_never_invoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe two, the case that wastes an afternoon.

    `docker compose version` failing while `docker-compose` exists is the
    hyphen-vs-space trap. We say so by name - and we do NOT fall back to it: the
    compose file's depends_on.condition is v2-only, so under v1 the `migrate`
    one-shot would not gate app and workers at all.
    """
    monkeypatch.setattr(docker, "which", both)
    runner = FakeRunner(version=1)

    found = docker.detect(runner)

    assert not found.usable
    assert found.problem is not None
    assert docker.LEGACY_COMPOSE in found.problem
    assert "Compose V2 plugin" in found.problem
    assert "depends_on.condition" in found.problem
    # The v1 binary is detected, never executed, and `docker info` is not
    # reached either - there is nothing to ask a daemon once compose is missing.
    assert runner.argvs == [("docker", "compose", "version", "--short")]


def test_a_missing_plugin_with_no_legacy_binary_still_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker, "which", only_engine)
    runner = FakeRunner(version=1)

    found = docker.detect(runner)

    assert not found.usable
    assert found.problem is not None
    assert docker.LEGACY_COMPOSE not in found.problem
    assert "Compose V2 plugin is missing" in found.problem


def test_a_dead_daemon_carries_its_own_stderr_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe three. Docker's sentence is better than any paraphrase of it.

    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock" names
    the socket, which is the single fact that distinguishes "not started" from
    "wrong DOCKER_HOST" from "you are not in the docker group".
    """
    monkeypatch.setattr(docker, "which", both)
    runner = FakeRunner(info=1)
    runner.stderr["info"] = (
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
        "Is the docker daemon running?"
    )

    found = docker.detect(runner)

    assert not found.usable
    assert found.detail == runner.stderr["info"]
    assert found.compose == "v2.29.1"
    assert runner.argvs == [
        ("docker", "compose", "version", "--short"),
        ("docker", "info", "--format", "{{.ServerVersion}}"),
    ]


def test_a_working_daemon_reports_both_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker, "which", both)
    runner = FakeRunner()

    found = docker.detect(runner)

    assert found.usable
    assert found.problem is None
    assert (found.compose, found.server) == ("v2.29.1", "27.1.1")
    # Both probes are CAPTURED - they are short and we parse them.
    assert runner.captured == [True, True]


def test_the_runner_protocol_is_what_the_real_one_implements() -> None:
    """A fake that drifted from ``subprocess_runner``'s shape would prove nothing."""
    from tripl_cli.install.shell import subprocess_runner

    real: docker.Runner = subprocess_runner
    fake: docker.Runner = FakeRunner()
    assert callable(real) and callable(fake)
    assert isinstance(fake(Command(("docker", "info")), True), CommandResult)
