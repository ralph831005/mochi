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
                await asyncio.sleep(5)  # Back off on errors

    async def ack(self) -> None:
        """Send a quick getUpdates to tell Telegram we processed up to the current offset."""
        try:
            http = await self._get_http()
            await http.get(
                f"{self.base_url}/getUpdates",
                params={"offset": self._offset, "timeout": 1},
            )
        except Exception:
            pass

    def _parse_update(self, update: dict) -> IncomingMessage | None:
        """Extract an IncomingMessage from a Telegram update."""
        # Handle regular messages
        msg = update.get("message")
        if msg:
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

        # Handle callback queries (inline keyboard button presses)
        callback = update.get("callback_query")
        if callback:
            user = callback.get("from", {})
            user_id = str(user.get("id", ""))
            message = callback.get("message", {})
            chat_id = str(message.get("chat", {}).get("id", ""))
            data = callback.get("data", "")
            callback_id = callback.get("id", "")

            if not user_id or not chat_id:
                return None

            # Answer the callback to dismiss the loading spinner
            # (fire-and-forget, don't block the poll loop)
            import asyncio
            asyncio.create_task(self._answer_callback(callback_id))

            return IncomingMessage(
                user_id=user_id,
                chat_id=chat_id,
                text=data,  # callback data becomes the message text
                source="callback",
            )

        return None

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
                "parse_mode": "HTML",
            }
            payload.update(kwargs)

            try:
                resp = await http.post(f"{self.base_url}/sendMessage", json=payload)
                data = resp.json()
                if not data.get("ok"):
                    # Retry without parse_mode if HTML fails for any unclosed tags
                    payload.pop("parse_mode", None)
                    await http.post(f"{self.base_url}/sendMessage", json=payload)
            except Exception as e:
                logger.error(f"Failed to send message to {chat_id}: {e}")

    def format_response(self, agent_response: str) -> str:
        """Convert standard markdown (LLM output) to Telegram HTML format."""
        import re
        import html
        
        # 1. Escape HTML constraints to avoid injection errors
        text = html.escape(agent_response)
        
        # 2. Code blocks (we use DOTALL to capture newlines)
        text = re.sub(r'```[a-zA-Z0-9]*\n(.*?)\n```', r'<pre>\1</pre>', text, flags=re.DOTALL)
        
        # 3. Inline code
        text = re.sub(r'`(.*?)`', r'<code>\1</code>', text)
        
        # 4. Bold Headings
        text = re.sub(r'^#+\s+(.*)', r'<b>\1</b>', text, flags=re.MULTILINE)
        
        # 5. Bold text
        text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
        
        # 6. Italic text (single *, taking care not to wrap bold again if we did it out of order but it's already <b>)
        # Note: we skip _italic_ conversion here to avoid breaking snake_case variables.
        text = re.sub(r'(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
        
        return text

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

    async def send_with_keyboard(
        self,
        chat_id: str,
        text: str,
        buttons: list[list[dict[str, str]]],
    ) -> None:
        """Send a message with an inline keyboard.

        Args:
            chat_id: Target chat ID.
            text: Message text.
            buttons: 2D array of button rows. Each button is
                     {"text": "Label", "callback_data": "data_string"}.
        """
        http = await self._get_http()

        payload = {
            "chat_id": chat_id,
            "text": self.format_response(text),
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": buttons,
            },
        }

        try:
            resp = await http.post(f"{self.base_url}/sendMessage", json=payload)
            data = resp.json()
            if not data.get("ok"):
                # Retry without parse_mode
                payload.pop("parse_mode", None)
                await http.post(f"{self.base_url}/sendMessage", json=payload)
        except Exception as e:
            logger.error(f"Failed to send keyboard message to {chat_id}: {e}")

    async def _answer_callback(self, callback_id: str) -> None:
        """Answer a callback query to dismiss the loading spinner."""
        try:
            http = await self._get_http()
            await http.post(
                f"{self.base_url}/answerCallbackQuery",
                json={"callback_query_id": callback_id},
            )
        except Exception:
            pass  # non-critical, just dismisses the spinner

    async def set_commands(self, agents: list[dict[str, str]]) -> None:
        """Register slash commands with Telegram so they appear in the / menu.

        Args:
            agents: list of {"name": "nutritionist", "description": "Manages daily diet..."}
        """
        http = await self._get_http()

        commands = [
            {"command": "reload", "description": "Reload config, registry, and tools"},
        ]
        for agent in agents:
            commands.append({
                "command": agent["name"],
                "description": agent.get("description", agent["name"])[:256],
            })

        try:
            resp = await http.post(
                f"{self.base_url}/setMyCommands",
                json={"commands": commands},
            )
            data = resp.json()
            if data.get("ok"):
                logger.info(f"Registered {len(commands)} Telegram commands")
            else:
                logger.warning(f"Failed to set Telegram commands: {data}")
        except Exception as e:
            logger.error(f"Failed to set Telegram commands: {e}")

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http and not self._http.is_closed:
            await self._http.aclose()
