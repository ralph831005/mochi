"""Reloadable configuration singleton.

Merges `config.yaml` (non-secret, committed) and `secret.yaml` (gitignored, created by setup).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


def _find_project_root() -> Path:
    """Walk up from CWD to find the directory containing config.yaml."""
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / "config.yaml").exists():
            return parent
    return current


class Settings(BaseModel):
    """Merged configuration from config.yaml + secret.yaml."""

    # --- From secret.yaml ---
    gemini_api_key: str = ""
    telegram_bot_token: str = ""
    api_keys: dict[str, str] = Field(default_factory=dict)  # named key registry

    # --- From config.yaml ---
    active_client: str = "telegram"
    allowed_user_ids: list[int] = Field(default_factory=list)
    data_dir: Path = Path("./data")
    agents_dir: Path = Path("./agents")
    system_dir: Path = Path("./system")
    dashboard_port: int = 8080
    dashboard_enabled: bool = True
    timezone: str = "America/Los_Angeles"  # IANA timezone for local dates

    # User-specific cron schedules (separate from agent mission.yaml defaults)
    # Each entry: {agent, name, cron, action, mode?, args?, target_user_ids?}
    schedules: list[dict] = Field(default_factory=list)

    # Runtime overrides per agent (model_config, mcp_servers, etc.)
    # Written to config.yaml at runtime, merged on top of mission.yaml defaults.
    agent_overrides: dict[str, dict] = Field(default_factory=dict)

    # Google Search grounding — agents not in exclude list get grounding
    # {exclude_agents: ["manager"]}
    google_search_grounding: dict = Field(default_factory=dict)

    # URL Context — agents not in exclude list can read web page content
    # {exclude_agents: ["manager"]}
    url_context: dict = Field(default_factory=dict)

    # Thinking — per-agent thinking level for deep reasoning
    # {agents: {learner: "medium", admin: "low"}}
    thinking: dict = Field(default_factory=dict)

    # Internal: project root (not from YAML)
    project_root: Path = Field(default_factory=_find_project_root)

    def resolve_path(self, relative: Path | str) -> Path:
        """Resolve a relative path against the project root."""
        relative = Path(relative)
        if relative.is_absolute():
            return relative
        return self.project_root / relative

    def get_tz(self):
        """Return a ZoneInfo object for the configured timezone."""
        from zoneinfo import ZoneInfo
        return ZoneInfo(self.timezone)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_settings: Settings | None = None


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file; return empty dict if missing."""
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_settings(project_root: Path | None = None) -> Settings:
    """Read config.yaml + secret.yaml and return a validated Settings instance."""
    root = project_root or _find_project_root()

    config_path = root / "config.yaml"
    secret_path = root / "secret.yaml"

    config_data = _load_yaml(config_path)
    secret_data = _load_yaml(secret_path)

    if not secret_path.exists():
        import sys
        print(
            "\n⚠️  secret.yaml not found. Run `mochi-agents setup` to configure your API keys.\n",
            file=sys.stderr,
        )

    merged = {**config_data, **secret_data, "project_root": root}
    return Settings(**merged)


def get_settings() -> Settings:
    """Return the cached Settings instance, loading on first access."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reload_settings() -> Settings:
    """Re-read YAML files from disk and replace the cached instance."""
    global _settings
    root = _settings.project_root if _settings else _find_project_root()
    _settings = load_settings(project_root=root)
    return _settings


def set_api_key(name: str, key: str) -> None:
    """Save an API key to secret.yaml and reload settings."""
    settings = get_settings()
    secret_path = settings.project_root / "secret.yaml"

    secret_data = _load_yaml(secret_path)
    if not isinstance(secret_data.get("api_keys"), dict):
        secret_data["api_keys"] = {}

    secret_data["api_keys"][name] = key

    with open(secret_path, "w") as f:
        yaml.dump(secret_data, f, default_flow_style=False)

    reload_settings()


def remove_api_key(name: str) -> bool:
    """Remove an API key from secret.yaml and reload settings. Returns True if removed."""
    settings = get_settings()
    secret_path = settings.project_root / "secret.yaml"

    secret_data = _load_yaml(secret_path)
    if isinstance(secret_data.get("api_keys"), dict) and name in secret_data["api_keys"]:
        del secret_data["api_keys"][name]
        with open(secret_path, "w") as f:
            yaml.dump(secret_data, f, default_flow_style=False)
        reload_settings()
        return True
    return False


def save_agent_override(agent_name: str, key: str, value: Any) -> None:
    """Persist a runtime override for an agent to config.yaml.

    This keeps mission.yaml clean (committed defaults) while storing
    user-specific changes (model_config, mcp_servers) in config.yaml.

    Args:
        agent_name: e.g., "secretary"
        key: e.g., "model_config" or "mcp_servers"
        value: the override value (will be deep-merged for dicts)
    """
    settings = get_settings()
    config_path = settings.project_root / "config.yaml"

    config_data = _load_yaml(config_path)
    overrides = config_data.setdefault("agent_overrides", {})
    agent_section = overrides.setdefault(agent_name, {})

    # Deep-merge for dicts, replace for other types
    if isinstance(value, dict) and isinstance(agent_section.get(key), dict):
        agent_section[key].update(value)
    else:
        agent_section[key] = value

    with open(config_path, "w") as f:
        yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)

    reload_settings()
