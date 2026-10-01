"""Cooldown gate for an embedding provider that is believed to be down.

An embedder outage is expensive to rediscover. The cascade re-runs a file
whenever its mtime moves, and a file holding rows that still lack vectors
re-attempts those embeddings on every run; a search re-embeds its query on
every request. Without a gate, one outage turns each 30 s sweep into
thousands of doomed HTTP calls, and each search pays the provider's timeout
before degrading.

After a failure the gate opens for ``cooldown_seconds`` and callers
short-circuit to their fallback (``vector = NULL`` on the write path,
keyword-only recall on the read path).

Callers own their instance rather than sharing one: the cascade and the
search manager react to the same provider but want independent signals, and
a shared singleton would let a test that trips one subsystem silently
short-circuit the other.
"""

from __future__ import annotations

import time


class EmbedGuard:
    """Cooldown gate in front of the embedding provider.

    Uses :func:`time.monotonic` (not wall-clock) so a system clock change
    cannot leave the gate stuck open.
    """

    def __init__(self, cooldown_seconds: float = 300.0) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._open_until: float = 0.0

    def is_open(self) -> bool:
        """True while the gate is closed to traffic (provider believed down)."""
        return time.monotonic() < self._open_until

    def record_failure(self) -> None:
        """Open the gate for one cooldown window."""
        self._open_until = time.monotonic() + self.cooldown_seconds

    def record_success(self) -> None:
        """Close the gate — the provider answered."""
        self._open_until = 0.0
