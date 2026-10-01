"""Unit tests for the runtime-interop block renderers.

Every timestamp is a fixed UTC instant and — where the renderer accepts one —
``relative_time`` gets an explicit ``now=``, so no test touches the wall clock.
"""

from __future__ import annotations

import datetime as _dt
import re

import pytest

from corti.component.utils.datetime import to_display_tz
from corti.memory.runtime_context.dto import RuntimeHit, SessionSummaryItem
from corti.memory.runtime_context.render import (
    relative_time,
    render_prefetch_block,
    render_prefetch_display,
    render_session_end_display,
    render_session_start_block,
    render_session_start_display,
)

_FIXED_TS = _dt.datetime(2026, 1, 2, 12, 0, tzinfo=_dt.UTC)


# ── Helpers ──────────────────────────────────────────────────────────────


def _hit(
    hit_id: str = "ep_1",
    subject: str = "Failover design",
    summary: str = "The primary failed over.",
    timestamp: _dt.datetime = _FIXED_TS,
    score: float = 0.9,
    sender_ids: list[str] | None = None,
) -> RuntimeHit:
    return RuntimeHit(
        id=hit_id,
        subject=subject,
        summary=summary,
        timestamp=timestamp,
        score=score,
        sender_ids=sender_ids if sender_ids is not None else [],
    )


def _window(*hit_ids: str) -> list[RuntimeHit]:
    return [_hit(hit_id=hid, subject=f"subject {hid}") for hid in hit_ids]


def _last_session(
    *,
    ended_at: _dt.datetime | None = _FIXED_TS,
    recorded_at: _dt.datetime = _FIXED_TS,
    turn_count: int = 3,
    first_prompt: str = "fix the failover",
) -> SessionSummaryItem:
    return SessionSummaryItem(
        session_id="s_prev",
        user_id="alice",
        first_prompt=first_prompt,
        turn_count=turn_count,
        started_at=_dt.datetime(2026, 1, 2, 11, 0, tzinfo=_dt.UTC),
        ended_at=ended_at,
        recorded_at=recorded_at,
    )


# ── prefetch block ───────────────────────────────────────────────────────


def test_prefetch_block_empty_for_no_hits() -> None:
    """No hits means nothing to inject."""
    assert render_prefetch_block([], max_chars=3500) == ""


def test_prefetch_block_wraps_body() -> None:
    """The body sits inside the ``<relevant-memories>`` envelope."""
    out = render_prefetch_block([_hit()], max_chars=3500)
    assert out.startswith("<relevant-memories>")
    assert out.endswith("</relevant-memories>")


def test_prefetch_block_includes_subject_and_date() -> None:
    """Each entry carries its subject and a ``YYYY-MM-DD`` stamp."""
    hit = _hit(subject="Failover design")
    out = render_prefetch_block([hit], max_chars=3500)
    assert "Failover design" in out
    day = to_display_tz(hit.timestamp)
    assert day is not None
    assert day.strftime("%Y-%m-%d") in out
    assert re.search(r"\[\d{4}-\d{2}-\d{2}\]", out)


def test_prefetch_block_single_named_sender_tagged() -> None:
    """Exactly one non-default sender renders as a parenthetical."""
    out = render_prefetch_block([_hit(sender_ids=["alice"])], max_chars=3500)
    assert "(alice)" in out


def test_prefetch_block_multiple_senders_not_tagged() -> None:
    """Two attributable senders are ambiguous, so no parenthetical appears."""
    out = render_prefetch_block([_hit(sender_ids=["alice", "bob"])], max_chars=3500)
    assert "(alice)" not in out
    assert "(bob)" not in out
    assert "(" not in out


def test_prefetch_block_default_sender_not_tagged() -> None:
    """The anonymous ``default`` sender is never attributed."""
    out = render_prefetch_block([_hit(sender_ids=["default"])], max_chars=3500)
    assert "(default)" not in out
    assert "(" not in out


def test_prefetch_block_respects_max_chars() -> None:
    """A small budget truncates the tail rather than overshooting."""
    hits = _window(*(f"ep_{i}" for i in range(5)))
    full = render_prefetch_block(hits, max_chars=100_000)
    out = render_prefetch_block(hits, max_chars=80)
    assert len(out) <= 80 + 1
    assert len(out) < len(full)
    assert out.endswith(" …")
    assert "</relevant-memories>" not in out


# ── prefetch display ─────────────────────────────────────────────────────


def test_prefetch_display_empty_for_no_hits() -> None:
    """No hits means no terminal line."""
    assert render_prefetch_display([]) == ""


def test_prefetch_display_counts_hits() -> None:
    """The one-liner reports how many hits came back."""
    out = render_prefetch_display(_window("ep_1", "ep_2", "ep_3"))
    assert "(3)" in out


def test_prefetch_display_truncates_long_subject() -> None:
    """A subject over 20 chars is clipped with a double-dot."""
    out = render_prefetch_display([_hit(subject="x" * 30)])
    assert "x" * 20 + ".." in out


# ── session/start block ──────────────────────────────────────────────────


