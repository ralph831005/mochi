"""Dashboard REST API server — runs alongside the bot on a configurable port.

Uses aiohttp for async HTTP. Binds to 127.0.0.1 only (local access).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from aiohttp import web

from mochi_agents.config import get_settings

if TYPE_CHECKING:
    from mochi_agents.bot import MochiBot

logger = logging.getLogger(__name__)

# Track startup time for uptime calculation
_start_time: float = 0.0
_bot_ref: MochiBot | None = None

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, default=str),
        content_type="application/json",
        status=status,
    )


def _load_mission(agent_name: str) -> dict:
    """Load an agent's mission.yaml."""
    settings = get_settings()
    agents_dir = settings.resolve_path(settings.agents_dir)
    mission_path = agents_dir / agent_name / "mission.yaml"
    if not mission_path.exists():
        return {}
    with open(mission_path) as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

async def api_status(request: web.Request) -> web.Response:
    """GET /api/status — system overview."""
    bot = _bot_ref
    settings = get_settings()

    agents = bot.registry.list_agents() if bot else []
    shortcuts_count = 0
    if bot and bot.shortcut_runner:
        for agent in agents:
            shortcuts_count += len(bot.shortcut_runner.list_shortcuts(agent.name))

    return _json_response({
        "status": "running",
        "uptime_seconds": int(time.time() - _start_time),
        "agent_count": len(agents),
        "agents": [a.name for a in agents],
        "shortcuts_count": shortcuts_count,
        "dashboard_port": settings.dashboard_port,
    })


async def api_agents(request: web.Request) -> web.Response:
    """GET /api/agents — list all agents with config."""
    bot = _bot_ref
    if not bot:
        return _json_response([], 200)

    agents = []
    for a in bot.registry.list_agents():
        mission = _load_mission(a.name)
        model_config = mission.get("model_config", {})
        shortcuts = bot.shortcut_runner.list_shortcuts(a.name) if bot.shortcut_runner else []

        agents.append({
            "name": a.name,
            "display_name": a.display_name,
            "description": a.description,
            "aliases": list(a.aliases) if hasattr(a, "aliases") else [],
            "routing_keys": list(a.routing_keys) if hasattr(a, "routing_keys") else [],
            "model": model_config.get("model", "unknown"),
            "provider": model_config.get("provider", "google"),
            "temperature": model_config.get("temperature", 0.3),
            "status": getattr(a, "status", "active"),
            "shortcuts": shortcuts,
        })

    return _json_response(agents)


async def api_agent_detail(request: web.Request) -> web.Response:
    """GET /api/agents/:name — single agent detail."""
    name = request.match_info["name"]
    bot = _bot_ref
    if not bot:
        return _json_response({"error": "Bot not ready"}, 503)

    agent = bot.registry.get_agent(name)
    if not agent:
        return _json_response({"error": f"Agent '{name}' not found"}, 404)

    mission = _load_mission(agent.name)
    return _json_response({
        "name": agent.name,
        "display_name": agent.display_name,
        "description": agent.description,
        "mission": mission,
    })


async def api_agent_model_update(request: web.Request) -> web.Response:
    """PUT /api/agents/{name}/model — permanently update model config."""
    name = request.match_info["name"]
    bot = _bot_ref
    if not bot:
        return _json_response({"error": "Bot not ready"}, 503)

    agent = bot.registry.get_agent(name)
    if not agent:
        return _json_response({"error": f"Agent '{name}' not found"}, 404)

    data = await request.json()
    model = data.get("model")
    api_key = data.get("api_key")
    temperature = data.get("temperature")

    updates = {}
    if model:
        updates["model"] = model
    if api_key:
        updates["api_key"] = api_key
    if temperature is not None:
        try:
            updates["temperature"] = float(temperature)
        except ValueError:
            return _json_response({"error": "Invalid temperature value"}, 400)

    if not updates:
        return _json_response({"error": "No updates provided"}, 400)

    try:
        mission = bot.runtime.update_model_config(name, updates)
        return _json_response({
            "status": "ok",
            "message": "Model config updated",
            "new_config": mission.get("model_config", {})
        })
    except Exception as e:
        return _json_response({"error": str(e)}, 500)


async def api_shortcuts(request: web.Request) -> web.Response:
    """GET /api/shortcuts — all shortcuts grouped by agent."""
    bot = _bot_ref
    if not bot or not bot.shortcut_runner:
        return _json_response({})

    result = {}
    for agent in bot.registry.list_agents():
        shortcuts = bot.shortcut_runner._shortcuts.get(agent.name, {})
        if shortcuts:
            result[agent.name] = {
                "display_name": agent.display_name,
                "shortcuts": shortcuts,
            }
    return _json_response(result)


