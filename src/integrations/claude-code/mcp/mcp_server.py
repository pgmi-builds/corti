#!/usr/bin/env python3
"""Corti OSS MCP Server (stdio transport).

Thin MCP layer over the Corti OSS REST API.
Claude Code spawns this as a child process; it reads JSON-RPC from stdin,
calls Corti via HTTP, and writes JSON-RPC to stdout.

Tools (mirror Hermes corti plugin):
  mem_search   - Search memories (keyword/hybrid/vector/agentic)
  mem_recall   - Get recent memories (briefing)
  mem_add      - Store a fact immediately (add + flush)
  mem_flush    - Trigger extraction for a session

Scoping (shared with Hermes + Claude Code hooks):
  app_id      = shared-agent-memory
  project_id  = default
  user_id     = default
  agent_id    = pc-claude-code (configurable via env)

Env vars:
  CORTI_BASE_URL   - Corti API URL (default: http://127.0.0.1:5473)
  CORTI_USER_ID    - default
  CORTI_AGENT_ID   - pc-claude-code
  CORTI_APP_ID     - shared-agent-memory
  CORTI_PROJECT_ID - default
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ── Config ────────────────────────────────────────────────────────────────────

CORTI_URL = os.getenv("CORTI_BASE_URL", "http://127.0.0.1:5473")
APP_ID = os.getenv("CORTI_APP_ID", "shared-agent-memory")
PROJECT_ID = os.getenv("CORTI_PROJECT_ID", "default")
USER_ID = os.getenv("CORTI_USER_ID", "default")
AGENT_ID = os.getenv("CORTI_AGENT_ID", "pc-claude-code")
MCP_SESSION = "claude-code-mcp"

_client: httpx.AsyncClient | None = None
_server = Server("corti-oss")

# ── Corti REST helper ────────────────────────────────────────────────────────


def _scope() -> dict:
    return {"app_id": APP_ID, "project_id": PROJECT_ID, "user_id": USER_ID}


async def _post(path: str, body: dict) -> dict:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=CORTI_URL, timeout=120.0)
    resp = await _client.post(path, json={**body, **_scope()})
    resp.raise_for_status()
    envelope = resp.json()
    return envelope.get("data", envelope)


# ── Tool definitions ──────────────────────────────────────────────────────────

_TOOLS = [
    Tool(
        name="mem_search",
        description=(
            "Search the Corti memory store for relevant episodes, atomic "
            "facts, and the user profile. Call this before answering "
            "context-dependent questions about the user or prior work."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search query.",
                },
                "method": {
                    "type": "string",
                    "enum": ["keyword", "vector", "hybrid", "agentic"],
                    "default": "hybrid",
                },
                "top_k": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="mem_recall",
        description=(
            "Get a briefing of recent memories from Corti. Useful at the "
            "start of a task to recall what was done recently."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 50,
                },
            },
        },
    ),
    Tool(
        name="mem_add",
        description=(
            "Store an important fact or piece of information to Corti "
            "memory. Use when the user explicitly asks to remember "
            "something, or when you discover a durable fact worth "
            "preserving across sessions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The information to remember.",
                },
            },
            "required": ["content"],
        },
    ),
    Tool(
        name="mem_flush",
        description=(
            "Trigger memory extraction (episode/atomic_fact/foresight) "
            "for the current MCP session. Normally automatic, but can "
            "be called manually to force extraction."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
]


# ── Handlers ──────────────────────────────────────────────────────────────────


@_server.list_tools()
async def list_tools() -> list[Tool]:
    return _TOOLS


@_server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
    args = arguments or {}
    try:
        if name == "mem_search":
            query = args["query"]
            method = args.get("method", "hybrid")
            top_k = args.get("top_k", 5)
            data = await _post(
                "/api/v1/memory/search",
                {
                    "query": query,
                    "method": method,
                    "top_k": top_k,
                },
            )
            episodes = data.get("episodes", [])
            # The server renders the sentence; this layer only prints it. An
            # empty result and a keyword-only result must not read the same.
            note = data.get("degraded_note", "")
            if not episodes:
                body = "No relevant memories found."
                return [
                    TextContent(type="text", text=f"{body}\n{note}" if note else body)
                ]
            lines = []
            for i, ep in enumerate(episodes, 1):
                score = ep.get("score", 0)
                ts = ep.get("timestamp", "?")[:10]
                subject = ep.get("subject", "")
                text = ep.get("episode") or ep.get("summary", "")
                lines.append(f"[{i}] (score: {score:.2f}, {ts}) {subject}\n{text}")
            body = "\n\n---\n\n".join(lines)
            return [TextContent(type="text", text=f"{body}\n{note}" if note else body)]

        elif name == "mem_recall":
            count = args.get("count", 5)
            data = await _post(
                "/api/v1/memory/get",
                {
                    "memory_type": "episode",
                    "page": 1,
                    "page_size": 100,
                },
            )
            episodes = data.get("episodes", [])
            if not episodes:
                return [TextContent(type="text", text="No memories yet.")]
            episodes.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
            episodes = episodes[:count]
            lines = []
            for i, ep in enumerate(episodes, 1):
                ts = ep.get("timestamp", "?")[:10]
                subject = ep.get("subject", "")
                full = ep.get("episode") or ep.get("summary", "")
                text = full[:500] + ("..." if len(full) > 500 else "")
                lines.append(f"[{i}] ({ts}) {subject}\n{text}")
            return [TextContent(type="text", text="\n\n---\n\n".join(lines))]

        elif name == "mem_add":
            content = args["content"]
            await _post(
                "/api/v1/memory/add",
                {
                    "session_id": MCP_SESSION,
                    "messages": [
                        {
                            "sender_id": AGENT_ID,
                            "sender_name": "pc-claude-code",
                            "role": "assistant",
                            "timestamp": int(time.time() * 1000),
                            "content": content,
                        }
                    ],
                },
            )
            # /flush is also async now: it queues a forced boundary pass
            # behind the add. Both calls ack instantly; extraction runs
            # on the server's per-session worker.
            await _post(
                "/api/v1/memory/flush",
                {
                    "session_id": MCP_SESSION,
                },
            )
            return [
                TextContent(
                    type="text",
                    text="Fact stored (extraction queued in background).",
                )
            ]

        elif name == "mem_flush":
            await _post(
                "/api/v1/memory/flush",
                {
                    "session_id": MCP_SESSION,
                },
            )
            return [
                TextContent(
                    type="text",
                    text="Flush accepted (extraction runs in background).",
                )
            ]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


# ── Main ──────────────────────────────────────────────────────────────────────


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await _server.run(
            read_stream,
            write_stream,
            _server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
