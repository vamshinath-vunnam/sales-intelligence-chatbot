"""
agent.py — Claude API agent loop with MCP tool execution and adaptive model routing.

Authentication (tried in order):
  1. TR AI Platform token exchange — when WORKSPACE_ID is set in .env
     POST https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token
       → {"workspace_id": WORKSPACE_ID}
       → returns {"anthropic_api_key": "<short-lived-key>"}
     Token is cached for 3500 seconds and refreshed automatically.
     Uses httpx with SSL verification disabled (required for TR corporate proxy).

  2. Direct Claude API key — fallback when WORKSPACE_ID is not set
     Uses ANTHROPIC_API_KEY from .env directly.
     Uses httpx with SSL verification enabled (standard).

Model routing:
  - HAIKU_MODEL  → simple, scoped queries (single metric, recent period)
  - SONNET_MODEL → complex queries (trends, multi-brand/region comparisons)

  On TR AI Platform, only claude-sonnet-4-20250514 is available — both tiers
  use sonnet. On the direct API, haiku routing is fully enabled via
  ANTHROPIC_HAIKU_MODEL in .env.
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
# Config
# ---------------------------------------------------------------------------

# [OPTIONAL] TR AI Platform workspace ID.
# When set, token exchange is used. When blank, falls back to ANTHROPIC_API_KEY.
WORKSPACE_ID = os.environ.get("WORKSPACE_ID", "").strip()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# Primary model (sonnet) — used for complex queries and as fallback for haiku
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

# Haiku model — used for simple queries when available (direct API only)
# Falls back to ANTHROPIC_MODEL when not set (e.g. on TR AI Platform)
ANTHROPIC_HAIKU_MODEL = os.environ.get("ANTHROPIC_HAIKU_MODEL", "").strip() or ANTHROPIC_MODEL

# Determine whether we're running via TR AI Platform or direct API
_USING_TR_PLATFORM = bool(WORKSPACE_ID)

# ---------------------------------------------------------------------------
# TR AI Platform token exchange
# ---------------------------------------------------------------------------

TR_AI_PLATFORM_BASE = "https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic"
TOKEN_URL = f"{TR_AI_PLATFORM_BASE}/token"
TOKEN_TTL_SECONDS = 3500  # tokens valid ~1 hour; refresh slightly early

# In-memory cache for the short-lived key
_api_key_cache: dict = {"key": "", "fetched_at": 0.0}


def _fetch_api_key() -> str:
    """
    Return a valid Anthropic API key.

    If WORKSPACE_ID is set: exchanges it via TR AI Platform and caches the result.
    Otherwise: returns ANTHROPIC_API_KEY directly.
    """
    # --- TR AI Platform path ---
    if _USING_TR_PLATFORM:
        now = time.time()
        cached = _api_key_cache
        if cached["key"] and (now - cached["fetched_at"]) < TOKEN_TTL_SECONDS:
            return cached["key"]

        try:
            resp = requests.post(
                TOKEN_URL,
                json={"workspace_id": WORKSPACE_ID},
                timeout=10,
            )
            resp.raise_for_status()
            creds = resp.json()
            if "anthropic_api_key" not in creds:
                raise RuntimeError(f"TR AI Platform returned unexpected response: {creds}")

            _api_key_cache["key"] = creds["anthropic_api_key"]
            _api_key_cache["fetched_at"] = now
            return _api_key_cache["key"]

        except Exception as tr_err:
            # TR platform unavailable — fall back to direct API key if configured
            if ANTHROPIC_API_KEY:
                print(f"[auth] TR AI Platform failed ({tr_err}), falling back to ANTHROPIC_API_KEY")
                return ANTHROPIC_API_KEY
            raise RuntimeError(
                f"TR AI Platform token exchange failed and no ANTHROPIC_API_KEY fallback is set. "
                f"Error: {tr_err}"
            ) from tr_err

    # --- Direct Claude API key path ---
    if not ANTHROPIC_API_KEY:
        raise ValueError(
            "No credentials found. Set WORKSPACE_ID (TR AI Platform) or "
            "ANTHROPIC_API_KEY (direct Claude API) in .env"
        )
    return ANTHROPIC_API_KEY


def _make_async_client() -> anthropic.AsyncAnthropic:
    """
    Create an AsyncAnthropic client.

    TR AI Platform: SSL verification disabled (required for corporate proxy).
    Direct API:     SSL verification enabled (standard).
    """
    ssl_verify = not _USING_TR_PLATFORM  # False for TR, True for direct API
    return anthropic.AsyncAnthropic(
        api_key=_fetch_api_key(),
        http_client=httpx.AsyncClient(verify=ssl_verify),
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

# Model assignments (resolved from env at startup)
HAIKU_MODEL = ANTHROPIC_HAIKU_MODEL   # simple queries
SONNET_MODEL = ANTHROPIC_MODEL        # complex queries


def _select_model(user_message: str) -> str:
    """Route to haiku or sonnet based on query complexity signals."""
    words = set(user_message.lower().split())
    signals_found = words & COMPLEXITY_SIGNALS
    return SONNET_MODEL if len(signals_found) >= 2 else HAIKU_MODEL


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

MAX_TOOL_ITERATIONS = 10


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

    # Safety fallback after max iterations
    return _extract_text(response) if response else "I don't have that data.", model


def _extract_text(response) -> str:
    parts = [block.text for block in response.content if hasattr(block, "text")]
    return "\n".join(parts).strip() or "I don't have that data."
