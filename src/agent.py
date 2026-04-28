"""
agent.py — Claude API agent loop with MCP tool execution and adaptive model routing.

Authentication:
  Uses TR AI Platform token exchange (same pattern as gtm-ai_agentic-service).
  POST https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token
    → {"workspace_id": WORKSPACE_ID}
    → returns {"anthropic_api_key": "<short-lived-key>"}
  Falls back to direct ANTHROPIC_API_KEY if WORKSPACE_ID is not set.

Model routing:
  - Haiku  → simple, scoped queries (low complexity)
  - Sonnet → trend analysis, multi-brand/region comparisons (high complexity)
"""

import os
import time
import requests
import httpx
import anthropic
from dotenv import load_dotenv
from src.mcp_client import MCPClient

load_dotenv()

# ---------------------------------------------------------------------------
# TR AI Platform auth
# ---------------------------------------------------------------------------

TR_AI_PLATFORM_BASE = "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic"
TOKEN_URL = f"{TR_AI_PLATFORM_BASE}/token"
TOKEN_TTL_SECONDS = 3500  # tokens are valid ~1 hour; refresh slightly early

WORKSPACE_ID = os.environ.get("WORKSPACE_ID", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

# In-memory token cache
_api_key_cache: dict = {"key": "", "fetched_at": 0.0}


def _fetch_api_key() -> str:
    """Return a valid Anthropic API key, refreshing via TR AI Platform if needed."""
    now = time.time()
    cached = _api_key_cache
    if cached["key"] and (now - cached["fetched_at"]) < TOKEN_TTL_SECONDS:
        return cached["key"]

    # Fallback: direct API key
    if not WORKSPACE_ID:
        direct_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if direct_key:
            return direct_key
        raise ValueError(
            "Set WORKSPACE_ID (for TR AI Platform) or ANTHROPIC_API_KEY in .env"
        )

    # Token exchange via TR AI Platform
    resp = requests.post(
        TOKEN_URL,
        json={"workspace_id": WORKSPACE_ID},
        verify=False,  # corporate proxy — SSL verification disabled
        timeout=10,
    )
    resp.raise_for_status()
    creds = resp.json()
    if "anthropic_api_key" not in creds:
        raise RuntimeError(f"Token endpoint did not return an API key: {creds}")

    _api_key_cache["key"] = creds["anthropic_api_key"]
    _api_key_cache["fetched_at"] = now
    return _api_key_cache["key"]


def _make_async_client() -> anthropic.AsyncAnthropic:
    """Create an AsyncAnthropic client with a fresh token and SSL-disabled httpx client."""
    return anthropic.AsyncAnthropic(
        api_key=_fetch_api_key(),
        http_client=httpx.AsyncClient(verify=False),
    )


# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------

# Keywords that signal a complex query → use the more capable model
COMPLEXITY_SIGNALS = {
    "trend", "trends", "growth", "compare", "comparison", "year-over-year",
    "yoy", "over time", "historical", "trajectory", "forecast", "risk",
    "best", "worst", "top", "bottom", "rank", "all regions", "across",
    "national", "overall", "strategic", "insight", "why", "analysis",
    "highest", "lowest", "which brand", "all brands",
}

# Model IDs on TR AI Platform
# NOTE: TR AI Platform gateway currently exposes claude-sonnet-4-20250514.
# Routing logic is preserved — swap HAIKU_MODEL to a haiku endpoint when available.
HAIKU_MODEL = ANTHROPIC_MODEL     # falls back to sonnet on TR AI Platform
SONNET_MODEL = ANTHROPIC_MODEL    # claude-sonnet-4-20250514


def _select_model(user_message: str) -> str:
    words = set(user_message.lower().split())
    signals_found = words & COMPLEXITY_SIGNALS
    return SONNET_MODEL if len(signals_found) >= 2 else HAIKU_MODEL


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

MAX_TOOL_ITERATIONS = 6


async def run(
    user_message: str,
    system_prompt: str,
    history: list[dict],
    mcp_client: MCPClient,
) -> tuple[str, str]:
    """
    Run one turn of the agent loop.

    Returns:
        (answer_text, model_used)
    """
    client = _make_async_client()
    model = _select_model(user_message)
    tools = mcp_client.get_tools()

    # Sliding window: last 10 turns + new message
    messages = list(history) + [{"role": "user", "content": user_message}]

    response = None
    for _ in range(MAX_TOOL_ITERATIONS):
        response = await client.messages.create(
            model=model,
            max_tokens=1024,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return _extract_text(response), model

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result_text = await mcp_client.call_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })

            messages.append({"role": "user", "content": tool_results})
            continue

        break

    # Safety fallback
    return _extract_text(response) if response else "I don't have that data.", model


def _extract_text(response) -> str:
    parts = [block.text for block in response.content if hasattr(block, "text")]
    return "\n".join(parts).strip() or "I don't have that data."