async def api_shortcut_create(request: web.Request) -> web.Response:
    """POST /api/shortcuts — create a shortcut."""
    bot = _bot_ref
    if not bot or not bot.shortcut_runner:
        return _json_response({"error": "ShortcutRunner not available"}, 503)

    data = await request.json()
    agent_name = data.get("agent_name", "")
    command = data.get("command", "")
    if not agent_name or not command:
        return _json_response({"error": "agent_name and command required"}, 400)

    result = bot.shortcut_runner.save_shortcut(agent_name, command, data.get("definition", data))
    return _json_response(result, 201)


async def api_shortcut_update(request: web.Request) -> web.Response:
    """PUT /api/shortcuts/:agent/:cmd — update shortcut fields."""
    agent = request.match_info["agent"]
    cmd = request.match_info["cmd"]
    bot = _bot_ref
    if not bot or not bot.shortcut_runner:
        return _json_response({"error": "ShortcutRunner not available"}, 503)

    existing = bot.shortcut_runner.get_shortcut(agent, cmd)
    if not existing:
        return _json_response({"error": f"Shortcut not found"}, 404)

    data = await request.json()
    updated = {**existing, **data}
    result = bot.shortcut_runner.save_shortcut(agent, cmd, updated)
    return _json_response(result)


async def api_shortcut_delete(request: web.Request) -> web.Response:
    """DELETE /api/shortcuts/:agent/:cmd — delete a shortcut."""
    agent = request.match_info["agent"]
    cmd = request.match_info["cmd"]
    bot = _bot_ref
    if not bot or not bot.shortcut_runner:
        return _json_response({"error": "ShortcutRunner not available"}, 503)

    result = bot.shortcut_runner.delete_shortcut(agent, cmd)
    status = 200 if result.get("status") == "ok" else 404
    return _json_response(result, status)


async def api_aliases(request: web.Request) -> web.Response:
    """GET /api/aliases — all global aliases."""
    from mochi_agents.core.shortcut_runner import get_alias_registry
    registry = get_alias_registry()
    if not registry:
        return _json_response({})
    return _json_response(registry.list_all())


async def api_alias_create(request: web.Request) -> web.Response:
    """POST /api/aliases — add an alias."""
    from mochi_agents.core.shortcut_runner import get_alias_registry

    data = await request.json()
    alias = data.get("alias", "")
    agent_name = data.get("agent_name", "")
    shortcut = data.get("shortcut", "")
    if not alias or not agent_name or not shortcut:
        return _json_response({"error": "alias, agent_name, and shortcut required"}, 400)

    registry = get_alias_registry()
    if not registry:
        return _json_response({"error": "Alias registry not initialized"}, 503)
    result = registry.add(alias, agent_name, shortcut)
    return _json_response(result, 201)


async def api_alias_delete(request: web.Request) -> web.Response:
    """DELETE /api/aliases/:alias — remove an alias."""
    from mochi_agents.core.shortcut_runner import get_alias_registry

    alias = request.match_info["alias"]
    registry = get_alias_registry()
    if not registry:
        return _json_response({"error": "Alias registry not initialized"}, 503)
    result = registry.remove(alias)
    status = 200 if result.get("status") == "ok" else 404
    return _json_response(result, status)


async def api_config(request: web.Request) -> web.Response:
    """GET /api/config — current config (secrets masked)."""
    settings = get_settings()

    masked_api_keys = {}
    if settings.api_keys:
        for k, v in settings.api_keys.items():
            masked_api_keys[k] = "***" + str(v)[-4:] if len(str(v)) > 4 else "***"

    return _json_response({
        "active_client": settings.active_client,
        "allowed_user_ids": settings.allowed_user_ids,
        "data_dir": str(settings.data_dir),
        "agents_dir": str(settings.agents_dir),
        "system_dir": str(settings.system_dir),
        "dashboard_port": settings.dashboard_port,
        "dashboard_enabled": settings.dashboard_enabled,
        "api_keys": masked_api_keys,
        "gemini_api_key": "***" + settings.gemini_api_key[-4:] if len(settings.gemini_api_key) > 4 else "***",
        "telegram_bot_token": "***" + settings.telegram_bot_token[-6:] if len(settings.telegram_bot_token) > 6 else "***",
    })


_cached_models = []

