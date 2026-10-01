"""A vectorless row must never be able to fail a read.

Two layers of defence, pinned here:

1. **The query.** ``1 - (vector <=> q)`` is NULL when ``vector`` is NULL, and
   every dense query therefore carries ``vector IS NOT NULL``. Without it, one
   unbackfilled row in a scope turned `hybrid` into a 500 the moment the
   embedder came back — which is exactly what happened on 2026-10-01, and it
   failed *silently* for as long as the embedder was down, because the whole
   vector leg was skipped.

2. **The row mapper.** ``float(None)`` must not propagate. A data condition
   is not allowed to become a request failure.

There is no scheduled backfill (see ``src/scripts/backfill_vectors.py``), so
a vectorless row can persist indefinitely and this guard is load-bearing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from corti.infra.persistence.pg import pg_repo
from corti.memory.search.recall import (
    pg_atomic_fact,
    pg_episode,
    pg_foresight,
    pg_knowledge_topic,
)
from corti.memory.search.recall.pg_atomic_fact import _row_to_candidate as af_row
from corti.memory.search.recall.pg_episode import _row_to_candidate as ep_row
from corti.memory.search.recall.pg_foresight import _row_to_candidate as fs_row
from corti.memory.search.recall.pg_knowledge_topic import _row_to_candidate as kt_row

_RECALL_MODULES = (pg_episode, pg_atomic_fact, pg_foresight, pg_knowledge_topic)
_REPO_SEARCH = Path(pg_repo.__file__)


# ── the row mapper ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mapper",
    [ep_row, af_row, fs_row, kt_row],
    ids=["episode", "atomic_fact", "foresight", "knowledge_topic"],
)
def test_null_score_becomes_zero_instead_of_raising(mapper) -> None:
    """A NULL ``_score`` is data, not an error condition."""
    candidate = mapper({"id": "x", "entry_id": "e", "_score": None}, source="vector")
    assert candidate.score == 0.0


@pytest.mark.parametrize(
    "mapper",
    [ep_row, af_row, fs_row, kt_row],
    ids=["episode", "atomic_fact", "foresight", "knowledge_topic"],
)
def test_missing_score_defaults_to_zero(mapper) -> None:
    candidate = mapper({"id": "x", "entry_id": "e"}, source="keyword")
    assert candidate.score == 0.0


# ── the query ──────────────────────────────────────────────────────────────


def _dense_statements(source: str) -> list[str]:
    """Every SQL fragment that scores with ``1 - (<col> <=> ...)``."""
    statements = []
    for match in re.finditer(r"1 - \((\w+) <=> %s::vector\)", source):
        # The WHERE clause sits within a few lines of the scoring SELECT.
        window = source[match.start() : match.start() + 700]
        statements.append(window)
    return statements


def _assert_null_guarded(source: str, label: str) -> None:
    statements = _dense_statements(source)
    assert statements, f"{label}: expected a dense-scoring SQL fragment"
    for window in statements:
        assert "IS NOT NULL" in window, (
            f"{label}: a dense query scores `1 - (col <=> ...)` without "
            "excluding NULL vectors; that yields a NULL score and fails the "
            "request. See this module's docstring."
        )


@pytest.mark.parametrize("module", _RECALL_MODULES, ids=lambda m: m.__name__)
def test_recall_dense_queries_exclude_null_vectors(module) -> None:
    _assert_null_guarded(Path(module.__file__).read_text(), module.__name__)


def test_repo_search_dense_query_excludes_null_vectors() -> None:
    _assert_null_guarded(_REPO_SEARCH.read_text(), "pg_repo.DbRepoBase.search")
