"""Tests for AtomicFact / Foresight daily-log writers.

The daily-log kinds (episode + atomic-fact + foresight) all share ``BaseDailyWriter``
plumbing — exhaustive chassis tests live in ``test_base.py`` and
``test_episode_writer.py`` indirectly via the e2e flows. Here we focus
on the per-kind path resolution + frontmatter shape that each
subclass owns: ``schema``, ``_frontmatter_updates``, and the
writer ↔ reader round-trip on a fresh tmp memory_root.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

import pytest

from corti.core.persistence import MarkdownReader, MemoryRoot
from corti.infra.persistence.markdown import (
    AtomicFactReader,
    AtomicFactWriter,
    ForesightReader,
    ForesightWriter,
)


@pytest.fixture
def memory_root(tmp_path: Path) -> MemoryRoot:
    mr = MemoryRoot(tmp_path)
    mr.ensure()
    return mr


# ── AtomicFact ────────────────────────────────────────────────────────────


async def test_atomic_fact_writer_round_trip(memory_root: MemoryRoot) -> None:
    writer = AtomicFactWriter(memory_root)
    today = _dt.date(2026, 5, 15)
    eid = await writer.append_entry(
        "u1",
        inline={
            "owner_id": "u1",
            "session_id": "s1",
            "timestamp": "2026-05-15T10:00:00+00:00",
            "parent_id": "mc_1",
            "sender_ids": ["u1"],
        },
        sections={"Fact": "Alice prefers Italian."},
        date=today,
    )
    path = (
        memory_root.users_dir() / "u1" / ".atomic_facts" / "atomic_fact-2026-05-15.md"
    )
    parsed = await MarkdownReader.read(path)

    # frontmatter
    fm = parsed.frontmatter
    assert fm["id"] == "atomic_fact_log_u1_2026-05-15"
    assert fm["type"] == "atomic_fact_daily"
    assert fm["file_type"] == "atomic_fact_daily"
    assert fm["user_id"] == "u1"
    assert fm["track"] == "user"
    assert fm["date"] == "2026-05-15"
    assert fm["entry_count"] == 1

    # entry body
    assert len(parsed.entries) == 1
    entry = parsed.entries[0]
    assert entry.id == eid.format()
    structured = entry.as_structured()
    assert structured.inline["owner_id"] == "u1"
    assert structured.inline["parent_id"] == "mc_1"
    assert structured.sections["Fact"] == "Alice prefers Italian."

    # reader is symmetric
    reader = AtomicFactReader(memory_root)
    assert reader.path_for("u1", today) == path
    found = await reader.find_structured("u1", eid)
    assert found is not None
    assert found.sections["Fact"] == "Alice prefers Italian."


async def test_atomic_fact_writer_appends_multiple(memory_root: MemoryRoot) -> None:
    writer = AtomicFactWriter(memory_root)
    today = _dt.date(2026, 5, 15)
    eid1 = await writer.append_entry(
        "u1",
        inline={
            "owner_id": "u1",
            "session_id": "s1",
            "timestamp": "2026-05-15T10:00:00+00:00",
            "parent_id": "mc_1",
        },
        sections={"Fact": "fact 1"},
        date=today,
    )
    eid2 = await writer.append_entry(
        "u1",
        inline={
            "owner_id": "u1",
            "session_id": "s1",
            "timestamp": "2026-05-15T11:00:00+00:00",
            "parent_id": "mc_2",
        },
        sections={"Fact": "fact 2"},
        date=today,
    )
    assert eid1.format() != eid2.format()
    assert eid2.format().endswith("0002")


# ── Foresight ─────────────────────────────────────────────────────────────


async def test_foresight_writer_round_trip(memory_root: MemoryRoot) -> None:
    writer = ForesightWriter(memory_root)
    today = _dt.date(2026, 5, 15)
    eid = await writer.append_entry(
        "u1",
        inline={
            "owner_id": "u1",
            "session_id": "s1",
            "timestamp": "2026-05-15T10:00:00+00:00",
            "parent_id": "mc_1",
            "start_time": "2026-05-15T12:00:00+00:00",
            "end_time": "2026-05-15T13:00:00+00:00",
            "duration_days": 1,
        },
        sections={
            "Foresight": "User will book lunch at noon.",
            "Evidence": "Past calendar pattern.",
        },
        date=today,
    )
    path = memory_root.users_dir() / "u1" / ".foresights" / "foresight-2026-05-15.md"
    parsed = await MarkdownReader.read(path)
    fm = parsed.frontmatter
    assert fm["id"] == "foresight_log_u1_2026-05-15"
    assert fm["type"] == "foresight_daily"

    structured = parsed.entries[0].as_structured()
    assert structured.sections["Foresight"] == "User will book lunch at noon."
    assert structured.sections["Evidence"] == "Past calendar pattern."
    assert structured.inline["duration_days"] == "1"
    assert structured.inline["start_time"].startswith("2026-05-15T12:00:00")

    reader = ForesightReader(memory_root)
    found = await reader.find_structured("u1", eid)
    assert found is not None
    assert found.sections["Evidence"] == "Past calendar pattern."


# ── round-trip with cascade handler (md → Postgres row mapping) ─────────────


async def test_atomic_fact_writer_output_feeds_handler(
    memory_root: MemoryRoot,
) -> None:
    """The writer's md is exactly what AtomicFactHandler expects to read."""
    from corti.component.embedding import EmbeddingProvider
    from corti.component.tokenizer import Tokenizer
    from corti.memory.cascade.handlers import AtomicFactHandler, HandlerDeps
    from corti.memory.cascade.handlers._daily_log_base import ParsedEntry

    class _T(Tokenizer):
        def tokenize(self, t):  # type: ignore[no-untyped-def]
            return [x for x in t.split() if x]

        def tokenize_batch(self, ts):  # type: ignore[no-untyped-def]
            return [self.tokenize(x) for x in ts]

    class _E(EmbeddingProvider):
        dim = 1024

        async def embed(self, t):  # type: ignore[no-untyped-def]
            return [0.0] * self.dim

        async def embed_batch(self, ts):  # type: ignore[no-untyped-def]
            return [await self.embed(x) for x in ts]

    today = _dt.date(2026, 5, 15)
    eid = await AtomicFactWriter(memory_root).append_entry(
        "u1",
        inline={
            "owner_id": "u1",
            "session_id": "s1",
            "timestamp": "2026-05-15T10:00:00+00:00",
            "parent_id": "mc_1",
            "sender_ids": ["u1"],
        },
        sections={"Fact": "Alice prefers Italian."},
        date=today,
    )
    path = (
        memory_root.users_dir() / "u1" / ".atomic_facts" / "atomic_fact-2026-05-15.md"
    )
    rel = path.relative_to(memory_root.root).as_posix()
    parsed = await MarkdownReader.read(path)
    entry = parsed.entries[0]
    handler = AtomicFactHandler(
        HandlerDeps(memory_root=memory_root, embedder=_E(), tokenizer=_T())
    )
    structured = entry.as_structured()
    pe = ParsedEntry(entry.id, structured, handler._content_sha256(structured))
    row = await handler._build_row(
        owner_id="u1", owner_type="user", md_path=rel, entry=pe
    )
    assert row.id == f"u1_{eid.format()}"
    assert row.fact == "Alice prefers Italian."
    assert row.parent_id == "mc_1"
    assert row.sender_ids == ["u1"]
    assert len(row.vector) == 1024


