# 🍡 Mochi — Multi-Agent Service Bot

A modular, multi-agent bot that routes tasks to domain-specific specialist agents. Each agent has its own memory, skills, and model configuration.

## Quick Start

```bash
# Install in editable mode
pip install -e .

# Interactive setup (tokens, config)
mochi-agents setup

# Enable tab completion (optional)
eval "$(mochi-agents completions)"

# Run the bot
mochi-agents
```

### Shell Completion

Enable tab completion for all commands, agent names, and flags:

```bash
# Current session only
eval "$(mochi-agents completions)"

# Make permanent (bash)
mochi-agents completions >> ~/.bashrc && source ~/.bashrc

# Make permanent (zsh)
mochi-agents completions >> ~/.zshrc && source ~/.zshrc
```

## CLI Reference

| Command | Description |
|---|---|
| `mochi-agents` | Start the bot + dashboard |
| `mochi-agents setup` | Interactive first-time setup |
| `mochi-agents config show` | Show current configuration |
| `mochi-agents config set KEY VALUE` | Set a configuration value |
| `mochi-agents agent list` | List all registered agents |
| `mochi-agents agent info NAME` | Show agent details |
| `mochi-agents agent export NAME` | Export agent as `.agent` bundle |
| `mochi-agents agent import FILE` | Import agent from `.agent` bundle |
| `mochi-agents agent remove NAME` | Remove an agent (with confirmation) |
| `mochi-agents reset` | Clear all agents' conversation history |
| `mochi-agents reset NAME` | Clear a specific agent's history |
| `mochi-agents reset --all` | Full factory reset (deletes all data) |
| `mochi-agents completions` | Print shell completion script |

## Agent Lifecycle

### Export & Share

```bash
# Export an agent (config + code + shortcuts)
mochi-agents agent export nutritionist

# Include learned memory notes
mochi-agents agent export nutritionist --with-memory

# Output: nutritionist.agent
```

### Import

```bash
# Import from a .agent bundle
mochi-agents agent import nutritionist.agent

# Also import memory notes
mochi-agents agent import nutritionist.agent --with-memory
```

### Remove

```bash
# Remove an agent (prompts for confirmation, suggests backup first)
mochi-agents agent remove nutritionist
```

### Reset

```bash
# Clear Sora's chat history (keeps zones, reminders, expirables)
mochi-agents reset secretary

# Full reset for one agent (deletes everything)
mochi-agents reset secretary --all
```

## Architecture

- **Input Router** — routes messages to the correct agent via keyword matching or Manager delegation
- **Manager Agent** — resolves ambiguous routing, orchestrates multi-agent tasks
- **Nutritionist Agent** — daily diet tracking, macro estimation, meal suggestions
- **Secretary Agent** — location-based reminders, expirable item tracking (credits, coupons, subscriptions)
- **Scheduler** — recurring cron jobs + one-off timed tasks
- **ToolRunner** — dynamic tool discovery with hot-reload support

## Configuration Architecture

Mochi separates committed defaults from user-specific runtime config:

| What | Location | Committed? |
|---|---|---|
| Agent defaults (model, routing, tools) | `agents/*/mission.yaml` | ✅ Yes |
| Default schedules (daily checks) | `agents/*/mission.yaml` | ✅ Yes |
| Model config changes (via admin) | `config.yaml` → `agent_overrides` | ❌ User-specific |
| MCP tool registrations | `config.yaml` → `agent_overrides` | ❌ User-specific |
| User cron schedules | `config.yaml` → `schedules` | ❌ User-specific |
| API keys & tokens | `secret.yaml` | ❌ Gitignored |

### User Cron Schedules

Add recurring tasks in `config.yaml` (not in mission.yaml):

```yaml
schedules:
  - agent: secretary
    name: monthly_uber_credit
    cron: "0 0 1 * *"
    action: add_expirable
    mode: tool
    args:
      title: "Uber Credit"
      value: 25.0
      category: "credit"
      expiration_date: "{end_of_month}"
    target_user_ids: all
```

Supported date templates: `{end_of_month}`, `{start_of_next_month}`, `{today}`, `{today+N}`

## Project Structure

```
mochi/
├── system/              # Soul file (personality) + agent registry
├── agents/              # Per-agent mission files (YAML + system prompts)
├── data/                # Per-agent SQLite databases (gitignored)
├── mochi_agents/        # Python package (core runtime, comm, memory)
│   └── agents/          # Agent tool implementations
├── skills_server/       # MCP skill files (auto-generated tools)
├── tests/               # Unit and integration tests
├── config.yaml          # User-specific settings (gitignored, created by setup)
└── secret.yaml          # API keys & tokens (gitignored, created by setup)
```

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
```
