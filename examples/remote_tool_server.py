"""
Mochi MCP Tool Server — Example
================================
A real MCP-compliant tool server using the official `mcp` SDK (FastMCP).

Install:
    pip install "mcp[cli]"

Run:
    python examples/remote_tool_server.py

Test with the MCP Inspector:
    npx @modelcontextprotocol/inspector python examples/remote_tool_server.py

Connect from Mochi by adding to any agent's mission.yaml:
    mcp_servers:
      - "http://localhost:8000/mcp"

This server exposes tools that any MCP client (including Mochi) can discover
and execute over the streamable-http transport.
"""

from mcp.server.fastmcp import FastMCP

# ─── 1. Create the server ────────────────────────────────────────────
mcp = FastMCP(
    "Mochi Tool Server",
    instructions="A collection of utility tools for the Mochi bot.",
)


# ─── 2. Define tools with the @mcp.tool() decorator ─────────────────
#
#   • The function name becomes the tool name.
#   • The docstring becomes the tool description (shown to LLMs).
#   • Type hints on parameters become the JSON Schema automatically.
#   • Return value is sent back as the tool result.

@mcp.tool()
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    # Replace with a real weather API call
    return f"The weather in {city} is sunny and 75°F."


@mcp.tool()
def calculate_bmi(weight_kg: float, height_m: float) -> str:
    """Calculate Body Mass Index given weight in kg and height in meters."""
    bmi = weight_kg / (height_m ** 2)
    category = (
        "underweight" if bmi < 18.5
        else "normal" if bmi < 25
        else "overweight" if bmi < 30
        else "obese"
    )
    return f"BMI: {bmi:.1f} ({category})"


import hashlib

@mcp.tool()
def calculate_hash(text: str, algorithm: str = "sha256") -> str:
    """Calculate the hash of a string. Supported algorithms: sha256, md5, sha1."""
    if algorithm not in ("sha256", "md5", "sha1"):
        return f"Unsupported algorithm: {algorithm}"
    h = hashlib.new(algorithm)
    h.update(text.encode())
    return h.hexdigest()


# ─── 3. (Optional) Define resources — data the LLM can read ─────────

@mcp.resource("info://server-status")
def server_status() -> str:
    """Returns the current server status."""
    return "All systems operational."


# ─── 4. Run the server ───────────────────────────────────────────────
#
#   Transport options:
#     "streamable-http"  → recommended for network (Mochi connects here)
#     "sse"              → legacy, backward-compat only
#     "stdio"            → for local subprocess integrations (Claude Desktop)

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8000)
