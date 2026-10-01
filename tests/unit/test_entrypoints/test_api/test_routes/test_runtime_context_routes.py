"""Route contract for the runtime-interop endpoints.

Two things are asserted here that the service tests cannot see:

- the **wire envelope**. ``docs/api.md`` states that every 200 wraps its
  payload in ``{"request_id", "data"}``. These endpoints returned flat
  objects at first, which made all three shipped adapters write the same
  ``envelope.data ?? envelope`` shim; this test pins the documented shape.
- the 200-vs-422 boundary at the route layer. A blank prompt is a *skip*,
  not a validation error — a host that submits an empty prompt must not get
  an error it then has to special-case.

Nothing is seeded and no service is reached: the service functions are
patched per test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from importlib import import_module

import pytest
from httpx import ASGITransport, AsyncClient

from corti.entrypoints.api.app import create_app

runtime_service = import_module("corti.service.runtime_context")
# The routes bind the service functions at import time, so patching the
# service module alone would not intercept the call.
routes_mod = import_module("corti.entrypoints.api.routes.runtime_context")


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """FastAPI app with no lifespan — no Postgres, no SQLite, no cascade."""
    app = create_app(lifespan_providers=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── envelope ───────────────────────────────────────────────────────────


async def test_prefetch_wraps_payload_in_envelope(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200 carries ``request_id`` at the top and the payload under ``data``."""

    async def fake_prefetch(req: object) -> object:
        return runtime_service.PrefetchData(
            skipped="no_relevant_hits", block="", display="", degraded=["embedding"]
        )

    monkeypatch.setattr(routes_mod, "run_prefetch", fake_prefetch)

    resp = await client.post(
        "/api/v1/memory/prefetch",
        json={"user_id": "u1", "query": "anything"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"request_id", "data"}
    assert isinstance(body["request_id"], str) and body["request_id"]
    assert "request_id" not in body["data"]
    assert body["data"]["skipped"] == "no_relevant_hits"
    assert body["data"]["degraded"] == ["embedding"]


async def test_session_end_wraps_payload_in_envelope(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``session/end`` uses the same envelope as every other 200."""

    async def fake_session_end(req: object) -> object:
        return runtime_service.SessionEndData(
            stored=True,
            summary=runtime_service.SessionSummaryItem(
                session_id="s1",
                user_id="u1",
                recorded_at="2026-10-02T00:00:00Z",  # type: ignore[arg-type]
            ),
            display="📝 Session (1 turns)",
        )

    monkeypatch.setattr(routes_mod, "run_session_end", fake_session_end)

    resp = await client.post(
        "/api/v1/memory/session/end",
        json={"user_id": "u1", "session_id": "s1"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"request_id", "data"}
    assert body["data"]["stored"] is True
    assert body["data"]["summary"]["session_id"] == "s1"


# ── 200 vs 422 ─────────────────────────────────────────────────────────


async def test_blank_prompt_is_a_skip_not_a_422(client: AsyncClient) -> None:
    """Entering an empty prompt must not surface as a validation error."""
    resp = await client.post(
        "/api/v1/memory/prefetch",
        json={"user_id": "u1", "query": "   "},
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["skipped"] == "trivial_prompt"


async def test_session_end_without_session_id_returns_422(
    client: AsyncClient,
) -> None:
    """``session_id`` is the digest's conflict key — it is required."""
    resp = await client.post(
        "/api/v1/memory/session/end",
        json={"user_id": "u1", "first_prompt": "x"},
    )

    assert resp.status_code == 422


async def test_prefetch_without_user_id_returns_422(client: AsyncClient) -> None:
    """Scope is mandatory on every interop request."""
    resp = await client.post(
        "/api/v1/memory/prefetch",
        json={"query": "anything"},
    )

    assert resp.status_code == 422
