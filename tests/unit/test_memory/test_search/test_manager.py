"""Unit tests for ``SearchManager`` with in-memory stub recallers.

These tests exercise the orchestration without touching Postgres. Every
recaller is replaced by a hand-rolled stub that returns a small
candidate list; the manager's job is to:

* honour the ``owner_type`` hard partition,
* run KEYWORD as sparse-only and leave ``atomic_facts`` empty,
* run VECTOR via MaxSim (ANN atomic_facts -> max-pool -> resolve episodes)
  and refuse when no embedding is wired,
* let HYBRID run without an LLM by default; require LLM only when the
  caller sets ``enable_llm_rerank=True``,
* delegate AGENTIC to ``search_episodes_agentic`` and return its result.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

import pytest

import corti.memory.search.manager as manager_mod
from corti.component.embedding import EmbeddingServiceError, EmbedGuard
from corti.core.errors import RerankServiceError
from corti.memory.search.callbacks import build_rerank_fn
from corti.memory.search.dto import SearchMethod, SearchRequest
from corti.memory.search.manager import SearchManager
from everalgo.types import Candidate, FactCandidate

# ── Stubs ───────────────────────────────────────────────────────────────


def _ts() -> _dt.datetime:
    return _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)


def _episode_row(
    eid: str, score: float = 0.8, memcell_id: str | None = None
) -> Candidate:
    return Candidate(
        id=eid,
        score=score,
        source="keyword",
        metadata={
            "owner_id": "alice",
            "owner_type": "user",
            "session_id": "sess_a",
            "timestamp": _ts(),
            "sender_ids": ["alice"],
            "subject": f"subj {eid}",
            "summary": f"summary {eid}",
            "episode": f"body {eid}",
            "entry_id": eid,
            "parent_id": memcell_id if memcell_id is not None else f"mc_{eid}",
        },
    )


class _StubEpisodeRecaller:
    kind: ClassVar[str] = "episode"
    everalgo_memory_type: ClassVar[str] = "episodic"
    text_field: ClassVar[str] = "episode"

    def __init__(self, sparse: list[Candidate], dense: list[Candidate]) -> None:
        self._sparse = sparse
        self._dense = dense
        self.last_where: str | None = None

    async def sparse_recall(
        self, query: str, where: str, *, limit: int
    ) -> list[Candidate]:
        self.last_where = where
        return list(self._sparse[:limit])

    async def dense_recall(
        self, vector: Sequence[float], where: str, *, limit: int
    ) -> list[Candidate]:
        self.last_where = where
        return list(self._dense[:limit])

    async def fetch_by_parent_ids(
        self, parent_ids: Sequence[str], where: str
    ) -> list[Candidate]:
        by_parent = {str(c.metadata.get("parent_id", "")): c for c in self._dense}
        return [by_parent[p] for p in parent_ids if p in by_parent]

    async def fetch_by_entry_ids(
        self, entry_ids: Sequence[str], where: str
    ) -> list[Candidate]:
        by_entry = {str(c.metadata.get("entry_id", "")): c for c in self._dense}
        return [by_entry[e] for e in entry_ids if e in by_entry]


class _StubAtomicFactRecaller:
    kind: ClassVar[str] = "atomic_fact"
    everalgo_memory_type: ClassVar[str] = "episodic"
    text_field: ClassVar[str] = "fact"

    def __init__(
        self,
        facts_map: dict[str, list[FactCandidate]] | None = None,
        dense: list[Candidate] | None = None,
    ) -> None:
        self._facts_map = facts_map or {}
        self._dense = dense or []

    async def sparse_recall(self, *_: Any, **__: Any) -> list[Candidate]:
        return []

    async def dense_recall(self, *_: Any, **__: Any) -> list[Candidate]:
        return list(self._dense)

    async def facts_for_episodes(
        self,
        ep_to_parents: Mapping[str, Sequence[str]],
        where: str,
        *,
        per_episode: int,
        query_vector: Any = None,
    ) -> dict[str, list[FactCandidate]]:
        return {
            eid: self._facts_map.get(eid, [])[:per_episode] for eid in ep_to_parents
        }


class _StubProfileRecaller:
    async def fetch(self, owner_id: str) -> list:
        return []


class _StubEmbedding:
    def __init__(self, dim: int = 4) -> None:
        self.dim = dim

    async def embed(self, text: str) -> list[float]:
        return [0.0] * self.dim

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]


# ── Fixtures ────────────────────────────────────────────────────────────


def _build_manager(
    *,
    episode_sparse: list[Candidate] | None = None,
    episode_dense: list[Candidate] | None = None,
    facts_map: dict[str, list[FactCandidate]] | None = None,
    atomic_fact_dense: list[Candidate] | None = None,
    embedding: _StubEmbedding | None = None,
    reranker: Any = None,
    llm_client: Any = None,
) -> SearchManager:
    ep_recaller = _StubEpisodeRecaller(episode_sparse or [], episode_dense or [])
    return SearchManager(
        episode_recaller=ep_recaller,
        atomic_fact_recaller=_StubAtomicFactRecaller(facts_map, atomic_fact_dense),
        profile_recaller=_StubProfileRecaller(),
        embedding=embedding,
        reranker=reranker,
        llm_client=llm_client,
    )


def _user_req(
    method: SearchMethod = SearchMethod.KEYWORD, **kwargs: Any
) -> SearchRequest:
    return SearchRequest(user_id="alice", query="hi", method=method, **kwargs)


# ── KEYWORD: user owner ────────────────────────────────────────────────


async def test_user_keyword_returns_episodes_only() -> None:
    mgr = _build_manager(episode_sparse=[_episode_row("ep_1")])
    resp = await mgr.search(_user_req())
    assert len(resp.request_id) == 32 and all(
        c in "0123456789abcdef" for c in resp.request_id
    )
    assert len(resp.data.episodes) == 1
    assert resp.data.episodes[0].id == "ep_1"
    assert resp.data.episodes[0].user_id == "alice"
    assert resp.data.episodes[0].type == "Conversation"
    assert resp.data.profiles == []


async def test_user_keyword_leaves_atomic_facts_empty() -> None:
    """KEYWORD never back-fills facts — only HYBRID produces relevance-scored facts.

    Even if the facts repository would return rows for the matched
    episode, the keyword path must leave ``atomic_facts=[]``: there is
    no per-query score for those facts, so emitting them would muddy
    the contract (mirrors enterprise where event_log is a separate
    memory_type, not auto-attached to episodic results).
    """
    fact = FactCandidate(
        id="f1",
        parent_episode_id="ep_1",
        score=0.0,
        metadata={"fact": "Alice prefers oat milk"},
    )
    mgr = _build_manager(
        episode_sparse=[_episode_row("ep_1")],
        facts_map={"ep_1": [fact]},
    )
    resp = await mgr.search(_user_req())
    ep = resp.data.episodes[0]
    assert ep.atomic_facts == []


async def test_user_keyword_no_results() -> None:
    resp = await _build_manager().search(_user_req())
    assert resp.data.episodes == []


async def test_user_keyword_filters_compile_pinned_owner() -> None:
    """``compile_filters`` should pin owner_id / owner_type on the where."""
    recaller = _StubEpisodeRecaller([_episode_row("ep_1")], [])
    mgr = SearchManager(
        episode_recaller=recaller,
        atomic_fact_recaller=_StubAtomicFactRecaller(),
        profile_recaller=_StubProfileRecaller(),
        embedding=None,
        reranker=None,
        llm_client=None,
    )
    await mgr.search(_user_req())
    assert recaller.last_where is not None
    assert "owner_id = 'alice'" in recaller.last_where
    assert "owner_type = 'user'" in recaller.last_where


def _atomic_fact_row(fid: str, *, parent_id: str, score: float) -> Candidate:
    """Atomic-fact candidate emitted by ``AtomicFactRecaller.dense_recall``."""
    return Candidate(
        id=fid,
        score=score,
        source="vector",
        metadata={
            "owner_id": "alice",
            "owner_type": "user",
            "session_id": "sess_a",
            "timestamp": _ts(),
            "sender_ids": ["alice"],
            "parent_id": parent_id,
            "fact": f"fact {fid}",
        },
    )


# ── VECTOR (MaxSim atomic) ────────────────────────────────────────────


async def test_vector_method_requires_embedding() -> None:
    mgr = _build_manager()  # embedding=None by default
    with pytest.raises(RuntimeError, match="embedding"):
        await mgr.search(_user_req(method=SearchMethod.VECTOR))


async def test_vector_method_returns_episodes_via_maxsim() -> None:
    mgr = _build_manager(
        episode_sparse=[_episode_row("should_not_appear")],
        episode_dense=[_episode_row("ep_dense")],
        atomic_fact_dense=[
            _atomic_fact_row("f1", parent_id="ep_dense", score=0.85),
        ],
        embedding=_StubEmbedding(),
    )
    resp = await mgr.search(_user_req(method=SearchMethod.VECTOR))
    assert [e.id for e in resp.data.episodes] == ["ep_dense"]


async def test_vector_radius_filter_drops_below_threshold() -> None:
    mgr = _build_manager(
        episode_dense=[
            _episode_row("ep_low"),
            _episode_row("ep_high"),
        ],
        atomic_fact_dense=[
            _atomic_fact_row("f_low", parent_id="ep_low", score=0.3),
            _atomic_fact_row("f_high", parent_id="ep_high", score=0.9),
        ],
        embedding=_StubEmbedding(),
    )
    resp = await mgr.search(_user_req(method=SearchMethod.VECTOR, radius=0.5))
    assert [e.id for e in resp.data.episodes] == ["ep_high"]


async def test_unlimited_mode_applies_default_radius_for_vector() -> None:
    """``top_k=-1`` without an explicit radius gets the project default 0.5.

    Mirrors enterprise's auto-floor behaviour — unlimited mode must not
    return arbitrarily low-similarity tail.
    """
    mgr = _build_manager(
        episode_dense=[
            _episode_row("ep_low"),
            _episode_row("ep_mid"),
            _episode_row("ep_high"),
        ],
        atomic_fact_dense=[
            _atomic_fact_row("f_low", parent_id="ep_low", score=0.3),  # below 0.5
            _atomic_fact_row("f_mid", parent_id="ep_mid", score=0.55),  # above 0.5
            _atomic_fact_row("f_high", parent_id="ep_high", score=0.9),
        ],
        embedding=_StubEmbedding(),
    )
    resp = await mgr.search(_user_req(method=SearchMethod.VECTOR, top_k=-1))
    # Ordered by max-pooled fact score descending.
    assert [e.id for e in resp.data.episodes] == ["ep_high", "ep_mid"]


async def test_unlimited_mode_explicit_radius_overrides_default() -> None:
    """Caller-supplied radius (even ``0.0``) wins over the unlimited default."""
    mgr = _build_manager(
        episode_dense=[
            _episode_row("ep_low"),
            _episode_row("ep_high"),
        ],
        atomic_fact_dense=[
            _atomic_fact_row("f_low", parent_id="ep_low", score=0.2),
            _atomic_fact_row("f_high", parent_id="ep_high", score=0.9),
        ],
        embedding=_StubEmbedding(),
    )
    resp = await mgr.search(_user_req(method=SearchMethod.VECTOR, top_k=-1, radius=0.1))
    # 0.1 threshold keeps both rows (the default 0.5 would have dropped ep_low).
    assert {e.id for e in resp.data.episodes} == {"ep_low", "ep_high"}


async def test_normal_mode_keeps_full_pool_when_no_radius() -> None:
    """``top_k > 0`` without a radius applies no threshold — truncation handles tail."""
    mgr = _build_manager(
        episode_dense=[
            _episode_row("ep_low"),
            _episode_row("ep_high"),
        ],
        atomic_fact_dense=[
            _atomic_fact_row("f_low", parent_id="ep_low", score=0.2),
            _atomic_fact_row("f_high", parent_id="ep_high", score=0.9),
        ],
        embedding=_StubEmbedding(),
    )
    resp = await mgr.search(_user_req(method=SearchMethod.VECTOR, top_k=10))
    # No radius default in normal mode -> both kept.
    assert {e.id for e in resp.data.episodes} == {"ep_low", "ep_high"}


async def test_vector_maxsim_max_pools_facts_to_episodes() -> None:
    """ANN atomic_facts -> max-pool by episode entry_id -> resolve to
    episode, ordering episodes by the per-episode maximum fact score."""
    mgr = _build_manager(
        episode_dense=[
            _episode_row("ep_A", memcell_id="mc_A"),
            _episode_row("ep_B", memcell_id="mc_B"),
        ],
        atomic_fact_dense=[
            _atomic_fact_row("f_A1", parent_id="ep_A", score=0.95),
            _atomic_fact_row("f_A2", parent_id="ep_A", score=0.40),
            _atomic_fact_row("f_B1", parent_id="ep_B", score=0.75),
        ],
        embedding=_StubEmbedding(),
    )
    resp = await mgr.search(_user_req(method=SearchMethod.VECTOR, top_k=5))
    eps = resp.data.episodes
    # Both episodes returned, ordered by max-pool score desc.
    assert [e.id for e in eps] == ["ep_A", "ep_B"]
    assert eps[0].score == pytest.approx(0.95)  # max(0.95, 0.40)
    assert eps[1].score == pytest.approx(0.75)


async def test_vector_returns_empty_when_no_facts() -> None:
    """No fact recall -> no episodes to score -> empty episode list."""
    mgr = _build_manager(
        episode_dense=[_episode_row("ep_A", memcell_id="mc_A")],
        atomic_fact_dense=[],
        embedding=_StubEmbedding(),
    )
    resp = await mgr.search(_user_req(method=SearchMethod.VECTOR, top_k=5))
    assert resp.data.episodes == []


# ── HYBRID / AGENTIC: prerequisite errors ──────────────────────────────


async def test_hybrid_without_embedding_degrades_to_keyword() -> None:
    """HYBRID is multi-leg: a missing embedder is not fatal.

    The keyword leg needs no provider, so an outage (or an unconfigured
    ``[embedding]``) must degrade to BM25 rather than reject the request —
    the failure mode this guards against is one dead provider taking down
    search entirely.
    """
    mgr = _build_manager()
    resp = await mgr.search(_user_req(method=SearchMethod.HYBRID))
    assert resp.data.episodes == []  # stub recallers → empty, but no raise


async def test_vector_without_embedding_still_fails_fast() -> None:
    """``method='vector'`` without a provider is a configuration error."""
    mgr = _build_manager()
    with pytest.raises(RuntimeError, match="embedding"):
        await mgr.search(_user_req(method=SearchMethod.VECTOR))


async def test_hybrid_does_not_require_llm_by_default() -> None:
    """HYBRID no longer auto-pulls LLM. With enable_llm_rerank=False the
    fusion-only path (RRF / LR) should run without an LLM client."""
    mgr = _build_manager(embedding=_StubEmbedding())
    # Should not raise: no LLM needed when caller opts out of Phase-5 rerank.
    resp = await mgr.search(_user_req(method=SearchMethod.HYBRID))
    assert resp.data.episodes == []  # empty stub recallers → empty result


async def test_hybrid_requires_llm_when_enable_llm_rerank_true() -> None:
    """Setting ``enable_llm_rerank=True`` makes the LLM mandatory."""
    mgr = _build_manager(embedding=_StubEmbedding())
    with pytest.raises(RuntimeError, match="enable_llm_rerank"):
        await mgr.search(_user_req(method=SearchMethod.HYBRID, enable_llm_rerank=True))


async def test_user_hybrid_episode_fuses_and_evicts_facts() -> None:
    """HYBRID episode path: heap-expand pipeline (RRF -> LR -> expansion).

    ep_1 has a fact scoring higher than its LR score -> fact evicts episode.
    ep_2 has no facts -> episode emitted as-is.
    """
    ep1 = _episode_row("ep_1", score=0.8, memcell_id="mc_1")
    ep2 = _episode_row("ep_2", score=0.7, memcell_id="mc_2")
    fact1 = FactCandidate(
        id="f1",
        parent_episode_id="ep_1",
        score=0.95,
        metadata={"fact": "Alice prefers oat milk"},
    )
    mgr = _build_manager(
        episode_sparse=[ep1, ep2],
        episode_dense=[ep1, ep2],
        facts_map={"ep_1": [fact1]},
        embedding=_StubEmbedding(),
    )
    resp = await mgr.search(_user_req(method=SearchMethod.HYBRID, top_k=10))
    eps = resp.data.episodes
    assert len(eps) >= 1
    ep1_result = next((e for e in eps if e.id == "ep_1"), None)
    assert ep1_result is not None
    assert len(ep1_result.atomic_facts) == 1
    assert ep1_result.atomic_facts[0].id == "f1"


class _StubReranker:
    """Minimal reranker stub — returns trivial scores."""

    async def rerank(self, query: str, documents: Sequence[str]) -> list[Any]:
        from corti.component.rerank.protocol import RerankResult

        return [RerankResult(index=i, score=1.0) for i in range(len(documents))]


class _StubLLM:
    """Minimal LLM stub — satisfies protocol without making real calls."""

    async def chat(self, *args: Any, **kwargs: Any) -> Any:
        return ""


async def test_agentic_requires_reranker_and_llm() -> None:
    mgr = _build_manager(embedding=_StubEmbedding())
    with pytest.raises(RuntimeError, match="rerank provider"):
        await mgr.search(_user_req(method=SearchMethod.AGENTIC))


async def test_agentic_episode_delegates_to_search_episodes_agentic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AGENTIC method delegates to search_episodes_agentic and returns its result."""
    import datetime as _dt

    from corti.memory.search.dto import SearchEpisodeItem

    fake_result = [
        SearchEpisodeItem(
            id="ep_1",
            score=0.9,
            session_id="s",
            user_id="alice",
            timestamp=_dt.datetime(2026, 1, 1, tzinfo=_dt.UTC),
            sender_ids=["alice"],
            subject="s",
            summary="s",
            episode="body",
            type="Conversation",
            atomic_facts=[],
        )
    ]

    async def _fake_agentic(*args: Any, **kwargs: Any) -> list[SearchEpisodeItem]:
        return fake_result

    monkeypatch.setattr(
        "corti.memory.search.manager.search_episodes_agentic", _fake_agentic
    )

    mgr = _build_manager(
        embedding=_StubEmbedding(),
        reranker=_StubReranker(),
        llm_client=_StubLLM(),
    )
    resp = await mgr.search(_user_req(method=SearchMethod.AGENTIC))
    assert resp.data.episodes == fake_result


