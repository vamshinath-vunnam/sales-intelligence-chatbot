"""
app.py — Chainlit UI for the Sales Intelligence Chatbot.

Flow:
  @cl.on_chat_start → role-selection widget → build persona → init MCP
  @cl.on_message    → agent loop → stream answer → update history
"""

import chainlit as cl
from src.personas import build_system_prompt, REP_REGION_MAP, ALL_REPS
from src.mcp_client import MCPClient
from src import agent

MAX_HISTORY_TURNS = 10  # sliding window — keeps context cost bounded


# ---------------------------------------------------------------------------
# Chat start: role selection
# ---------------------------------------------------------------------------

@cl.on_chat_start
async def on_chat_start():
    # Step 1: choose persona
    persona_action = await cl.AskActionMessage(
        content="👋 Welcome to **Sales Intelligence**.\n\nWho are you?",
        actions=[
            cl.Action(name="sales_rep", payload={"persona": "sales_rep"}, label="👤  Sales Rep"),
            cl.Action(name="country_head", payload={"persona": "country_head"}, label="🌍  Country Head"),
        ],
        timeout=120,
    ).send()

    if not persona_action:
        await cl.Message(content="Session timed out. Please refresh.").send()
        return

    persona = persona_action.get("payload", {}).get("persona") or persona_action.get("name")

    if persona == "sales_rep":
        # Step 2: choose rep name
        rep_actions = [
            cl.Action(name=rep.replace(" ", "_"), payload={"rep": rep}, label=rep)
            for rep in ALL_REPS
        ]
        rep_action = await cl.AskActionMessage(
            content="Select your name:",
            actions=rep_actions,
            timeout=120,
        ).send()

        if not rep_action:
            await cl.Message(content="Session timed out. Please refresh.").send()
            return

        rep_name = rep_action.get("payload", {}).get("rep") or rep_action.get("name", "").replace("_", " ")
        region = REP_REGION_MAP[rep_name]
        system_prompt = build_system_prompt("sales_rep", rep_name=rep_name, region=region)
        badge = f"👤 **{rep_name}** | {region} Region"
        welcome = (
            f"Hi {rep_name.split()[0]}! I can answer questions about your sales in the "
            f"**{region}** region for the last 3 months.\n\n"
            f"Try asking: *\"How is NovaBev performing in my region?\"*"
        )
    else:
        rep_name = ""
        region = ""
        system_prompt = build_system_prompt("country_head")
        badge = "🟢 **Country Head** | All Regions"
        welcome = (
            "Welcome! I have full visibility across all regions and brands (2022–2024).\n\n"
            "Try asking: *\"Which brand has the highest growth trajectory?\"*"
        )

    # Initialise MCP client for this session
    mcp_client = MCPClient()
    await mcp_client.connect()

    # Store session state
    cl.user_session.set("persona", persona)
    cl.user_session.set("rep_name", rep_name)
    cl.user_session.set("region", region)
    cl.user_session.set("system_prompt", system_prompt)
    cl.user_session.set("history", [])
    cl.user_session.set("mcp_client", mcp_client)

    await cl.Message(content=f"{badge}\n\n{welcome}").send()


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

@cl.on_message
async def on_message(message: cl.Message):
    system_prompt: str = cl.user_session.get("system_prompt")
    history: list[dict] = cl.user_session.get("history")
    mcp_client: MCPClient = cl.user_session.get("mcp_client")

    if not system_prompt or mcp_client is None:
        await cl.Message(content="Session not initialised. Please refresh.").send()
        return

    # Show a placeholder while the agent works
    thinking_msg = cl.Message(content="")
    await thinking_msg.send()

    answer, model_used = await agent.run(
        user_message=message.content,
        system_prompt=system_prompt,
        history=history,
        mcp_client=mcp_client,
    )

    # Update the placeholder with the real answer
    thinking_msg.content = answer
    await thinking_msg.update()

    # Update sliding window history
    history.append({"role": "user", "content": message.content})
    history.append({"role": "assistant", "content": answer})
    # Keep last MAX_HISTORY_TURNS turns (each turn = 2 entries)
    if len(history) > MAX_HISTORY_TURNS * 2:
        history = history[-(MAX_HISTORY_TURNS * 2):]
    cl.user_session.set("history", history)


# ---------------------------------------------------------------------------
# Cleanup on disconnect
# ---------------------------------------------------------------------------

@cl.on_chat_end
async def on_chat_end():
    mcp_client: MCPClient = cl.user_session.get("mcp_client")
    if mcp_client:
        await mcp_client.disconnect()
