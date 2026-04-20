"""CLI entry point for the `mochi-agents` command.

Usage:
    mochi-agents                Start the bot + dashboard
    mochi-agents setup          Interactive first-time setup wizard
    mochi-agents config show    Show current config
    mochi-agents config set K V Set a config value
    mochi-agents agent list     List registered agents
    mochi-agents agent info X   Show agent details
    mochi-agents reset          Reset conversation history (keeps data)
    mochi-agents reset --all    Full factory reset (deletes everything)
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from pathlib import Path

import yaml


def main() -> None:
    """Entry point registered in pyproject.toml [project.scripts]."""
    if len(sys.argv) < 2:
        run_bot()
        return

    command = sys.argv[1]

    if command == "setup":
        run_setup()
    elif command == "config":
        run_config()
    elif command == "agent":
        run_agent()
    elif command == "reset":
        run_reset()
    elif command in ("--help", "-h"):
        print_help()
    else:
        print(f"Unknown command: {command}")
        print_help()
        sys.exit(1)


def print_help() -> None:
    """Print CLI usage."""
    print("""🍡 Mochi — Multi-Agent Service Bot

Usage:
  mochi-agents                  Start the bot + dashboard
  mochi-agents setup            Interactive first-time setup
  mochi-agents config show      Show current configuration
  mochi-agents config set K V   Set a configuration value
  mochi-agents agent list       List all registered agents
  mochi-agents agent info NAME    Show agent details
  mochi-agents agent export NAME  Export an agent as a .agent bundle
  mochi-agents agent import FILE  Import an agent from a .agent bundle
  mochi-agents agent remove NAME  Remove an agent (with confirmation)
  mochi-agents reset            Reset all agents' conversation history
  mochi-agents reset NAME       Reset a specific agent's history
  mochi-agents reset --all      Full factory reset (deletes all data)
  mochi-agents reset NAME --all Full reset for a specific agent
  mochi-agents --help           Show this help
