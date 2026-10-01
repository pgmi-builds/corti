"""PG recaller for atomic_fact - tsvector BM25 + pgvector HNSW cosine."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from corti.infra.persistence.pg import atomic_fact_repo
from everalgo.types import Candidate, FactCandidate

from .base import RecallerDeps, tsquery_text

_NOISE_COLUMNS = frozenset(
    {"vector", "subject_vector", "_distance", "_score", "created_at", "updated_at"}
)


class PgAtomicFactRecaller:
    """BM25 + vector recall over the PG ``atomic_fact`` table."""

    kind: ClassVar[str] = "atomic_fact"
    everalgo_memory_type: ClassVar[str] = "atomic"
    text_field: ClassVar[str] = "fact"

    def __init__(self, deps: RecallerDeps) -> None:
        self._deps = deps

    async def sparse_recall(
        self, query: str, where: str, *, limit: int
    ) -> list[Candidate]:
        """BM25 recall via tsvector + plainto_tsquery."""
        q = tsquery_text(self._deps, query)
        pool = await atomic_fact_repo._pool()
        sql = (
            "SELECT *, ts_rank_cd(fact_tokens_tsv, "
            "  plainto_tsquery('simple', %s)) AS _score "
            "FROM atomic_fact "
            "WHERE fact_tokens_tsv @@ plainto_tsquery('simple', %s) "
            f"AND ({where}) "
            "ORDER BY _score DESC LIMIT %s"
        )
        async with pool.connection() as conn:
            cur = await conn.execute(sql, (q, q, limit))
            rows = await cur.fetchall()
        return [_row_to_candidate(r, source="keyword") for r in rows]

    async def dense_recall(
        self, vector: Sequence[float], where: str, *, limit: int
    ) -> list[Candidate]:
        """Vector recall via pgvector HNSW cosine (<=>)."""
        if not vector:
            return []
        vec_str = json.dumps(list(vector))
        pool = await atomic_fact_repo._pool()
        sql = (
            "SELECT *, 1 - (vector <=> %s::vector) AS _score "
            "FROM atomic_fact "
            f"WHERE ({where}) "
            "ORDER BY vector <=> %s::vector LIMIT %s"
        )
        async with pool.connection() as conn:
            cur = await conn.execute(sql, (vec_str, vec_str, limit))
            rows = await cur.fetchall()
        return [_row_to_candidate(r, source="vector") for r in rows]

    async def sparse_recall_as_child(
        self, query: str, where: str, *, limit: int
    ) -> list[Candidate]:
        cands = await self.sparse_recall(query, where, limit=limit)
        return [
            Candidate(
                id=c.id,
                score=c.score,
                source=c.source,
                metadata={**c.metadata, "parent_id": c.metadata.get("entry_id", c.id)},
            )
            for c in cands
        ]

    async def dense_recall_as_child(
        self, vector: Sequence[float], where: str, *, limit: int
    ) -> list[Candidate]:
        cands = await self.dense_recall(vector, where, limit=limit)
        return [
            Candidate(
                id=c.id,
                score=c.score,
                source=c.source,
                metadata={**c.metadata, "parent_id": c.metadata.get("entry_id", c.id)},
            )
            for c in cands
        ]

    async def facts_for_episodes(
        self,
        ep_to_parents: Mapping[str, Sequence[str]],
        where: str,
        *,
        per_episode: int,
        query_vector: Sequence[float] | None = None,
    ) -> dict[str, list[FactCandidate]]:
        """Pull facts for a set of episodes, bucketed by episode id.

        Mirrors the Postgres ``AtomicFactRecaller.facts_for_episodes`` dual
        parent_id strategy (post-1.5: ``parent_id = episode_entry_id``;
        pre-1.5: ``parent_id = memcell_id``). Facts are queried by
        ``parent_id IN (all_unique_parent_ids)`` and re-bucketed under
        every episode that claims each parent_id.

        When ``query_vector`` is provided, cosine ANN is layered on top of
        the ``parent_id IN (...)`` filter, so each fact lands with a real
        query-fact relevance score. Without it, every fact ships with
        ``score=0.0``.
        """
        if not ep_to_parents:
            return {}

        parent_to_eps = _build_parent_to_episode_map(ep_to_parents)
        if not parent_to_eps:
            return {}

        rows = await self._query_facts_for_parents(
            parent_to_eps, where, per_episode=per_episode, query_vector=query_vector
        )

        # Bucket rows by episode and cap each bucket.
        buckets: dict[str, list[FactCandidate]] = defaultdict(list)
        for r in rows:
            d = dict(r) if not isinstance(r, dict) else r
            fact_parent_id = d.get("parent_id")
            fid = d.get("id")
            if not isinstance(fact_parent_id, str) or not isinstance(fid, str):
                continue
            metadata = {
                k: v
                for k, v in d.items()
                if k not in _NOISE_COLUMNS and k != "id" and not k.endswith("_tsv")
            }
            score = float(d.get("_score", 0.0)) if query_vector else 0.0
            for ep_id in parent_to_eps.get(fact_parent_id, ()):
                buckets[ep_id].append(
                    FactCandidate(
                        id=fid,
                        parent_episode_id=ep_id,
                        score=score,
                        metadata=metadata,
                    )
                )
        # With query_vector the rows arrive sorted by cosine ascending
        # (closest first) so slicing keeps the most relevant facts.
        return {ep_id: bucket[:per_episode] for ep_id, bucket in buckets.items()}

    async def _query_facts_for_parents(
        self,
        parent_to_eps: dict[str, list[str]],
        where: str,
        *,
        per_episode: int,
        query_vector: Sequence[float] | None,
    ) -> list[dict[str, Any]]:
        """Construct and execute the PG query for parent_id IN (...)."""
        parent_ids = list(parent_to_eps.keys())
        # Use parameterized placeholders (no SQL injection risk).
        placeholders = ", ".join(["%s"] * len(parent_ids))
        clause = f"parent_id IN ({placeholders})"
        full_where = f"({where}) AND ({clause})"
        limit = per_episode * max(len(parent_to_eps), 1)

        pool = await atomic_fact_repo._pool()
        params: list[Any] = list(parent_ids)
        if query_vector:
            vec_str = json.dumps(list(query_vector))
            sql = (
                "SELECT *, 1 - (vector <=> %s::vector) AS _score "
                "FROM atomic_fact "
                f"WHERE {full_where} "
                "ORDER BY vector <=> %s::vector LIMIT %s"
            )
            params = [vec_str, *params, vec_str, limit]
        else:
            sql = f"SELECT * FROM atomic_fact WHERE {full_where} LIMIT %s"
            params = [*params, limit]

        async with pool.connection() as conn:
            cur = await conn.execute(sql, tuple(params))
            rows = await cur.fetchall()
        return [dict(r) if not isinstance(r, dict) else r for r in rows]


def _build_parent_to_episode_map(
    ep_to_parents: Mapping[str, Sequence[str]],
) -> dict[str, list[str]]:
    """Invert ep-to-parents map to a parent-to-episodes map."""
    parent_to_eps: dict[str, list[str]] = defaultdict(list)
    for ep_id, parent_ids in ep_to_parents.items():
        for pid in parent_ids:
            if pid:
                parent_to_eps[pid].append(ep_id)
    return parent_to_eps


def _row_to_candidate(row, *, source: str) -> Candidate:
    d = dict(row) if not isinstance(row, dict) else row
    score = float(d.pop("_score", 0.0))
    for k in ("vector", "subject_vector"):
        d.pop(k, None)
    for k in list(d.keys()):
        if k.endswith("_tsv"):
            del d[k]
    return Candidate(
        id=str(d.get("id", "")),
        score=score,
        source=source,
        metadata=d,
    )
