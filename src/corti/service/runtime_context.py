"""Runtime-interop use cases — ``session/start``, ``prefetch``, ``session/end``.

This module is the answer to "the same policy got written three times".
Before it, each agent-runtime plugin owned its own recall threshold, its own
trivial-prompt rule, its own block format and its own session bookkeeping —
so a fix landed once and drifted three ways, and the fourth runtime would
have needed a fourth copy.

Each function here composes primitives the server already had (``/search``,
``/get``, ``/flush``) and returns *finished text*. An adapter's whole job is
to move those strings between the host's hook channel and the wire.
"""

from __future__ import annotations

from corti.component.utils.datetime import get_utc_now
from corti.core.observability.logging import get_logger
from corti.infra.persistence.sqlite import (
    SessionSummary,
    get_latest_session_summary,
    upsert_session_summary,
)
from corti.memory.get import GetEpisodeItem, GetMemoryType, GetProfileItem, GetRequest
from corti.memory.runtime_context import (
    PrefetchData,
    PrefetchRequest,
    RuntimeHit,
    SessionEndData,
    SessionEndRequest,
    SessionStartData,
    SessionStartRequest,
    SessionSummaryItem,
    is_trivial_prompt,
    render_prefetch_block,
    render_prefetch_display,
    render_session_end_display,
    render_session_start_block,
    render_session_start_display,
    sample_random,
    truncate_block,
)
from corti.memory.search import SearchEpisodeItem, SearchProfileItem, SearchRequest

from .get import get as _get_memory
from .search import search as _search_memory

logger = get_logger(__name__)

_PAGE_CAP = 100
"""``/get`` refuses ``page_size > 100``, so a wider window pages."""

_PROFILE_KEYS = ("name", "summary", "bio", "description", "title")


# ── prefetch ─────────────────────────────────────────────────────────────


async def prefetch(req: PrefetchRequest) -> PrefetchData:
    """Recall for one user turn, rendered for injection.

    Trivial prompts short-circuit *here* rather than in each adapter: the
    rule is a product decision, and a runtime that forgets to apply it just
    pays for a pointless search instead of injecting noise.
    """

    if is_trivial_prompt(req.query):
        return PrefetchData(skipped="trivial_prompt")

    response = await _search_memory(
        SearchRequest(
            user_id=req.user_id,
            app_id=req.app_id,
            project_id=req.project_id,
            query=req.query,
            method=req.method,
            top_k=req.top_k,
            include_profile=req.include_profile,
        )
    )
    data = response.data
    degraded = list(response.degraded)
    scored = [_search_hit(ep) for ep in data.episodes]
    hits = [h for h in scored if h.score >= req.min_score][: req.top_k]

    if not hits:
        return PrefetchData(
            skipped="no_relevant_hits",
            degraded=degraded,
        )

    prefixes = []
    if req.include_profile:
        profile_line = _profile_line(data.profiles)
        if profile_line:
            prefixes.append(profile_line)
    note = _degradation_note(degraded)
    if note:
        prefixes.append(note)

    head = "\n".join(prefixes)
    budget = max(req.max_chars - (len(head) + 1 if head else 0), 0)
    block = render_prefetch_block(hits, max_chars=budget)
    if head:
        block = f"{head}\n{block}" if block else head

    return PrefetchData(
        block=block,
        display=render_prefetch_display(hits),
        hits=hits,
        degraded=degraded,
    )


# ── session/start ────────────────────────────────────────────────────────


async def session_start(req: SessionStartRequest) -> SessionStartData:
    """Breadth for a fresh session: profile, last session, recent catalog."""

    window, total = await _recent_episodes(req)

    row = await get_latest_session_summary(req.app_id, req.project_id, req.user_id)
    last = _summary_item(row) if row is not None else None

    profile_line = ""
    if req.include_profile:
        profile_line = await _fetch_profile_line(req)

    if req.recency_sample > 0:
        chosen = sample_random(window, req.recency_sample)
        sampled = True
    else:
        chosen = window[: req.recent_count]
        sampled = False

    catalog = [_get_hit(ep) for ep in chosen]
    block = render_session_start_block(
        user_id=req.user_id,
        app_id=req.app_id,
        project_id=req.project_id,
        profile_line=profile_line,
        last_session=last,
        catalog=catalog,
        recency_window=req.recency_window,
        sampled=sampled,
        max_chars=req.max_chars,
    )
    return SessionStartData(
        block=block,
        display=render_session_start_display(catalog=catalog, last_session=last),
        catalog=catalog,
        total_episodes=total,
        last_session=last,
        profile_line=profile_line,
    )


