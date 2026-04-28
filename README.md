# Sales Intelligence Chatbot

A persona-aware AI assistant that answers questions over real sales data.
Built with Chainlit, Claude (Anthropic), and SQLite via MCP.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env — add your WORKSPACE_ID or ANTHROPIC_API_KEY

# 3. Load data
python scripts/load_data.py

# 4. Run
chainlit run app.py --port 8100
```

Open `http://localhost:8100` in your browser.

---

## Build Log

### 1. AI Coding Agent Used

**Agent:** Claude Code (claude-sonnet-4-5, 1M context window)

**Approach:** Spec-first. The entire design was validated before a single line of
code was written — personas, data flow, MCP connection pattern, context window
strategy, and cost model were all locked in a design document first.

**Key prompts / specs given to the agent:**

```
"Build a persona-aware sales intelligence chatbot. Two personas:
 Sales Rep (scoped to their region, last 3 months, direct answers)
 and Country Head (all regions, 3-year trends, strategic narrative).
 Use Chainlit for UI, Claude API for reasoning, SQLite via mcp-server-sqlite
 for data access. The AI agent must query the DB through MCP — not hardcoded SQL.
 Persona difference must be enforced in the system prompt, not just described in UI."
```

```
"Implement the MCP client using the mcp Python package with AsyncExitStack
 to hold the stdio_client context manager open across method calls.
 Expose get_tools() and call_tool() methods for the agent loop."
```

```
"The agent loop must: build messages from sliding window history (last 10 turns),
 select haiku or sonnet based on query complexity signals, execute tool calls
 via MCP up to 6 iterations, return the final text response."
```

**Iteration notes:**
- Initial `stdio_client` connection used raw `__aenter__` — caused anyio cancel scope
  errors. Fixed by switching to `contextlib.AsyncExitStack`.
- Chainlit 2.x requires `payload={}` on `cl.Action` (not `value=`) — updated after
  Pydantic validation error on first run.
- MCP tool name is `read_query` (underscore), not `read-query` (hyphen) — discovered
  by calling `get_tools()` and inspecting the returned list.
- `MAX_TOOL_ITERATIONS` raised from 3 to 6 — model needs multiple calls to first
  find the latest date, then query actual data, then compute breakdowns.

---

### 2. MCP Server Setup

**Server:** `mcp-server-sqlite` (run via `uvx`)

**Connection pattern:**
```bash
uvx mcp-server-sqlite --db-path ./data/sales.db
```

The server is spawned as a subprocess by the app at chat session start — not
started manually. The `mcp` Python package handles the stdio transport.

**What worked:**
- `AsyncExitStack` pattern keeps the subprocess alive for the full session duration
- `read_query` tool is the only one used — enforces read-only access
- The server exposes the full SQLite schema to Claude automatically via `list_tables`
  and `describe_table` tools, so no schema injection is needed in the system prompt

**What didn't work initially:**
- Raw `__aenter__` / `__aexit__` on `stdio_client` caused `RuntimeError: Attempted
  to exit cancel scope in a different task` — anyio requires context managers to be
  entered and exited in the same task. `AsyncExitStack` resolves this.
- Tool name discovered at runtime: `read_query` not `read-query`

**MCP client code (core pattern):**
```python
async def connect(self):
    server_params = StdioServerParameters(
        command="uvx",
        args=["mcp-server-sqlite", "--db-path", DB_PATH],
    )
    read, write = await self._exit_stack.enter_async_context(
        stdio_client(server_params)
    )
    self._session = await self._exit_stack.enter_async_context(
        ClientSession(read, write)
    )
    await self._session.initialize()
```

---

### 3. Persona-Aware System Prompts

The persona difference is enforced entirely in the system prompt — not in the UI.
The same question produces materially different answers depending on which prompt
is active.

**Sales Rep system prompt (parameterized per rep):**
```
You are a sales assistant for {rep_name}, a Sales Rep covering the {region} region.

SCOPE RULES (enforce strictly):
- Only answer questions about {rep_name}'s data in the {region} region
- Only look at the last 3 months of available data
- If asked about other regions or other reps, respond: "I don't have that data."

ANSWER STYLE:
- Be direct, specific, and actionable — lead with the number, then context
- Use markdown tables for multi-row comparisons
- Always show revenue_usd, target_usd, and achieved_pct when discussing performance
```

**Country Head system prompt:**
```
You are a strategic sales analyst with full visibility across all regions and brands.

SCOPE RULES:
- Access all regions: North, South, East, West
- Access all brands: NovaBev, PureLeaf, FrostDrink, ZenWater, BoostFuel
- Default time horizon: all available years (2022–2024) — show year-over-year trends

ANSWER STYLE:
- Strategic, comparative, and narrative
- Identify patterns, risks, and growth opportunities proactively
- Use tables for regional/brand comparisons, narrative for trend summaries
```

**Same question, two answers (verified in testing):**

*Question: "How is NovaBev performing?"*

- **Sales Rep (Alice Sharma / North):** Revenue and target attainment for NovaBev
  in North region, last 3 months (Oct–Dec 2024), broken down by month with
  achieved_pct per period. Total: $123,796 at 93.7% of target.

