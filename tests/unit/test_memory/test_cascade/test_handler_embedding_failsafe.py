"""The ingest embedding path is best-effort, including against a rude provider.

A memory that cannot be embedded is still a memory: the row is written with
``vector = NULL`` and stays readable and BM25-searchable. The only way this
path can lose a record is by raising, so nothing here is allowed to raise —
not an unreachable provider, and not a provider that answers with the wrong
number of vectors.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from corti.component.embedding import EmbedGuard
from corti.memory.cascade.handlers import _daily_log_base as base


@pytest.fixture(autouse=True)
def _fresh_embed_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is process-wide; a failure in one test must not leak."""
    monkeypatch.setattr(base, "_embed_guard", EmbedGuard())


class _StubHandler(base.BaseDailyLogHandler):
    """Exercises ``_embed_many`` without a DB, a pool, or a real entry."""

    kind = "episode"
    db_repo: Any = None
    content_change_keys: tuple[str, ...] = ()

    def _embed_texts(self, entry: Any) -> tuple[str, ...]:
        return (entry.text,)

    async def _build_row(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("_build_row is not exercised here")


def _handler(embedder: Any) -> _StubHandler:
    handler = object.__new__(_StubHandler)
    handler._deps = SimpleNamespace(embedder=embedder)  # type: ignore[assignment]
    return handler


class _Embedder:
    def __init__(self, reply: Any) -> None:
        self._reply = reply

    async def embed_batch(self, texts: list[str]) -> Any:
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


@pytest.mark.asyncio
async def test_short_batch_leaves_the_missing_slots_unvectorised() -> None:
    """A provider answering short must not fail the file."""
    handler = _handler(_Embedder([[1.0, 2.0]]))  # 1 vector for 3 entries
    entries = [SimpleNamespace(text=f"t{i}") for i in range(3)]

    grouped = await handler._embed_many(entries, "episode-2026-10-02.md")

    assert len(grouped) == 3, "one group per entry, no matter what"
    assert grouped[0] == [[1.0, 2.0]]
    assert grouped[1] == [None]
    assert grouped[2] == [None]


@pytest.mark.asyncio
async def test_long_batch_is_trimmed() -> None:
    """Extra vectors are dropped rather than misaligned onto entries."""
    handler = _handler(_Embedder([[1.0], [2.0], [3.0]]))
    entries = [SimpleNamespace(text="only one")]

    grouped = await handler._embed_many(entries, "episode-2026-10-02.md")

    assert grouped == [[[1.0]]]


@pytest.mark.asyncio
async def test_empty_batch_leaves_every_slot_unvectorised() -> None:
    handler = _handler(_Embedder([]))
    entries = [SimpleNamespace(text="a"), SimpleNamespace(text="b")]

    grouped = await handler._embed_many(entries, "episode-2026-10-02.md")

    assert grouped == [[None], [None]]


@pytest.mark.asyncio
async def test_provider_error_leaves_every_slot_unvectorised() -> None:
    """An outage is a degradation, not a failed write."""
    from corti.component.embedding import EmbeddingServiceError

    handler = _handler(_Embedder(EmbeddingServiceError("provider down")))
    entries = [SimpleNamespace(text="a")]

    grouped = await handler._embed_many(entries, "episode-2026-10-02.md")

    assert grouped == [[None]]


@pytest.mark.asyncio
async def test_entry_with_no_embed_texts_gets_no_slots() -> None:
    """An entry that embeds nothing is not an error and costs no call."""

    class _Textless(_StubHandler):
        def _embed_texts(self, entry: Any) -> tuple[str, ...]:
            return ()

    handler = object.__new__(_Textless)
    handler._deps = SimpleNamespace(embedder=_Embedder([]))  # type: ignore[assignment]

    assert await handler._embed_many([SimpleNamespace(text="")], "p.md") == [[]]
