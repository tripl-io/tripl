# syntax=docker/dockerfile:1
#
# The frontend's development image: compose.dev.yaml builds the `dev` target
# and runs the Vite dev server in it, syncing ./src into it as you edit. The
# image tripl ships is the root Dockerfile's `runtime` target, where the API
# serves the built SPA itself.

FROM oven/bun:1.4.2-slim AS base

WORKDIR /app

COPY package.json bun.lock bunfig.toml ./
RUN --mount=type=cache,id=bun,target=/root/.bun/install/cache \
    bun install --frozen-lockfile

COPY . .

FROM base AS dev

EXPOSE 5173

CMD ["bun", "run", "dev", "--host", "0.0.0.0", "--port", "5173", "--strictPort"]
