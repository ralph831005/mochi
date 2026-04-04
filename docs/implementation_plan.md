# Mochi — MVP Implementation Plan

## Goal

Scaffold and implement the Mochi multi-agent service bot MVP: Input Router + Manager Agent + Nutritionist Agent, served over Telegram, built as a single-process Python bot with long polling.

---

## Project Structure

```
mochi/
├── docs/
│   ├── user_story_and_requirements.md
│   └── implementation_plan.md
├── system/
│   ├── soul.md                    # Shared personality & rules
│   └── registry.yaml              # Agent directory
├── agents/
│   ├── manager/
│   │   ├── mission.yaml           # Manager config & prompt
│   │   └── mission_prompt.md      # Manager system prompt (detailed)
│   └── nutritionist/
│       ├── mission.yaml           # Nutritionist config & prompt
│       └── mission_prompt.md      # Nutritionist system prompt (detailed)
├── data/                          # SQLite databases (gitignored)
│   ├── manager.db
│   └── nutritionist.db
├── mochi_agents/                  # Main Python package
│   ├── __init__.py
│   ├── __main__.py                # Allows `python -m mochi_agents` as fallback
│   ├── cli.py                     # CLI entry point (setup, run)
│   ├── bot.py                     # Main bot loop (init → poll → route → respond)
│   ├── config.py                  # Reloadable config singleton (YAML-based)
│   ├── core/
│   │   ├── __init__.py
│   │   ├── router.py              # Input Router logic + /reload system command
│   │   ├── registry.py            # Registry loader & agent discovery
│   │   ├── agent_runtime.py       # Agent execution engine (Soul + Mission + Memory → LLM)
│   │   ├── tool_runner.py         # ToolRunner protocol + ImportlibToolRunner (MVP)
│   │   ├── model_factory.py       # LLM client factory (provider/model dispatch)
│   │   └── scheduler.py           # Cron + one-off job scheduler
│   ├── memory/
│   │   ├── __init__.py
│   │   ├── database.py            # SQLite connection manager (per-agent)
│   │   └── models.py              # SQLAlchemy models (conversations, meals, etc.)
│   ├── agents/
│   │   ├── __init__.py
│   │   └── nutritionist/
│   │       ├── __init__.py
│   │       └── tools.py           # Nutritionist-specific tools (meal CRUD, macro queries)
│   └── comm/
│       ├── __init__.py
│       ├── base.py                # Abstract CommunicationClient protocol
│       └── telegram.py            # Telegram long-polling implementation
├── tests/
│   ├── __init__.py
│   ├── test_router.py
│   ├── test_agent_runtime.py
│   └── test_nutritionist.py
├── secret.yaml              # API keys & tokens (gitignored, created by `mochi-agents setup`)
├── config.yaml              # Non-secret settings (paths, allowed users, active client)
├── .gitignore
├── pyproject.toml           # Declares `mochi-agents` CLI command
└── README.md
```

---

## Proposed Changes

### 1. Project Setup & Configuration

#### [NEW] `pyproject.toml`
Python project configuration using `uv` or `pip`.

**CLI entry point:**
```toml
[project.scripts]
mochi-agents = "mochi_agents.cli:main"
```
After `pip install -e .`, the `mochi-agents` command becomes available system-wide.

**Dependencies:**
- `sqlalchemy[asyncio]`, `aiosqlite` — async SQLite per-agent
- `pydantic` — config validation
- `google-genai` — Gemini SDK
- `httpx` — async HTTP client (Telegram Bot API calls)
- `pyyaml` — YAML parsing for config, mission files & registry
- `pytest`, `pytest-asyncio` — testing (dev)

#### [NEW] `secret.yaml` *(gitignored, created by `mochi-agents setup`)*
```yaml
gemini_api_key: "your-key-here"
telegram_bot_token: "your-token-here"
```

#### [NEW] `config.yaml`
```yaml
active_client: telegram        # Which CommunicationClient to use
allowed_user_ids:
  - 123456789
  - 987654321
data_dir: ./data
agents_dir: ./agents
system_dir: ./system
```

