FROM ghcr.io/astral-sh/uv:0.9.30 AS uv
FROM python:3.13-slim

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
COPY discord-adapter ./discord-adapter
COPY core/pyproject.toml ./core/pyproject.toml
RUN uv sync --frozen --no-dev --package unified-inbox-discord-adapter

USER 1000:1000
CMD ["unified-inbox-discord"]
