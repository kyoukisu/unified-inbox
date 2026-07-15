from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is missing")
    return value


def _required_int(name: str) -> int:
    raw = _required(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


def _read_secret(path_name: str) -> str:
    path = Path(_required(path_name))
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Unable to read secret file configured by {path_name}") from exc
    if not value:
        raise RuntimeError(f"Secret file configured by {path_name} is empty")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    bind: str
    port: int
    database_path: Path
    internal_token: str
    telegram_token: str
    telegram_chat_id: int
    telegram_allowed_user_id: int
    telegram_poll_timeout: int
    discord_adapter_url: str
    steam_adapter_url: str
    max_image_bytes: int
    log_level: str

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            bind=os.environ.get("CORE_BIND", "0.0.0.0"),
            port=int(os.environ.get("CORE_PORT", "8080")),
            database_path=Path(os.environ.get("CORE_DATABASE", "/data/unified-inbox.sqlite3")),
            internal_token=_read_secret("CORE_INTERNAL_TOKEN_FILE"),
            telegram_token=_read_secret("TELEGRAM_BOT_TOKEN_FILE"),
            telegram_chat_id=_required_int("TELEGRAM_CHAT_ID"),
            telegram_allowed_user_id=_required_int("TELEGRAM_ALLOWED_USER_ID"),
            telegram_poll_timeout=int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "30")),
            discord_adapter_url=_required("DISCORD_ADAPTER_URL").rstrip("/"),
            steam_adapter_url=_required("STEAM_ADAPTER_URL").rstrip("/"),
            max_image_bytes=int(os.environ.get("MAX_IMAGE_BYTES", "20971520")),
            log_level=os.environ.get("CORE_LOG_LEVEL", "INFO").upper(),
        )
