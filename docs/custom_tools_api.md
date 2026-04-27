# Mochi Custom Tool Architecture API

Mochi supports a "drop-in" custom capabilities architecture via `custom_tools.py` modules. This allows you (or the Learner agent) to rapidly build strictly local modifications to LLM capabilities without running the risk of Git pulling upstream repository changes and triggering merge conflicts.

This document clearly outlines the system's "contracts" — what you can do, what Mochi expects, and what may break in future releases.

## 1. File Placement & Discovery

Mochi looks for custom tool implementations directly inside any registered Agent's folder alongside their `tools.py`.

```
agents/
  nutritionist/
    mission.yaml
    tools.py          <-- Version Controlled (Upstream code)
    custom_tools.py   <-- Ignored by Git (Your custom code)
```

**The Core Contract:**
If `agents/{agent}/tools.py` exists, Mochi will automatically look for `agents/{agent}/custom_tools.py` at startup and concatenate their exports.

## 2. API Format (What you must implement)

Any tools placed inside `custom_tools.py` must follow **two strict rules** to be captured by the system runner:

### Rule A: Type Hinted Functions with Docstrings
The signature must be strongly typed with Python Type Hints, and possess a descriptive Docstring. This is what the LLM will read to understand *what* the tool does and *what* variables it expects.

```python
# ✅ CORRECT
def calculate_bmi(height_cm: float, weight_kg: float) -> str:
    """Calculates user BMI given height and weight."""
    ...

# ❌ INCORRECT (Missing type hints and docstring)
def calculate_bmi(height, weight):
    ...
```

### Rule B: API Version Constant
The file must declare an `API_VERSION` constant. This ensures future releases of Mochi won't blindly load legacy tools that could crash the bot if internal signatures change. Currently, this must be set to `"1.0"`.

### Rule C: `get_tools()` Export Array
The file must feature a module-level `get_tools()` function that returns a list of your callable functions. This is how the ToolRunner safely introspects your tools.

```python
API_VERSION = "1.0"

def get_tools() -> list:
    """Return Learner-specific tools."""
    return [calculate_bmi, search_database]
```

## 3. Sandboxing & Future Compatibility

### What is Safe (Backwards Compatible)
1. **Adding pure mathematical tools**: Code that doesn't utilize any of Mochi's internal engine interfaces (e.g. basic math calculations, external API pings via `requests`).
2. **Accessing built-in Python Modules**: Modules like `datetime`, `json`, `math`.

### What Might Require Migration (Breaking Changes)
If Mochi releases a major version update, internal schemas could change. Specifically, be careful when using the following in your custom tools, as they are not guaranteed to remain stable:

> [!WARNING]
> If you utilize any of Mochi's **internal engines**, those tools may require updates in future releases:
> 
> *   **SQLAlchemy Database Memory Models (`mochi_agents.memory.models`)**: If upstream renames `MealLog` to `MealRecord`, your custom script querying `MealLog` will break until updated.
> *   **System Paths (`mochi_agents.config`)**: The structure of the `config.yaml` and where data lives.
> *   **Telegram / Comm Layer**: Don't build custom tools that attempt to send messages back to the user directly skipping the `bot.py` router. Return a string in your tool instead and let the LLM format the display.

## 4. Example `custom_tools.py`

This is a complete, well-formatted drop-in tool logic sequence.

```python
# agents/nutritionist/custom_tools.py

API_VERSION = "1.0"

def bulk_log_groceries(items_csv: str) -> str:
    """Scans a comma-separated list of items and logs them as groceries."""
    
    items = [x.strip() for x in items_csv.split(",")]
    
    # Internal logic...
    count = len(items)
    
    return f"Successfully imported {count} items into the pantry."

def get_tools() -> list:
    """Export array mandated by Mochi's ToolLoader."""
    return [bulk_log_groceries]
```
