"""Generic CRUD repository for PG-backed tables (psycopg3 async).

``PgRepoBase`` mirrors the public surface of ``DbRepoBase`` so cascade
handlers and recallers can swap the storage backend with minimal churn.

Uses ``psycopg_pool.AsyncConnectionPool`` for proper async connection
pooling — unlike PGLite's single-threaded WASM model, PG 18.4 handles
real concurrent connections natively.

Key differences from the original backend:
    - Parameterized queries (``%s`` placeholders) — no SQL injection risk;
      no need for ``_q()`` escape helper.
    - ``vector`` is serialised as pgvector ``'[0.1,0.2,...]'`` string
      format, cast to ``::vector`` where needed.
    - ``upsert`` uses SQL-standard ``ON CONFLICT ... DO UPDATE``.
    - ``search`` uses pgvector ``<=>`` cosine distance operator.
    - ``optimize`` maps to ``VACUUM ANALYZE``.
    - ``rebuild_indexes`` maps to ``REINDEX TABLE``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any, ClassVar, TypeVar

from psycopg_pool import AsyncConnectionPool

from corti.core.observability.logging import get_logger

from .base import PgBaseModel

logger = get_logger(__name__)

T = TypeVar("T", bound=PgBaseModel)

# Columns auto-managed by the DB — don't include in INSERT/UPDATE
_SERVER_COLUMNS: frozenset[str] = frozenset(
    {
        "created_at",
        "updated_at",
        # tsvector columns are GENERATED ALWAYS
    }
)

# Suffixes that identify generated columns
_GENERATED_SUFFIXES = ("_tsv",)


def _vector_to_str(vec: Sequence[float] | None) -> str | None:
    """Convert a Python list to pgvector string format ``[1.0,2.0,...]``."""
    if vec is None:
        return None
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


def _parse_vector_str(s: str) -> list[float]:
    """Convert a pgvector string ``'[1.0,2.0,...]'`` back to ``list[float]``."""
    if not s or s == "[]":
        return []
    # Strip brackets and split
    inner = s.strip("[]")
    if not inner:
        return []
    return [float(x.strip()) for x in inner.split(",")]


def _apply_vector_default(d: dict[str, Any], key: str, dim: int = 1024) -> None:
    """Ensure a ``vector`` column has a valid value; fill with zero-vector
    if missing."""
    if key in d and d[key] is not None:
        return
    d[key] = f"[{','.join(['0'] * dim)}]"


def _clean_row_dict(row: Any) -> dict[str, Any]:
    """Convert a psycopg row to a plain dict.

    Handles psycopg3's various row types: tuples, Row, dict-like, and
    asyncpg-style records.
    """
    if isinstance(row, dict):
        return row
    if hasattr(row, "_asdict"):
        return row._asdict()
    try:
        # psycopg3 Row is dict-like
        return dict(row)
    except (TypeError, ValueError):
        pass
    # tuple fallback (shouldn't happen with row_factory but be safe)
    if isinstance(row, (tuple, list)):
        return {"_raw": row}
    return row


class PgRepoBase:
    """Generic CRUD repository for one PG business table.

    Subclass and bind to a schema::

        class _EpisodeRepo(PgRepoBase):
            schema = Episode

            async def _pool_lookup(self):
                return await get_pool()

        episode_repo = _EpisodeRepo()

    Write paths use a per-table ``asyncio.Lock`` to serialise upsert/delete
    on the same table. Reads are concurrent.
    """

    schema: type[T]
    _table_locks: ClassVar[dict[str, asyncio.Lock]] = {}

    @property
    def table_name(self) -> str:
        return self.schema.TABLE_NAME

    async def _pool_lookup(self) -> AsyncConnectionPool:
        """Resolve the pool at runtime. Override in subclass."""
        raise NotImplementedError(
            f"{type(self).__name__}: override _pool_lookup() to wire the PG manager."
        )

    async def _pool(self) -> AsyncConnectionPool:
        return await self._pool_lookup()

    @classmethod
    def _write_lock(cls, table_name: str) -> asyncio.Lock:
        """Return or create the write lock for ``table_name``."""
        return cls._table_locks.setdefault(table_name, asyncio.Lock())

    def _writable_columns(self) -> list[str]:
        """Columns to include in INSERT (exclude generated + server-managed)."""
        fields = set(self.schema.model_fields.keys())
        result = []
        for f in sorted(fields):
            if f.endswith(_GENERATED_SUFFIXES):
                continue
            if f in _SERVER_COLUMNS:
                continue
            result.append(f)
        return result

    def _model_to_params(self, record: T) -> tuple[list[str], list[Any]]:
        """Convert a Pydantic model to (columns, values) for parameterized SQL.

        Vector fields and jsonb fields are serialised to their PG text format.
        Server-managed and generated columns are excluded.
        ``None`` on a vector field is written as SQL ``NULL``. That is the
        sentinel for "embedding not computed yet" (see ``ddl.py``) and is
        deliberately *not* coerced to a zero-vector, which would corrupt
        cosine distance instead of merely hiding the row.
        Empty vectors are replaced with a 1024-dim zero-vector — pgvector rejects
        ``[]`` with "must have at least 1 dimension".
        """
        d = record.model_dump(mode="json")
        cols = []
        vals = []
        for f in sorted(d.keys()):
            if f.endswith(_GENERATED_SUFFIXES):
                continue
            if f in _SERVER_COLUMNS:
                continue
            value = d[f]
            if f in ("vector", "subject_vector") and isinstance(value, (list, tuple)):
                if len(value) == 0:
                    value = _vector_to_str([0.0] * 1024)
                else:
                    value = _vector_to_str(value)
            # jsonb columns: PG needs JSON string, not Python object
            elif isinstance(value, (list, dict)) and (
                f in ("sender_ids", "source_case_ids", "content_labels")
                or f.endswith("_json")
            ):
                value = json.dumps(value)
            cols.append(f)
            vals.append(value)
        return cols, vals

    def _row_to_model(self, row: dict[str, Any]) -> T:
        """Convert a PG row dict to a Pydantic model instance.

        Filters out columns not in the schema (e.g. ``_score``, ``_rank``
        from search queries), strips generated suffixes, and converts
        pgvector/jsonb string representations back to Python types.
        """
        valid = set(self.schema.model_fields.keys())
        filtered: dict[str, Any] = {}
        for k, v in row.items():
            if k.endswith(_GENERATED_SUFFIXES):
                continue
            if k not in valid:
                continue
            # Convert pgvector string -> list[float]
            if k in ("vector", "subject_vector") and isinstance(v, str):
                try:
                    filtered[k] = _parse_vector_str(v)
                except (ValueError, SyntaxError):
                    filtered[k] = v
            # Convert jsonb string -> Python object (list/dict)
            elif k in ("sender_ids", "source_case_ids", "content_labels") or k.endswith(
                "_json"
            ):
                if isinstance(v, str):
                    # ``*_json`` fields are stored as ``text`` and Pydantic
                    # expects ``str``; pass through without parsing.
                    # ``sender_ids`` etc. are stored as jsonb and Pydantic
                    # expects ``list`` — parse from JSON string.
                    if k.endswith("_json"):
                        filtered[k] = v
                    else:
                        try:
                            filtered[k] = json.loads(v)
                        except json.JSONDecodeError:
                            filtered[k] = v
                elif isinstance(v, (list, dict)):
                    # psycopg3 returns jsonb columns as parsed Python objects.
                    # ``*_json`` (text column) never takes this path.
                    # ``sender_ids`` etc. (jsonb column) — pass through as list.
                    filtered[k] = v
                else:
                    filtered[k] = v
            else:
                filtered[k] = v
        return self.schema(**filtered)

    # ── Create ─────────────────────────────────────────────────────────

    async def add(self, records: Sequence[T]) -> None:
        """Insert one or more records (unconditional insert)."""
        if not records:
            return
        cols = self._writable_columns()
        col_list = ", ".join(cols)
        placeholders = ", ".join(["%s"] * len(cols))
        sql = f"INSERT INTO {self.table_name} ({col_list}) VALUES ({placeholders})"

        lock = self._write_lock(self.table_name)
        async with lock:
            pool = await self._pool()
            async with pool.connection() as conn:
                for record in records:
                    _, vals = self._model_to_params(record)
                    await conn.execute(sql, vals)

    # ── Upsert ─────────────────────────────────────────────────────────

    async def upsert(
        self,
        records: Sequence[T],
        *,
        by: str | Sequence[str] = "id",
    ) -> None:
        """Upsert records keyed by ``by`` (default ``"id"``).

        ``by`` may name a **composite** key. The daily-log tables use
        ``("app_id", "project_id", "id")``: an ``id`` is
        ``<owner_id>_<entry_id>`` and an entry_id is unique only inside one
        memory space, so keying on ``id`` alone lets a second app_id /
        project_id partition overwrite the first one's rows. Every named
        column is excluded from the ``DO UPDATE`` clause so the key itself
        can never be rewritten.
        """
        if not records:
            return
        keys = (by,) if isinstance(by, str) else tuple(by)
        cols = self._writable_columns()
        cols_no_pk = [c for c in cols if c not in keys]
        col_list = ", ".join(cols)
        placeholders = ", ".join(["%s"] * len(cols))
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols_no_pk)
        if set_clause:
            set_clause = f"{set_clause}, updated_at = now()"
        else:
            set_clause = "updated_at = now()"

        sql = (
            f"INSERT INTO {self.table_name} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {set_clause}"
        )

        lock = self._write_lock(self.table_name)
        async with lock:
            pool = await self._pool()
            async with pool.connection() as conn:
                for record in records:
                    _, vals = self._model_to_params(record)
                    await conn.execute(sql, vals)

    # ── Read ───────────────────────────────────────────────────────────

    async def count(self) -> int:
        """Total row count."""
        pool = await self._pool()
        async with pool.connection() as conn:
            cur = await conn.execute(f"SELECT count(*) AS cnt FROM {self.table_name}")
            row = await cur.fetchone()
            return row["cnt"] if row else 0

    async def get_by_id(self, id_value: str) -> T | None:
        """Fetch one row by scalar PK; ``None`` if missing."""
        pool = await self._pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {self.table_name} WHERE id = %s LIMIT 1",
                (id_value,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return self._row_to_model(_clean_row_dict(row))

    async def find_where(
        self,
        where: str,
        *,
        limit: int = 100,
    ) -> list[T]:
        """Scalar query returning typed schema instances.

        ``where`` is a parameterized WHERE clause (use ``%s`` for values).
        """
        pool = await self._pool()
        limit_clause = f"LIMIT {limit}" if limit > 0 else ""
        sql = f"SELECT * FROM {self.table_name} WHERE {where} {limit_clause}"
        async with pool.connection() as conn:
            cur = await conn.execute(sql)
            rows = await cur.fetchall()
        return [self._row_to_model(_clean_row_dict(r)) for r in rows]

    async def find_entry_state(self, md_path: str) -> dict[str, tuple[str, bool]]:
        """``entry_id -> (content_sha256, has_vector)`` for one ``md_path``.

        The cascade's md-vs-Postgres diff needs only the content hash, plus
        whether a vector exists yet so rows written while the embedder was
        down can be backfilled. Selecting whole rows instead would pull one
        1024-d vector per entry: a single daily log can hold 10K+ entries.

        Returns **every** row for ``md_path`` with no limit. A capped fetch
        silently hides the tail of large daily logs, and every hidden entry
        then looks "new" on each pass and is re-embedded forever.
        """
        pool = await self._pool()
        sql = (
            f"SELECT entry_id, content_sha256, (vector IS NOT NULL) AS has_vector "
            f"FROM {self.table_name} WHERE md_path = %s"
        )
        async with pool.connection() as conn:
            cur = await conn.execute(sql, (md_path,))
            rows = await cur.fetchall()
        return {
            row["entry_id"]: (row["content_sha256"], bool(row["has_vector"]))
            for row in rows
        }

    async def find_md_paths_missing_vectors(self) -> list[str]:
        """Distinct ``md_path`` values holding at least one vectorless row.

        Drives ``corti cascade backfill``. A row written while the embedder
        was down carries ``vector = NULL``; because its content hash is
        already current, neither the watcher nor the scanner will revisit the
        file — a frozen daily log never changes again, so its missing vectors
        would stay missing forever. Re-enqueueing these paths makes the
        daily-log handler re-run and embed the gaps.
        """
        pool = await self._pool()
        sql = f"SELECT DISTINCT md_path FROM {self.table_name} WHERE vector IS NULL"
        async with pool.connection() as conn:
            cur = await conn.execute(sql)
            rows = await cur.fetchall()
        return [row["md_path"] for row in rows]

    async def find_one_where(self, where: str) -> T | None:
        """Single-row variant of ``find_where`` (``None`` if no match)."""
        pool = await self._pool()
        sql = f"SELECT * FROM {self.table_name} WHERE {where} LIMIT 1"
        async with pool.connection() as conn:
            cur = await conn.execute(sql)
            row = await cur.fetchone()
            if row is None:
                return None
            return self._row_to_model(_clean_row_dict(row))

    async def find_where_paginated(
        self,
        where: str,
        *,
        sort_by: str = "created_at",
        descending: bool = True,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[T], int]:
        """Paginated scalar query with native ORDER BY + LIMIT/OFFSET."""
        pool = await self._pool()
        order_dir = "DESC" if descending else "ASC"
        offset = (page - 1) * page_size

        # Count
        count_sql = f"SELECT count(*) AS cnt FROM {self.table_name} WHERE {where}"
        data_sql = (
            f"SELECT * FROM {self.table_name} WHERE {where} "
            f"ORDER BY {sort_by} {order_dir} "
            f"LIMIT {page_size} OFFSET {offset}"
        )

        async with pool.connection() as conn:
            cur = await conn.execute(count_sql)
            total = (await cur.fetchone())["cnt"]
            cur = await conn.execute(data_sql)
            rows = await cur.fetchall()

        return [self._row_to_model(_clean_row_dict(r)) for r in rows], total

    async def find_by_owner(
        self,
        owner_id: str,
        *,
        limit: int = 100,
    ) -> list[T]:
        """Fetch rows by ``owner_id`` (parameterized)."""
        pool = await self._pool()
        limit_clause = f"LIMIT {limit}" if limit > 0 else ""
        sql = f"SELECT * FROM {self.table_name} WHERE owner_id = %s {limit_clause}"
        async with pool.connection() as conn:
            cur = await conn.execute(sql, (owner_id,))
            rows = await cur.fetchall()
        return [self._row_to_model(_clean_row_dict(r)) for r in rows]

    async def find_by_md_path(self, md_path: str) -> T | None:
        """Reverse-lookup from md path (cascade maps md edit → row)."""
        pool = await self._pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {self.table_name} WHERE md_path = %s LIMIT 1",
                (md_path,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return self._row_to_model(_clean_row_dict(row))

    async def search(
        self,
        *,
        vector: Sequence[float] | None = None,
        where: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Hybrid search: optional vector ANN via ``<=>`` + scalar WHERE.

        Returns raw dicts (with ``_score`` added when ``vector`` is given)
        so callers can access pgvector-specific metadata.
        """
        pool = await self._pool()
        where_clause = where if where else "TRUE"

        if vector is not None:
            vec_str = _vector_to_str(vector)
            sql = (
                f"SELECT *, 1 - (vector <=> %s::vector) AS _score "
                f"FROM {self.table_name} "
                f"WHERE {where_clause} "
                f"ORDER BY vector <=> %s::vector "
                f"LIMIT {limit}"
            )
            async with pool.connection() as conn:
                cur = await conn.execute(sql, (vec_str, vec_str))
                rows = await cur.fetchall()
        else:
            sql = f"SELECT * FROM {self.table_name} WHERE {where_clause} LIMIT {limit}"
            async with pool.connection() as conn:
                cur = await conn.execute(sql)
                rows = await cur.fetchall()

        return [_clean_row_dict(r) for r in rows]

    # ── Update ─────────────────────────────────────────────────────────

    async def update(
        self,
        updates: dict[str, Any],
        *,
        where: str,
    ) -> None:
        """Partial column update for rows matching ``where``.

        Args:
            updates: Column-name to new-value mapping.
            where: SQL WHERE predicate (parameterized okay, but must be complete).
        """

        lock = self._write_lock(self.table_name)
        async with lock:
            pool = await self._pool()
            set_parts = []
            params = []
            for k, v in updates.items():
                if k in ("vector", "subject_vector") and isinstance(v, (list, tuple)):
                    set_parts.append(f"{k} = %s::vector")
                    params.append(_vector_to_str(v))
                else:
                    set_parts.append(f"{k} = %s")
                    params.append(v)
            set_parts.append("updated_at = now()")
            set_clause = ", ".join(set_parts)

            sql = f"UPDATE {self.table_name} SET {set_clause} WHERE {where}"
            async with pool.connection() as conn:
                await conn.execute(sql, tuple(params))

    # ── Delete ─────────────────────────────────────────────────────────

    async def delete(self, predicate: str) -> int:
        """Delete rows matching a SQL predicate; return number deleted."""
        lock = self._write_lock(self.table_name)
        async with lock:
            pool = await self._pool()
            sql = f"DELETE FROM {self.table_name} WHERE {predicate}"
            async with pool.connection() as conn:
                cur = await conn.execute(sql)
                return cur.rowcount or 0

    async def delete_by_md_path(self, md_path: str) -> int:
        """Delete every row whose ``md_path`` matches; return rows deleted."""
        lock = self._write_lock(self.table_name)
        async with lock:
            pool = await self._pool()
            async with pool.connection() as conn:
                cur = await conn.execute(
                    f"DELETE FROM {self.table_name} WHERE md_path = %s",
                    (md_path,),
                )
                return cur.rowcount or 0

    # ── Maintenance ────────────────────────────────────────────────────

    async def optimize(self, *, cleanup_older_than: object | None = None) -> None:
        """No-op. PG manages its own storage via autovacuum; no manual
        optimize needed."""
        pass

    async def rebuild_indexes(self) -> None:
        """Run ``REINDEX TABLE`` on this table."""
        pool = await self._pool()
        async with pool.connection() as conn:
            await conn.execute(f"REINDEX TABLE {self.table_name}")


