from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

import aiohttp

from unified_inbox_core.errors import DeliveryError
from unified_inbox_core.models import OutboundMessage, Platform


class AdapterError(DeliveryError):
    """Raised when an external adapter rejects or fails a delivery."""


@dataclass(frozen=True, slots=True)
class AdapterDelivery:
    message_id: str


class AdapterClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        urls: dict[Platform, str],
    ) -> None:
        self._session = session
        self._token = token
        self._urls = urls

    async def send(self, platform: Platform, message: OutboundMessage) -> AdapterDelivery:
        metadata: dict[str, object] = {
            "idempotency_key": message.idempotency_key,
            "conversation_id": message.conversation_id,
            "text": message.text,
            "reply_to_message_id": message.reply_to_message_id,
        }
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{self._urls[platform]}/v1/messages"

        if message.image is None:
            async with self._session.post(
                url,
                json=metadata,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as response:
                payload = await self._response_payload(response)
        else:
            form = aiohttp.FormData()
            form.add_field(
                "metadata",
                json.dumps(metadata, separators=(",", ":")),
            )
            form.add_field(
                "image",
                message.image,
                filename=message.image_filename or "image",
                content_type=message.image_mime_type or "application/octet-stream",
            )
            async with self._session.post(
                url,
                data=form,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=90),
            ) as response:
                payload = await self._response_payload(response)

        message_id = payload.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise AdapterError(f"{platform} adapter returned no message_id")
        return AdapterDelivery(message_id=message_id)

    async def edit(
        self,
        platform: Platform,
        conversation_id: str,
        message_id: str,
        text: str | None,
    ) -> AdapterDelivery:
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{self._urls[platform]}/v1/messages/{message_id}"
        async with self._session.patch(
            url,
            json={"conversation_id": conversation_id, "text": text},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=45),
        ) as response:
            payload = await self._response_payload(response)
        edited_message_id = payload.get("message_id")
        if not isinstance(edited_message_id, str) or not edited_message_id:
            raise AdapterError(f"{platform} adapter returned no edited message_id")
        return AdapterDelivery(message_id=edited_message_id)

    async def status(self, platform: Platform) -> dict[str, object]:
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{self._urls[platform]}/health"
        try:
            async with self._session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                return await self._response_payload(response)
        except (aiohttp.ClientError, TimeoutError, AdapterError) as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    async def _response_payload(response: aiohttp.ClientResponse) -> dict[str, object]:
        try:
            raw_object: object = await response.json()
        except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
            message = f"adapter returned HTTP {response.status} with invalid JSON"
            raise AdapterError(message, retryable=response.status >= 500) from exc
        if not isinstance(raw_object, dict):
            raise AdapterError("adapter response must be an object")
        payload = cast(dict[str, object], raw_object)
        if response.status >= 400:
            error = payload.get("error")
            retry_after_value = payload.get("retry_after")
            retry_after = (
                float(retry_after_value) if isinstance(retry_after_value, int | float) else None
            )
            raise AdapterError(
                str(error or f"adapter returned HTTP {response.status}"),
                retryable=response.status == 429 or response.status >= 500,
                retry_after=retry_after,
            )
        return payload
