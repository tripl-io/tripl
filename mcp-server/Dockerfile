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

# Pinned on purpose: :latest makes the image non-reproducible, and uv.lock
# declares revision = 3, which needs uv >= 0.8. A stage of its own rather than
# `COPY --from=<image>` because Dependabot bumps FROM lines only. Bump it
# together with the uv images of the root Dockerfile and backend/Dockerfile.
FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

FROM python:3.14-slim-trixie

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app/mcp-server
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# The sibling package first, and whole: uv builds it from source, so it needs
# pyproject.toml, src/, README.md (declared as `readme`) and LICENSE.
COPY cli /app/cli
COPY mcp-server/pyproject.toml mcp-server/uv.lock mcp-server/README.md ./
COPY mcp-server/src ./src
RUN uv sync --frozen --no-dev

# Third-party license notices for the venv and the Debian packages, in
# /app/licenses (README.md is the index).
RUN --mount=type=bind,source=backend/scripts/third_party_licenses.py,target=/tmp/third_party_licenses.py \
    .venv/bin/python /tmp/third_party_licenses.py --out /app/licenses

ENV PATH="/app/mcp-server/.venv/bin:$PATH"

# UV_COMPILE_BYTECODE above compiles what uv installs into the venv, `tripl`
# from ../cli included, but not tripl-mcp itself: the project is installed
# editable and runs from ./src.
RUN python -m compileall -q src

# Drop privileges, matching the root Dockerfile. The venv and the source are
# built as root and stay read-only to this user, with their .pyc files already
# written, so nothing needs to write at runtime.
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid 1000 --no-create-home app
USER app

# stdio by default; compose sidecars override the command with flags only
# (the ENTRYPOINT below already provides the `tripl-mcp` executable):
#   command: ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
EXPOSE 8765
ENTRYPOINT ["tripl-mcp"]