#### [NEW] `mochi_agents/config.py`
Reloadable configuration singleton:
- `Settings` Pydantic model with fields from both `config.yaml` and `secret.yaml`
- `load_settings()` → reads and merges both YAML files, returns validated `Settings`
- `get_settings()` → returns the current cached `Settings` instance
- `reload_settings()` → re-reads YAML files from disk and replaces the cached instance
- Secrets and config are separated: `secret.yaml` is gitignored, `config.yaml` is committed
- On first access, if `secret.yaml` is missing, raises a clear error pointing the user to `mochi-agents setup`

#### [NEW] `mochi_agents/cli.py`
CLI entry point — the single `main()` function registered in `pyproject.toml`:

```python
def main():
    """Entry point for the `mochi-agents` command."""
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        run_setup()
    else:
        run_bot()
```

**`mochi-agents setup`:**
- Interactive prompts for each secret:
  - `Gemini API Key:` (input masked)
  - `Telegram Bot Token:` (input masked)
- Writes secrets to `secret.yaml` with `0600` file permissions
- Generates a default `config.yaml` if one doesn't exist
- Validates the tokens by making a test API call (e.g., Telegram `getMe`)
- Prints a success message with the bot username

**`mochi-agents` (no args):**
- Loads config, selects the `CommunicationClient`, and starts the bot

---

### 2. System Files (Soul, Registry)

#### [NEW] `system/soul.md`
Shared personality and safety rules. Content:
- Mochi's name and personality traits (helpful, concise, friendly)
- Safety guardrails (no medical diagnoses, no financial advice beyond budgeting)
- Output formatting guidelines (prefer structured responses, use emoji sparingly)
- Multi-agent awareness note: "You are one agent in a larger system. Stay within your specialty."

#### [NEW] `system/registry.yaml`
Initial registry with Manager and Nutritionist entries (as shown in requirements §5.4.2).

---

### 3. Agent Mission Files

#### [NEW] `agents/manager/mission.yaml`
```yaml
name: manager
display_name: "Manager"
description: "Routes ambiguous tasks to the appropriate specialist agent."
routing_keys: []
model_config:
  provider: google
  model: gemini-2.0-flash
  temperature: 0.2
  max_tokens: 1024
capabilities:
  - text_prompt
```

#### [NEW] `agents/manager/mission_prompt.md`
Detailed system prompt instructing the Manager to:
1. Read the registry to know available agents
2. Analyze user intent and match to an agent's `routing_keys` / `description`
3. Return a structured routing decision (JSON: `{route_to: "agent_name", reason: "..."}`)
4. If no match, compose a user-friendly "I can't do that yet" reply

#### [NEW] `agents/nutritionist/mission.yaml`
```yaml
name: nutritionist
display_name: "Nutritionist"
description: "Manages daily diet tracking, macro estimation, and dietary suggestions."
routing_keys: [nutrition, diet, food, meal, calories, macros]
tools_module: mochi_agents.agents.nutritionist.tools    # Auto-discovered by ToolRunner
model_config:
  provider: google
  model: gemini-2.0-flash
  temperature: 0.3
  max_tokens: 2048
capabilities:
  - text_prompt
  - voice_transcript
schedules:
  - name: daily_summary
    cron: "0 21 * * *"
    action: daily_diet_summary
    target_user_ids: all
```

#### [NEW] `agents/manager/mission_prompt.md`
Detailed system prompt instructing the Manager to:
1. Read the registry to know available agents
2. Analyze user intent and match to an agent's `routing_keys` / `description`
3. Return a structured routing decision (JSON: `{route_to: "agent_name", reason: "..."}`)
4. If no match, compose a user-friendly "I can't do that yet" reply

#### [NEW] `agents/nutritionist/mission.yaml`
As shown in requirements §5.4.1.

#### [NEW] `agents/nutritionist/mission_prompt.md`
Detailed system prompt for the Nutritionist covering:
- How to parse meal descriptions
- How to estimate macros (calories, protein, carbs, fat)
- When to query vs. insert into the database
- Output formatting (structured meal log vs. conversational reply)
- Tool usage instructions (meal logging, history queries)

