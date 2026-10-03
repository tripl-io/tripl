# syntax=docker/dockerfile:1

FROM oven/bun:1.4.2-slim AS base

WORKDIR /app

COPY package.json bun.lock bunfig.toml ./
RUN --mount=type=cache,id=bun,target=/root/.bun/install/cache \
    bun install --frozen-lockfile

COPY . .

FROM base AS dev

EXPOSE 5173

CMD ["bun", "run", "dev", "--host", "0.0.0.0", "--port", "5173", "--strictPort"]

FROM base AS build

RUN bun run build

# Install nginx AND the brotli module from the same alpine repo so their
# versions match. The official `nginx:*-alpine` image compiles nginx itself,
# which diverges from the community `nginx-mod-http-brotli` build and can fail
# at load with a module-version mismatch. Pinning the alpine release keeps nginx
# and the brotli module in lockstep.
FROM alpine:3.24 AS runtime

RUN apk add --no-cache nginx nginx-mod-http-brotli wget

COPY --from=build /app/dist /usr/share/nginx/html
# Our nginx.conf has top-level load_module directives for brotli and fully
# replaces the packaged config.
COPY nginx.conf /etc/nginx/nginx.conf

# Run nginx as the non-root `nginx` user (shipped by the alpine package). The
# master writes its pid and temp/cache files plus logs, so make those paths
# owned by that user. Listening on 5173 (>1024) needs no extra capabilities.
RUN mkdir -p /var/cache/nginx /var/lib/nginx/tmp \
    && touch /var/run/nginx.pid \
    && chown -R nginx:nginx /var/cache/nginx /var/lib/nginx /var/log/nginx \
        /var/run/nginx.pid /usr/share/nginx/html
USER nginx

EXPOSE 5173

# Probe the SPA document; nginx serves it at the root path.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["wget", "--quiet", "--tries=1", "--spider", "http://localhost:5173/"]

CMD ["nginx", "-g", "daemon off;"]
