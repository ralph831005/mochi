"""CLI entry point for the `mochi-agents` command.

Usage:
    mochi-agents          Start the bot
    mochi-agents setup    Interactive token & config setup
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from pathlib import Path

import yaml


def main() -> None:
    """Entry point registered in pyproject.toml [project.scripts]."""
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        run_setup()
    else:
        run_bot()


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def run_setup() -> None:
    """Interactive setup: prompt for API keys, validate, write secret.yaml."""
    print("🍡 Mochi — Setup\n")

    project_root = _find_project_root()
    secret_path = project_root / "secret.yaml"
    config_path = project_root / "config.yaml"

    # Collect secrets
    gemini_key = getpass.getpass("Gemini API Key: ").strip()
    telegram_token = getpass.getpass("Telegram Bot Token: ").strip()

    if not gemini_key or not telegram_token:
        print("❌ Both keys are required.")
        sys.exit(1)

    # Validate Telegram token
    print("\n🔍 Validating Telegram token...")
    bot_info = _validate_telegram_token(telegram_token)
    if bot_info is None:
        print("❌ Invalid Telegram bot token.")
        sys.exit(1)

    bot_username = bot_info.get("username", "unknown")
    print(f"✅ Telegram bot: @{bot_username}")

    # Write secret.yaml
    secrets = {
        "gemini_api_key": gemini_key,
        "telegram_bot_token": telegram_token,
    }
    with open(secret_path, "w") as f:
        yaml.dump(secrets, f, default_flow_style=False)

    # Set restrictive permissions (Unix only)
    try:
        secret_path.chmod(0o600)
    except OSError:
        pass  # Windows doesn't support Unix permissions

    print(f"🔒 Secrets written to {secret_path}")

    # Generate default config.yaml if missing
    if not config_path.exists():
        default_config = {
            "active_client": "telegram",
            "allowed_user_ids": [],
            "data_dir": "./data",
            "agents_dir": "./agents",
            "system_dir": "./system",
        }
        with open(config_path, "w") as f:
            yaml.dump(default_config, f, default_flow_style=False)
        print(f"📄 Default config written to {config_path}")

    print(
        f"\n✅ Setup complete! Add your Telegram user ID to config.yaml "
        f"(allowed_user_ids), then run: mochi-agents"
    )


def _validate_telegram_token(token: str) -> dict | None:
    """Call Telegram getMe to validate the token. Returns bot info dict or None."""
    import httpx

    try:
        resp = httpx.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return data.get("result", {})
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Bot runner
# ---------------------------------------------------------------------------

def run_bot() -> None:
    """Load config, select communication client, start the bot."""
    from mochi_agents.config import get_settings

    settings = get_settings()

    if not settings.gemini_api_key or not settings.telegram_bot_token:
        print(
            "❌ Missing API keys. Run `mochi-agents setup` first.",
            file=sys.stderr,
        )
        sys.exit(1)

    client = _get_client(settings)

    from mochi_agents.bot import MochiBot

    bot = MochiBot(client=client, settings=settings)
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        print("\n🍡 Mochi stopped.")


def _get_client(settings):
    """Factory: select CommunicationClient based on config."""
    if settings.active_client == "telegram":
        from mochi_agents.comm.telegram import TelegramClient

        return TelegramClient(
            token=settings.telegram_bot_token,
            allowed_user_ids=settings.allowed_user_ids,
        )
    raise ValueError(f"Unknown communication client: {settings.active_client}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_project_root() -> Path:
    """Walk up from CWD to find the directory containing config.yaml."""
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / "config.yaml").exists():
            return parent
    return current
