"""Per-request record of retrieval legs that went missing.

Recall is deliberately multi-leg: keyword/BM25 needs no provider, the vector
leg needs an embedder, the rerank pass needs a cross-encoder. When a leg is
down the request still succeeds on the remaining legs — correct, but *quiet*:
a keyword-only answer that presents itself as hybrid misleads the caller, the
model, and whoever reads the logs.

So every degradation point records itself here, and the response carries the
list. A ``ContextVar`` keeps the accumulator request-scoped: each asyncio
task gets its own copy, so concurrent searches never see each other's legs,
and no mutable list has to be threaded through every recall helper.
"""

from __future__ import annotations

from contextvars import ContextVar

_degraded: ContextVar[list[str] | None] = ContextVar("search_degraded", default=None)


def reset_degradation() -> None:
    """Start a fresh accumulator for the request about to run."""
    _degraded.set([])


def mark_degraded(leg: str) -> None:
    """Record that ``leg`` was unavailable. Idempotent per request."""
    current = _degraded.get()
    if current is not None and leg not in current:
        current.append(leg)


def degraded_legs() -> list[str]:
    """The legs degraded so far in this request (never ``None``)."""
    return list(_degraded.get() or [])
