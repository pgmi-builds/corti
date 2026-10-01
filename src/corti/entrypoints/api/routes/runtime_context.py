"""Runtime-interop endpoints — ``session/start``, ``prefetch``, ``session/end``.

Thin adapters by design: validate the DTO, dispatch to the service layer,
return the envelope. All the policy those endpoints encode — thresholds,
ordering, block text, session bookkeeping, degradation reporting — lives in
:mod:`corti.memory.runtime_context` and :mod:`corti.service.runtime_context`,
so it is shared by every agent runtime instead of re-implemented per plugin.
"""

from __future__ import annotations

from fastapi import APIRouter

from corti.memory.runtime_context import (
    PrefetchRequest,
    PrefetchResponse,
    SessionEndRequest,
    SessionEndResponse,
    SessionStartRequest,
    SessionStartResponse,
)
from corti.service.runtime_context import (
    prefetch as run_prefetch,
)
from corti.service.runtime_context import (
    session_end as run_session_end,
)
from corti.service.runtime_context import (
    session_start as run_session_start,
)

router = APIRouter(prefix="/api/v1/memory", tags=["runtime"])


@router.post("/prefetch", response_model=PrefetchResponse)
async def post_prefetch(req: PrefetchRequest) -> PrefetchResponse:
    """Per-turn recall, rendered as a ready-to-inject block.

    ``skipped`` is a normal outcome ("nothing worth recalling"), not an
    error — the adapter injects nothing and moves on.
    """
    return await run_prefetch(req)


@router.post("/session/start", response_model=SessionStartResponse)
async def post_session_start(req: SessionStartRequest) -> SessionStartResponse:
    """Once-per-session breadth: profile, last session, random recent catalog."""
    return await run_session_start(req)


@router.post("/session/end", response_model=SessionEndResponse)
async def post_session_end(req: SessionEndRequest) -> SessionEndResponse:
    """Record a finished session so ``session/start`` can report it later.

    Replaces the per-plugin session files (Claude Code kept its own
    ``sessions.jsonl`` that no other runtime could see).
    """
    return await run_session_end(req)
