"""Runtime-interop endpoints — ``session/start``, ``prefetch``, ``session/end``.

Thin adapters by design: validate the DTO, dispatch to the service layer,
wrap the payload in the standard envelope. All the policy these endpoints
encode — thresholds, ordering, block text, session bookkeeping, degradation
reporting — lives in :mod:`corti.memory.runtime_context` and
:mod:`corti.service.runtime_context`, so it is shared by every agent runtime
instead of re-implemented per plugin.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from corti.memory.runtime_context import (
    PrefetchData,
    PrefetchRequest,
    SessionEndData,
    SessionEndRequest,
    SessionStartData,
    SessionStartRequest,
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

from ..utils import extract_request_id
from .memorize import SuccessEnvelope

router = APIRouter(prefix="/api/v1/memory", tags=["runtime"])


@router.post("/prefetch", response_model=SuccessEnvelope[PrefetchData])
async def post_prefetch(
    req: PrefetchRequest, request: Request
) -> SuccessEnvelope[PrefetchData]:
    """Per-turn recall, rendered as a ready-to-inject block.

    ``skipped`` is a normal outcome ("nothing worth recalling"), not an
    error — the adapter injects nothing and moves on.
    """
    return SuccessEnvelope(
        request_id=extract_request_id(request),
        data=await run_prefetch(req),
    )


@router.post("/session/start", response_model=SuccessEnvelope[SessionStartData])
async def post_session_start(
    req: SessionStartRequest, request: Request
) -> SuccessEnvelope[SessionStartData]:
    """Once-per-session breadth: profile, last session, random recent catalog."""
    return SuccessEnvelope(
        request_id=extract_request_id(request),
        data=await run_session_start(req),
    )


@router.post("/session/end", response_model=SuccessEnvelope[SessionEndData])
async def post_session_end(
    req: SessionEndRequest, request: Request
) -> SuccessEnvelope[SessionEndData]:
    """Record a finished session so ``session/start`` can report it later.

    Replaces the per-plugin session files (Claude Code kept its own
    ``sessions.jsonl`` that no other runtime could see).
    """
    return SuccessEnvelope(
        request_id=extract_request_id(request),
        data=await run_session_end(req),
    )
