"""LLM client factory — creates configured clients based on model_config from mission files.

Key resolution order:
  1. model_config["api_key"] name → settings.api_keys[name]
  2. settings.api_keys["default"] (if exists)
  3. settings.gemini_api_key (backward compat)
"""

from __future__ import annotations

import logging
from typing import Any

from google import genai
from google.genai import types

from mochi_agents.config import get_settings

logger = logging.getLogger(__name__)

VALID_MODELS = (
    "gemini-3.1-pro",
    "gemini-3.1-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-pro-preview",
    "gemini-2.5-flash-preview",
    "gemini-2.5-flash-preview-05-20",
)


def resolve_api_key(model_config: dict[str, Any]) -> str:
    """Resolve the API key for a model_config, following the fallback chain."""
    settings = get_settings()

    # 1. Named key from model_config
    key_name = model_config.get("api_key")
    if key_name and settings.api_keys:
        key = settings.api_keys.get(key_name)
        if key:
            return key
        logger.warning(f"API key '{key_name}' not found in api_keys registry")

    # 2. Default named key
    if settings.api_keys and "default" in settings.api_keys:
        return settings.api_keys["default"]

    # 3. Legacy single key
    if settings.gemini_api_key:
        return settings.gemini_api_key

    raise ValueError(
        "No API key configured. Add api_keys to secret.yaml or set gemini_api_key."
    )


def create_client(model_config: dict[str, Any]) -> genai.Client:
    """Create a Google GenAI client using the resolved API key."""
    api_key = resolve_api_key(model_config)
    return genai.Client(api_key=api_key)


def get_model_name(model_config: dict[str, Any]) -> str:
    """Extract the model name from model_config."""
    return model_config.get("model", "gemini-2.0-flash")


def get_generation_config(model_config: dict[str, Any]) -> types.GenerateContentConfig:
    """Build generation config from mission file model_config."""
    return types.GenerateContentConfig(
        temperature=model_config.get("temperature", 0.5),
        max_output_tokens=model_config.get("max_tokens", 2048),
    )
