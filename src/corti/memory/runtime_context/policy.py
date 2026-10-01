"""Interop policy — the rules every agent runtime shares, in one place.

Corti is reached by heterogeneous agent runtimes (DeepSeek Harness, Hermes,
Claude Code, ...). Each one only knows how to speak *its own* hook protocol;
none of them should know how much text to inject, when a prompt is too
trivial to search for, or what a recall block looks like.

Before this module those rules lived inside each plugin, so the fleet ended
up with three different "trivial prompt" definitions, three block formats,
three truncation budgets and three degradation stories — every one of them
drifting independently. They live here now: a new runtime adapter supplies
transport (read the hook's stdin, print the hook's stdout) and nothing else.

Pure functions and constants only — no I/O, so both the domain renderers and
the service layer can depend on it cheaply.
"""

from __future__ import annotations

import random
import re

# ── Injection budgets ────────────────────────────────────────────────────

DEFAULT_INJECT_TOP_K = 5
"""Episode hits injected per turn (``prefetch``). Recall breadth for the
agent-facing tool is deliberately wider than what gets injected."""

DEFAULT_RECALL_TOP_K = 8
"""Recall breadth used when a caller does not pin ``top_k`` for prefetch."""

DEFAULT_MIN_SCORE = 0.0
"""Relevance floor applied to injected hits. ``0.0`` disables it.

Deliberately off by default. Recall scores are not comparable across
methods — a HYBRID fusion score and a BM25 score live on different scales
(measured on the live corpus: the same episode scored 0.0079 fused and
0.2078 lexical), so one shared threshold silently drops every hybrid hit
and looks exactly like "memory has nothing for me". ``top_k`` is the
pruning control that works everywhere; a caller that knows its own scale
can still set a floor explicitly.
"""

DEFAULT_MAX_INJECT_CHARS = 3500
"""Hard character ceiling for one injected block. Applied last, after the
relevance filter, so truncation only ever costs the tail."""

DEFAULT_RECENT_COUNT = 5
"""Newest episodes listed by ``session/start`` when no recency sample is
requested."""

DEFAULT_RECENCY_WINDOW = 200
"""How many of the newest records the startup sample is drawn from."""

DEFAULT_RECENCY_SAMPLE = 10
"""Catalog entries drawn per session start."""

MIN_PROMPT_CHARS = 4
"""Prompts shorter than this are never searched for."""

# ── Trivial-prompt detection ─────────────────────────────────────────────

_TRIVIAL_PROMPT_RE = re.compile(
    r"^(?:"
    r"hi|hihi|hello|hey|ok|okay|test|"
    r"yes|no|sure|thanks|thank you|y|n|yep|nope|yeah|nah|"
    r"continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|"
    r"lgtm|k"
    r")[.!?]*$",
    re.IGNORECASE,
)

_SLASH_COMMAND_RE = re.compile(r"^/")


def is_trivial_prompt(text: str | None) -> bool:
    """Return ``True`` when ``text`` is not worth a retrieval round-trip.

    Covers the three cases every runtime used to re-implement separately:
    empty/whitespace, a host slash command (the runtime expands those
    itself), and short acknowledgements ("ok", "thanks", "继续") that carry
    no retrievable intent.
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    if _SLASH_COMMAND_RE.match(stripped):
        return True
    if len(stripped) < MIN_PROMPT_CHARS:
        return True
    return bool(_TRIVIAL_PROMPT_RE.match(stripped))


# ── Selection helpers ────────────────────────────────────────────────────


def sample_random[T](
    items: list[T], n: int, *, rng: random.Random | None = None
) -> list[T]:
    """Draw ``n`` items uniformly without replacement.

    The startup catalog samples from the newest ``recency_window`` records
    instead of reading the newest ``n``, so one long session cannot occupy
    every slot. Sampling is what makes the catalog *breadth* rather than
    *recency*, which is why the injected text says so out loud.
    """
    if n <= 0 or not items:
        return []
    pool = list(items)
    (rng or random).shuffle(pool)
    return pool[:n]


def truncate_block(text: str, max_chars: int) -> str:
    """Trim ``text`` to ``max_chars``, preferring a line boundary.

    Returns ``""`` for a non-positive budget so callers can treat "no room"
    and "nothing to say" identically.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    budget = max_chars - len(" …")
    if budget <= 0:
        return " …"
    window = text[:budget]
    boundary = window.rfind("\n")
    if boundary > 0:
        window = window[:boundary]
    return window.rstrip() + " …"
