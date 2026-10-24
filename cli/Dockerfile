# The tripl CLI for CI jobs, published as ghcr.io/tripl-io/tripl-cli by
# publish-cli.yml on every cli-v* tag: Python, `tripl` on PATH and a shell, and
# no ENTRYPOINT, so GitLab CI, Bitbucket Pipelines, Jenkins, CircleCI or Azure
# Pipelines run a job's script in it as they would in python:3.x-slim, without
# installing the CLI from PyPI on every run.
#
# Build context is the REPOSITORY ROOT, not cli/, for the license-notice script
# under backend/scripts: `docker build -f cli/Dockerfile .`. ci.yml and
# publish-cli.yml both pass context `.`.

# Pinned on purpose: :latest makes the image non-reproducible. A stage of its
# own rather than `COPY --from=<image>` because Dependabot bumps FROM lines
# only. Bump it together with the uv images of the root Dockerfile,
# backend/Dockerfile and mcp-server/Dockerfile.
FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

FROM python:3.14-slim-trixie AS build

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app/cli
# No Python downloads: the venv must run on this stage's interpreter, which the
# runtime stage has at the same path. A Python uv fetched would stay behind here.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

# What uv builds the package from: pyproject.toml, src/, README.md (declared as
# `readme`) and LICENSE.
COPY cli/pyproject.toml cli/uv.lock cli/README.md cli/LICENSE ./
COPY cli/src ./src
# --locked, like CI: it fails on a uv.lock that no longer describes
# pyproject.toml. --no-editable installs the package itself into the venv, so
# the runtime stage needs neither the source nor uv.
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim-trixie

COPY --from=build /app/cli/.venv /app/cli/.venv
# A CI log shows stdout and stderr in the order the CLI wrote them.
ENV PATH="/app/cli/.venv/bin:$PATH" PYTHONUNBUFFERED=1

# Third-party license notices for the venv and the Debian packages, in
# /app/licenses (README.md is the index).
RUN --mount=type=bind,source=backend/scripts/third_party_licenses.py,target=/tmp/third_party_licenses.py \
    /app/cli/.venv/bin/python /tmp/third_party_licenses.py --out /app/licenses

# Root, unlike the service images and like the python:3.x-slim images CI jobs
# use: a job writes its reports into a workspace that belongs to the runner,
# and a uid of this image's own could not where a runner mounts that workspace
# from the host and keeps the image's user (GitHub's container jobs, for one).
# The image serves nothing; each job runs one command in it and exits.
#
# For `docker run --rm -v "$PWD:/src" ghcr.io/tripl-io/tripl-cli tripl check`;
# a CI job runs in its own checkout directory instead.
WORKDIR /src
CMD ["tripl", "--help"]