async def api_models(request: web.Request) -> web.Response:
    """GET /api/models — return valid model names dynamically."""
    global _cached_models
    from mochi_agents.core.model_factory import VALID_MODELS, resolve_api_key
    
    if not _cached_models:
        try:
            import aiohttp
            api_key = resolve_api_key({})
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=3.0) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        fetched = [m["name"].replace("models/", "") for m in data.get("models", [])]
                        _cached_models.extend(fetched)
        except Exception as e:
            logger.warning(f"Could not dynamically load models from API: {e}")
            
        # Only fallback to static ones if the dynamic network call completely failed
        if not _cached_models:
            _cached_models = list(VALID_MODELS)

    return _json_response({"models": _cached_models})


async def api_schedules(request: web.Request) -> web.Response:
    """GET /api/schedules — all cron schedules per agent."""
    result = {}
    for agent in (_bot_ref.registry.list_agents() if _bot_ref else []):
        mission = _load_mission(agent.name)
        schedules = mission.get("schedules", [])
        if schedules:
            result[agent.name] = {
                "display_name": agent.display_name,
                "schedules": schedules,
            }
    return _json_response(result)


async def api_chat(request: web.Request) -> web.Response:
    """POST /api/chat — send a message through the Router."""
    bot = _bot_ref
    if not bot:
        return _json_response({"error": "Bot not ready"}, 503)

    data = await request.json()
    text = data.get("message", "").strip()
    if not text:
        return _json_response({"error": "message is required"}, 400)

    # Use a dashboard-specific user_id so it doesn't conflict with Telegram sessions
    user_id = "dashboard"

    try:
        from mochi_agents.core.router import RouteResponse
        response = await bot.router.route(text=text, user_id=user_id, chat_id="dashboard")

        # Unwrap RouteResponse if needed
        if isinstance(response, RouteResponse):
            response_text = response.text
        else:
            response_text = str(response)

        return _json_response({"response": response_text})
    except Exception as e:
        logger.error(f"Dashboard chat error: {e}", exc_info=True)
        return _json_response({"error": str(e)}, 500)


async def api_key_create(request: web.Request) -> web.Response:
    """POST /api/keys — set a new API key."""
    from mochi_agents.config import set_api_key

    data = await request.json()
    name = data.get("name", "").strip()
    key = data.get("key", "").strip()

    if not name or not key:
        return _json_response({"error": "name and key are required"}, 400)

    set_api_key(name, key)
    return _json_response({"status": "ok", "message": f"Key '{name}' updated"})


async def api_key_delete(request: web.Request) -> web.Response:
    """DELETE /api/keys/{name} — delete an API key."""
    from mochi_agents.config import remove_api_key

    name = request.match_info["name"]
    removed = remove_api_key(name)
    if removed:
        return _json_response({"status": "ok", "message": f"Key '{name}' removed"})
    else:
        return _json_response({"error": f"Key '{name}' not found"}, 404)

async def index_handler(request: web.Request) -> web.Response:
    """Serve the dashboard SPA with no-cache headers."""
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return web.Response(text="Dashboard not found", status=404)
    return web.Response(
        text=index_path.read_text(),
        content_type="text/html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(bot: MochiBot | None = None) -> web.Application:
    """Create the aiohttp application with all routes."""
    global _bot_ref, _start_time
    _bot_ref = bot
    _start_time = time.time()

    app = web.Application()

    # API routes
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/agents", api_agents)
    app.router.add_get("/api/agents/{name}", api_agent_detail)
    app.router.add_put("/api/agents/{name}/model", api_agent_model_update)
    app.router.add_get("/api/shortcuts", api_shortcuts)
    app.router.add_post("/api/shortcuts", api_shortcut_create)
    app.router.add_put("/api/shortcuts/{agent}/{cmd}", api_shortcut_update)
    app.router.add_delete("/api/shortcuts/{agent}/{cmd}", api_shortcut_delete)
    app.router.add_get("/api/aliases", api_aliases)
    app.router.add_post("/api/aliases", api_alias_create)
    app.router.add_delete("/api/aliases/{alias}", api_alias_delete)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/models", api_models)
    app.router.add_post("/api/keys", api_key_create)
    app.router.add_delete("/api/keys/{name}", api_key_delete)
    app.router.add_get("/api/schedules", api_schedules)
    app.router.add_post("/api/chat", api_chat)

    # Static files and SPA fallback
    if STATIC_DIR.exists():
        app.router.add_static("/static", STATIC_DIR)
    app.router.add_get("/", index_handler)
    app.router.add_get("/{path:.*}", index_handler)  # SPA fallback

    return app


async def start_dashboard(bot: MochiBot, port: int = 8080) -> web.AppRunner:
    """Start the dashboard server. Returns the runner for cleanup."""
    app = create_app(bot)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()

    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    logger.info(f"🌐 Dashboard running at http://127.0.0.1:{port}")
    print(f"🌐 Dashboard: http://127.0.0.1:{port}")

    return runner
