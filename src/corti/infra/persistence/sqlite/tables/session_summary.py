"""``session_summary`` — per-session digest owned by the memory server.

The row is scoped by ``(app_id, project_id, user_id, session_id)`` so the
same ``session_id`` may recur in different spaces without colliding; those
four segments lead the composite ``UniqueConstraint``. One row per session
records what happened in that session (first prompt, turn count, time
window, and the reason it ended).

This table lets the memory server own "what happened in the last session"
instead of every agent-runtime plugin keeping its own plugin-local session
file (JSONL) — the plugins can then stay stateless.
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint

from corti.component.utils.datetime import UtcDatetime
from corti.core.persistence.sqlite import BaseTable, Field
from corti.core.persistence.sqlite.base import UtcDateTimeColumn


class SessionSummary(BaseTable, table=True):
    """One row per (app, project, user, session). Latest activity digest."""

    __tablename__ = "session_summary"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint(
            "app_id",
            "project_id",
            "user_id",
            "session_id",
            name="uq_session_summary_scope",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    app_id: str = Field(default="default")
    project_id: str = Field(default="default")
    """App / project scope segments (default ``"default"``)."""
    user_id: str = Field(index=True)
    agent_id: str | None = Field(default=None)
    session_id: str = Field(index=True)
    first_prompt: str = Field(default="")
    """The opening user prompt, used as the one-line session label."""
    turn_count: int = Field(default=0)
    """Number of user turns observed in the session so far."""
    started_at: UtcDatetime | None = Field(default=None, sa_type=UtcDateTimeColumn)
    """When the session's first turn arrived; ``None`` if unknown."""
    ended_at: UtcDatetime | None = Field(default=None, sa_type=UtcDateTimeColumn)
    """When the session's last turn arrived; ``None`` while still open."""
    reason: str | None = Field(default=None)
    """Why the session closed (e.g. ``"end"`` / ``"timeout"``); ``None``
    while still open."""
    recorded_at: UtcDatetime = Field(sa_type=UtcDateTimeColumn)
    """When this digest was last written; the lookup orders by it."""
