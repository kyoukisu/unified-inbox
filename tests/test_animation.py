from __future__ import annotations

import shutil

import pytest

from unified_inbox_core import animation


@pytest.mark.asyncio
async def test_gif_conversion_retries_with_smaller_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[animation.GifEncodingProfile] = []

    async def fake_encode(
        content: bytes,
        profile: animation.GifEncodingProfile,
        max_output_bytes: int,
    ) -> bytes | None:
        assert content == b"mp4"
        assert max_output_bytes == 100
        attempts.append(profile)
        return b"GIF89a" if len(attempts) == 2 else None

    def fake_which(command: str) -> str | None:
        assert command == "ffmpeg"
        return "/usr/bin/ffmpeg"

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(animation, "_encode_gif", fake_encode)

    converted = await animation.convert_mp4_to_gif(b"mp4", 100)

    assert converted == b"GIF89a"
    assert [(profile.width, profile.fps, profile.colors) for profile in attempts] == [
        (480, 12, 128),
        (360, 10, 96),
    ]


@pytest.mark.asyncio
async def test_gif_conversion_falls_back_when_ffmpeg_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_ffmpeg(command: str) -> str | None:
        assert command == "ffmpeg"
        return None

    monkeypatch.setattr(shutil, "which", missing_ffmpeg)

    assert await animation.convert_mp4_to_gif(b"mp4", 100) is None
