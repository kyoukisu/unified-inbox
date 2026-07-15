FROM ghcr.io/astral-sh/uv:0.11.28 AS uv
FROM python:3.13-slim

ARG APP_UID=1000

COPY --from=uv /uv /usr/local/bin/uv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=300 \
    UV_HTTP_RETRIES=5

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY core ./core
COPY discord-adapter/pyproject.toml ./discord-adapter/pyproject.toml
RUN uv sync --frozen --no-dev --package unified-inbox-core \
    && mkdir -p /data \
    && chown "${APP_UID}:0" /data

USER ${APP_UID}:0
CMD ["unified-inbox-core"]
