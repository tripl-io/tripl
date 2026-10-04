# Build context is the REPOSITORY ROOT, not mcp-server/ — note the COPY paths.
#
# tripl-mcp depends on the `tripl` distribution, which uv resolves from ../cli
# via [tool.uv.sources] while it is unpublished, and ../cli sits
# outside an mcp-server/ context. So `docker build ./mcp-server` now fails at
# `uv sync`; build it as `docker build -f mcp-server/Dockerfile .`. compose.yaml,
# compose.dev.yaml and release.yml all pass context `.` for this reason.
#
# The in-image layout mirrors the repo (/app/cli, /app/mcp-server) so the
# ../cli path recorded in uv.lock resolves unchanged.
FROM python:3.14-slim-trixie

# Pinned on purpose: :latest makes the image non-reproducible, and uv.lock
# declares revision = 3, which needs uv >= 0.8. Bump deliberately.
COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv

WORKDIR /app/mcp-server
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# The sibling package first, and whole: uv builds it from source, so it needs
# pyproject.toml, src/, README.md (declared as `readme`) and LICENSE.
COPY cli /app/cli
COPY mcp-server/pyproject.toml mcp-server/uv.lock mcp-server/README.md ./
COPY mcp-server/src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/mcp-server/.venv/bin:$PATH"

# Drop privileges, matching backend/Dockerfile and the root Dockerfile. The
# venv is built as root and stays read-only to this user; UV_COMPILE_BYTECODE
# above already wrote the .pyc files, so nothing needs to write at runtime.
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid 1000 --no-create-home app
USER app

# stdio by default; compose sidecars override the command with flags only
# (the ENTRYPOINT below already provides the `tripl-mcp` executable):
#   command: ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
EXPOSE 8765
ENTRYPOINT ["tripl-mcp"]
