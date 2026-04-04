"""LLM client factory — creates configured clients based on model_config from mission files."""

from __future__ import annotations

from typing import Any

from google import genai
from google.genai import types

from mochi_agents.config import get_settings


def create_client(model_config: dict[str, Any]) -> genai.Client:
    """Create a Google GenAI client using the API key from settings."""
    settings = get_settings()
    return genai.Client(api_key=settings.gemini_api_key)


def get_model_name(model_config: dict[str, Any]) -> str:
    """Extract the model name from model_config."""
    return model_config.get("model", "gemini-2.0-flash")


def get_generation_config(model_config: dict[str, Any]) -> types.GenerateContentConfig:
    """Build generation config from mission file model_config."""
    return types.GenerateContentConfig(
        temperature=model_config.get("temperature", 0.5),
        max_output_tokens=model_config.get("max_tokens", 2048),
    )