# ── Top-k behaviour ───────────────────────────────────────────────────


async def test_top_k_truncates_results() -> None:
    rows = [_episode_row(f"ep_{i}", score=1.0 - i * 0.01) for i in range(10)]
    mgr = _build_manager(episode_sparse=rows)
    resp = await mgr.search(_user_req(top_k=3))
    assert [e.id for e in resp.data.episodes] == ["ep_0", "ep_1", "ep_2"]


async def test_top_k_minus_one_caps_at_100() -> None:
    rows = [_episode_row(f"ep_{i}") for i in range(120)]
    mgr = _build_manager(episode_sparse=rows)
    resp = await mgr.search(_user_req(top_k=-1))
    assert len(resp.data.episodes) == 100


# ── Degradation: a dead provider must not take down search ──────────────
#
# The keyword/BM25 leg needs no external component. Every other leg is an
# optimisation over it, so when one is unavailable the request degrades to
# the best available route instead of returning 503.


class _FailingEmbedding:
    """Embedder whose provider is in arrears — every call fails."""

    dim = 4

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        raise EmbeddingServiceError("Arrearage: account not in good standing")

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        raise EmbeddingServiceError("Arrearage: account not in good standing")


class _FailingReranker:
    """Cross-encoder whose provider rejects every request."""

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        instruction: str | None = None,
    ) -> list[Any]:
        raise RerankServiceError("rerank upstream error (HTTP 400)")


