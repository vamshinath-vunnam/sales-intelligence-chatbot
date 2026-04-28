# Auth Notes — TR AI Platform vs Direct Claude API Key

## Current Setup (TR AI Platform)

Uses Thomson Reuters' internal AI gateway via a workspace token exchange.

**Files changed:** `src/agent.py`

**How it works:**
1. `POST https://aiplatform.gcs.int.thomsonreuters.com/v1/anthropic/token`
   with body `{"workspace_id": WORKSPACE_ID}`
2. Response: `{"anthropic_api_key": "<short-lived-key>"}`
3. Key cached for 3500 seconds, then refreshed automatically
4. `AsyncAnthropic` client created with `httpx.AsyncClient(verify=False)` (corporate SSL proxy)

**`.env` required:**
```
WORKSPACE_ID=GTMGenAITraiUq7Q
ANTHROPIC_MODEL=claude-sonnet-4-20250514
```

---

## To Switch to Direct Claude API Key

**Step 1:** Update `.env`:
```
# Remove or comment out WORKSPACE_ID
# WORKSPACE_ID=...

ANTHROPIC_API_KEY=sk-ant-...your-key-here...
ANTHROPIC_MODEL=claude-sonnet-4-5-20251001   # or any model you have access to
```

**Step 2:** In `src/agent.py`, the `_fetch_api_key()` function already handles this fallback:
```python
# If WORKSPACE_ID is not set, it falls back to ANTHROPIC_API_KEY automatically
if not WORKSPACE_ID:
    direct_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if direct_key:
        return direct_key
```
No code changes needed — just update `.env`.

**Step 3:** The `_make_async_client()` function uses `verify=False` for the httpx client
(needed for corporate proxy). If running outside the TR network, you can change this to:
```python
http_client=httpx.AsyncClient(verify=True),  # standard SSL verification
```

**Step 4:** Update model IDs in `src/agent.py` if switching models:
```python
HAIKU_MODEL = "claude-3-5-haiku-20241022"   # available on direct API
SONNET_MODEL = ANTHROPIC_MODEL               # from .env
```

---

## Model Availability

| Model | TR AI Platform | Direct API |
|---|---|---|
| `claude-sonnet-4-20250514` | ✅ Available | ✅ Available |
| `claude-3-5-haiku-20241022` | ❌ Not found | ✅ Available |
| `claude-sonnet-4-5-20251001` | Not tested | ✅ Available |

On TR AI Platform, only `claude-sonnet-4-20250514` was confirmed working.
On direct API, haiku routing can be enabled by setting `HAIKU_MODEL = "claude-3-5-haiku-20241022"`.
