"""DDL for the Corti business tables on PostgreSQL 18.4 + pgvector 0.8.1.

Each table mirrors the original schema 1:1 with these PG-native replacements:

- ``vector(1024)`` column with HNSW cosine index (pgvector 0.8.1)
- ``*_tsv`` generated columns (``tsvector`` over ``*_tokens``) with GIN index
- ``ON CONFLICT (id) DO UPDATE`` for idempotent cascade upserts
- ``created_at DEFAULT now()`` + ``updated_at DEFAULT now()`` timestamps

Tables (5):
    1. episode          — conversation episodes (daily-log)
    2. atomic_fact      — extracted atomic facts (daily-log)
    3. foresight        — forward-looking predictions (daily-log)
    4. knowledge_topic  — L2 knowledge graph nodes
    5. user_profile     — user profile (single-row per user)

Index strategy:
    - HNSW on ``vector`` column (cosine distance, m=16, ef_construction=200)
    - GIN on each ``*_tsv`` generated column (full-text search)
    - B-tree on ``owner_id`` (partition filter for all user/agent queries)
    - B-tree on ``md_path`` (reverse-lookup for cascade re-reconcile)
    - B-tree on deprecated_by WHERE NULL (partial index — fast active-only scans)
"""

from __future__ import annotations

import textwrap
from typing import Final

VECTOR_DIM: Final[int] = 1024

# Zero-vector literal rendered into ``DEFAULT`` clauses as a pgvector
# array (e.g. ``'[0,0,...]'::vector``).
_ZERO_VECTOR_SQL: Final[str] = f"[{','.join(['0'] * VECTOR_DIM)}]"

# pgvector operator class for cosine distance.
# pgvector 0.8.1 provides: vector_cosine_ops (<=> operator), vector_l2_ops,
# vector_ip_ops, vector_halfvec_ops, etc.
HNSW_COSINE_OPS: Final[str] = "vector_cosine_ops"

# HNSW tuning — balanced for 20K–200K rows per table
HNSW_M: Final[int] = 16  # max connections per layer
HNSW_EF_CONSTRUCTION: Final[int] = 200  # build-time search depth


# ── Common CTE used by tsvector generated columns ──────────────────────────


def _tsv_column(col_name: str) -> str:
    """Return SQL for a ``tsvector`` generated column over ``*_tokens``.

    ``*_tokens`` is always space-joined pre-tokenised text (jieba output).
    ``to_tsvector('simple', ...)`` treats each space-delimited token as a
    word — no stemming, no stop-word removal (both are owned by the app
    layer's jieba tokenizer). The GIN index on this column provides BM25
    via ``ts_rank_cd`` in recall queries.
    """
    return (
        f"{col_name}_tsv tsvector GENERATED ALWAYS AS "
        f"(to_tsvector('simple', COALESCE({col_name}, ''))) STORED"
    )


# ── DDL fragments ──────────────────────────────────────────────────────────

_DAILY_LOG_COMMON = """\
    owner_id        text NOT NULL,
    owner_type      text NOT NULL,
    app_id          text NOT NULL DEFAULT 'default',
    project_id      text NOT NULL DEFAULT 'default',
    session_id      text,
    timestamp       timestamptz NOT NULL,
    parent_type     text NOT NULL DEFAULT 'memcell',
    parent_id       text NOT NULL,
    sender_ids      jsonb NOT NULL DEFAULT '[]'::jsonb,
    md_path         text NOT NULL,
    content_sha256  text NOT NULL,
    deprecated_by   text,
    -- NULL means "embedding not computed yet": the embedder was
    -- unreachable when the row was written. The row is still written so the
    -- memory stays keyword/BM25 searchable, and the cascade backfills the
    -- vector once the embedder recovers. pgvector skips NULL rows in HNSW
    -- and in ``<=>`` comparisons, so an unbackfilled row is simply invisible
    -- to vector recall rather than poisoning the ordering.
    vector          vector({dim})"""

_DAILY_LOG_INDEXES = """\
    -- HNSW cosine index on vector
    CREATE INDEX IF NOT EXISTS idx_{table}_vector
        ON {table} USING hnsw (vector {hnsw_ops})
        WITH (m = {m}, ef_construction = {ef});

    -- GIN index on generated tsvector column(s)
    {gin_indexes}

    -- Partition filter (owner-scoped queries)
    CREATE INDEX IF NOT EXISTS idx_{table}_owner_id ON {table} (owner_id);

    -- Reverse-lookup from md_path (cascade re-reconcile)
    CREATE INDEX IF NOT EXISTS idx_{table}_md_path ON {table} (md_path);

    -- Active-only partial index (deprecated_by IS NULL)
    CREATE INDEX IF NOT EXISTS idx_{table}_active
        ON {table} (owner_id, timestamp DESC) WHERE deprecated_by IS NULL;"""


