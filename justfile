set shell := ["bash", "-euo", "pipefail", "-c"]

init:
    ./scripts/init-secrets.sh
    test -f .env || cp .env.example .env

lock:
    uv lock
    cd steam-adapter && npm install --package-lock-only --ignore-scripts

lint:
    uv run ruff check .
    uv run ruff format --check .
    uv run pyright

format:
    uv run ruff check --fix .
    uv run ruff format .

unit:
    uv run pytest
    cd steam-adapter && npm test

build:
    docker compose build

config:
    docker compose config --quiet

up:
    docker compose up -d --build

down:
    docker compose down

logs:
    docker compose logs -f --tail=100

steam-auth:
    docker compose --profile tools run --rm steam-auth
