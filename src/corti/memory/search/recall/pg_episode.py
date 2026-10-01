"""PG recaller for episode — tsvector BM25 + pgvector HNSW cosine.

Uses ``psycopg_pool.AsyncConnectionPool`` for proper async connections
to PostgreSQL 18.4.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import ClassVar

from corti.infra.persistence.pg import episode_repo
from everalgo.types import Candidate

from .base import RecallerDeps, tsquery_text


def _inject_parent_id(c: Candidate) -> Candidate:
    """Wrap candidate with ``parent_id`` for MaxSim group-by-parent."""
    return Candidate(
        id=c.id,
        score=c.score,
        source=c.source,
        metadata={**c.metadata, "parent_id": c.metadata.get("entry_id", c.id)},
    )


class PgEpisodeRecaller:
    """BM25 + vector recall over the PG ``episode`` table."""

    kind: ClassVar[str] = "episode"
    everalgo_memory_type: ClassVar[str] = "episodic"
    text_field: ClassVar[str] = "episode"

    def __init__(self, deps: RecallerDeps) -> None:
        self._deps = deps

    async def sparse_recall(
        self, query: str, where: str, *, limit: int
    ) -> list[Candidate]:
        """BM25 recall via tsvector + plainto_tsquery (parameterized)."""
        q = tsquery_text(self._deps, query)
        pool = await episode_repo._pool()
        sql = (
            "SELECT *, ts_rank_cd(episode_tokens_tsv, "
            "  plainto_tsquery('simple', %s)) AS _score "
            "FROM episode "
            "WHERE episode_tokens_tsv @@ plainto_tsquery('simple', %s) "
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
        pool = await episode_repo._pool()
        sql = (
            "SELECT *, 1 - (vector <=> %s::vector) AS _score "
            "FROM episode "
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
        return [_inject_parent_id(c) for c in cands]

    async def dense_recall_as_child(
        self, vector: Sequence[float], where: str, *, limit: int
    ) -> list[Candidate]:
        cands = await self.dense_recall(vector, where, limit=limit)
        return [_inject_parent_id(c) for c in cands]

    async def dense_recall_subject(
        self, vector: Sequence[float], where: str, *, limit: int
    ) -> list[Candidate]:
        """ANN over the ``subject_vector`` column via pgvector cosine.

        Rows with ``subject_vector IS NULL`` are naturally excluded by
        the ``IS NOT NULL`` guard (pgvector rejects NULL vectors in ANN).
        """
        if not vector:
            return []
        vec_str = json.dumps(list(vector))
        pool = await episode_repo._pool()
        sql = (
            "SELECT *, 1 - (subject_vector <=> %s::vector) AS _score "
            "FROM episode "
            f"WHERE ({where}) AND subject_vector IS NOT NULL "
            "ORDER BY subject_vector <=> %s::vector LIMIT %s"
        )
        async with pool.connection() as conn:
            cur = await conn.execute(sql, (vec_str, vec_str, limit))
            rows = await cur.fetchall()
        return [_row_to_candidate(r, source="vector") for r in rows]

    async def dense_recall_subject_as_child(
        self, vector: Sequence[float], where: str, *, limit: int
    ) -> list[Candidate]:
        """Subject-vector ANN returning as MaxSim children."""
        cands = await self.dense_recall_subject(vector, where, limit=limit)
        return [_inject_parent_id(c) for c in cands]

    async def fetch_all_for_owner(self, where: str) -> list[Candidate]:
        """Flat scan - all episodes for this owner, keyed by entry_id.

        Cluster membership matching in ``acluster_retrieve`` compares
        ``Candidate.id`` against ``Cluster.members``. Both are now
        episode entry_ids regardless of parent_type.

        No ``limit`` - the full owner partition is required for cluster
        membership matching.
        """
        pool = await episode_repo._pool()
        sql = f"SELECT * FROM episode WHERE ({where})"
        async with pool.connection() as conn:
            cur = await conn.execute(sql)
            rows = await cur.fetchall()
        result: list[Candidate] = []
        for r in rows:
            d = dict(r) if not isinstance(r, dict) else r
            entry_id = d.get("entry_id")
            if not isinstance(entry_id, str) or not entry_id:
                continue
            # Strip noise columns like _row_to_candidate does.
            metadata = {
                k: v
                for k, v in d.items()
                if k not in ("vector", "subject_vector", "_score")
                and not k.endswith("_tsv")
            }
            result.append(
                Candidate(
                    id=entry_id,
                    score=0.0,
                    source="vector",
                    metadata={**metadata, "episode_id": d.get("id", "")},
                )
            )
        return result

    async def fetch_by_entry_ids(
        self, entry_ids: list[str], where: str
    ) -> list[Candidate]:
        """Fetch episodes by entry_id (for facts whose parent_id is an entry_id)."""
        if not entry_ids:
            return []
        pool = await episode_repo._pool()
        placeholders = ", ".join(["%s"] * len(entry_ids))
        sql = (
            "SELECT * FROM episode "
            f"WHERE ({where}) AND entry_id IN ({placeholders}) "
            "LIMIT %s"
        )
        async with pool.connection() as conn:
            cur = await conn.execute(sql, (*entry_ids, len(entry_ids)))
            rows = await cur.fetchall()
        return [_row_to_candidate(r, source="vector") for r in rows]


def _row_to_candidate(row, *, source: str) -> Candidate:
    d = dict(row) if not isinstance(row, dict) else row
    score = float(d.pop("_score", 0.0))
    for k in ("vector", "subject_vector"):
        d.pop(k, None)
    # Strip tsvector columns from metadata
    for k in list(d.keys()):
        if k.endswith("_tsv"):
            del d[k]
    return Candidate(
        id=str(d.get("id", "")),
        score=score,
        source=source,
        metadata=d,
    )
