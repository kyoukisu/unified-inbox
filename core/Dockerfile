FROM ghcr.io/astral-sh/uv:0.9.30 AS uv
FROM python:3.14-slim

ARG APP_UID=1000

COPY --from=uv /uv /usr/local/bin/uv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=300 \
    UV_HTTP_RETRIES=5

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY core ./core
COPY discord-adapter/pyproject.toml ./discord-adapter/pyproject.toml
RUN uv sync --frozen --no-dev --package unified-inbox-core \
    && mkdir -p /data \
    && chown "${APP_UID}:0" /data

USER ${APP_UID}:0
CMD ["unified-inbox-core"]
