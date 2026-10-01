"""Rendering for the runtime-interop blocks.

Every string an agent runtime injects into its model's context is built
here. Before this module each plugin owned its own prose — Claude Code's
``<relevant-memories>``, Hermes' ``## Corti Memory``, the harness' subject
catalog — so the same memory looked like three different products depending
on which runtime you happened to be in.

Design boundary worth keeping: the block never *names a tool*. Tool names
are per-runtime (``memory_search``, ``mem_search``, ``mem_recall``), so the
adapter owns the guidance sentence that points at its own tool surface. The
server owns the content.
"""

from __future__ import annotations

import datetime as _dt

from corti.component.utils.datetime import to_display_tz

from .dto import RuntimeHit, SessionSummaryItem
from .policy import truncate_block

PREFETCH_OPEN = "<relevant-memories>"
PREFETCH_CLOSE = "</relevant-memories>"
SESSION_OPEN = "<session-memory>"
SESSION_CLOSE = "</session-memory>"

_PREFETCH_PREAMBLE = (
    "Memories recalled from past sessions that may bear on this turn. Each "
    "entry is a stored episode; the date is when it was recorded. Where a "
    "memory disagrees with what you observe now, prefer the newer observation."
)

_PREFETCH_FOOTER = (
    "Full text for any entry is available through the memory search tool."
)

_SAMPLED_DISCLAIMER = (
    "This is a random fetch over stored memory, not a summary of any complete "
    "task — the entries are not ranked by relevance and need not be recent; "
    "treat them as unrelated fragments. Subjects only; the search tool returns "
    "the detail."
)

_NEWEST_DISCLAIMER = (
    "These are the newest stored episodes, newest first — not a summary of any "
    "complete task. Subjects only; the search tool returns the detail."
)


# ── small helpers ────────────────────────────────────────────────────────


def relative_time(when: _dt.datetime | None, *, now: _dt.datetime | None = None) -> str:
    """Human "2h ago" for a timestamp. Empty string when unknown."""
    if when is None:
        return ""
    from corti.component.utils.datetime import get_utc_now

    reference = now or get_utc_now()
    delta = reference - when
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    return f"{days // 30}mo ago"


def _day(when: _dt.datetime | None) -> str:
    local = to_display_tz(when) or when
    return local.strftime("%Y-%m-%d") if local else ""


def _actor(hit: RuntimeHit) -> str:
    """Attributable sender of an episode, when there is exactly one."""
    agents = [s for s in hit.sender_ids if s and s != "default"]
    return agents[0] if len(agents) == 1 else ""


def _first_line(text: str, limit: int = 160) -> str:
    line = (text or "").strip().split("\n")[0].strip()
    return line[:limit]


def _catalog_line(hit: RuntimeHit, *, detail: bool) -> str:
    subject = (hit.subject or hit.summary or "").strip() or "(no subject)"
    actor = _actor(hit)
    stamp = _day(hit.timestamp)
    prefix = f"- [{stamp}]" + (f" ({actor})" if actor else "")
    if not detail:
        return f"{prefix} {subject}"
    first = _first_line(hit.summary or hit.subject)
    return f"{prefix} {subject}" + (f" — {first}" if first else "")


# ── prefetch ─────────────────────────────────────────────────────────────


def render_prefetch_block(hits: list[RuntimeHit], *, max_chars: int) -> str:
    """The per-turn injection: one line per hit, relevance-ranked."""
    if not hits:
        return ""
    lines = [PREFETCH_OPEN, _PREFETCH_PREAMBLE, ""]
    lines.extend(_catalog_line(hit, detail=True) for hit in hits)
    lines.extend(["", _PREFETCH_FOOTER, PREFETCH_CLOSE])
    return truncate_block("\n".join(lines), max_chars)


def render_prefetch_display(hits: list[RuntimeHit]) -> str:
    """One terminal line summarising what was recalled."""
    if not hits:
        return ""
    subjects = []
    for hit in hits[:3]:
        subject = (hit.subject or hit.summary or "").strip()
        if len(subject) > 20:
            subject = subject[:20] + ".."
        if subject:
            subjects.append(subject)
    suffix = f": {', '.join(subjects)}" if subjects else ""
    return f"📝 Memory recalled ({len(hits)}){suffix}"


# ── session/start ────────────────────────────────────────────────────────


def render_session_start_block(
    *,
    user_id: str,
    app_id: str,
    project_id: str,
    profile_line: str = "",
    last_session: SessionSummaryItem | None = None,
    catalog: list[RuntimeHit],
    recency_window: int,
    sampled: bool,
    max_chars: int,
) -> str:
    """The once-per-session injection: profile, last session, catalog."""
    lines = [SESSION_OPEN, "## Corti memory", ""]
    lines.append(f"- Owner: {user_id}")
    lines.append(f"- Scope: {app_id}/{project_id}")

    if profile_line:
        lines.extend(["", "### Profile", profile_line])

    if last_session is not None:
        ago = relative_time(last_session.ended_at or last_session.recorded_at)
        turns = last_session.turn_count
        head = f"### Last session ({ago}, {turns} turns)" if ago else "### Last session"
        summary = _first_line(last_session.first_prompt, 200)
        lines.extend(["", head, f'"{summary}"' if summary else "(no prompt captured)"])

    if catalog:
        if sampled:
            head = (
                f"### Recent activity ({len(catalog)} entries drawn at random from "
                f"the {recency_window} most recent memory records)"
            )
            note = _SAMPLED_DISCLAIMER
        else:
            head = f"### Recent activity (newest {len(catalog)})"
            note = _NEWEST_DISCLAIMER
        lines.extend(["", head, note, ""])
        lines.extend(_catalog_line(hit, detail=False) for hit in catalog)

    lines.extend(["", SESSION_CLOSE])
    return truncate_block("\n".join(lines), max_chars)


def render_session_start_display(
    *,
    catalog: list[RuntimeHit],
    last_session: SessionSummaryItem | None = None,
) -> str:
    """One terminal line summarising what the session started with."""
    parts: list[str] = []
    if last_session is not None:
        ago = relative_time(last_session.ended_at or last_session.recorded_at)
        prompt = _first_line(last_session.first_prompt, 40)
        head = (
            f"Last ({ago}, {last_session.turn_count} turns)" if ago else "Last session"
        )
        parts.append(f'{head}: "{prompt}"' if prompt else head)
    if catalog:
        subjects = []
        for hit in catalog[:2]:
            subject = (hit.subject or hit.summary or "").strip()
            if len(subject) > 15:
                subject = subject[:15] + ".."
            if subject:
                subjects.append(subject)
        tail = f": {', '.join(subjects)}" if subjects else ""
        parts.append(f"{len(catalog)} memories{tail}")
    if not parts:
        return "💡 Corti: Ready"
    return "💡 Corti: " + " | ".join(parts)


# ── session/end ──────────────────────────────────────────────────────────


def render_session_end_display(summary: SessionSummaryItem) -> str:
    """One terminal line reporting what was just recorded."""
    parts = [f"{summary.turn_count} turns"]
    if summary.started_at and summary.ended_at:
        seconds = int((summary.ended_at - summary.started_at).total_seconds())
        if seconds < 60:
            parts.append("<1min")
        elif seconds < 3600:
            parts.append(f"{seconds // 60}min")
        else:
            hours, minutes = divmod(seconds // 60, 60)
            parts.append(f"{hours}h{minutes}m" if minutes else f"{hours}h")
    prompt = _first_line(summary.first_prompt, 50)
    tail = f': "{prompt}"' if prompt else ""
    return f"📝 Session ({', '.join(parts)}){tail}"