- **Country Head:** NovaBev's national revenue trend 2022–2024 ($4.79M → $5.15M
  → $4.76M), regional breakdown showing East improving to 98.6%, South persistently
  weakest at 94.2%, strategic risk flag on 2024 volume decline.

---

### 4. Context Window Strategy

**What is passed per query:**

| Component | Tokens (approx) |
|---|---|
| System prompt | ~200 |
| Conversation history (last 10 turns) | ~1,500 |
| MCP tool result (SQL rows) | ~300 |
| User message | ~30 |
| **Total input per turn** | **~2,000** |
| Output | ~400 |

**How cost is controlled:**
- **Sliding window:** Only the last 10 conversation turns are passed. Older turns
  are dropped. This caps history at ~1,500 tokens regardless of conversation length.
- **SQL filters always applied:** Queries always include WHERE clauses (region, rep,
  date range) — no full table scans returned to the model.
- **No schema injection:** MCP's `describe_table` is available to Claude as a tool,
  so schema is only fetched when Claude needs it — not pre-loaded every turn.

---

### 5. Token Cost Estimate (50 Daily Active Users)

**Model routing logic:**
- Simple queries (single metric, recent period) → `claude-3-5-haiku` (~80% of queries)
- Complex queries (trends, multi-brand comparisons, risk analysis) → `claude-sonnet-4`
  (~20% of queries)

**Pricing (Anthropic public rates):**

| Model | Input ($/MTok) | Output ($/MTok) |
|---|---|---|
| claude-3-5-haiku | $0.80 | $4.00 |
| claude-sonnet-4 | $3.00 | $15.00 |

**Cost per conversation (10 turns assumed):**

| Tier | Turns | Input cost | Output cost | Subtotal |
|---|---|---|---|---|
| Haiku (8 turns) | 8 × 2,000 tok = 16K | $0.013 | $0.010 | $0.023 |
| Sonnet (2 turns) | 2 × 2,500 tok = 5K | $0.015 | $0.015 | $0.030 |
| **Per conversation** | | | | **~$0.053** |

**At 50 DAU:**
- Per day: 50 × $0.053 = **$2.65/day**
- Per month: **~$79/month**

*Note: TR AI Platform (enterprise gateway) costs may differ from public API rates.
The estimate above uses public Anthropic pricing as the baseline.*

---

### 6. Security Decisions

| Decision | Implementation |
|---|---|
| API credentials | `.env` file, loaded via `python-dotenv`, excluded from git via `.gitignore` |
| Data sent to Claude | SQL query results only (aggregated numbers). No raw CSV, no PII blobs. |
| PII assessment | Rep names in the dataset are fictional. No real personal data present. |
| Database access | Read-only via MCP `read_query` tool. `write_query` tool exposed by server but never called by the agent. |
| Network | Runs on localhost only. No external ports exposed. |
| SSL | `httpx.AsyncClient(verify=False)` used for corporate proxy compatibility on TR network. Set `verify=True` for standard deployments. |

---

### 7. What I Would Build Next (One More Day)

1. **Streaming responses** — pipe Claude's output token-by-token to Chainlit
   using `client.messages.stream()` so answers appear progressively instead of
   after full generation. Significantly improves perceived responsiveness.

2. **SQL result preview panel** — show the raw data table alongside the narrative
   answer so users can verify numbers themselves. Builds trust and reduces
   "hallucination anxiety."

3. **Query complexity classifier** — replace the keyword-based model router with
   a lightweight classifier (or a fast haiku pre-call) that more accurately
   identifies when sonnet is needed, reducing unnecessary sonnet usage and cost.

---

## Project Structure

```
sales-chatbot/
├── app.py                # Chainlit UI entry point
├── src/
│   ├── agent.py          # Claude API loop + TR AI Platform auth + model routing
│   ├── mcp_client.py     # MCP subprocess client (AsyncExitStack pattern)
│   └── personas.py       # System prompts + rep→region mapping
├── scripts/
│   └── load_data.py      # CSV → SQLite (run once)
├── data/
│   ├── sales_data.csv
│   └── sales.db          # generated, gitignored
├── .env                  # credentials (gitignored)
├── .env.example          # template
├── AUTH_NOTES.md         # how to switch between TR AI Platform and direct API key
└── requirements.txt
```

## Data Schema

Table: `sales`

| Column | Type | Description |
|---|---|---|
| year, month | Integer | Time dimension — 2022 to 2024 |
| region | Text | North / South / East / West |
| sales_rep | Text | 8 reps, 2 per region |
| brand | Text | NovaBev, PureLeaf, FrostDrink, ZenWater, BoostFuel |
| channel | Text | Retail / Wholesale / E-Commerce |
| units_sold | Integer | Volume sold |
| revenue_usd | Float | Actual revenue |
| target_usd | Float | Sales target |
| achieved_pct | Float | Revenue as % of target |

Total rows: 4,320 (3 years × 12 months × 4 regions × 2 reps × 5 brands × 3 channels)
