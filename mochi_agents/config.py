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

    # --- From config.yaml ---
    active_client: str = "telegram"
    allowed_user_ids: list[int] = Field(default_factory=list)
    data_dir: Path = Path("./data")
    agents_dir: Path = Path("./agents")
    system_dir: Path = Path("./system")

    # Internal: project root (not from YAML)
    project_root: Path = Field(default_factory=_find_project_root)

    def resolve_path(self, relative: Path) -> Path:
        """Resolve a relative path against the project root."""
        if relative.is_absolute():
            return relative
        return self.project_root / relative


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
