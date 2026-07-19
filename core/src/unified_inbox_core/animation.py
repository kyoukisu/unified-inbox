from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)
DISCORD_GIF_LIMIT_BYTES = 9_500_000


@dataclass(frozen=True, slots=True)
class GifEncodingProfile:
    width: int
    fps: int
    colors: int


_PROFILES = (
    GifEncodingProfile(width=480, fps=12, colors=128),
    GifEncodingProfile(width=360, fps=10, colors=96),
    GifEncodingProfile(width=320, fps=8, colors=64),
)


class AnimationConversionError(RuntimeError):
    """Raised when ffmpeg cannot convert an animation."""


async def convert_mp4_to_gif(content: bytes, max_output_bytes: int) -> bytes | None:
    if max_output_bytes <= 0:
        raise ValueError("GIF output limit must be positive")
    if shutil.which("ffmpeg") is None:
        _LOGGER.error("ffmpeg is unavailable; relaying Telegram animation as MP4")
        return None

    for profile in _PROFILES:
        try:
            converted = await _encode_gif(content, profile, max_output_bytes)
        except AnimationConversionError as exc:
            _LOGGER.warning("Unable to convert Telegram animation to GIF: %s", exc)
            return None
        if converted is not None:
            return converted

    _LOGGER.warning(
        "Converted Telegram animation exceeds the %s-byte Discord GIF limit; relaying MP4",
        max_output_bytes,
    )
    return None


async def _encode_gif(
    content: bytes,
    profile: GifEncodingProfile,
    max_output_bytes: int,
) -> bytes | None:
    input_descriptor, input_name = tempfile.mkstemp(prefix="unified-inbox-", suffix=".mp4")
    output_descriptor, output_name = tempfile.mkstemp(prefix="unified-inbox-", suffix=".gif")
    os.close(input_descriptor)
    os.close(output_descriptor)
    input_path = Path(input_name)
    output_path = Path(output_name)
    filter_graph = (
        f"[0:v]fps={profile.fps},"
        f"scale=w='min({profile.width},iw)':h=-2:flags=lanczos,split[s0][s1];"
        f"[s0]palettegen=max_colors={profile.colors}:stats_mode=diff[p];"
        "[s1][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle"
    )
    try:
        await asyncio.to_thread(input_path.write_bytes, content)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-an",
            "-filter_complex",
            filter_graph,
            "-loop",
            "0",
            str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise AnimationConversionError("ffmpeg timed out") from exc
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()[-500:]
            raise AnimationConversionError(detail or f"ffmpeg exited with {process.returncode}")
        return await asyncio.to_thread(_read_limited, output_path, max_output_bytes)
    except OSError as exc:
        raise AnimationConversionError("ffmpeg could not be started") from exc
    finally:
        await asyncio.gather(
            asyncio.to_thread(input_path.unlink, missing_ok=True),
            asyncio.to_thread(output_path.unlink, missing_ok=True),
        )


def _read_limited(path: Path, limit: int) -> bytes | None:
    with path.open("rb") as file:
        content = file.read(limit + 1)
    return content if len(content) <= limit else None
