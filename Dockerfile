# One image, every service: the API, the scheduler, the stream, both relays
# and every `yfin` command run from it with all four extras; only the CMD
# differs. Two stages so the build tools and the uv cache do not ship.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies before source: this layer is rebuilt only when the lock
# file changes, not on every code edit.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project \
        --extra api --extra scheduler --extra kafka --extra otel

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra api --extra scheduler --extra kafka --extra otel


# The browser UI. Built here so the runtime image needs no Node; the
# output lands where the Python package expects it (pyproject:
# package-data "yfin.ui" = static/dist/**).
FROM node:22-slim AS web

WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
# vite.config.ts writes to ../src/yfin/ui/static/dist, i.e. /app/src/...
RUN npm run build


FROM python:3.13-slim-bookworm AS runtime

# Not root. The API reads a database and answers HTTP; nothing it does
# needs privileges, and a container that runs as root turns any code
# execution bug into a much larger problem.
RUN groupadd --system yfin && useradd --system --gid yfin --home /app yfin

WORKDIR /app
COPY --from=builder --chown=yfin:yfin /app/.venv /app/.venv
COPY --from=builder --chown=yfin:yfin /app/src /app/src
COPY --from=web --chown=yfin:yfin /app/src/yfin/ui/static/dist /app/src/yfin/ui/static/dist
COPY --chown=yfin:yfin docker/entrypoint.sh /app/docker/entrypoint.sh
# `yfin db upgrade` and `yfin config seed` resolve alembic.ini and
# config/settings.seed.json relative to /app, so the image must carry them.
COPY --chown=yfin:yfin alembic.ini /app/alembic.ini
COPY --chown=yfin:yfin migrations /app/migrations
COPY --chown=yfin:yfin config /app/config
RUN chmod +x /app/docker/entrypoint.sh

# yfinance's tz/cookie/ISIN cache is SQLite and it WRITES. The default is
# relative, which puts it under the root-owned WORKDIR, and a sync
# subprocess running as `yfin` dies on its first request trying to create
# it. Made here and owned by the user; compose mounts a volume over it so
# the cache survives a container replacement.
ENV YF_TZ_CACHE_DIR=/var/cache/yfin
RUN mkdir -p /var/cache/yfin && chown yfin:yfin /var/cache/yfin

# PROMETHEUS_MULTIPROC_DIR is deliberately not set here: it is read at
# import time, and only the `api` service (in compose) may run in
# multiprocess mode.

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER yfin
EXPOSE 8000

# Empties PROMETHEUS_MULTIPROC_DIR when it is set, then execs the command.
# `exec` matters: without it this shell stays PID 1, SIGTERM stops here,
# and `docker stop` waits out its timeout before SIGKILLing a scheduler
# mid-job.
ENTRYPOINT ["/app/docker/entrypoint.sh"]

# The API's liveness probe; every other service disables it in compose.
# /health touches no dependency, /health/ready is the readiness check.
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health').read()"

# Workers, not threads: the endpoints are synchronous.
# --no-proxy-headers is load-bearing: the API resolves the client address
# itself against YFAPI_TRUSTED_PROXIES; if uvicorn rewrote request.client
# from X-Forwarded-For first, every rate limit key would be attacker-chosen.
CMD ["uvicorn", "yfin.api.app:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4", \
     "--no-proxy-headers"]
