"""
mcp_client.py — Connects to mcp-server-sqlite via subprocess (stdio transport).
Exposes get_tools() and call_tool() for use by the agent loop.

Uses AsyncExitStack to hold the stdio_client and ClientSession context managers
open for the lifetime of the chat session (anyio requirement).
"""

import contextlib
import json
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DB_PATH = str(Path(__file__).parent.parent / "data" / "sales.db")


class MCPClient:
    """Async MCP client wrapping mcp-server-sqlite."""

    def __init__(self):
        self._session: ClientSession | None = None
        self._tools: list[dict] = []
        self._exit_stack = contextlib.AsyncExitStack()

    async def connect(self):
        """Spawn the MCP server subprocess and initialise the session."""
        server_params = StdioServerParameters(
            command="uvx",
            args=["mcp-server-sqlite", "--db-path", DB_PATH],
        )
        # AsyncExitStack keeps context managers alive across method calls
        read, write = await self._exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await self._session.initialize()

        # Cache tool definitions in Anthropic API format
        mcp_tools = await self._session.list_tools()
        self._tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.inputSchema,
            }
            for t in mcp_tools.tools
        ]

    async def disconnect(self):
        await self._exit_stack.aclose()

    def get_tools(self) -> list[dict]:
        """Return tool definitions in Anthropic API format."""
        return self._tools

    async def call_tool(self, name: str, tool_input: dict) -> str:
        """Execute a tool call and return the result as a string."""
        if not self._session:
            raise RuntimeError("MCPClient not connected. Call connect() first.")
        result = await self._session.call_tool(name, tool_input)
        # Extract text content from MCP result
        parts = []
        for content in result.content:
            if hasattr(content, "text"):
                parts.append(content.text)
            else:
                parts.append(json.dumps(content, default=str))
        return "\n".join(parts)
