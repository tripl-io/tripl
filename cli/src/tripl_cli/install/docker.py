"""Is there a usable Docker Engine with the Compose v2 plugin, and is it up?

Three probes in a fixed order, each one answering a question the next one
presupposes. They run BEFORE anything is written, so "you have no docker" costs
the operator an exit code and not a half-provisioned directory.

The legacy ``docker-compose`` v1 binary is DETECTED and never USED. It is named
in the error because "compose: command not found" sends people to the wrong
install page for an afternoon, but falling back to it would be a lie: the
production compose file uses ``depends_on.condition``, which is v2-only, so the
`migrate` one-shot's ``service_completed_successfully`` gate — the thing that
keeps a multi-worker deploy from racing the schema upgrade — simply does not
exist under v1.
"""

from __future__ import annotations

from dataclasses import dataclass
from shutil import which

from tripl_cli.install.shell import Command, Runner

DOCS_PREREQUISITES = "https://docs.tripl.io/run/deployment#prerequisites"

# Both spellings, so a message can name the one the operator actually typed.
DOCKER = "docker"
LEGACY_COMPOSE = "docker-compose"


@dataclass(frozen=True)
class DockerInfo:
    """What we found. ``problem is None`` is the only "go" signal."""

    engine: str | None
    compose: str | None
    server: str | None
    problem: str | None = None
    # Verbatim stderr from the probe that failed, echoed under a `docker:`
    # prefix. Never paraphrased: the daemon's own message names the socket.
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.problem is None


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0].strip() if text.strip() else ""


def detect(runner: Runner) -> DockerInfo:
    """Probe for docker, then for Compose v2, then for a reachable daemon.

    ``which`` is imported into this module's namespace and called unqualified so
    a test can rebind it, the same trick ``runner.run_async`` uses for
    ``create_http_client``.
    """
    engine = which(DOCKER)
    if engine is None:
        return DockerInfo(
            engine=None,
            compose=None,
            server=None,
            problem=(
                "docker was not found on PATH. tripl runs as a Docker Compose stack, so "
                "you need Docker Engine with the Compose v2 plugin (or Docker Desktop). "
                f"Install it first: {DOCS_PREREQUISITES}"
            ),
        )

    version = runner(Command((DOCKER, "compose", "version", "--short")), True)
    if not version.ok:
        legacy = which(LEGACY_COMPOSE)
        if legacy is not None:
            problem = (
                f"found the legacy `{LEGACY_COMPOSE}` v1 binary at {legacy}, but tripl "
                "requires the Compose V2 plugin (`docker compose`, no hyphen). tripl's "
                "compose file uses depends_on.condition, which v1 ignores - the schema "
                "migration would race the app instead of gating it. Install the plugin: "
                f"{DOCS_PREREQUISITES}"
            )
        else:
            problem = (
                "`docker compose version` failed, so the Compose V2 plugin is missing or "
                f"broken. tripl needs it: {DOCS_PREREQUISITES}"
            )
        return DockerInfo(
            engine=engine,
            compose=None,
            server=None,
            problem=problem,
            detail=version.stderr,
        )

    info = runner(Command((DOCKER, "info", "--format", "{{.ServerVersion}}")), True)
    if not info.ok:
        return DockerInfo(
            engine=engine,
            compose=_first_line(version.stdout),
            server=None,
            problem=(
                "the Docker daemon did not answer. Start it (or add yourself to the "
                "`docker` group) and re-run. Docker said:"
            ),
            detail=info.stderr,
        )

    return DockerInfo(
        engine=engine,
        compose=_first_line(version.stdout),
        server=_first_line(info.stdout),
    )
