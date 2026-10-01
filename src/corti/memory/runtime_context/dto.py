"""Wire DTOs for the runtime-interop endpoints.

Three endpoints carry the whole agent-runtime contract:

* ``POST /api/v1/memory/session/start`` — the once-per-session block.
* ``POST /api/v1/memory/prefetch``     — the once-per-turn block.
* ``POST /api/v1/memory/session/end``  — the session summary.

They exist so that a runtime adapter never decides *what* to inject, only
*where* to put it (the host's hook channel). ``add`` / ``search`` / ``flush``
stay primitive: they back the tool surface, which is already uniform.
"""

from __future__ import annotations

import datetime as _dt
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from corti.component.utils.datetime import ensure_utc
from corti.memory.search.dto import SearchMethod

from .policy import (
    DEFAULT_INJECT_TOP_K,
    DEFAULT_MAX_INJECT_CHARS,
    DEFAULT_MIN_SCORE,
    DEFAULT_RECENCY_SAMPLE,
    DEFAULT_RECENCY_WINDOW,
    DEFAULT_RECENT_COUNT,
)

DEFAULT_MAX_START_CHARS = 4000
"""Character ceiling for the session-start block (it carries the catalog)."""


def _as_utc(value: _dt.datetime | str | int | float | None) -> _dt.datetime | None:
    """Accept ISO-8601 or epoch from a hook payload; normalise to UTC."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return _dt.datetime.fromtimestamp(value, tz=_dt.UTC)
    if isinstance(value, str):
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return ensure_utc(parsed) if parsed.tzinfo else parsed.replace(tzinfo=_dt.UTC)
    return ensure_utc(value)


class _ScopedRequest(BaseModel):
    """Scope every interop request carries — pinned by the caller's plugin."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    app_id: str = "default"
    project_id: str = "default"
    agent_id: str | None = None
    """Sender tag written onto any row this endpoint creates. ``None`` keeps
    the value anonymous (shared memory with no attributable contributor)."""
    session_id: str | None = None


# ── Shared items ─────────────────────────────────────────────────────────


class RuntimeHit(BaseModel):
    """One episode hit shaping an injected block."""

    model_config = ConfigDict(extra="forbid")

    id: str
    subject: str
    summary: str
    timestamp: _dt.datetime
    score: float
    sender_ids: list[str] = Field(default_factory=list)


class SessionSummaryItem(BaseModel):
    """Summary of one finished session, as stored server-side.

    Replaces the per-plugin ``sessions.jsonl`` files that Claude Code kept
    (and that no other runtime could see).
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    user_id: str
    app_id: str = "default"
    project_id: str = "default"
    agent_id: str | None = None
    first_prompt: str = ""
    turn_count: int = 0
    started_at: _dt.datetime | None = None
    ended_at: _dt.datetime | None = None
    reason: str | None = None
    recorded_at: _dt.datetime


# ── prefetch ─────────────────────────────────────────────────────────────


class PrefetchRequest(_ScopedRequest):
    """Per-turn recall: "is anything from before relevant to this prompt?"."""

    query: str = ""
    method: SearchMethod = SearchMethod.HYBRID
    top_k: int = Field(default=DEFAULT_INJECT_TOP_K, ge=1, le=100)
    min_score: Annotated[float, Field(ge=0.0, le=1.0)] = DEFAULT_MIN_SCORE
    max_chars: int = Field(default=DEFAULT_MAX_INJECT_CHARS, ge=0)
    include_profile: bool = True

    @field_validator("query")
    @classmethod
    def _strip_query(cls, v: str) -> str:
        """Normalise whitespace; a blank prompt is a *skip*, not a 422.

        An empty prompt is the most trivial prompt there is, and the hook
        that sends it (a user pressing enter) must not receive an error it
        then has to special-case. The service layer turns this into
        ``skipped="trivial_prompt"``.
        """
        return v.strip()


class PrefetchResponse(BaseModel):
    """``block`` is injected verbatim; ``display`` is the human one-liner."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    skipped: Literal["trivial_prompt", "no_relevant_hits"] | None = None
    """Non-``None`` means the adapter injects nothing. Skipping is a normal
    outcome, not an error — the adapter must treat it as such."""
    block: str = ""
    display: str = ""
    hits: list[RuntimeHit] = Field(default_factory=list)
    degraded: list[str] = Field(default_factory=list)
    """Search legs that were unavailable and got substituted. Empty on a
    healthy provider; ``["embedding"]`` means keyword-only recall."""


# ── session/start ────────────────────────────────────────────────────────


class SessionStartRequest(_ScopedRequest):
    """Once-per-session breadth: profile, last session, recent catalog."""

    recency_window: int = Field(default=DEFAULT_RECENCY_WINDOW, ge=1, le=1000)
    recency_sample: int = Field(default=DEFAULT_RECENCY_SAMPLE, ge=0, le=100)
    recent_count: int = Field(default=DEFAULT_RECENT_COUNT, ge=0, le=100)
    """Used only when ``recency_sample`` is ``0`` (plain newest-first list)."""
    max_chars: int = Field(default=DEFAULT_MAX_START_CHARS, ge=0)
    include_profile: bool = True


class SessionStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    block: str = ""
    display: str = ""
    catalog: list[RuntimeHit] = Field(default_factory=list)
    total_episodes: int = 0
    last_session: SessionSummaryItem | None = None
    profile_line: str = ""
    degraded: list[str] = Field(default_factory=list)


# ── session/end ──────────────────────────────────────────────────────────


class SessionEndRequest(_ScopedRequest):
    """The adapter parses its own transcript; the server keeps the record.

    Transcript shapes are host-specific (Claude Code's JSONL, DSH's turn
    events), so extraction stays in the adapter. What the adapter must *not*
    do is decide where the summary lives — that is server state, visible to
    every runtime and to ``session/start``.
    """

    session_id: str = Field(min_length=1)
    """Narrowed from the base: a summary with no session key cannot be
    matched to anything later."""

    first_prompt: str = ""
    turn_count: int = Field(default=0, ge=0)
    started_at: _dt.datetime | str | int | float | None = None
    ended_at: _dt.datetime | str | int | float | None = None
    reason: str | None = None

    _coerce_started = field_validator("started_at")(_as_utc)
    _coerce_ended = field_validator("ended_at")(_as_utc)


class SessionEndResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    stored: bool
    summary: SessionSummaryItem
    display: str