def _gin_index_lines(table: str, tsv_cols: list[str]) -> str:
    """Generate ``CREATE INDEX IF NOT EXISTS ... USING gin`` lines."""
    lines = []
    for tsv_col in tsv_cols:
        lines.append(
            f"    CREATE INDEX IF NOT EXISTS idx_{table}_{tsv_col}"
            f" ON {table} USING gin ({tsv_col});"
        )
    return "\n".join(lines) if lines else "    -- (no tsvector columns)"


# ── Per-table DDL ──────────────────────────────────────────────────────────

_EPISODE_DDL = textwrap.dedent(f"""\
    CREATE TABLE IF NOT EXISTS episode (
        id              text PRIMARY KEY,
        entry_id        text NOT NULL,
        {_DAILY_LOG_COMMON.format(dim=VECTOR_DIM)},
        subject         text,
        summary         text,
        episode         text NOT NULL,
        episode_tokens  text NOT NULL DEFAULT '',
        {_tsv_column("episode_tokens")},
        subject_vector  vector({VECTOR_DIM}),
        created_at      timestamptz NOT NULL DEFAULT now(),
        updated_at      timestamptz NOT NULL DEFAULT now()
    );""")

_ATOMIC_FACT_DDL = textwrap.dedent(f"""\
    CREATE TABLE IF NOT EXISTS atomic_fact (
        id              text PRIMARY KEY,
        entry_id        text NOT NULL,
        {_DAILY_LOG_COMMON.format(dim=VECTOR_DIM)},
        fact            text NOT NULL,
        fact_tokens     text NOT NULL DEFAULT '',
        {_tsv_column("fact_tokens")},
        created_at      timestamptz NOT NULL DEFAULT now(),
        updated_at      timestamptz NOT NULL DEFAULT now()
    );""")

_FORESIGHT_DDL = textwrap.dedent(f"""\
    CREATE TABLE IF NOT EXISTS foresight (
        id              text PRIMARY KEY,
        entry_id        text NOT NULL,
        {_DAILY_LOG_COMMON.format(dim=VECTOR_DIM)},
        start_time      timestamptz,
        end_time        timestamptz,
        duration_days   int,
        foresight       text NOT NULL,
        foresight_tokens text NOT NULL DEFAULT '',
        {_tsv_column("foresight_tokens")},
        evidence        text,
        evidence_tokens text,
        {_tsv_column("evidence_tokens")},
        created_at      timestamptz NOT NULL DEFAULT now(),
        updated_at      timestamptz NOT NULL DEFAULT now()
    );""")

_KNOWLEDGE_TOPIC_DDL = textwrap.dedent(f"""\
    CREATE TABLE IF NOT EXISTS knowledge_topic (
        id              text PRIMARY KEY,
        doc_id          text NOT NULL,
        category_id     text NOT NULL,
        app_id          text NOT NULL,
        project_id      text NOT NULL,
        topic_name      text NOT NULL,
        topic_path      text NOT NULL,
        depth           int NOT NULL,
        parent_node_id  text NOT NULL DEFAULT '',
        summary         text NOT NULL,
        summary_tokens  text NOT NULL DEFAULT '',
        {_tsv_column("summary_tokens")},
        content_tokens  text NOT NULL DEFAULT '',
        {_tsv_column("content_tokens")},
        content_labels  jsonb NOT NULL DEFAULT '[]'::jsonb,
        md_path         text NOT NULL,
        content_sha256  text NOT NULL,
        vector          vector({VECTOR_DIM}) NOT NULL,
        created_at      timestamptz NOT NULL DEFAULT now(),
        updated_at      timestamptz NOT NULL DEFAULT now()
    );""")

_USER_PROFILE_DDL = textwrap.dedent(f"""\
    CREATE TABLE IF NOT EXISTS user_profile (
        id                  text PRIMARY KEY,
        owner_id            text NOT NULL,
        owner_type          text NOT NULL,
        app_id              text NOT NULL DEFAULT 'default',
        project_id          text NOT NULL DEFAULT 'default',
        summary             text NOT NULL,
        explicit_info_json  text NOT NULL,
        implicit_traits_json text NOT NULL,
        profile_timestamp_ms bigint NOT NULL,
        md_path             text NOT NULL,
        content_sha256      text NOT NULL,
        vector              vector({VECTOR_DIM}) NOT NULL
            DEFAULT '{_ZERO_VECTOR_SQL}'::vector,
        created_at          timestamptz NOT NULL DEFAULT now(),
        updated_at          timestamptz NOT NULL DEFAULT now()
    );""")


