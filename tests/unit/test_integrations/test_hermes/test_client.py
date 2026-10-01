"""Contract tests for ``integrations/hermes/_client.py``.

Pins the synchronous ``CortiApiClient`` surface:

- ``add_messages`` payload shape (roles / sender_ids / ms timestamps /
  content), default-scope omission of ``app_id``/``project_id``, chunking
  into ``_ADD_BATCH_SIZE`` batches with merged ``message_count``.
- ``flush_session`` payload.
- ``search`` / ``get`` body construction, ``include_profile`` / ``top_k`` /
  ``method`` / pagination forwarding, and the ``user_id``/``agent_id`` XOR.
- Error normalisation: server ``EXTERNAL_SERVICE_UNAVAILABLE`` /
  ``INVALID_INPUT`` codes, ``httpx.ConnectError`` → transient, non-JSON 4xx
  → ``INTERNAL_ERROR``, and ``CLIENT_CLOSED`` after ``close()``.

Network is faked via ``httpx.MockTransport`` injected through a patched
``_make_client`` (no ``respx`` / ``requests_mock``).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from hermes._client import CortiApiClient
from hermes._constants import _ADD_BATCH_SIZE
from hermes._types import CortiClientError


def _ok(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"request_id": "t", "data": data})


def _interop(payload: dict[str, Any]) -> httpx.Response:
    """A runtime-interop 200: payload under the standard ``data`` envelope."""
    return httpx.Response(200, json={"request_id": "req", "data": payload})


def _err(
    status: int,
    code: str,
    *,
    message: str = "boom",
) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "request_id": "t",
            "error": {
                "code": code,
                "message": message,
                "timestamp": "2026-01-01T00:00:00Z",
                "path": "/api/v1/memory/x",
            },
        },
    )


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    """Return a factory ``(handler, **kwargs) -> CortiApiClient``.

    The factory wires the client's ``httpx.AsyncClient`` to an
    ``httpx.MockTransport`` built from ``handler`` by patching
    ``_make_client``. Created clients are closed at test teardown.
    """
    import hermes._client as client_mod

    real_async_client = client_mod.httpx.AsyncClient
    created: list[CortiApiClient] = []

    def factory(
        handler,
        *,
        base_url: str = "http://test.local",
        timeout: float = 5.0,
    ) -> CortiApiClient:
        transport = httpx.MockTransport(handler)

        async def _make(self: CortiApiClient) -> None:
            self._client = real_async_client(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={"Content-Type": "application/json"},
                transport=transport,
            )

        monkeypatch.setattr(client_mod.CortiApiClient, "_make_client", _make)
        client = client_mod.CortiApiClient(base_url, timeout=timeout)
        created.append(client)
        return client

    request.addfinalizer(lambda: [c.close() for c in created])
    return factory


def _msg(i: int) -> dict[str, Any]:
    return {
        "sender_id": "u1" if i % 2 == 0 else "a1",
        "role": "user" if i % 2 == 0 else "assistant",
        "timestamp": 1000 + i,
        "content": f"message-{i}",
    }


# ── add_messages ────────────────────────────────────────────────────────────


def test_add_messages_payload_and_default_scope_omission(make_client) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _ok({"message_count": 2, "status": "accumulated"})

    client = make_client(handler)
    messages = [_msg(0), _msg(1)]
    resp = client.add_messages("sess-1", "default", "default", messages)

    assert resp == {"message_count": 2, "status": "accumulated"}
    assert len(seen) == 1
    body = seen[0]
    assert body["session_id"] == "sess-1"
    assert "app_id" not in body
    assert "project_id" not in body
    assert body["messages"] == messages
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert [m["sender_id"] for m in body["messages"]] == ["u1", "a1"]
    assert [m["timestamp"] for m in body["messages"]] == [1000, 1001]
    assert [m["content"] for m in body["messages"]] == [
        "message-0",
        "message-1",
    ]


def test_add_messages_includes_non_default_scope(make_client) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _ok({"message_count": 1, "status": "accumulated"})

    client = make_client(handler)
    client.add_messages("s", "myapp", "myproj", [_msg(0)])
    body = seen[0]
    assert body["app_id"] == "myapp"
    assert body["project_id"] == "myproj"


def test_add_messages_chunks_over_batch_size_and_merges_count(make_client) -> None:
    assert _ADD_BATCH_SIZE == 500
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        n = len(body["messages"])
        return _ok({"message_count": n, "status": "accumulated"})

    client = make_client(handler)
    messages = [_msg(i) for i in range(501)]
    resp = client.add_messages("s", "default", "default", messages)

    assert len(calls) == 2
    assert len(calls[0]["messages"]) == 500
    assert len(calls[1]["messages"]) == 1
    assert resp["message_count"] == 501
    assert resp["status"] == "accumulated"


# ── flush_session ───────────────────────────────────────────────────────────


def test_flush_session_payload(make_client) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _ok({"status": "extracted"})

    client = make_client(handler)
    resp = client.flush_session("sess-7", "default", "default")
    assert resp == {"status": "extracted"}
    assert seen[0] == {"session_id": "sess-7"}


# ── search ──────────────────────────────────────────────────────────────────


def test_search_user_body_and_forwarded_kwargs(make_client) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _ok(
            {
                "episodes": [],
                "profiles": [],
                "unprocessed_messages": [],
            }
        )

    client = make_client(handler)
    client.search(
        "u-1",
        None,
        "default",
        "default",
        "hello",
        include_profile=True,
        top_k=7,
        method="hybrid",
    )
    body = seen[0]
    assert body["query"] == "hello"
    assert body["user_id"] == "u-1"
    assert "agent_id" not in body
    assert body["include_profile"] is True
    assert body["top_k"] == 7
    assert body["method"] == "hybrid"
    assert "app_id" not in body
    assert "project_id" not in body


def test_search_agent_owner(make_client) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _ok(
            {
                "episodes": [],
                "profiles": [],
                "unprocessed_messages": [],
            }
        )

    client = make_client(handler)
    client.search(None, "a-1", "default", "default", "q")
    body = seen[0]
    assert body["agent_id"] == "a-1"
    assert "user_id" not in body


@pytest.mark.parametrize("user_id, agent_id", [(None, None), ("u", "a")])
def test_search_xor_enforced(
    make_client, user_id: str | None, agent_id: str | None
) -> None:
    client = make_client(lambda r: _ok({}))
    with pytest.raises(ValueError, match="exactly one"):
        client.search(user_id, agent_id, "default", "default", "q")


# ── get ─────────────────────────────────────────────────────────────────────


def test_get_body_and_pagination_forwarding(make_client) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _ok(
            {
                "episodes": [],
                "profiles": [],
                "total_count": 0,
                "count": 0,
            }
        )

    client = make_client(handler)
    client.get(
        "u-1",
        None,
        "default",
        "default",
        "episode",
        page=2,
        page_size=15,
        sort_by="timestamp",
        sort_order="desc",
    )
    body = seen[0]
    assert body["memory_type"] == "episode"
    assert body["user_id"] == "u-1"
    assert body["page"] == 2
    assert body["page_size"] == 15
    assert body["sort_by"] == "timestamp"
    assert body["sort_order"] == "desc"
    assert "agent_id" not in body


@pytest.mark.parametrize("user_id, agent_id", [(None, None), ("u", "a")])
def test_get_xor_enforced(
    make_client, user_id: str | None, agent_id: str | None
) -> None:
    client = make_client(lambda r: _ok({}))
    with pytest.raises(ValueError, match="exactly one"):
        client.get(user_id, agent_id, "default", "default", "episode")


# ── runtime interop (standard data envelope) ─────────────────────────────────────────


def test_prefetch_envelope_body_and_default_scope_omission(
    make_client,
) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _interop(
            {
                "skipped": None,
                "block": "## Corti Memory",
                "display": "",
                "hits": [],
                "degraded": [],
            }
        )

    client = make_client(handler)
    resp = client.prefetch("u-1", "default", "default", "hello", "sess-1")
    assert resp["block"] == "## Corti Memory"
    assert resp["skipped"] is None
    body = seen[0]
    assert body["query"] == "hello"
    assert body["user_id"] == "u-1"
    assert body["session_id"] == "sess-1"
    assert "app_id" not in body
    assert "project_id" not in body


def test_prefetch_reports_skipped(make_client) -> None:
    client = make_client(
        lambda r: _interop(
            {
                "skipped": "trivial_prompt",
                "block": "",
                "display": "",
                "hits": [],
                "degraded": [],
            }
        )
    )
    resp = client.prefetch("u-1", "default", "default", "ok")
    assert resp["skipped"] == "trivial_prompt"


def test_session_start_forwards_scope_and_policy_kwargs(make_client) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _interop(
            {
                "block": "b",
                "display": "",
                "catalog": [],
                "total_episodes": 0,
                "last_session": None,
                "profile_line": "",
                "degraded": [],
            }
        )

    client = make_client(handler)
    client.session_start(
        "u-1",
        "myapp",
        "myproj",
        "sess-1",
        agent_id="a-1",
        recent_count=3,
        include_profile=False,
    )
    body = seen[0]
    assert body["user_id"] == "u-1"
    assert body["agent_id"] == "a-1"
    assert body["session_id"] == "sess-1"
    assert body["app_id"] == "myapp"
    assert body["project_id"] == "myproj"
    assert body["recent_count"] == 3
    assert body["include_profile"] is False


def test_session_end_forwards_transcript_fields(make_client) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _interop(
            {
                "stored": True,
                "summary": {"session_id": "sess-9"},
                "display": "",
            }
        )

    client = make_client(handler)
    resp = client.session_end(
        "u-1",
        "default",
        "default",
        "sess-9",
        first_prompt="hi",
        turn_count=2,
    )
    assert resp["stored"] is True
    body = seen[0]
    assert body["session_id"] == "sess-9"
    assert body["first_prompt"] == "hi"
    assert body["turn_count"] == 2
    assert "agent_id" not in body


def test_non_object_json_maps_to_internal_error(make_client) -> None:
    client = make_client(lambda r: httpx.Response(200, json=[1, 2, 3]))
    with pytest.raises(CortiClientError) as exc:
        client.flush_session("s", "default", "default")
    assert exc.value.code == "INTERNAL_ERROR"


# ── error mapping ───────────────────────────────────────────────────────────


def test_error_503_external_service_unavailable(make_client) -> None:
    client = make_client(lambda r: _err(503, "EXTERNAL_SERVICE_UNAVAILABLE"))
    with pytest.raises(CortiClientError) as exc:
        client.search("u", None, "default", "default", "q")
    assert exc.value.code == "EXTERNAL_SERVICE_UNAVAILABLE"


def test_error_422_invalid_input(make_client) -> None:
    client = make_client(lambda r: _err(422, "INVALID_INPUT"))
    with pytest.raises(CortiClientError) as exc:
        client.flush_session("s", "default", "default")
    assert exc.value.code == "INVALID_INPUT"


def test_connection_refused_maps_to_unavailable(make_client) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = make_client(handler)
    with pytest.raises(CortiClientError) as exc:
        client.search("u", None, "default", "default", "q")
    assert exc.value.code == "EXTERNAL_SERVICE_UNAVAILABLE"


def test_non_json_4xx_maps_to_internal_error(make_client) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            content=b"<html>bad</html>",
            headers={"content-type": "text/html"},
        )

    client = make_client(handler)
    with pytest.raises(CortiClientError) as exc:
        client.search("u", None, "default", "default", "q")
    assert exc.value.code == "INTERNAL_ERROR"


# ── close() ─────────────────────────────────────────────────────────────────


def test_method_after_close_raises_client_closed(make_client) -> None:
    client = make_client(lambda r: _ok({}))
    client.close()
    with pytest.raises(CortiClientError) as exc:
        client.search("u", None, "default", "default", "q")
    assert exc.value.code == "CLIENT_CLOSED"