---

### 4. Memory Layer (SQLite per-agent)

#### [NEW] `src/memory/database.py`
- `get_engine(agent_name: str)` → creates/returns an async SQLAlchemy engine pointing to `data/{agent_name}.db`
- `get_session(agent_name: str)` → async session factory
- `init_db(agent_name: str)` → creates tables on startup

#### [NEW] `src/memory/models.py`
SQLAlchemy models:

**Shared (all agents):**
- `ConversationMessage`: `id`, `role` (user/assistant/system), `content`, `created_at` — stores conversation history for memory injection
- `ScheduledJob`: `id`, `agent_name`, `run_at` (datetime UTC), `action`, `payload` (JSON), `target_user_id`, `status` (pending/completed/cancelled), `created_at` — persists one-off scheduled jobs

**Nutritionist-specific:**
- `MealLog`: `id`, `user_id`, `meal_type` (breakfast/lunch/dinner/snack), `description`, `calories`, `protein_g`, `carbs_g`, `fat_g`, `logged_at`, `created_at`

---

### 5. Core Runtime

#### [NEW] `mochi_agents/core/tool_runner.py`
Abstract `ToolRunner` protocol and MVP implementation:

```python
class ToolRunner(Protocol):
    """Interface for tool execution — swap implementations without touching agents."""
    async def execute(self, agent_name: str, tool_name: str, args: dict) -> ToolResult: ...
    def get_tool_declarations(self, agent_name: str) -> list[ToolDeclaration]: ...
    def reload(self, agent_name: str) -> None: ...
```

**MVP implementation — `ImportlibToolRunner`:**
- Reads `tools_module` from the agent's `mission.yaml`
- Uses `importlib.import_module()` to load the tools module
- Calls `module.get_tools()` (convention) to get a list of tool functions
- On `reload()`, purges `sys.modules` cache and re-imports
- Each tool function follows LLM function-calling conventions (typed args, returns dict)

**Phase 2 migration — `SubprocessToolRunner`:**
- Same interface, swaps `importlib` for `asyncio.create_subprocess_exec()`
- Enables sandboxed execution of Learner-generated tools
- Migration cost: ~100 lines new runner + ~10 lines CLI wrapper per tool module
- Zero changes to `agent_runtime.py`, `router.py`, or any agent code

#### [NEW] `src/core/model_factory.py`
Factory that reads `model_config` from a Mission File and returns the appropriate LLM client:
- `create_client(model_config: dict)` → returns configured `google.genai` client
- Extensible for future providers (OpenAI, Anthropic) via a provider registry pattern
- API keys pulled from `config.py`, never from mission files

#### [NEW] `src/core/registry.py`
- `load_registry()` → parses `system/registry.yaml`, returns list of `AgentEntry` dataclasses
- `find_agent_by_key(keyword: str)` → searches `routing_keys` across all active agents
- `get_agent(name: str)` → returns a specific agent entry
- Registry is loaded once at startup and reloaded on `/reload` command

#### [NEW] `src/core/agent_runtime.py`
The core execution engine. Given an agent name and a user message:
1. Load **Soul File** (`system/soul.md`)
2. Load **Mission File** (`agents/{name}/mission.yaml` + `mission_prompt.md`)
3. Load **Memory** (recent `ConversationMessage` rows from `data/{name}.db`)
4. Load **Tools** via `tool_runner.get_tool_declarations(agent_name)` — auto-discovered from `tools_module`
5. Assemble system instruction = Soul + Mission prompt
6. Assemble conversation history = Memory rows
7. Call LLM via `model_factory`, passing tool declarations
8. Parse response for tool calls → execute via `tool_runner.execute(agent_name, tool_name, args)`
9. Save the assistant response to conversation memory
10. Return final response text

Key design: the runtime is **agent-agnostic**. It doesn't know what a Nutritionist is. It discovers tools dynamically via the `ToolRunner` interface.

#### [NEW] `src/core/router.py`
Input Router logic:
- `route_message(message: IncomingMessage)` → `RoutingDecision`
- **Step 0 — System commands**: Intercept `/reload` before agent routing:
  - `/reload` → calls `reload_settings()`, reloads registry, re-scans schedules, reloads tool modules
  - Responds with a summary of what was reloaded
