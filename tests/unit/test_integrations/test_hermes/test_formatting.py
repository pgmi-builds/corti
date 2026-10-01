"""Contract tests for ``integrations/hermes/_formatting.py``.

Pins the two helpers the plugin still builds itself:

- ``format_tool_result`` serialises via ``json.dumps``.
- ``format_memory_write_message`` builds a ``user``-role ``MessageItem``
  with the required fields.

Every memory block — the once-per-session block and the per-turn prefetch
block — is composed by the server's runtime-interop endpoints and injected
verbatim, so there is no local block composer left to test.
"""

from __future__ import annotations

import json

from hermes._formatting import format_memory_write_message, format_tool_result

# ── format_tool_result ──────────────────────────────────────────────────────


def test_format_tool_result_is_json_dumps() -> None:
    payload = {"count": 3, "items": ["a", "b", "c"], "nested": {"k": 1}}
    assert format_tool_result(payload) == json.dumps(payload, ensure_ascii=False)


def test_format_tool_result_preserves_unicode() -> None:
    assert format_tool_result({"k": "café"}) == '{"k": "café"}'


# ── format_memory_write_message ─────────────────────────────────────────────


def test_format_memory_write_message_fields() -> None:
    msg = format_memory_write_message("hello world", "u-1", 12345)
    assert msg["sender_id"] == "u-1"
    assert msg["role"] == "user"
    assert msg["timestamp"] == 12345
    assert msg["content"] == "hello world"
    # Required MessageItem keys are all present.
    assert {"sender_id", "role", "timestamp", "content"} <= set(msg)
