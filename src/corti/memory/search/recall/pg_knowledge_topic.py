"""PG recaller for knowledge_topic — dual-column tsvector BM25 + pgvector HNSW cosine.

Uses ``summary_tokens_tsv`` (primary anchor) and ``content_tokens_tsv``
(secondary) merged by max score across columns — mirroring the Postgres
multi-BM25-column pattern.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import ClassVar

from corti.infra.persistence.pg import knowledge_topic_repo
from everalgo.types import Candidate

from .base import RecallerDeps, tsquery_text


class PgKnowledgeTopicRecaller:
    """BM25 (dual-column) + vector recall over the PG ``knowledge_topic`` table."""

    kind: ClassVar[str] = "knowledge_topic"
    everalgo_memory_type: ClassVar[str] = "knowledge"
    text_field: ClassVar[str] = "summary"

    def __init__(self, deps: RecallerDeps) -> None:
        self._deps = deps

    async def sparse_recall(
        self, query: str, where: str, *, limit: int
    ) -> list[Candidate]:
        """Dual-column BM25 recall via tsvector (summary + content)."""
        q = tsquery_text(self._deps, query)
        pool = await knowledge_topic_repo._pool()
        sql = (
            "SELECT *, GREATEST("
            "  COALESCE(ts_rank_cd("
            "summary_tokens_tsv, plainto_tsquery('simple', %s)), 0), "
            "  COALESCE(ts_rank_cd("
            "content_tokens_tsv, plainto_tsquery('simple', %s)), 0)"
            ") AS _score "
            "FROM knowledge_topic "
            "WHERE (summary_tokens_tsv @@ plainto_tsquery('simple', %s)"
            "   OR content_tokens_tsv @@ plainto_tsquery('simple', %s)) "
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
        pool = await knowledge_topic_repo._pool()
        sql = (
            "SELECT *, 1 - (vector <=> %s::vector) AS _score "
            "FROM knowledge_topic "
            f"WHERE vector IS NOT NULL AND ({where}) "
            "ORDER BY vector <=> %s::vector LIMIT %s"
        )
        async with pool.connection() as conn:
            cur = await conn.execute(sql, (vec_str, vec_str, limit))
            rows = await cur.fetchall()
        return [_row_to_candidate(r, source="vector") for r in rows]


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
