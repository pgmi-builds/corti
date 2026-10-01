"""PG recaller for foresight — tsvector BM25 + pgvector HNSW cosine."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import ClassVar

from corti.infra.persistence.pg import foresight_repo
from everalgo.types import Candidate

from .base import RecallerDeps, tsquery_text


class PgForesightRecaller:
    """BM25 + vector recall over the PG ``foresight`` table.

    Uses dual-column BM25: ``foresight_tokens_tsv`` and
    ``evidence_tokens_tsv`` merged by max score.
    """

    kind: ClassVar[str] = "foresight"
    everalgo_memory_type: ClassVar[str] = "foresight"
    text_field: ClassVar[str] = "foresight"

    def __init__(self, deps: RecallerDeps) -> None:
        self._deps = deps

    async def sparse_recall(
        self, query: str, where: str, *, limit: int
    ) -> list[Candidate]:
        """BM25 recall via tsvector (dual-column: foresight + evidence)."""
        q = tsquery_text(self._deps, query)
        pool = await foresight_repo._pool()
        # Dual-column UNION ALL with max score merge
        sql = (
            "SELECT *, GREATEST("
            "  COALESCE(ts_rank_cd("
            "foresight_tokens_tsv, plainto_tsquery('simple', %s)), 0), "
            "  COALESCE(ts_rank_cd("
            "evidence_tokens_tsv, plainto_tsquery('simple', %s)), 0)"
            ") AS _score "
            "FROM foresight "
            "WHERE (foresight_tokens_tsv @@ plainto_tsquery('simple', %s)"
            "   OR evidence_tokens_tsv @@ plainto_tsquery('simple', %s)) "
            f"AND ({where}) "
            "ORDER BY _score DESC LIMIT %s"
        )
        async with pool.connection() as conn:
            cur = await conn.execute(sql, (q, q, q, q, limit))
            rows = await cur.fetchall()
        return [_row_to_candidate(r, source="keyword") for r in rows]

    async def dense_recall(
        self, vector: Sequence[float], where: str, *, limit: int
    ) -> list[Candidate]:
        """Vector recall via pgvector HNSW cosine (<=>)."""
        if not vector:
            return []
        vec_str = json.dumps(list(vector))
        pool = await foresight_repo._pool()
        sql = (
            "SELECT *, 1 - (vector <=> %s::vector) AS _score "
            "FROM foresight "
            f"WHERE vector IS NOT NULL AND ({where}) "
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


def _row_to_candidate(row, *, source: str) -> Candidate:
    d = dict(row) if not isinstance(row, dict) else row
    # A NULL here means the row has no vector yet (see the
    # ``IS NOT NULL`` guard in dense_recall). Treat it as a zero
    # score rather than letting float(None) fail the request.
    score = float(d.pop("_score", 0.0) or 0.0)
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
