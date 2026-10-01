"""Unit tests for the runtime-interop service layer.

White-box surfaces monkeypatched at ``corti.service.runtime_context``:
    ``_search_memory``        (``POST /search`` facade)
    ``_get_memory``           (``POST /get`` facade)
    ``get_latest_session_summary`` (SQLite session-digest read)
    ``upsert_session_summary``     (SQLite session-digest write)

No Postgres, no SQLite, no network: every call into an infrastructure
primitive is served by a hand-rolled async double that records its calls.
"""

from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import corti.service.runtime_context as rc
from corti.memory.get import (
    GetData,
    GetEpisodeItem,
    GetMemoryType,
    GetRequest,
    GetResponse,
)
from corti.memory.runtime_context import (
    PrefetchRequest,
    SessionEndRequest,
    SessionStartRequest,
)
from corti.memory.search import (
    SearchData,
    SearchEpisodeItem,
    SearchRequest,
    SearchResponse,
)

_FIXED_TS = _dt.datetime(2026, 1, 2, 12, 0, tzinfo=_dt.UTC)

# ── Fixtures / doubles ───────────────────────────────────────────────────


def _search_episode(
    ep_id: str,
    *,
    score: float = 0.9,
    sender_ids: list[str] | None = None,
) -> SearchEpisodeItem:
    return SearchEpisodeItem(
        id=ep_id,
        user_id="alice",
        timestamp=_FIXED_TS,
        summary=f"summary {ep_id}",
        subject=f"subject {ep_id}",
        episode=f"body {ep_id}",
        type="Conversation",
        score=score,
        sender_ids=sender_ids if sender_ids is not None else [],
    )


def _search_response(
    episodes: list[SearchEpisodeItem],
    *,
    degraded: list[str] | None = None,
) -> SearchResponse:
    return SearchResponse(
        request_id="req_search",
        data=SearchData(
            episodes=episodes,
            degraded=degraded if degraded is not None else [],
        ),
    )


def _get_episode(ep_id: str) -> GetEpisodeItem:
    return GetEpisodeItem(
        id=ep_id,
        user_id="alice",
        session_id="s_1",
        timestamp=_FIXED_TS,
        summary=f"summary {ep_id}",
        subject=f"subject {ep_id}",
        episode=f"body {ep_id}",
        type="Conversation",
    )


class _FakeSearch:
    """Async stand-in for ``_search_memory`` returning one canned response."""

    def __init__(self, response: SearchResponse) -> None:
        self._response = response
        self.calls: list[SearchRequest] = []

    async def __call__(self, req: SearchRequest) -> SearchResponse:
        self.calls.append(req)
        return self._response


class _FakeGet:
    """Async stand-in for ``_get_memory`` serving episodes (and empty profiles)."""

    def __init__(self, episodes: list[GetEpisodeItem], total: int) -> None:
        self._episodes = episodes
        self._total = total
        self.calls: list[GetRequest] = []

    async def __call__(self, req: GetRequest) -> GetResponse:
        self.calls.append(req)
        if req.memory_type == GetMemoryType.PROFILE:
            return GetResponse(request_id="req_profile", data=GetData())
        return GetResponse(
            request_id="req_get",
            data=GetData(
                episodes=list(self._episodes),
                total_count=self._total,
                count=len(self._episodes),
            ),
        )


class _FakeSessionSummaries:
    """Replays one canned ``get_latest`` row and records ``upsert`` calls."""

    def __init__(self, row: Any = None) -> None:
        self._row = row
        self.upserts: list[Any] = []
        self.latest_calls: list[tuple[str, str, str]] = []

    async def get_latest(self, app_id: str, project_id: str, user_id: str) -> Any:
        self.latest_calls.append((app_id, project_id, user_id))
        return self._row

    async def upsert(self, row: Any) -> None:
        self.upserts.append(row)


def _patch_search(
    monkeypatch: pytest.MonkeyPatch, response: SearchResponse
) -> _FakeSearch:
    fake = _FakeSearch(response)
    monkeypatch.setattr(rc, "_search_memory", fake)
    return fake


def _patch_get(
    monkeypatch: pytest.MonkeyPatch, episodes: list[GetEpisodeItem], total: int
) -> _FakeGet:
    fake = _FakeGet(episodes, total)
    monkeypatch.setattr(rc, "_get_memory", fake)
    return fake


def _patch_repo(
    monkeypatch: pytest.MonkeyPatch, row: Any = None
) -> _FakeSessionSummaries:
    fake = _FakeSessionSummaries(row)
    monkeypatch.setattr(rc, "get_latest_session_summary", fake.get_latest)
    monkeypatch.setattr(rc, "upsert_session_summary", fake.upsert)
    return fake


