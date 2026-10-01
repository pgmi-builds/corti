"""RuntimeContext — the agent-runtime interop surface.

Corti is consumed by heterogeneous agent runtimes. Each one differs in *how*
it calls out (hooks, MCP, a plugin API) and is identical in *what* it needs:
a block to inject at session start, a block to inject per turn, and durable
memory across turns. These three endpoints carry that contract so the
per-runtime adapters stay transport-only.

External usage:
    from corti.memory.runtime_context import (
        PrefetchRequest,
        PrefetchResponse,
        SessionEndRequest,
        SessionEndResponse,
        SessionStartRequest,
        SessionStartResponse,
        is_trivial_prompt,
        sample_random,
        truncate_block,
    )
"""

from .dto import PrefetchRequest as PrefetchRequest
from .dto import PrefetchResponse as PrefetchResponse
from .dto import RuntimeHit as RuntimeHit
from .dto import SessionEndRequest as SessionEndRequest
from .dto import SessionEndResponse as SessionEndResponse
from .dto import SessionStartRequest as SessionStartRequest
from .dto import SessionStartResponse as SessionStartResponse
from .dto import SessionSummaryItem as SessionSummaryItem
from .policy import DEFAULT_INJECT_TOP_K as DEFAULT_INJECT_TOP_K
from .policy import DEFAULT_MAX_INJECT_CHARS as DEFAULT_MAX_INJECT_CHARS
from .policy import DEFAULT_MIN_SCORE as DEFAULT_MIN_SCORE
from .policy import is_trivial_prompt as is_trivial_prompt
from .policy import sample_random as sample_random
from .policy import truncate_block as truncate_block
from .render import render_prefetch_block as render_prefetch_block
from .render import render_prefetch_display as render_prefetch_display
from .render import render_session_end_display as render_session_end_display
from .render import render_session_start_block as render_session_start_block
from .render import render_session_start_display as render_session_start_display

__all__ = [
    "DEFAULT_INJECT_TOP_K",
    "DEFAULT_MAX_INJECT_CHARS",
    "DEFAULT_MIN_SCORE",
    "PrefetchRequest",
    "PrefetchResponse",
    "RuntimeHit",
    "SessionEndRequest",
    "SessionEndResponse",
    "SessionStartRequest",
    "SessionStartResponse",
    "SessionSummaryItem",
    "is_trivial_prompt",
    "render_prefetch_block",
    "render_prefetch_display",
    "render_session_end_display",
    "render_session_start_block",
    "render_session_start_display",
    "sample_random",
    "truncate_block",
]
