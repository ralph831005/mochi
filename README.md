# 🍡 Mochi — Multi-Agent Service Bot

A modular, multi-agent bot that routes tasks to domain-specific specialist agents. Each agent has its own memory, skills, and model configuration.

## Quick Start

```bash
# Install in editable mode
pip install -e .

# Interactive setup (tokens, config)
mochi-agents setup

# Run the bot
mochi-agents
```

## Architecture

- **Input Router** — routes messages to the correct agent via keyword matching or Manager delegation
- **Manager Agent** — resolves ambiguous routing, orchestrates multi-agent tasks
- **Nutritionist Agent** — daily diet tracking, macro estimation, meal suggestions
- **Scheduler** — recurring cron jobs + one-off timed tasks
- **ToolRunner** — dynamic tool discovery with hot-reload support

## Project Structure

```
mochi/
├── system/          # Soul file (personality) + agent registry
├── agents/          # Per-agent mission files (YAML + system prompts)
├── data/            # Per-agent SQLite databases (gitignored)
├── mochi_agents/    # Python package (core runtime, comm, memory)
├── tests/           # Unit and integration tests
├── config.yaml      # Non-secret settings (committed)
└── secret.yaml      # API keys & tokens (gitignored, created by setup)
```

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
```
