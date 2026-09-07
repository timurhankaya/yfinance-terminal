# The API image.
#
# It carries the whole project, not a trimmed-down subset, and that is a
# consequence worth naming: the dataset catalogue is built from the
# dataset registry at import, so serving the API pulls in yfinance,
# pandas and numpy. The alternative -- declaring exposures away from the
# datasets that own them -- would re-create the hand-maintained map the
# design exists to avoid. A larger image is the cheaper of the two costs.
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
    uv sync --frozen --no-install-project --extra api

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra api


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

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER yfin
EXPOSE 8000

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
