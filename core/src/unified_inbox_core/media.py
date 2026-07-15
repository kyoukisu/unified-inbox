from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import aiohttp

_ALLOWED_HOST_SUFFIXES: dict[str, tuple[str, ...]] = {
    "discord": (
        ".discordapp.com",
        ".discordapp.net",
        ".discord.media",
        ".discord.com",
    ),
    "steam": (
        ".steamusercontent.com",
        ".steamuserimages-a.akamaihd.net",
        ".steamstatic.com",
        ".steampowered.com",
        ".giphy.com",
    ),
}


class MediaDownloadError(RuntimeError):
    """Raised when a remote attachment cannot be downloaded safely."""


def is_allowed_media_url(platform: str, url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
        return False
    hostname = (parsed.hostname or "").lower()
    suffixes = _ALLOWED_HOST_SUFFIXES.get(platform, ())
    return any(hostname == suffix[1:] or hostname.endswith(suffix) for suffix in suffixes)


async def _bounded_chunks(
    response: aiohttp.ClientResponse,
    max_bytes: int,
) -> AsyncIterator[bytes]:
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise MediaDownloadError(f"attachment exceeds {max_bytes} bytes")
        yield chunk


async def download_media(
    session: aiohttp.ClientSession,
    platform: str,
    url: str,
    max_bytes: int,
) -> bytes:
    if not is_allowed_media_url(platform, url):
        raise MediaDownloadError("attachment URL host is not allowlisted")

    timeout = aiohttp.ClientTimeout(total=45, connect=10)
    try:
        async with session.get(url, allow_redirects=False, timeout=timeout) as response:
            if response.status != 200:
                raise MediaDownloadError(f"attachment download returned HTTP {response.status}")
            if response.content_length is not None and response.content_length > max_bytes:
                raise MediaDownloadError(f"attachment exceeds {max_bytes} bytes")
            chunks = [chunk async for chunk in _bounded_chunks(response, max_bytes)]
    except aiohttp.ClientError as exc:
        raise MediaDownloadError("attachment download failed") from exc

    return b"".join(chunks)