# ── Index DDL (per-table) ─────────────────────────────────────────────────


def _idx_ddl(table: str, gin_cols: list[str]) -> str:
    return _DAILY_LOG_INDEXES.format(
        table=table,
        dim=VECTOR_DIM,
        hnsw_ops=HNSW_COSINE_OPS,
        m=HNSW_M,
        ef=HNSW_EF_CONSTRUCTION,
        gin_indexes=_gin_index_lines(table, gin_cols),
    )


_EPISODE_IDX = _idx_ddl("episode", ["episode_tokens_tsv"])
_ATOMIC_FACT_IDX = _idx_ddl("atomic_fact", ["fact_tokens_tsv"])
_FORESIGHT_IDX = _idx_ddl("foresight", ["foresight_tokens_tsv", "evidence_tokens_tsv"])
# knowledge_topic has no owner_id/deprecated_by/timestamp — its own index set
_KNOWLEDGE_TOPIC_IDX = textwrap.dedent(f"""\
    CREATE INDEX IF NOT EXISTS idx_knowledge_topic_vector
        ON knowledge_topic USING hnsw (vector {HNSW_COSINE_OPS})
        WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION});
    {_gin_index_lines("knowledge_topic", ["summary_tokens_tsv", "content_tokens_tsv"])}
    CREATE INDEX IF NOT EXISTS idx_knowledge_topic_md_path ON knowledge_topic (md_path);
    CREATE INDEX IF NOT EXISTS idx_knowledge_topic_doc_id ON knowledge_topic (doc_id);
    CREATE INDEX IF NOT EXISTS idx_knowledge_topic_category_id
        ON knowledge_topic (category_id);""")

# user_profile has no tsvector columns (profile is KV-by-owner
# today)
_USER_PROFILE_IDX = textwrap.dedent(f"""\
    CREATE INDEX IF NOT EXISTS idx_user_profile_vector
        ON user_profile USING hnsw (vector {HNSW_COSINE_OPS})
        WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION});
    CREATE INDEX IF NOT EXISTS idx_user_profile_owner_id ON user_profile (owner_id);
    CREATE INDEX IF NOT EXISTS idx_user_profile_md_path ON user_profile (md_path);""")


# ── Ordered DDL list (table + indexes per table) ───────────────────────────

DDL_STATEMENTS: Final[list[str]] = [
    # Tables
    _EPISODE_DDL,
    _ATOMIC_FACT_DDL,
    _FORESIGHT_DDL,
    _KNOWLEDGE_TOPIC_DDL,
    _USER_PROFILE_DDL,
    # Indexes
    _EPISODE_IDX,
    _ATOMIC_FACT_IDX,
    _FORESIGHT_IDX,
    _KNOWLEDGE_TOPIC_IDX,
    _USER_PROFILE_IDX,
    # Idempotent migrations — ``CREATE TABLE IF NOT EXISTS`` leaves
    # pre-existing tables untouched, so the embedding column's NOT NULL
    # constraint needs an explicit ALTER. ``DROP NOT NULL`` is a no-op on an
    # already-nullable column, so re-running on every startup is safe.
    "ALTER TABLE episode ALTER COLUMN vector DROP NOT NULL;",
    "ALTER TABLE atomic_fact ALTER COLUMN vector DROP NOT NULL;",
    "ALTER TABLE foresight ALTER COLUMN vector DROP NOT NULL;",
]


# ── Table metadata map (used by repos) ─────────────────────────────────────

TABLE_TSV_COLUMNS: Final[dict[str, list[str]]] = {
    "episode": ["episode_tokens_tsv"],
    "atomic_fact": ["fact_tokens_tsv"],
    "foresight": ["foresight_tokens_tsv", "evidence_tokens_tsv"],
    "knowledge_topic": ["summary_tokens_tsv", "content_tokens_tsv"],
    "user_profile": [],
}

TABLE_VECTOR_COLUMNS: Final[dict[str, list[str]]] = {
    "episode": ["vector", "subject_vector"],
    "atomic_fact": ["vector"],
    "foresight": ["vector"],
    "knowledge_topic": ["vector"],
    "user_profile": ["vector"],
}


async def run_ddl(pool) -> None:
    """Execute all DDL statements in order via the async connection pool.

    Each ``CREATE TABLE IF NOT EXISTS`` and ``CREATE INDEX IF NOT EXISTS``
    is idempotent — safe to call on every startup.
    """
    async with pool.connection() as conn:
        for stmt in DDL_STATEMENTS:
            await conn.execute(stmt)