- **Step 1 — Direct match**: Check if the message contains a slash command (`/nutrition ...`) or if keyword analysis matches a `routing_key` in the registry with high confidence
- **Step 2 — Delegated routing**: If no direct match, invoke the Manager Agent via `agent_runtime.execute("manager", message)` and parse the Manager's routing decision
- **Step 3 — Execute**: Forward the message to the resolved agent via `agent_runtime.execute(agent_name, message)`
- Supports recursive calls (an agent's response can contain a `route_to` directive)

---

### 6. Nutritionist Tools

#### [NEW] `src/agents/nutritionist/tools.py`
Tool functions that the Nutritionist agent can invoke (via function-calling / tool-use in the LLM API):
- `log_meal(user_id, meal_type, description, calories, protein, carbs, fat)` → inserts to `MealLog`
- `get_today_summary(user_id)` → queries `MealLog` for today, returns totals
- `get_history(user_id, days=7)` → queries recent meal history
- `search_meals(user_id, query)` → text search over meal descriptions
- `schedule_job(user_id, run_at, action, payload)` → creates a one-off scheduled job in SQLite (shared tool, available to all agents)
- `cancel_job(job_id)` → marks a pending job as cancelled

---

### 7. Scheduler

#### [NEW] `mochi_agents/core/scheduler.py`
Runs as a parallel `asyncio` task alongside the polling loop.

**Recurring jobs (cron):**
- On startup, scans all agent `mission.yaml` files for `schedules` blocks
- Parses cron expressions and registers each as a recurring task
- When a cron job fires:
  1. Creates a synthetic `IncomingMessage` with `source=scheduler`
  2. Executes via `AgentRuntime.execute(agent_name, synthetic_message)`
  3. Sends the result to target users via `client.send()`
- Cron schedules are re-scanned on `reload_settings()`

**One-off jobs:**
- On startup, loads all `pending` `ScheduledJob` rows from every agent's SQLite
- Registers each as a one-shot `asyncio` delayed task
- When a job fires:
  1. Executes through the same `AgentRuntime` path as cron jobs
  2. Marks the job as `completed` in SQLite
- If a job's `run_at` is in the past (missed during downtime), it fires immediately on startup

**Main loop:**
```python
async def run(self):
    while True:
        await self._check_cron_jobs()
        await self._check_oneoff_jobs()
        await asyncio.sleep(30)  # tick every 30 seconds
```

---

### 7. Communication Client (Abstract + Telegram)

#### [NEW] `mochi_agents/comm/base.py`
Abstract `CommunicationClient` protocol that all platform clients must implement:
```python
class CommunicationClient(Protocol):
    async def poll(self) -> AsyncIterator[IncomingMessage]: ...
    async def send(self, chat_id: str, text: str, **kwargs) -> None: ...
    def format_response(self, agent_response: str) -> str: ...
```
Also defines the platform-agnostic `IncomingMessage` dataclass:
- `user_id: str`, `chat_id: str`, `text: str | None`, `attachments: list`, `timestamp: datetime`

#### [NEW] `mochi_agents/comm/telegram.py`
Telegram implementation of `CommunicationClient` using `httpx` for long polling:
- `TelegramClient(token, allowed_user_ids)` — constructor, stores config
- `poll()` — async generator that long-polls `getUpdates`, yields `IncomingMessage` objects
- `parse_update(update: dict)` → extracts fields from Telegram update into `IncomingMessage`
- `check_allowlist(user_id: int)` → silently ignores unauthorized users
- `send(chat_id, text, reply_markup=None)` → sends response via `sendMessage` API
- `format_response(agent_response: str)` → converts agent markdown to Telegram MarkdownV2

Future implementations (e.g., `SlackClient`, `CliClient`) would go in the same `mochi_agents/comm/` directory.

---

### 8. Bot Entry Point

#### [NEW] `mochi_agents/bot.py`
Main bot orchestration loop — **client-agnostic**:
- `MochiBot(client: CommunicationClient)` — accepts any communication client
- `async start()` method:
  1. Load config via `get_settings()`
  2. Load registry, initialize SQLite databases for all registered agents
  3. Create `Router`, `AgentRuntime`, `Scheduler`
  4. Log startup summary (active agents, model configs, registered schedules)
  5. Run both loops in parallel:
     ```python
     await asyncio.gather(
         self.poll_loop(),      # reactive: incoming messages
         self.scheduler.run(),  # proactive: cron + one-off jobs
     )
     ```
  6. For each polled message: route → execute agent → send response back via `self.client.send()`
- Graceful shutdown on `SIGINT`/`SIGTERM` cancels both tasks

#### [NEW] `mochi_agents/__main__.py`
Fallback entry point for `python -m mochi_agents` — simply calls `cli.main()`.

The primary way to run the bot is via the installed `mochi-agents` command:
```bash
# Install the package (editable mode for development)
pip install -e .

# Now available as a CLI command:
mochi-agents          # Start the bot
mochi-agents setup    # Interactive token setup
```

---

### 9. Tests

#### [NEW] `tests/test_router.py`
- Test that "I had pizza for lunch" routes to Nutritionist
- Test that "What's the weather?" falls through to Manager
- Test that `/nutrition summary` direct-routes to Nutritionist

#### [NEW] `tests/test_agent_runtime.py`
- Test that Soul + Mission + Memory are correctly assembled into a prompt
- Test that conversation messages are persisted after execution

#### [NEW] `tests/test_nutritionist.py`
- Test `log_meal` inserts correctly into SQLite
- Test `get_today_summary` aggregates macros
- Test `get_history` returns correct date range

#### [NEW] `tests/test_scheduler.py`
- Test recurring cron jobs fire at the correct time (mocked clock)
- Test one-off jobs fire and are marked `completed`
- Test one-off jobs survive simulated restart (load from SQLite)
- Test past-due one-off jobs fire immediately on startup
- Test `cancel_job` marks a pending job as `cancelled`

---

## Verification Plan

### Automated Tests
```bash
pytest tests/ -v
```

### Integration Smoke Test
```bash
# Install the package
pip install -e .

# First-time setup
mochi-agents setup

# Start the bot (will begin long-polling Telegram)
mochi-agents

# Send test messages from Telegram and verify responses
```

### End-to-End (Telegram)
1. Run `pip install -e .` to install the package
2. Run `mochi-agents setup` — enter Gemini API key and Telegram bot token
3. Run `mochi-agents`
4. Send messages from Telegram:
   - *"I had eggs and toast for breakfast"* → should route to Nutritionist, log meal, reply with macros
   - *"What's the weather?"* → should route to Manager, reply "no weather agent yet"
   - *"How many calories today?"* → should route to Nutritionist, query SQLite, reply with totals
5. Edit `config.yaml` to change `allowed_user_ids` — verify it takes effect without restart

---

## Execution Order

| Phase | Components | Depends On |
|---|---|---|
| **1** | Project setup, `pyproject.toml`, `config.yaml`, `secret.yaml` | — |
| **2** | Config module (config.py) + CLI entry point (cli.py) | Phase 1 |
| **3** | System files (soul.md, registry.yaml), Mission files (incl. `tools_module`) | Phase 2 |
| **4** | Memory layer (database.py, models.py incl. ScheduledJob) | Phase 2 |
| **5** | ToolRunner protocol + ImportlibToolRunner | Phase 3 |
| **6** | Core runtime (model_factory, registry, agent_runtime) | Phase 3, 4, 5 |
| **7** | Input Router (incl. `/reload`) | Phase 6 |
| **8** | Nutritionist tools (incl. schedule_job, cancel_job) | Phase 4, 5 |
| **9** | Scheduler (cron + one-off) | Phase 4, 6 |
| **10** | Communication client (base.py + telegram.py) | Phase 2 |
| **11** | Bot entry point (bot.py, __main__.py) | Phase 6, 7, 8, 9, 10 |
| **12** | Tests | Phase 7, 8, 9 |
