"""What an operator is told to set has to reach something.

Compose passes an explicit allowlist (the `x-app-environment` anchor) and no
`env_file:`, so a variable that .env.example or the configuration reference
names, and that compose.yaml neither forwards nor reads itself, is a setting
that does nothing: the application default silently wins. VITE_API_URL,
POSTGRES_USER and the photo storage settings sat in that state, and so did the
Enterprise KMS settings, whose absence kept every stored secret under
ENCRYPTION_KEY on an install configured for a customer-managed key.

test_contract.py checks the other direction (a forwarded name the template
never documents) and that the packaged compose.yaml is this one, byte for byte.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Documented, and kept out of the production containers on purpose.
NOT_FORWARDED = {
    # .env.example is the development template and sets DEBUG=true; the
    # production stack relies on DEBUG being off.
    "DEBUG",
    # Baked into the image, which is where the built SPA is.
    "SERVE_FRONTEND",
    "FRONTEND_DIST_DIR",
    # The local photo backend must write to the mounted photos volume.
    "PHOTO_LOCAL_DIR",
    # Enterprise's test-only KMS provider, accepted with DEBUG=true only.
    "KMS_LOCAL_KEY",
}


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _forwarded(compose: str) -> set[str]:
    """The names a top-level `x-*-environment` anchor passes to the containers."""
    return set(re.findall(r"^  ([A-Z][A-Z0-9_]*):", compose, re.MULTILINE))


def _read_by_compose(compose: str) -> set[str]:
    """The names Compose interpolates itself (`${POSTGRES_PASSWORD:?...}`, ...)."""
    return set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose))


def _unreached(names: set[str]) -> list[str]:
    compose = _read("compose.yaml")
    return sorted(names - _forwarded(compose) - _read_by_compose(compose) - NOT_FORWARDED)


def test_every_env_example_variable_reaches_the_production_stack() -> None:
    template = _read(".env.example")
    # Commented entries count: the template ships optional settings commented
    # out so that `cp .env.example .env` does not set them.
    documented = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", template, re.MULTILINE))
    assert documented, "no variables found in .env.example; its shape must have changed"

    unreached = _unreached(documented)
    assert not unreached, (
        f".env.example names {unreached}, which compose.yaml neither forwards nor reads, "
        "so a value set there does nothing. Forward it in x-app-environment as "
        "`VAR: ${VAR:-}`, or drop it from the template."
    )


def test_every_documented_variable_reaches_the_production_stack() -> None:
    reference = _read("website/docs/run/configuration.md")
    # The first cell of every table row: `| `NAME` | default | ...`.
    first_cells = re.findall(r"^\|([^|\n]*)\|", reference, re.MULTILINE)
    documented = {name for cell in first_cells for name in re.findall(r"`([A-Z][A-Z0-9_]*)`", cell)}
    assert "DATABASE_URL" in documented, (
        "no variable tables found in configuration.md; its shape must have changed"
    )

    unreached = _unreached(documented)
    assert not unreached, (
        f"website/docs/run/configuration.md documents {unreached}, which compose.yaml "
        "neither forwards nor reads, so an operator who sets one gets the default. Forward "
        "it in x-app-environment, or document that compose.yaml leaves it out and why."
    )


def test_the_deliberate_omissions_stay_out_of_the_containers() -> None:
    forwarded = _forwarded(_read("compose.yaml"))
    assert not NOT_FORWARDED & forwarded, (
        f"compose.yaml now forwards {sorted(NOT_FORWARDED & forwarded)}; DEBUG in a "
        "production container turns off assert_production_ready(), and NOT_FORWARDED "
        "says why each of the others stays out"
    )


def _published_ports(compose: str) -> list[str]:
    """Every entry of every `ports:` list, comments skipped."""
    ports: list[str] = []
    indent: int | None = None
    for line in compose.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        depth = len(line) - len(line.lstrip())
        if stripped == "ports:":
            indent = depth
            continue
        if indent is not None and depth > indent and stripped.startswith("- "):
            ports.append(stripped[2:].strip().strip("\"'"))
            continue
        indent = None
    return ports


def test_the_dev_stack_publishes_nothing_beyond_the_loopback() -> None:
    """compose.dev.yaml runs DEBUG, tripl:tripl, guest:guest and a Redis with no auth.

    Published on every interface, that is the database (warehouse credentials
    included, unencrypted without ENCRYPTION_KEY) open to anyone on the same
    network, and on Linux Docker's own iptables rules bypass ufw.
    """
    ports = _published_ports(_read("compose.dev.yaml"))
    assert ports, "no published ports found in compose.dev.yaml; its shape must have changed"

    exposed = [port for port in ports if not port.startswith("127.0.0.1:")]
    assert not exposed, f"compose.dev.yaml publishes {exposed} beyond 127.0.0.1"
