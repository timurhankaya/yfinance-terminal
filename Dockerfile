# One image, every service.
#
# The API, the scheduler, the stream, both relays and every `yfin` command
# run from this image; only the CMD differs. One image rather than five
# because they share the whole package anyway -- the dataset catalogue is
# built from the dataset registry at import, so even serving the API pulls
# in yfinance, pandas and numpy. Five images would each carry that and
# would each be a separate thing to keep in step.
#
# All four extras are installed for the same reason: a service that had to
# be given its own image to gain a dependency would make "which image is
# this" a question with a wrong answer.
#
# Two stages so the build tools and the uv cache do not ship.

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
RUN chmod +x /app/docker/entrypoint.sh

# yfinance's tz/cookie/ISIN cache is SQLite and it WRITES. The default is
# relative, which puts it under the root-owned WORKDIR, and a sync
# subprocess running as `yfin` dies on its first request trying to create
# it. Made here and owned by the user; compose mounts a volume over it so
# the cache survives a container replacement.
ENV YF_TZ_CACHE_DIR=/var/cache/yfin
RUN mkdir -p /var/cache/yfin && chown yfin:yfin /var/cache/yfin

# NOT set here. `prometheus_client` reads PROMETHEUS_MULTIPROC_DIR at
# import time, process-wide, so an image-wide value would put the
# scheduler, the stream and both relays into multiprocess mode as well --
# where each writes mmap files nobody collects and its /metrics goes
# quiet. The `api` service sets it in compose, alone.
#
#   PROMETHEUS_MULTIPROC_DIR

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

# The API's. Every other service overrides it in compose with a probe on
# its own /metrics port -- a scheduler answering an HTTP health check on
# 8000 would be answering for a server it is not running.
#
# Liveness only: /health touches nothing else, so it stays truthful while
# the database or Redis is down. Readiness is the orchestrator's call, and
# /health/ready is there for it.
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health').read()"

# Workers, not threads, because the endpoints are synchronous (design K1):
# concurrency comes from processes here, and each one carries its own
# connection pool.
#
# --no-proxy-headers is deliberate and load-bearing. uvicorn's own proxy
# handling REPLACES request.client with whatever X-Forwarded-For claims,
# and the API resolves the client address itself against a configured
# CIDR list (YFAPI_TRUSTED_PROXIES). If uvicorn rewrote it first, the
# real peer -- the only thing that check has to go on -- would already be
# gone, and every rate limit key would be attacker-chosen.
CMD ["uvicorn", "yfin.api.app:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4", \
     "--no-proxy-headers"]
