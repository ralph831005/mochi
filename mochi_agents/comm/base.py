"""Abstract CommunicationClient protocol and shared data types.

All platform clients (Telegram, Slack, CLI) implement this protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Protocol, runtime_checkable


@dataclass
class IncomingMessage:
    """Platform-agnostic incoming message."""

    user_id: str
    chat_id: str
    text: str | None = None
    attachments: list[Any] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "user"  # "user", "scheduler", "internal"


@runtime_checkable
class CommunicationClient(Protocol):
    """Interface that all platform clients must implement."""

    async def poll(self) -> AsyncIterator[IncomingMessage]:
        """Async generator that yields incoming messages from the platform."""
        ...

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> None:
        """Send a response to the user through the platform's API."""
        ...

    def format_response(self, agent_response: str) -> str:
        """Convert agent response to platform-native format."""
        ...