class PgDailyLogRepoBase(PgRepoBase):
    """Extended repo for daily-log tables (episode, atomic_fact, foresight,
    agent_case).

    Adds ``find_by_owner_entry`` / ``find_by_owner_entries`` for the cascade
    handler's "find existing row for this md entry" lookup and the strategy
    layer's bulk-fetch-by-entry-id pattern.
    """

    async def find_by_owner_entry(
        self,
        owner_id: str,
        entry_id: str,
        *,
        app_id: str = "default",
        project_id: str = "default",
    ) -> T | None:
        """Single point-query by ``(app, project, owner_id, entry_id)``.

        ``entry_id`` is only unique within a (app, project, owner) scope -
        the same ``ac_<date>_<seq>`` recurs in another space - so the
        scope segments are part of the predicate to avoid a cross-space hit.
        """
        pool = await self._pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {self.table_name} "
                f"WHERE owner_id = %s AND entry_id = %s "
                f"AND app_id = %s AND project_id = %s LIMIT 1",
                (owner_id, entry_id, app_id, project_id),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return self._row_to_model(_clean_row_dict(row))

    async def find_by_owner_entries(
        self,
        owner_id: str,
        entry_ids: Sequence[str],
        *,
        app_id: str = "default",
        project_id: str = "default",
    ) -> list[T]:
        """Bulk point-query by ``(app, project, owner_id, entry_id IN ...)``.

        Empty ``entry_ids`` short-circuits to ``[]`` rather than emit a
        ``WHERE entry_id IN ()`` predicate. The query's ``limit`` is bound
        to ``len(entry_ids)`` because at most one row per id can exist
        under one (app, project, owner) scope.
        """
        if not entry_ids:
            return []
        pool = await self._pool()
        placeholders = ", ".join(["%s"] * len(entry_ids))
        async with pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {self.table_name} "
                f"WHERE owner_id = %s AND entry_id IN ({placeholders}) "
                f"AND app_id = %s AND project_id = %s LIMIT %s",
                (owner_id, *entry_ids, app_id, project_id, len(entry_ids)),
            )
            rows = await cur.fetchall()
        return [self._row_to_model(_clean_row_dict(r)) for r in rows]
