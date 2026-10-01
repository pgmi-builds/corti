"""Hermes-agnostic formatting helpers for the Corti plugin.

Only the two shapes this plugin still has to build itself survive here: the
JSON payload a tool call returns, and the ``MessageItem`` used to mirror a
Hermes memory write into Corti. Every *memory* block — the once-per-session
block and the per-turn prefetch block — is composed by Corti's
runtime-interop endpoints and injected verbatim, so there is no block
composer in this module any more.

Stdlib + local ``_types`` only.
"""

from __future__ import annotations

import json

from ._types import MessageItem


def degradation_note(degraded: object) -> str:
    """One line telling the model that recall was partial.

    "(no results)" and "the semantic leg is down, so this was a lexical
    answer" read identically otherwise, and a model cannot act on a
    distinction it never sees. The server reports the substituted legs in
    ``data.degraded``; this renders them. Mirrors the DSH adapter's tool
    note so the two runtimes say the same thing.
    """
    if not isinstance(degraded, list):
        return ""
    legs = ", ".join(str(leg) for leg in degraded if leg)
    if not legs:
        return ""
    return (
        f"[recall degraded: {legs} unavailable — these results are partial, "
        "not the full ranking]"
    )


def format_tool_result(data: object) -> str:
    """Serialize a tool result payload as JSON (the inner serializer)."""
    return json.dumps(data, ensure_ascii=False)


def format_memory_write_message(
    content: str, user_id: str, timestamp_ms: int
) -> MessageItem:
    """Build a user-role ``MessageItem`` for mirroring into Corti."""
    item: MessageItem = {
        "sender_id": user_id,
        "role": "user",
        "timestamp": timestamp_ms,
        "content": content,
    }
    return item
