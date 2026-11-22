# syntax=docker/dockerfile:1.7
# One Dockerfile for every service; each image contains only its own deps.
#
#   docker build --build-arg SERVICE=model_gateway --build-arg MODULE=model_gateway.main:app .
#   docker build --build-arg SERVICE=topic_ev_research --build-arg MODULE=ev_research.main:app \
#                --build-arg INSTALL_PLAYWRIGHT=true .
ARG PYTHON_IMAGE=python:3.13-slim-bookworm

FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

# ---------------------------------------------------------------- builder
FROM ${PYTHON_IMAGE} AS builder
ARG SERVICE
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY libs/agency_platform /src/libs/agency_platform
COPY services/${SERVICE}/pyproject.toml services/${SERVICE}/uv.lock /src/services/${SERVICE}/
WORKDIR /src/services/${SERVICE}
# Dependencies first (cached layer), then the service code.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-editable
COPY services/${SERVICE} /src/services/${SERVICE}
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
ARG MODULE
ARG INSTALL_PLAYWRIGHT=false
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/app/config \
    PROMPTS_DIR=/app/prompts \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CRAWL4AI_NO_CLOUD_NOTICE=1 \
    APP_MODULE=${MODULE}

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app app

COPY --from=builder /app/.venv /app/.venv

# Only the EV topic drives a headless browser (crawl4ai/Playwright).
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
        apt-get update && apt-get install -y --no-install-recommends libatomic1 \
        && playwright install --with-deps chromium \
        && chmod -R o+rx /ms-playwright \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# Baked-in defaults; Kubernetes/compose mount the live versions over these.
COPY config /app/config
COPY prompts /app/prompts
COPY migrations /app/migrations

WORKDIR /app
USER 10001:10001
EXPOSE 8000
# Proxy headers are handled explicitly by the app (trusted hop count), not uvicorn.
CMD ["sh", "-c", "exec uvicorn \"$APP_MODULE\" --host 0.0.0.0 --port 8000 --no-proxy-headers --no-server-header --timeout-graceful-shutdown 30"]