def test_session_start_block_owner_and_scope() -> None:
    """Owner and ``app/project`` scope always head the block."""
    out = render_session_start_block(
        user_id="alice",
        app_id="corti",
        project_id="mem",
        catalog=[],
        recency_window=200,
        sampled=True,
        max_chars=4000,
    )
    assert "- Owner: alice" in out
    assert "- Scope: corti/mem" in out


def test_session_start_block_profile_only_when_passed() -> None:
    """The ``### Profile`` section appears only when a line is supplied."""
    without = render_session_start_block(
        user_id="alice",
        app_id="corti",
        project_id="mem",
        catalog=[],
        recency_window=200,
        sampled=True,
        max_chars=4000,
    )
    with_profile = render_session_start_block(
        user_id="alice",
        app_id="corti",
        project_id="mem",
        profile_line="- Owner profile: alice builds recall systems",
        catalog=[],
        recency_window=200,
        sampled=True,
        max_chars=4000,
    )
    assert "### Profile" not in without
    assert "### Profile" in with_profile
    assert "alice builds recall systems" in with_profile


def test_session_start_block_last_session_section() -> None:
    """A stored summary renders turn count, relative age and first prompt."""
    out = render_session_start_block(
        user_id="alice",
        app_id="corti",
        project_id="mem",
        last_session=_last_session(ended_at=_dt.datetime(2000, 1, 1, tzinfo=_dt.UTC)),
        catalog=[],
        recency_window=200,
        sampled=True,
        max_chars=4000,
    )
    assert re.search(r"### Last session \(\d+mo ago, 3 turns\)", out)
    assert '"fix the failover"' in out


def test_session_start_block_sampled_heading() -> None:
    """``sampled=True`` says the catalog was drawn at random from the window."""
    out = render_session_start_block(
        user_id="alice",
        app_id="corti",
        project_id="mem",
        catalog=_window("ep_1", "ep_2", "ep_3"),
        recency_window=200,
        sampled=True,
        max_chars=4000,
    )
    assert "drawn at random from the 200 most recent" in out
    assert "newest 3" not in out


def test_session_start_block_newest_heading() -> None:
    """``sampled=False`` uses the plain newest-first heading."""
    out = render_session_start_block(
        user_id="alice",
        app_id="corti",
        project_id="mem",
        catalog=_window("ep_1", "ep_2", "ep_3"),
        recency_window=200,
        sampled=False,
        max_chars=4000,
    )
    assert "### Recent activity (newest 3)" in out
    assert "drawn at random" not in out


def test_session_start_block_empty_catalog_omits_recent_activity() -> None:
    """An empty catalog skips the ``### Recent activity`` section entirely."""
    out = render_session_start_block(
        user_id="alice",
        app_id="corti",
        project_id="mem",
        catalog=[],
        recency_window=200,
        sampled=True,
        max_chars=4000,
    )
    assert "### Recent activity" not in out


# ── session/start display ────────────────────────────────────────────────


def test_session_start_display_ready_when_empty() -> None:
    """Nothing to report renders the ready banner."""
    assert render_session_start_display(catalog=[], last_session=None) == (
        "💡 Corti: Ready"
    )


def test_session_start_display_reports_catalog() -> None:
    """A catalog reports its size and previews subjects."""
    out = render_session_start_display(catalog=_window("ep_1"))
    assert out.startswith("💡 Corti: ")
    assert "1 memories" in out


# ── session/end display ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds", "bucket"),
    [
        (30, "<1min"),
        (90, "1min"),
        (3600, "1h"),
        (3900, "1h5m"),
    ],
)
def test_session_end_display_duration_buckets(seconds: int, bucket: str) -> None:
    """The duration renders as <1min / Nmin / Nh / NhMm."""
    started = _dt.datetime(2026, 1, 2, 10, 0, tzinfo=_dt.UTC)
    ended = started + _dt.timedelta(seconds=seconds)
    summary = SessionSummaryItem(
        session_id="s_end",
        user_id="alice",
        turn_count=4,
        started_at=started,
        ended_at=ended,
        recorded_at=ended,
    )
    out = render_session_end_display(summary)
    assert "4 turns" in out
    assert bucket in out


# ── relative_time ────────────────────────────────────────────────────────

_REL_NOW = _dt.datetime(2026, 6, 1, 12, 0, tzinfo=_dt.UTC)


def test_relative_time_unknown_is_empty() -> None:
    """A missing timestamp has no human rendering."""
    assert relative_time(None, now=_REL_NOW) == ""


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (_dt.timedelta(seconds=0), "just now"),
        (_dt.timedelta(seconds=59), "just now"),
        (_dt.timedelta(minutes=1), "1m ago"),
        (_dt.timedelta(minutes=90), "1h ago"),
        (_dt.timedelta(hours=25), "1d ago"),
        (_dt.timedelta(days=60), "2mo ago"),
    ],
)
def test_relative_time_buckets(delta: _dt.timedelta, expected: str) -> None:
    """Each age band maps to its human label, against an explicit ``now``."""
    assert relative_time(_REL_NOW - delta, now=_REL_NOW) == expected
