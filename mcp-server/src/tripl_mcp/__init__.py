"""tripl-mcp — standalone MCP server that proxies a curated tripl REST toolset."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

# The DISTRIBUTION name, not the import package name: this is `tripl-mcp`, while
# the module you are reading is `tripl_mcp` — the same split the sibling `tripl`
# distribution documents in cli/src/tripl_cli/__init__.py.
DISTRIBUTION_NAME = "tripl-mcp"

try:
    __version__ = version(DISTRIBUTION_NAME)
except PackageNotFoundError:  # pragma: no cover - source tree with no install
    # Running straight out of a checkout without `uv sync`. Report something
    # honest rather than a plausible-looking number. Deliberately NOT a
    # hardcoded "0.1.0" duplicating pyproject's `version` — publish-mcp.yml gates
    # the release tag against `uv version --short`, which reads pyproject, so a
    # second literal here drifts unnoticed and ships in both the User-Agent and
    # the --help banner below.
    __version__ = "0.0.0+unknown"

# What this server calls itself on the wire. It lives HERE rather than in the
# client because the client now ships in the shared `tripl` distribution and
# must not claim to be tripl-mcp.
#
# Both transports have to pass it: server.py builds the stdio lifespan pool, and
# TriplClient builds a per-request transport for streamable-http. Miss either
# and half this server's traffic reports the CLI's default UA in the operator's
# access logs — the one signal they have for telling agent traffic from CLI
# traffic apart.
USER_AGENT = f"tripl-mcp/{__version__}"
