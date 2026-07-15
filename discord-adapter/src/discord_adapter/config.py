from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is missing")
    return value


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
    core_url: str
    internal_token: str
    discord_token: str
    max_image_bytes: int
    log_level: str

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            bind=os.environ.get("ADAPTER_BIND", "0.0.0.0"),
            port=int(os.environ.get("ADAPTER_PORT", "8081")),
            core_url=_required("CORE_URL").rstrip("/"),
            internal_token=_read_secret("CORE_INTERNAL_TOKEN_FILE"),
            discord_token=_read_secret("DISCORD_USER_TOKEN_FILE"),
            max_image_bytes=int(os.environ.get("MAX_IMAGE_BYTES", "20971520")),
            log_level=os.environ.get("ADAPTER_LOG_LEVEL", "INFO").upper(),
        )
