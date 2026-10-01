"""Repository for ``session_summary`` — per-session digest persistence.

One row per ``(app_id, project_id, user_id, session_id)``; the upsert is
idempotent on that unique scope key so a repeated session-end refreshes the
existing digest instead of appending a duplicate. The lookup returns the
most recent digest for a user scope, ordered by ``recorded_at``.

The module exposes the module-level :func:`upsert_session_summary` /
:func:`get_latest_session_summary` pair consumed by the runtime-interop
service layer; both delegate to the process-wide
:data:`session_summary_repo` singleton.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corti.component.utils.datetime import get_utc_now
from corti.core.persistence.sqlite import RepoBase, session_scope

from ..sqlite_manager import get_session_factory
from ..tables import SessionSummary


class _SessionSummaryRepo(RepoBase[SessionSummary]):
    """CRUD repository for the ``session_summary`` digest table."""

    model = SessionSummary

    def _factory_lookup(self) -> async_sessionmaker[AsyncSession]:
        return get_session_factory()

    async def upsert(self, summary: SessionSummary) -> None:
        """Insert or refresh one session digest.

        Conflicts on ``(app_id, project_id, user_id, session_id)`` so a
        repeated session-end is idempotent; ``agent_id``, ``first_prompt``,
        ``turn_count``, ``started_at``, ``ended_at``, ``reason`` and
        ``recorded_at`` are overwritten on conflict. ``recorded_at`` falls
        back to :func:`get_utc_now` when the row carries none.
        """
        recorded_at = (
            summary.recorded_at if summary.recorded_at is not None else get_utc_now()
        )
        stmt = (
            sqlite_insert(SessionSummary)
            .values(
                app_id=summary.app_id,
                project_id=summary.project_id,
                user_id=summary.user_id,
                agent_id=summary.agent_id,
                session_id=summary.session_id,
                first_prompt=summary.first_prompt,
                turn_count=summary.turn_count,
                started_at=summary.started_at,
                ended_at=summary.ended_at,
                reason=summary.reason,
                recorded_at=recorded_at,
            )
            .on_conflict_do_update(
                index_elements=["app_id", "project_id", "user_id", "session_id"],
                set_={
                    "agent_id": summary.agent_id,
                    "first_prompt": summary.first_prompt,
                    "turn_count": summary.turn_count,
                    "started_at": summary.started_at,
                    "ended_at": summary.ended_at,
                    "reason": summary.reason,
                    "recorded_at": recorded_at,
                },
            )
        )
        async with session_scope(self._factory) as s:
            await s.execute(stmt)
            await s.commit()

    async def get_latest_for_scope(
        self,
        app_id: str,
        project_id: str,
        user_id: str,
    ) -> SessionSummary | None:
        """Most recent digest for ``(app_id, project_id, user_id)``, or ``None``."""
        async with session_scope(self._factory) as s:
            stmt = (
                select(SessionSummary)
                .where(SessionSummary.app_id == app_id)
                .where(SessionSummary.project_id == project_id)
                .where(SessionSummary.user_id == user_id)
                .order_by(SessionSummary.recorded_at.desc())
                .limit(1)
            )
            return (await s.execute(stmt)).scalar_one_or_none()


session_summary_repo = _SessionSummaryRepo()


async def upsert_session_summary(summary: SessionSummary) -> None:
    """Insert or refresh one session digest via the process-wide repo.

    Idempotent on ``(app_id, project_id, user_id, session_id)``.
    """
    await session_summary_repo.upsert(summary)


async def get_latest_session_summary(
    app_id: str,
    project_id: str,
    user_id: str,
) -> SessionSummary | None:
    """Most recent digest for ``(app_id, project_id, user_id)``, or ``None``."""
    return await session_summary_repo.get_latest_for_scope(app_id, project_id, user_id)