""")


# ---------------------------------------------------------------------------
# Setup Wizard
# ---------------------------------------------------------------------------

def run_setup() -> None:
    """Enhanced interactive setup wizard with step-by-step flow."""
    print()
    print("🍡 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("    Mochi — First-Time Setup")
    print("   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()

    project_root = _find_project_root()
    secret_path = project_root / "secret.yaml"
    config_path = project_root / "config.yaml"

    # Step 1: API Keys
    print("  Step 1/4: API Keys")
    print("  " + "─" * 36)
    print("  You can add multiple keys for per-agent cost tracking.")
    print("  The first key will be named 'default'.")
    print()

    api_keys = {}
    gemini_key = getpass.getpass("  Gemini API Key (default): ").strip()
    if not gemini_key:
        print("  ❌ At least one API key is required.")
        sys.exit(1)
    api_keys["default"] = gemini_key
    print("  ✅ default key set")

    while True:
        more = input("  Add another key? (name or Enter to skip): ").strip()
        if not more:
            break
        key_val = getpass.getpass(f"  API Key for '{more}': ").strip()
        if key_val:
            api_keys[more] = key_val
            print(f"  ✅ {more} key set")

    print(f"  📦 {len(api_keys)} key(s) configured: {', '.join(api_keys.keys())}")
    print()

    # Step 2: Telegram Bot
    print("  Step 2/4: Telegram Bot")
    print("  " + "─" * 36)

    telegram_token = getpass.getpass("  Telegram Bot Token: ").strip()
    if not telegram_token:
        print("  ❌ Telegram token is required.")
        sys.exit(1)

    print("  🔍 Validating...")
    bot_info = _validate_telegram_token(telegram_token)
    if bot_info is None:
        print("  ❌ Invalid Telegram bot token.")
        sys.exit(1)

    bot_username = bot_info.get("username", "unknown")
    print(f"  ✅ Connected as @{bot_username}")
    print()

    # Step 3: User ID
    print("  Step 3/4: Your Telegram User ID")
    print("  " + "─" * 36)
    print("  Tip: Message @userinfobot on Telegram to get your ID")

    user_id_input = input("  User ID: ").strip()
    allowed_ids = []
    if user_id_input:
        try:
            for uid in user_id_input.split(","):
                allowed_ids.append(int(uid.strip()))
            print(f"  ✅ Allowlist: {allowed_ids}")
        except ValueError:
            print("  ⚠️  Invalid ID, skipping. Add later in config.yaml")
    else:
        print("  ⚠️  No ID provided. Add later in config.yaml")
    print()

    # Step 4: Verify agents
    print("  Step 4/4: Default Agents")
    print("  " + "─" * 36)

    agents_dir = project_root / "agents"
    if agents_dir.exists():
        for agent_dir in sorted(agents_dir.iterdir()):
            mission_path = agent_dir / "mission.yaml"
            if mission_path.exists():
                with open(mission_path) as f:
                    mission = yaml.safe_load(f) or {}
                name = mission.get("display_name", agent_dir.name)
                desc = mission.get("description", "")[:60]
                print(f"  ✅ {name} — {desc}")
    else:
        print("  ⚠️  No agents directory found at ./agents/")
    print()

    # Write files
    secrets = {
        "gemini_api_key": api_keys.get("default", gemini_key),  # backward compat
        "telegram_bot_token": telegram_token,
        "api_keys": api_keys,
    }
    with open(secret_path, "w") as f:
        yaml.dump(secrets, f, default_flow_style=False)

    try:
        secret_path.chmod(0o600)
    except OSError:
        pass

    print(f"  🔒 Secrets → {secret_path}")

    # Write config.yaml
    config = {
        "active_client": "telegram",
        "allowed_user_ids": allowed_ids,
        "data_dir": "./data",
        "agents_dir": "./agents",
        "system_dir": "./system",
        "dashboard_port": 8080,
        "dashboard_enabled": True,
    }

    if config_path.exists():
        with open(config_path) as f:
            existing = yaml.safe_load(f) or {}
        # Preserve existing values, only update allowed_user_ids if newly set
        if allowed_ids:
            existing["allowed_user_ids"] = allowed_ids
        # Add new fields that might be missing
        for k, v in config.items():
            if k not in existing:
                existing[k] = v
        config = existing

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    print(f"  📄 Config → {config_path}")
    print()
    print("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  🍡 Setup complete!")
    print()
    print(f"  Start:     mochi-agents")
    print(f"  Dashboard: http://127.0.0.1:{config.get('dashboard_port', 8080)}")
    print()


# ---------------------------------------------------------------------------
# Config Commands
# ---------------------------------------------------------------------------

def run_config() -> None:
    """Handle `mochi-agents config` subcommands."""
    if len(sys.argv) < 3:
        print("Usage: mochi-agents config <show|set>")
        sys.exit(1)

    sub = sys.argv[2]
    if sub == "show":
        _config_show()
    elif sub == "set":
        _config_set()
    elif sub == "set-key":
        _config_set_key()
    elif sub == "del-key":
        _config_del_key()
    else:
        print(f"Unknown config command: {sub}")
        sys.exit(1)


def _config_show() -> None:
    """Show current configuration."""
    project_root = _find_project_root()

    config = _load_yaml(project_root / "config.yaml")
    secret = _load_yaml(project_root / "secret.yaml")

    print("🍡 Mochi — Configuration\n")

    print("  config.yaml:")
    for key, val in config.items():
        print(f"    {key}: {val}")

    print()
    print("  secret.yaml:")
    for key, val in secret.items():
        if key == "api_keys" and isinstance(val, dict):
            print("    api_keys:")
            for k, v in val.items():
                masked = "***" + str(v)[-4:] if len(str(v)) > 4 else "***"
                print(f"      {k}: {masked}")
        else:
            masked = "***" + str(val)[-4:] if len(str(val)) > 4 else "***"
            print(f"    {key}: {masked}")

    print()
    print(f"  Project root: {project_root}")


def _config_set() -> None:
    """Set a config value."""
    if len(sys.argv) < 5:
        print("Usage: mochi-agents config set <key> <value>")
        print("Example: mochi-agents config set dashboard_port 9090")
        sys.exit(1)

    key = sys.argv[3]
    raw_value = sys.argv[4]

    project_root = _find_project_root()
    config_path = project_root / "config.yaml"

    config = _load_yaml(config_path)

    # Type coercion
    if raw_value.lower() in ("true", "false"):
        value = raw_value.lower() == "true"
    elif raw_value.isdigit():
        value = int(raw_value)
    elif "," in raw_value:
        # Support comma-separated lists (e.g., allowed_user_ids)
        try:
            value = [int(x.strip()) for x in raw_value.split(",")]
        except ValueError:
            value = [x.strip() for x in raw_value.split(",")]
    else:
        value = raw_value

    config[key] = value

    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    print(f"  ✅ Config updated: {key} = {raw_value}")


def _config_set_key() -> None:
    """Set an API key dynamically."""
    if len(sys.argv) < 5:
        print("Usage: mochi-agents config set-key <name> <value>")
        sys.exit(1)

    name = sys.argv[3]
    key_val = sys.argv[4]

    from mochi_agents.config import set_api_key
    set_api_key(name, key_val)
    print(f"✅ API key '{name}' updated successfully in secret.yaml")


def _config_del_key() -> None:
    """Delete an API key dynamically."""
    if len(sys.argv) < 4:
        print("Usage: mochi-agents config del-key <name>")
        sys.exit(1)

    name = sys.argv[3]

    from mochi_agents.config import remove_api_key
    removed = remove_api_key(name)
    if removed:
        print(f"✅ API key '{name}' removed successfully from secret.yaml")
    else:
        print(f"❌ API key '{name}' not found")
        sys.exit(1)

# ---------------------------------------------------------------------------
# Agent Commands
# ---------------------------------------------------------------------------

def run_agent() -> None:
    """Handle `mochi-agents agent` subcommands."""
    if len(sys.argv) < 3:
        print("Usage: mochi-agents agent <list|info|export|import|remove>")
        sys.exit(1)

    sub = sys.argv[2]
    if sub == "list":
        _agent_list()
    elif sub == "info":
        _agent_info()
    elif sub == "export":
        _agent_export()
    elif sub == "import":
        _agent_import()
    elif sub == "remove":
        _agent_remove()
    else:
        print(f"Unknown agent command: {sub}")
        sys.exit(1)


def _agent_list() -> None:
    """List all registered agents."""
    project_root = _find_project_root()
    agents_dir = project_root / "agents"

    print("🍡 Mochi — Agents\n")

    if not agents_dir.exists():
        print("  No agents directory found.")
        return

    for agent_dir in sorted(agents_dir.iterdir()):
        mission_path = agent_dir / "mission.yaml"
        if not mission_path.exists():
            continue

        with open(mission_path) as f:
            mission = yaml.safe_load(f) or {}

        name = agent_dir.name
        display = mission.get("display_name", name)
        desc = mission.get("description", "—")[:60]
        model = mission.get("model_config", {}).get("model", "—")
        shortcuts = len(mission.get("shortcuts", []))
        schedules = len(mission.get("schedules", []))

        print(f"  {display} ({name})")
        print(f"    Model: {model}")
        print(f"    {desc}")
        print(f"    Shortcuts: {shortcuts} | Schedules: {schedules}")
        print()


def _agent_info() -> None:
    """Show detailed info for a specific agent."""
    if len(sys.argv) < 4:
        print("Usage: mochi-agents agent info <name>")
        sys.exit(1)

    name = sys.argv[3]
    project_root = _find_project_root()
    mission_path = project_root / "agents" / name / "mission.yaml"

    if not mission_path.exists():
        print(f"  ❌ Agent '{name}' not found at {mission_path}")
        sys.exit(1)

    with open(mission_path) as f:
        mission = yaml.safe_load(f) or {}

    print(f"🍡 Agent: {mission.get('display_name', name)}\n")
    print(yaml.dump(mission, default_flow_style=False, sort_keys=False))


def _agent_remove() -> None:
    """Remove an agent completely.

    Usage: mochi-agents agent remove <name>

    Removes:
        - agents/<name>/  (config)
        - mochi_agents/agents/<name>/  (code, if not a default agent)
        - data/<name>.db  (database)
        - data/shortcuts/<name>.yaml  (shortcuts)
        - system/registry.yaml entry
        - config.yaml agent_overrides entry
    """
    import shutil

    if len(sys.argv) < 4:
        print("Usage: mochi-agents agent remove <name>")
        sys.exit(1)

    name = sys.argv[3]
    project_root = _find_project_root()

    # Gather what exists
    agent_config_dir = project_root / "agents" / name
    agent_code_dir = project_root / "mochi_agents" / "agents" / name
    config = _load_yaml(project_root / "config.yaml")
    data_dir = project_root / Path(config.get("data_dir", "./data"))
    db_file = data_dir / f"{name}.db"
    shortcuts_file = data_dir / "shortcuts" / f"{name}.yaml"
    registry_path = project_root / "system" / "registry.yaml"

    if not agent_config_dir.exists() and not db_file.exists():
        print(f"  ❌ Agent '{name}' not found.")
        sys.exit(1)

    # Load display name
    display_name = name
    mission_path = agent_config_dir / "mission.yaml"
    if mission_path.exists():
        with open(mission_path) as f:
            mission = yaml.safe_load(f) or {}
        display_name = mission.get("display_name", name)

    print()
    print("🍡 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"    Removing Agent: {display_name}")
    print("   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()

    # Show what will be deleted
    items = []
    if agent_config_dir.exists():
        files = list(agent_config_dir.rglob("*"))
        items.append((f"agents/{name}/", f"{len([f for f in files if f.is_file()])} files"))
        print(f"  🗑️  agents/{name}/ (mission config)")
    if agent_code_dir.exists():
        files = [f for f in agent_code_dir.rglob("*") if f.is_file() and "__pycache__" not in str(f)]
        items.append((f"mochi_agents/agents/{name}/", f"{len(files)} files"))
        print(f"  🗑️  mochi_agents/agents/{name}/ (tool code)")
    if db_file.exists():
        size_kb = db_file.stat().st_size // 1024
        items.append((db_file.name, f"{size_kb}KB"))
        print(f"  🗑️  data/{name}.db ({size_kb}KB)")
    if shortcuts_file.exists():
        items.append((f"shortcuts/{name}.yaml", ""))
        print(f"  🗑️  data/shortcuts/{name}.yaml")
    if config.get("agent_overrides", {}).get(name):
        print(f"  🗑️  config.yaml agent_overrides.{name}")

    # Check registry
    in_registry = False
    if registry_path.exists():
        with open(registry_path) as f:
            registry = yaml.safe_load(f) or {}
        if name in [a["name"] for a in registry.get("agents", [])]:
            in_registry = True
            print(f"  🗑️  system/registry.yaml entry")

    print()
    print(f"  ⚠️  This will permanently remove {display_name} and all its data.")
    print(f"  💡 Tip: run 'mochi-agents agent export {name}' first to back up.")
    print()
    confirm = input("  Are you sure? Type 'yes' to confirm: ").strip().lower()

    if confirm != "yes":
        print("  ❌ Removal cancelled.")
        return

    print()

    # Delete config dir
    if agent_config_dir.exists():
        shutil.rmtree(agent_config_dir)
        print(f"  ✅ Deleted agents/{name}/")

    # Delete code dir
    if agent_code_dir.exists():
        shutil.rmtree(agent_code_dir)
        print(f"  ✅ Deleted mochi_agents/agents/{name}/")

    # Delete database
    if db_file.exists():
        db_file.unlink()
        print(f"  ✅ Deleted data/{name}.db")

    # Delete shortcuts
    if shortcuts_file.exists():
        shortcuts_file.unlink()
        print(f"  ✅ Deleted data/shortcuts/{name}.yaml")

    # Remove from registry
    if in_registry:
        registry["agents"] = [a for a in registry.get("agents", []) if a["name"] != name]
        with open(registry_path, "w") as f:
            yaml.dump(registry, f, default_flow_style=False, sort_keys=False)
        print(f"  ✅ Removed from registry")

    # Remove config overrides
    config_path = project_root / "config.yaml"
    if config_path.exists() and config.get("agent_overrides", {}).get(name):
        del config["agent_overrides"][name]
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        print(f"  ✅ Removed config overrides")

    print()
    print("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  🍡 {display_name} has been removed. Send /reload to apply.")
    print()


def _agent_export() -> None:
    """Export an agent as a portable .agent bundle.

    Usage: mochi-agents agent export <name> [--with-memory]

    Includes:
        - agents/<name>/  (mission.yaml, mission_prompt.md)
        - mochi_agents/agents/<name>/  (tools code)
        - data/shortcuts/<name>.yaml  (if exists)
        - manifest.json  (metadata)
        - memory_notes.json  (if --with-memory)
    """
    import json
    import sqlite3
    import zipfile
    from datetime import datetime

    if len(sys.argv) < 4:
        print("Usage: mochi-agents agent export <name> [--with-memory]")
        sys.exit(1)

    name = sys.argv[3]
    with_memory = "--with-memory" in sys.argv
    project_root = _find_project_root()

    # Validate agent exists
    agent_config_dir = project_root / "agents" / name
    agent_code_dir = project_root / "mochi_agents" / "agents" / name

    if not agent_config_dir.exists():
        print(f"  ❌ Agent config not found: agents/{name}/")
        sys.exit(1)

    mission_path = agent_config_dir / "mission.yaml"
    if not mission_path.exists():
        print(f"  ❌ No mission.yaml found for agent '{name}'")
        sys.exit(1)

    with open(mission_path) as f:
        mission = yaml.safe_load(f) or {}

    display_name = mission.get("display_name", name)

    print()
    print("🍡 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"    Exporting Agent: {display_name}")
    print("   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()

    # Build manifest
    manifest = {
        "name": name,
        "display_name": display_name,
        "description": mission.get("description", ""),
        "exported_at": datetime.now().isoformat(),
        "tools_module": mission.get("tools_module", ""),
        "includes_memory": with_memory,
        "includes_code": agent_code_dir.exists(),
    }

    output_file = project_root / f"{name}.agent"

    with zipfile.ZipFile(output_file, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. Agent config (mission.yaml, mission_prompt.md)
        for f in agent_config_dir.iterdir():
            if f.is_file():
                arcname = f"agents/{name}/{f.name}"
                zf.write(f, arcname)
                print(f"  📦 {arcname}")

        # 2. Agent code (tools.py, __init__.py)
        if agent_code_dir.exists():
            for f in agent_code_dir.rglob("*"):
                if f.is_file() and "__pycache__" not in str(f):
                    arcname = str(f.relative_to(project_root))
                    zf.write(f, arcname)
                    print(f"  📦 {arcname}")

        # 3. Shortcuts
        shortcuts_file = project_root / "data" / "shortcuts" / f"{name}.yaml"
        if shortcuts_file.exists():
            arcname = f"data/shortcuts/{name}.yaml"
            zf.write(shortcuts_file, arcname)
            print(f"  📦 {arcname}")

        # 4. Memory notes (optional)
        if with_memory:
            db_path = project_root / "data" / f"{name}.db"
            if db_path.exists():
                try:
                    conn = sqlite3.connect(str(db_path))
                    cursor = conn.cursor()
                    tables = [r[0] for r in cursor.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()]

                    if "memory_notes" in tables:
                        rows = cursor.execute("SELECT * FROM memory_notes").fetchall()
                        cols = [d[0] for d in cursor.description]
                        notes = [dict(zip(cols, row)) for row in rows]
                        if notes:
                            zf.writestr("memory_notes.json", json.dumps(notes, indent=2, default=str))
                            print(f"  🧠 memory_notes.json ({len(notes)} notes)")
                    conn.close()
                except Exception as e:
                    print(f"  ⚠️  Could not export memory: {e}")

        # 5. MCP skill files (from config.yaml agent_overrides)
        config_data = _load_yaml(project_root / "config.yaml")
        agent_overrides = config_data.get("agent_overrides", {}).get(name, {})
        mcp_servers = agent_overrides.get("mcp_servers", [])

        if mcp_servers:
            # Save the MCP config
            zf.writestr("mcp_servers.json", json.dumps(mcp_servers, indent=2))
            print(f"  🔌 mcp_servers.json")

            # Find and bundle referenced skill files
            skills_dir = project_root / "skills_server" / "skills"
            for server in mcp_servers:
                if isinstance(server, dict):
                    for tool_name in server.get("tools", []):
                        skill_file = skills_dir / f"{tool_name}.py"
                        if skill_file.exists():
                            arcname = f"skills_server/skills/{tool_name}.py"
                            zf.write(skill_file, arcname)
                            print(f"  🔌 {arcname}")

            manifest["includes_mcp"] = True

        # 6. Manifest
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        print(f"  📝 manifest.json")

    print()
    print(f"  ✅ Exported to: {output_file}")
    print(f"  📤 Share this file — import with: mochi-agents agent import {output_file.name}")
    print()


def _agent_import() -> None:
    """Import an agent from a .agent bundle.

    Usage: mochi-agents agent import <file.agent> [--with-memory]
    """
    import json
    import sqlite3
    import zipfile

    if len(sys.argv) < 4:
        print("Usage: mochi-agents agent import <file.agent> [--with-memory]")
        sys.exit(1)

    zip_path = Path(sys.argv[3])
    import_memory = "--with-memory" in sys.argv

    if not zip_path.exists():
        print(f"  ❌ File not found: {zip_path}")
        sys.exit(1)

    project_root = _find_project_root()

    # Read manifest first
    with zipfile.ZipFile(zip_path, "r") as zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError:
            print("  ❌ Invalid agent bundle: missing manifest.json")
            sys.exit(1)

        name = manifest["name"]
        display_name = manifest.get("display_name", name)

        print()
        print("🍡 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"    Importing Agent: {display_name}")
        print("   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print()
        print(f"  Name:        {name}")
        print(f"  Description: {manifest.get('description', '—')[:60]}")
        print(f"  Exported:    {manifest.get('exported_at', 'unknown')}")
        print(f"  Has code:    {manifest.get('includes_code', False)}")
        print(f"  Has memory:  {manifest.get('includes_memory', False)}")
        print(f"  Has MCP:     {manifest.get('includes_mcp', False)}")
        print()

        # List contents
        print("  Contents:")
        for info in zf.infolist():
            if info.filename != "manifest.json":
                print(f"    📦 {info.filename} ({info.file_size:,} bytes)")
        print()

        # Check for conflicts
        agent_config_dir = project_root / "agents" / name
        agent_code_dir = project_root / "mochi_agents" / "agents" / name

        if agent_config_dir.exists():
            print(f"  ⚠️  Agent '{name}' already exists at agents/{name}/")
            overwrite = input("  Overwrite? Type 'yes' to confirm: ").strip().lower()
            if overwrite != "yes":
                print("  ❌ Import cancelled.")
                return
            print()

        # Extract files
        for info in zf.infolist():
            if info.filename == "manifest.json":
                continue
            if info.filename == "memory_notes.json":
                continue  # handle separately
            if info.filename == "mcp_servers.json":
                continue  # handle separately

            target = project_root / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info.filename))
            print(f"  ✅ {info.filename}")

        # Import memory notes (optional)
        if import_memory and "memory_notes.json" in zf.namelist():
            notes = json.loads(zf.read("memory_notes.json"))
            if notes:
                config = _load_yaml(project_root / "config.yaml")
                data_dir = project_root / Path(config.get("data_dir", "./data"))
                db_path = data_dir / f"{name}.db"

                try:
                    conn = sqlite3.connect(str(db_path))
                    cursor = conn.cursor()

                    # Create table if not exists
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS memory_notes (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            agent_name TEXT,
                            user_id TEXT,
                            key TEXT NOT NULL,
                            value TEXT NOT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)

                    imported = 0
                    for note in notes:
                        cursor.execute(
                            "INSERT INTO memory_notes (agent_name, user_id, key, value) VALUES (?, ?, ?, ?)",
                            (note.get("agent_name", name), note.get("user_id", ""),
                             note.get("key", ""), note.get("value", "")),
                        )
                        imported += 1

                    conn.commit()
                    conn.close()
                    print(f"  🧠 Imported {imported} memory notes")
                except Exception as e:
                    print(f"  ⚠️  Could not import memory: {e}")
        elif import_memory:
            print("  ℹ️  No memory notes in bundle")

        # Import MCP config (merge into config.yaml agent_overrides)
        if "mcp_servers.json" in zf.namelist():
            mcp_servers = json.loads(zf.read("mcp_servers.json"))
            config_path = project_root / "config.yaml"
            config_data = _load_yaml(config_path)
            overrides = config_data.setdefault("agent_overrides", {})
            agent_section = overrides.setdefault(name, {})
            agent_section["mcp_servers"] = mcp_servers
            with open(config_path, "w") as f:
                yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)
            print(f"  🔌 Imported MCP config into config.yaml")

    # Register agent in registry if not already there
    registry_path = project_root / "system" / "registry.yaml"
    if registry_path.exists():
        with open(registry_path) as f:
            registry = yaml.safe_load(f) or {"agents": []}

        existing_names = [a["name"] for a in registry.get("agents", [])]
        if name not in existing_names:
            registry.setdefault("agents", []).append({
                "name": name,
                "display_name": display_name,
                "description": manifest.get("description", ""),
                "status": "active",
            })
            with open(registry_path, "w") as f:
                yaml.dump(registry, f, default_flow_style=False, sort_keys=False)
            print(f"  📝 Registered '{name}' in system/registry.yaml")
        else:
            print(f"  ℹ️  '{name}' already in registry")

    print()
    print("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  🍡 Import complete! Run `mochi-agents` or send /reload to activate.")
    print()


# ---------------------------------------------------------------------------
# Reset Command
# ---------------------------------------------------------------------------

def run_reset() -> None:
    """Reset agent data with interactive confirmation.

    Modes:
        mochi-agents reset                Clear ALL agents' conversation history
        mochi-agents reset <agent>        Clear a specific agent's history
        mochi-agents reset --all          Full factory reset (all data)
        mochi-agents reset <agent> --all  Full reset for a specific agent
    """
    import sqlite3

    full_reset = "--all" in sys.argv
    # Check for agent name argument (skip flags)
    agent_filter = None
    for arg in sys.argv[2:]:
        if not arg.startswith("--"):
            agent_filter = arg
            break

    project_root = _find_project_root()
    config = _load_yaml(project_root / "config.yaml")
    data_dir = project_root / Path(config.get("data_dir", "./data"))

    if not data_dir.exists():
        print("  No data directory found. Nothing to reset.")
        return

    # Discover what we'll delete
    if agent_filter:
        target = data_dir / f"{agent_filter}.db"
        if not target.exists():
            print(f"  ❌ No database found for agent '{agent_filter}'")
            print(f"  Available: {', '.join(f.stem for f in sorted(data_dir.glob('*.db')))}")
            sys.exit(1)
        db_files = [target]
    else:
        db_files = sorted(data_dir.glob("*.db"))

    if not db_files:
        print("  No database files found. Nothing to reset.")
        return

    print()
    print("🍡 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if full_reset:
        title = f"Full Reset: {agent_filter}" if agent_filter else "Full Factory Reset"
    else:
        title = f"Reset: {agent_filter}" if agent_filter else "Reset Conversation History"
    print(f"     {title}")
    print("   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()

    # Show what will be affected
    for db_path in db_files:
        agent_name = db_path.stem
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            tables = [row[0] for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]

            print(f"  📦 {agent_name}.db")
            for table in sorted(tables):
                count = cursor.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
                if full_reset:
                    action = "DELETE"
                elif table == "conversation_messages":
                    action = "DELETE"
                elif table == "memory_notes":
                    action = "DELETE"
                else:
                    action = "keep"

                icon = "🗑️ " if action == "DELETE" else "  "
                print(f"    {icon}{table}: {count} rows {'→ DELETE' if action == 'DELETE' else '→ keep'}")

            conn.close()
        except Exception as e:
            print(f"  ⚠️  {agent_name}.db: error reading ({e})")

    if full_reset:
        # Also show non-db files
        other_files = [f for f in data_dir.rglob("*") if f.is_file() and f.suffix != ".db" and f.name != ".gitkeep"]
        if other_files:
            print()
            print("  📁 Other data files:")
            for f in other_files:
                print(f"    🗑️  {f.relative_to(data_dir)} → DELETE")

    print()

    if full_reset:
        print("  ⚠️  WARNING: This will delete ALL data including")
        print("  zones, reminders, expirables, shortcuts, and aliases.")
    else:
        print("  ℹ️  This will clear conversation history and memory notes only.")
        print("  Zones, reminders, expirables, and shortcuts will be preserved.")

    print()
    confirm = input("  Are you sure? Type 'yes' to confirm: ").strip().lower()

    if confirm != "yes":
        print("  ❌ Reset cancelled.")
        return

    print()

    # Perform the reset
    if full_reset:
        # Delete all db files
        for db_path in db_files:
            db_path.unlink()
            print(f"  🗑️  Deleted {db_path.name}")

        # Delete other data files (but keep .gitkeep)
        for f in data_dir.rglob("*"):
            if f.is_file() and f.name != ".gitkeep":
                f.unlink()
                print(f"  🗑️  Deleted {f.relative_to(data_dir)}")

        # Remove empty subdirectories
        for d in sorted(data_dir.rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    else:
        # Clear only conversation history and memory notes
        history_tables = ["conversation_messages", "memory_notes"]
        for db_path in db_files:
            try:
                conn = sqlite3.connect(str(db_path))
                cursor = conn.cursor()
                tables = [row[0] for row in cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()]

                cleared = []
                for table in history_tables:
                    if table in tables:
                        cursor.execute(f"DELETE FROM [{table}]")
                        cleared.append(table)

                conn.commit()
                conn.close()
                if cleared:
                    print(f"  ✅ {db_path.stem}: cleared {', '.join(cleared)}")
                else:
                    print(f"  ⏭️  {db_path.stem}: no history tables")
            except Exception as e:
                print(f"  ⚠️  {db_path.stem}: error ({e})")

    print()
    print("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  🍡 Reset complete! Restart mochi-agents to start fresh.")
    print()


# ---------------------------------------------------------------------------
# Bot Runner
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


def _load_yaml(path: Path) -> dict:
    """Load a YAML file; return empty dict if missing."""
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


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