@pytest.fixture(autouse=True)
def _fresh_embed_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a closed cooldown gate.

    ``_search_embed_guard`` is process-wide; a test that trips it would
    otherwise short-circuit every later test's embedding call.
    """
    monkeypatch.setattr(manager_mod, "_search_embed_guard", EmbedGuard())
    monkeypatch.setattr(manager_mod, "_warned_missing_embedding", False)


async def test_hybrid_with_dead_embedder_returns_keyword_hits() -> None:
    """HYBRID with a failing embedder answers from BM25 alone."""
    mgr = _build_manager(
        episode_sparse=[_episode_row("ep_sparse")],
        embedding=_FailingEmbedding(),
    )
    resp = await mgr.search(_user_req(method=SearchMethod.HYBRID))
    assert [e.id for e in resp.data.episodes] == ["ep_sparse"]


async def test_vector_with_dead_embedder_falls_back_to_keyword() -> None:
    """``method='vector'`` degrades to the lexical route, not an empty page."""
    mgr = _build_manager(
        episode_sparse=[_episode_row("ep_sparse")],
        episode_dense=[_episode_row("ep_dense")],
        embedding=_FailingEmbedding(),
    )
    resp = await mgr.search(_user_req(method=SearchMethod.VECTOR))
    assert [e.id for e in resp.data.episodes] == ["ep_sparse"]


async def test_dead_embedder_opens_cooldown_after_one_attempt() -> None:
    """The second query inside the cooldown window never reaches the provider."""
    embedding = _FailingEmbedding()
    mgr = _build_manager(embedding=embedding)
    assert await mgr._embed_query("one") == []
    assert embedding.calls == 1
    assert await mgr._embed_query("two") == []
    assert embedding.calls == 1


async def test_agentic_with_dead_embedder_degrades_to_fused_path() -> None:
    """AGENTIC needs all three components; without a vector it falls through."""
    mgr = _build_manager(
        episode_sparse=[_episode_row("ep_sparse")],
        embedding=_FailingEmbedding(),
        reranker=_FailingReranker(),
        llm_client=object(),
    )
    resp = await mgr.search(_user_req(method=SearchMethod.AGENTIC))
    assert [e.id for e in resp.data.episodes] == ["ep_sparse"]


async def test_rerank_failure_keeps_first_stage_order() -> None:
    """A rerank outage leaves the first-stage order (and scores) intact."""
    candidates = [
        _episode_row("ep_first", score=0.9),
        _episode_row("ep_second", score=0.5),
    ]
    rerank_fn = build_rerank_fn(_FailingReranker(), text_field="episode")
    out = await rerank_fn("query", candidates)
    assert [c.id for c in out] == ["ep_first", "ep_second"]
    assert [c.score for c in out] == [0.9, 0.5]