# ── session/end ──────────────────────────────────────────────────────────


async def session_end(req: SessionEndRequest) -> SessionEndData:
    """Record the finished session so *any* runtime can read it later.

    Transcript parsing stays in the adapter (transcript shapes are
    host-specific). Where the summary is *kept* is server state — that is
    what makes ``session/start`` work identically in every runtime.
    """
    now = get_utc_now()
    fields: dict[str, object] = {
        "app_id": req.app_id,
        "project_id": req.project_id,
        "user_id": req.user_id,
        "agent_id": req.agent_id,
        "session_id": req.session_id,
        "first_prompt": req.first_prompt,
        "turn_count": req.turn_count,
        "started_at": req.started_at,
        "ended_at": req.ended_at or now,
        "reason": req.reason,
        "recorded_at": now,
    }
    row = SessionSummary(**fields)
    await upsert_session_summary(row)
    item = _summary_item(row)
    return SessionEndData(
        stored=True,
        summary=item,
        display=render_session_end_display(item),
    )


# ── helpers ──────────────────────────────────────────────────────────────


def _degradation_note(legs: list[str]) -> str:
    """Tell the model — and the operator — that recall was partial.

    A keyword-only answer presented as semantic is worse than an honest
    one: the model trusts the ranking it did not get.
    """
    if not legs:
        return ""
    joined = ", ".join(legs)
    return (
        f"[memory notice: {joined} unavailable this turn — "
        "recall used the remaining legs]"
    )


def _search_hit(ep: SearchEpisodeItem) -> RuntimeHit:
    return RuntimeHit(
        id=ep.id,
        subject=ep.subject,
        summary=ep.episode,
        timestamp=ep.timestamp,
        score=ep.score,
        sender_ids=list(ep.sender_ids),
    )


def _get_hit(ep: GetEpisodeItem) -> RuntimeHit:
    """Catalog entry from ``/get`` — unranked, so no score."""
    return RuntimeHit(
        id=ep.id,
        subject=ep.subject,
        summary=ep.episode,
        timestamp=ep.timestamp,
        score=0.0,
        sender_ids=list(ep.sender_ids),
    )


def _profile_line(profiles: list[SearchProfileItem] | list[GetProfileItem]) -> str:
    """One-line owner profile, or ``""`` when there is nothing to say."""
    if not profiles:
        return ""
    data = profiles[0].profile_data or {}
    for key in _PROFILE_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return f"- Owner profile: {' '.join(value.split())}"
    for value in data.values():
        if isinstance(value, str) and value.strip():
            return f"- Owner profile: {' '.join(value.split())}"
    return ""


def _summary_item(row: SessionSummary) -> SessionSummaryItem:
    return SessionSummaryItem(
        session_id=row.session_id,
        user_id=row.user_id,
        app_id=row.app_id,
        project_id=row.project_id,
        agent_id=row.agent_id,
        first_prompt=row.first_prompt or "",
        turn_count=row.turn_count or 0,
        started_at=row.started_at,
        ended_at=row.ended_at,
        reason=row.reason,
        recorded_at=row.recorded_at,
    )


async def _recent_episodes(
    req: SessionStartRequest,
) -> tuple[list[GetEpisodeItem], int]:
    """Newest ``recency_window`` episodes, newest first, plus the total count.

    A short page means the corpus ended; we stop rather than keep asking.
    """
    page_size = min(req.recency_window, _PAGE_CAP)
    pages = max(1, (req.recency_window + page_size - 1) // page_size)
    collected: list[GetEpisodeItem] = []
    total = 0
    for page in range(1, pages + 1):
        response = await _get_memory(
            GetRequest(
                user_id=req.user_id,
                app_id=req.app_id,
                project_id=req.project_id,
                memory_type=GetMemoryType.EPISODE,
                page=page,
                page_size=page_size,
                sort_by="timestamp",
                sort_order="desc",
            )
        )
        total = response.data.total_count
        items = response.data.episodes
        collected.extend(items)
        if len(items) < page_size:
            break
    return collected[: req.recency_window], total


async def _fetch_profile_line(req: SessionStartRequest) -> str:
    response = await _get_memory(
        GetRequest(
            user_id=req.user_id,
            app_id=req.app_id,
            project_id=req.project_id,
            memory_type=GetMemoryType.PROFILE,
            page=1,
            page_size=1,
        )
    )
    return _profile_line(response.data.profiles)


__all__ = ["prefetch", "session_end", "session_start", "truncate_block"]
