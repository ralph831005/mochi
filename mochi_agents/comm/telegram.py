"""Telegram CommunicationClient — long-polling implementation using httpx."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

import httpx

from mochi_agents.comm.base import CommunicationClient, IncomingMessage

logger = logging.getLogger(__name__)

BASE_URL = "https://api.telegram.org/bot{token}"


class TelegramClient:
    """Telegram Bot API client using long polling (getUpdates)."""

    def __init__(self, token: str, allowed_user_ids: list[int]) -> None:
        self.token = token
        self.allowed_user_ids = set(allowed_user_ids)
        self.base_url = BASE_URL.format(token=token)
        self._offset: int = 0
        self._http: httpx.AsyncClient | None = None

    async def _get_http(self) -> httpx.AsyncClient:
        """Lazily create the async HTTP client."""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        return self._http

    async def poll(self) -> AsyncIterator[IncomingMessage]:
        """Long-poll getUpdates and yield parsed IncomingMessage objects."""
        http = await self._get_http()

        while True:
            try:
                resp = await http.get(
                    f"{self.base_url}/getUpdates",
                    params={"offset": self._offset, "timeout": 30},
                )
                data = resp.json()

                if not data.get("ok"):
                    logger.error(f"Telegram API error: {data}")
                    continue

                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    message = self._parse_update(update)
                    if message and self._check_allowlist(message.user_id):
                        yield message

            except httpx.TimeoutException:
                continue  # Normal timeout on long poll
            except Exception as e:
                logger.error(f"Telegram poll error: {e}", exc_info=True)
                import asyncio
                await asyncio.sleep(5)  # Back off on errors

    def _parse_update(self, update: dict) -> IncomingMessage | None:
        """Extract an IncomingMessage from a Telegram update."""
        msg = update.get("message")
        if not msg:
            return None

        user = msg.get("from", {})
        user_id = str(user.get("id", ""))
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "")

        if not user_id or not chat_id:
            return None

        return IncomingMessage(
            user_id=user_id,
            chat_id=chat_id,
            text=text,
        )

    def _check_allowlist(self, user_id: str) -> bool:
        """Check if a user is allowed. Empty allowlist = allow all."""
        if not self.allowed_user_ids:
            return True
        try:
            return int(user_id) in self.allowed_user_ids
        except ValueError:
            return False

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> None:
        """Send a message via the Telegram Bot API."""
        http = await self._get_http()

        formatted = self.format_response(text)

        # Telegram has a 4096 character limit per message
        chunks = self._chunk_text(formatted, max_len=4000)

        for chunk in chunks:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
            }
            payload.update(kwargs)

            try:
                resp = await http.post(f"{self.base_url}/sendMessage", json=payload)
                data = resp.json()
                if not data.get("ok"):
                    # Retry without parse_mode if Markdown fails
                    payload["parse_mode"] = None
                    await http.post(f"{self.base_url}/sendMessage", json=payload)
            except Exception as e:
                logger.error(f"Failed to send message to {chat_id}: {e}")

    def format_response(self, agent_response: str) -> str:
        """Pass through for now — agent responses are already Markdown-compatible."""
        return agent_response

    def _chunk_text(self, text: str, max_len: int = 4000) -> list[str]:
        """Split text into chunks that fit Telegram's message limit."""
        if len(text) <= max_len:
            return [text]

        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            # Try to break at a newline
            split_at = text.rfind("\n", 0, max_len)
            if split_at <= 0:
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")

        return chunks

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http and not self._http.is_closed:
            await self._http.aclose()