def _summary_row(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "session_id": "s_prev",
        "user_id": "alice",
        "app_id": "corti",
        "project_id": "mem",
        "agent_id": None,
        "first_prompt": "fix the failover",
        "turn_count": 3,
        "started_at": _FIXED_TS,
        "ended_at": _FIXED_TS,
        "reason": "end",
        "recorded_at": _FIXED_TS,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# ── prefetch ─────────────────────────────────────────────────────────────


async def test_prefetch_trivial_prompt_never_searches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trivial prompt skips before the search double is ever called."""
    search = _patch_search(monkeypatch, _search_response([]))
    resp = await rc.prefetch(PrefetchRequest(user_id="alice", query="ok"))
    assert resp.skipped == "trivial_prompt"
    assert resp.block == ""
    assert search.calls == []


async def test_prefetch_no_relevant_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Everything below min_score leaves an empty, skipped block."""
    _patch_search(
        monkeypatch, _search_response([_search_episode("ep_low", score=0.05)])
    )
    resp = await rc.prefetch(
        PrefetchRequest(
            user_id="alice",
            query="explain the failover design",
            min_score=0.5,
        )
    )
    assert resp.skipped == "no_relevant_hits"
    assert resp.block == ""
    assert resp.hits == []


async def test_prefetch_hit_above_min_score_produces_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hit above the floor yields a block, a display line and the hit list."""
    _patch_search(
        monkeypatch, _search_response([_search_episode("ep_high", score=0.9)])
    )
    resp = await rc.prefetch(
        PrefetchRequest(
            user_id="alice",
            query="explain the failover design",
            min_score=0.5,
        )
    )
    assert resp.skipped is None
    assert resp.block
    assert resp.display
    assert [hit.id for hit in resp.hits] == ["ep_high"]


async def test_prefetch_drops_hit_below_min_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A weak hit is filtered out while the strong one survives."""
    _patch_search(
        monkeypatch,
        _search_response(
            [
                _search_episode("ep_high", score=0.9),
                _search_episode("ep_low", score=0.05),
            ]
        ),
    )
    resp = await rc.prefetch(
        PrefetchRequest(
            user_id="alice",
            query="explain the failover design",
            min_score=0.5,
        )
    )
    assert [hit.id for hit in resp.hits] == ["ep_high"]


async def test_prefetch_degradation_notice_and_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead embedding leg is announced in the block and preserved on the DTO."""
    _patch_search(
        monkeypatch,
        _search_response(
            [_search_episode("ep_high", score=0.9)], degraded=["embedding"]
        ),
    )
    resp = await rc.prefetch(
        PrefetchRequest(user_id="alice", query="explain the failover design")
    )
    assert resp.degraded == ["embedding"]
    assert "memory notice" in resp.block
    assert "embedding" in resp.block


# ── session/start ────────────────────────────────────────────────────────


async def test_session_start_draws_sample_from_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``recency_sample > 0`` samples the window and passes the total through."""
    window = [_get_episode(f"ep_{i}") for i in range(6)]
    _patch_get(monkeypatch, window, total=42)
    _patch_repo(monkeypatch)

    resp = await rc.session_start(
        SessionStartRequest(
            user_id="alice",
            app_id="corti",
            project_id="mem",
            recency_window=6,
            recency_sample=3,
            include_profile=False,
        )
    )

    assert len(resp.catalog) == 3
    window_ids = {ep.id for ep in window}
    assert {hit.id for hit in resp.catalog} <= window_ids
    assert resp.total_episodes == 42


async def test_session_start_zero_sample_uses_newest_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``recency_sample == 0`` keeps the newest ``recent_count`` in served order."""
    window = [_get_episode(f"ep_{i}") for i in range(6)]
    _patch_get(monkeypatch, window, total=6)
    _patch_repo(monkeypatch)

    resp = await rc.session_start(
        SessionStartRequest(
            user_id="alice",
            recency_window=6,
            recency_sample=0,
            recent_count=2,
            include_profile=False,
        )
    )

    assert [hit.id for hit in resp.catalog] == ["ep_0", "ep_1"]


async def test_session_start_populates_last_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored digest becomes ``last_session`` and a rendered section."""
    _patch_get(monkeypatch, [], total=0)
    _patch_repo(monkeypatch, row=_summary_row())

    resp = await rc.session_start(
        SessionStartRequest(
            user_id="alice",
            recency_window=5,
            recency_sample=0,
            recent_count=5,
            include_profile=False,
        )
    )

    assert resp.last_session is not None
    assert resp.last_session.session_id == "s_prev"
    assert "### Last session" in resp.block
    assert "3 turns" in resp.block


async def test_session_start_without_last_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stored digest means ``last_session is None`` and no section."""
    _patch_get(monkeypatch, [], total=0)
    _patch_repo(monkeypatch, row=None)

    resp = await rc.session_start(
        SessionStartRequest(
            user_id="alice",
            recency_window=5,
            recency_sample=0,
            recent_count=5,
            include_profile=False,
        )
    )

    assert resp.last_session is None
    assert "### Last session" not in resp.block


# ── session/end ──────────────────────────────────────────────────────────


async def test_session_end_upserts_once_with_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request is persisted exactly once with its scope fields intact."""
    repo = _patch_repo(monkeypatch)
    started = _dt.datetime(2026, 1, 2, 11, 0, tzinfo=_dt.UTC)
    ended = _dt.datetime(2026, 1, 2, 11, 30, tzinfo=_dt.UTC)

    resp = await rc.session_end(
        SessionEndRequest(
            user_id="alice",
            app_id="corti",
            project_id="mem",
            agent_id="dsh",
            session_id="s_end",
            first_prompt="ship the interop layer",
            turn_count=3,
            started_at=started,
            ended_at=ended,
            reason="end",
        )
    )

    assert len(repo.upserts) == 1
    row = repo.upserts[0]
    assert row.app_id == "corti"
    assert row.project_id == "mem"
    assert row.user_id == "alice"
    assert row.agent_id == "dsh"
    assert row.session_id == "s_end"
    assert row.first_prompt == "ship the interop layer"
    assert row.turn_count == 3
    assert resp.stored is True
    assert resp.summary.session_id == "s_end"
    assert resp.summary.turn_count == 3
    assert resp.display
    assert "3 turns" in resp.display


def test_session_end_requires_session_id() -> None:
    """A digest with no session key is rejected by the DTO."""
    with pytest.raises(ValidationError):
        SessionEndRequest(user_id="alice")
