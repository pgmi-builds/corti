"""Tests for :mod:`corti.infra.persistence.sqlite.repos.session_summary`.

Verifies the scope idempotency of the upsert (same
``(app_id, project_id, user_id, session_id)`` updates in place rather than
duplicating), the latest-by-scope lookup ordering, scope isolation across
app ids, and the module-level functions the runtime-interop service layer
consumes.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlmodel import SQLModel

import corti.infra.persistence.sqlite.repos.session_summary as session_summary_module
from corti.config import SqliteSettings
from corti.core.persistence import (
    MemoryRoot,
    create_session_factory,
    create_system_engine,
)
from corti.infra.persistence.sqlite.repos.session_summary import (
    _SessionSummaryRepo,
    get_latest_session_summary,
    upsert_session_summary,
)
from corti.infra.persistence.sqlite.tables import SessionSummary


@pytest.fixture
async def sqlite_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh tmp SQLite factory; point the module-level wiring at it."""
    mr = MemoryRoot(tmp_path)
    mr.ensure()
    engine = create_system_engine(mr.system_db, SqliteSettings())
    factory = create_session_factory(engine)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    monkeypatch.setattr(session_summary_module, "get_session_factory", lambda: factory)
    yield factory
    await engine.dispose()


@pytest.fixture
async def repo(sqlite_factory) -> _SessionSummaryRepo:
    """Repo bound to the tmp factory — matches the sibling repo-test style."""
    return _SessionSummaryRepo(session_factory=sqlite_factory)


def _ts(day: int, hour: int = 12) -> dt.datetime:
    """A distinct, tz-aware UTC timestamp for deterministic ordering."""
    return dt.datetime(2026, 5, day, hour, 0, tzinfo=dt.UTC)


def _make_summary(
    *,
    session_id: str = "s_001",
    user_id: str = "u_alice",
    app_id: str = "default",
    project_id: str = "default",
    agent_id: str | None = None,
    first_prompt: str = "",
    turn_count: int = 0,
    started_at: dt.datetime | None = None,
    ended_at: dt.datetime | None = None,
    reason: str | None = None,
    recorded_at: dt.datetime | None = None,
) -> SessionSummary:
    return SessionSummary(
        app_id=app_id,
        project_id=project_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        first_prompt=first_prompt,
        turn_count=turn_count,
        started_at=started_at,
        ended_at=ended_at,
        reason=reason,
        recorded_at=recorded_at if recorded_at is not None else _ts(1),
    )


async def test_upsert_then_read_back(repo: _SessionSummaryRepo) -> None:
    """A written digest round-trips every business field."""
    await repo.upsert(
        _make_summary(
            session_id="s_001",
            user_id="u_alice",
            agent_id="agent_42",
            first_prompt="plan my tokyo trip",
            turn_count=3,
            started_at=_ts(1),
            ended_at=_ts(2),
            reason="end",
            recorded_at=_ts(2),
        )
    )

    got = await repo.get_latest_for_scope("default", "default", "u_alice")
    assert got is not None
    assert got.session_id == "s_001"
    assert got.user_id == "u_alice"
    assert got.app_id == "default"
    assert got.project_id == "default"
    assert got.agent_id == "agent_42"
    assert got.first_prompt == "plan my tokyo trip"
    assert got.turn_count == 3
    assert got.started_at == _ts(1)
    assert got.ended_at == _ts(2)
    assert got.reason == "end"
    assert got.recorded_at == _ts(2)


async def test_upsert_twice_same_scope_updates_in_place(
    repo: _SessionSummaryRepo,
) -> None:
    """Repeating the same key must refresh the row, not append a duplicate."""
    await repo.upsert(
        _make_summary(
            session_id="s_001",
            user_id="u_alice",
            first_prompt="hello",
            turn_count=1,
            recorded_at=_ts(1),
        )
    )
    await repo.upsert(
        _make_summary(
            session_id="s_001",
            user_id="u_alice",
            first_prompt="hello again",
            turn_count=5,
            reason="end",
            recorded_at=_ts(2),
        )
    )

    assert await repo.count() == 1
    got = await repo.get_latest_for_scope("default", "default", "u_alice")
    assert got is not None
    assert got.first_prompt == "hello again"
    assert got.turn_count == 5
    assert got.reason == "end"
    assert got.recorded_at == _ts(2)


async def test_lookup_returns_newest_of_two_sessions(
    repo: _SessionSummaryRepo,
) -> None:
    """Two sessions in one scope -> the later ``recorded_at`` wins."""
    await repo.upsert(
        _make_summary(
            session_id="s_old",
            user_id="u_alice",
            first_prompt="old session",
            recorded_at=_ts(1),
        )
    )
    await repo.upsert(
        _make_summary(
            session_id="s_new",
            user_id="u_alice",
            first_prompt="new session",
            recorded_at=_ts(3),
        )
    )

    got = await repo.get_latest_for_scope("default", "default", "u_alice")
    assert got is not None
    assert got.session_id == "s_new"
    assert got.first_prompt == "new session"


async def test_lookup_returns_none_for_unknown_user(
    repo: _SessionSummaryRepo,
) -> None:
    """No digest for the scope -> ``None``."""
    assert await repo.get_latest_for_scope("default", "default", "u_nobody") is None


async def test_different_app_ids_do_not_collide(
    repo: _SessionSummaryRepo,
) -> None:
    """Same user + session under a different app id is a separate row."""
    await repo.upsert(
        _make_summary(
            session_id="s_001",
            user_id="u_alice",
            app_id="app_a",
            first_prompt="from app a",
            recorded_at=_ts(1),
        )
    )
    await repo.upsert(
        _make_summary(
            session_id="s_001",
            user_id="u_alice",
            app_id="app_b",
            first_prompt="from app b",
            recorded_at=_ts(2),
        )
    )

    assert await repo.count() == 2
    app_a = await repo.get_latest_for_scope("app_a", "default", "u_alice")
    app_b = await repo.get_latest_for_scope("app_b", "default", "u_alice")
    assert app_a is not None
    assert app_a.first_prompt == "from app a"
    assert app_b is not None
    assert app_b.first_prompt == "from app b"


async def test_upsert_defaults_recorded_at_when_row_lacks_one(
    repo: _SessionSummaryRepo,
) -> None:
    """A row with no ``recorded_at`` gets a tz-aware UTC timestamp."""
    row = SessionSummary.model_construct(
        id=None,
        app_id="default",
        project_id="default",
        user_id="u_alice",
        agent_id=None,
        session_id="s_001",
        first_prompt="no clock",
        turn_count=1,
        started_at=None,
        ended_at=None,
        reason=None,
        recorded_at=None,
    )
    await repo.upsert(row)

    got = await repo.get_latest_for_scope("default", "default", "u_alice")
    assert got is not None
    assert got.recorded_at is not None
    assert got.recorded_at.tzinfo is not None


async def test_module_level_api_used_by_service_layer(sqlite_factory) -> None:
    """``upsert_session_summary`` / ``get_latest_session_summary`` round-trip."""
    await upsert_session_summary(
        _make_summary(
            session_id="s_001",
            user_id="u_alice",
            first_prompt="via module",
            recorded_at=_ts(4),
        )
    )

    got = await get_latest_session_summary("default", "default", "u_alice")
    assert got is not None
    assert got.first_prompt == "via module"
    assert got.recorded_at == _ts(4)