# ── Display-tz contract for frontmatter timestamps (Gap #5) ────────────


async def test_atomic_fact_frontmatter_last_appended_at_carries_display_tz_offset(
    memory_root: MemoryRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``last_appended_at`` in markdown frontmatter renders in the display tz.

    Markdown frontmatter is a display-side artefact (users read the file
    directly), so ``last_appended_at`` must use
    :func:`get_now_with_timezone` not :func:`get_utc_now`. Pins that
    contract end-to-end: configure ``CORTI_MEMORY__TIMEZONE=Asia/Shanghai``,
    write an entry, read the .md file, assert the literal string ends
    with ``+08:00``.

    Repeats the same check for ``ForesightWriter`` — both share
    ``BaseDailyWriter`` plumbing so a regression on one would likely
    affect the other, but pinning each rules out per-subclass shadowing
    of ``_frontmatter_updates``.
    """
    from corti.component.utils import datetime as _dt_module
    from corti.config import load_settings

    monkeypatch.setenv("CORTI_MEMORY__TIMEZONE", "Asia/Shanghai")
    load_settings.cache_clear()
    _dt_module._display_tz.cache_clear()

    today = _dt.date(2026, 5, 15)

    # AtomicFact
    af_writer = AtomicFactWriter(memory_root)
    await af_writer.append_entry(
        "u1",
        inline={
            "owner_id": "u1",
            "session_id": "s1",
            "timestamp": "2026-05-15T10:00:00+00:00",
            "parent_id": "mc_1",
            "sender_ids": ["u1"],
        },
        sections={"Fact": "x"},
        date=today,
    )
    af_path = (
        memory_root.users_dir() / "u1" / ".atomic_facts" / "atomic_fact-2026-05-15.md"
    )
    af_fm = (await MarkdownReader.read(af_path)).frontmatter
    assert af_fm["last_appended_at"].endswith("+08:00"), af_fm["last_appended_at"]

    # Foresight
    fs_writer = ForesightWriter(memory_root)
    await fs_writer.append_entry(
        "u1",
        inline={
            "owner_id": "u1",
            "session_id": "s1",
            "timestamp": "2026-05-15T10:00:00+00:00",
            "scope": "today",
            "horizon_days": 1,
        },
        sections={"Foresight": "x"},
        date=today,
    )
    fs_path = memory_root.users_dir() / "u1" / ".foresights" / "foresight-2026-05-15.md"
    fs_fm = (await MarkdownReader.read(fs_path)).frontmatter
    assert fs_fm["last_appended_at"].endswith("+08:00"), fs_fm["last_appended_at"]


# ── entry-id high-water mark ─────────────────────────────────────────────


async def test_entry_seq_is_not_reused_after_an_entry_is_deleted(
    memory_root: MemoryRoot,
) -> None:
    """A deletion must not let the next append re-mint a live entry's id.

    ``EntryId.next_for`` mints ``seq = entry_count + 1``. A cleanup pass that
    rewrites ``entry_count`` to the surviving block count therefore hands out
    a sequence number that the entry after the hole still owns, which shadows
    that entry in every ``entry_id`` join (cascade diff, ``cluster_member``,
    reflection). ``_current_count`` returns the high-water mark instead.
    """
    writer = AtomicFactWriter(memory_root)
    today = _dt.date(2026, 5, 15)
    path = (
        memory_root.users_dir() / "u1" / ".atomic_facts" / "atomic_fact-2026-05-15.md"
    )

    minted = [
        await writer.append_entry(
            "u1",
            inline={
                "owner_id": "u1",
                "session_id": "s1",
                "timestamp": f"2026-05-15T1{i}:00:00+00:00",
                "parent_id": f"mc_{i}",
            },
            sections={"Fact": f"fact {i}"},
            date=today,
        )
        for i in range(3)
    ]
    assert [e.seq for e in minted] == [1, 2, 3]

    # Operator cleanup: drop the middle entry, then lower entry_count to the
    # number of blocks that survived.
    text = path.read_text(encoding="utf-8")
    middle = minted[1].format()
    start = text.index(f"<!-- entry:{middle} -->")
    end = text.index(f"<!-- /entry:{middle} -->") + len(f"<!-- /entry:{middle} -->\n")
    trimmed = text[:start] + text[end:]
    text = re.sub(r"(?m)^entry_count:\s*\d+$", "entry_count: 2", trimmed)
    path.write_text(text, encoding="utf-8")

    fresh = await writer.append_entry(
        "u1",
        inline={
            "owner_id": "u1",
            "session_id": "s1",
            "timestamp": "2026-05-15T14:00:00+00:00",
            "parent_id": "mc_4",
        },
        sections={"Fact": "fact 4"},
        date=today,
    )

    parsed = await MarkdownReader.read(path)
    ids = [entry.id for entry in parsed.entries]
    assert fresh.seq == 4
    assert ids.count(fresh.format()) == 1
    assert middle not in ids
