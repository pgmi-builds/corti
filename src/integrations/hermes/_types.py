"""Type definitions for the Corti Hermes memory-provider plugin.

These shapes mirror the Corti HTTP API v1 contract documented in
``Corti/docs/api.md``. They are intentionally plain ``TypedDict`` / dataclass
shapes so they can be consumed without importing anything Corti-specific at
runtime (the plugin runs in the Hermes process).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NotRequired, Required, TypedDict

# ── /memory/add shape ────────────────────────────────────────────────────────

Role = Literal["user", "assistant", "tool"]
ContentType = Literal["text", "image", "audio", "doc", "pdf", "html", "email"]
# v1 contract: /add and /flush are async-accept endpoints — the response
# status is always "accepted" (raw content durably stored; boundary +
# extraction run on the server's per-session queue afterwards). The old
# synchronous outcomes ("accumulated" / "extracted") no longer appear in
# HTTP responses; they survive only as the service layer's internal
# outcome values.
AddStatus = Literal["accepted"]


class ContentItem(TypedDict):
    """A single content block within an Corti ``MessageItem.content`` list."""

    type: Required[ContentType]
    text: NotRequired[str | None]
    uri: NotRequired[str | None]
    base64: NotRequired[str | None]
    ext: NotRequired[str | None]
    name: NotRequired[str | None]
    extras: NotRequired[dict[str, object] | None]


class ToolFunction(TypedDict):
    """The function being invoked in a tool call."""

    name: Required[str]
    arguments: Required[str]


class ToolCall(TypedDict):
    """An OpenAI-shaped tool invocation attached to an assistant turn."""

    id: Required[str]
    type: NotRequired[str]
    function: Required[ToolFunction]


class MessageItem(TypedDict):
    """One turn in a ``POST /api/v1/memory/add`` request.

    ``content`` may be a plain string (text shorthand) or a list of content
    blocks. All other keys match the Corti v1 contract.
    """

    sender_id: Required[str]
    sender_name: NotRequired[str | None]
    role: Required[Role]
    timestamp: Required[int]
    content: Required[str | Sequence[ContentItem]]
    tool_calls: NotRequired[Sequence[ToolCall] | None]
    tool_call_id: NotRequired[str | None]


class AddRequest(TypedDict):
    """``POST /api/v1/memory/add`` request body."""

    session_id: Required[str]
    messages: Required[Sequence[MessageItem]]
    app_id: NotRequired[str]
    project_id: NotRequired[str]


class AddResponse(TypedDict):
    """``POST /api/v1/memory/add`` payload inside the success envelope."""

    message_count: Required[int]
    status: Required[AddStatus]


# ── /memory/flush shape ──────────────────────────────────────────────────────

FlushStatus = Literal["accepted"]


class FlushRequest(TypedDict):
    """``POST /api/v1/memory/flush`` request body."""

    session_id: Required[str]
    app_id: NotRequired[str]
    project_id: NotRequired[str]


class FlushResponse(TypedDict):
    """``POST /api/v1/memory/flush`` payload inside the success envelope."""

    status: Required[FlushStatus]


# ── /memory/search shape ─────────────────────────────────────────────────────

SearchMethod = Literal["keyword", "vector", "hybrid", "agentic"]

# The Corti filter DSL is a recursive boolean tree of predicates. A precise
# TypedDict is too restrictive; callers build plain dicts that match the DSL.
FilterNode = dict[str, object]


class SearchRequest(TypedDict):
    """``POST /api/v1/memory/search`` request body.

    Corti requires exactly one of ``user_id`` or ``agent_id``; type hints
    cannot express XOR, so callers must enforce it.
    """

    query: Required[str]
    user_id: NotRequired[str | None]
    agent_id: NotRequired[str | None]
    app_id: NotRequired[str]
    project_id: NotRequired[str]
    method: NotRequired[SearchMethod]
    top_k: NotRequired[int]
    radius: NotRequired[float | None]
    min_score: NotRequired[float | None]
    include_profile: NotRequired[bool]
    enable_llm_rerank: NotRequired[bool]
    filters: NotRequired[FilterNode | None]


class SearchAtomicFactItem(TypedDict):
    """Atomic fact nested under a ``SearchEpisodeItem``."""

    id: Required[str]
    content: Required[str]
    score: Required[float]


class SearchEpisodeItem(TypedDict):
    """A single episode hit from ``POST /api/v1/memory/search``."""

    id: Required[str]
    user_id: Required[str | None]
    app_id: Required[str]
    project_id: Required[str]
    session_id: Required[str]
    timestamp: Required[str]
    sender_ids: Required[Sequence[str]]
    summary: Required[str]
    subject: Required[str]
    episode: Required[str]
    type: Required[str]
    score: Required[float]
    atomic_facts: Required[Sequence[SearchAtomicFactItem]]


class SearchProfileItem(TypedDict):
    """User profile returned when ``include_profile=True``."""

    id: Required[str]
    user_id: Required[str | None]
    app_id: Required[str]
    project_id: Required[str]
    profile_data: Required[dict[str, object]]
    score: Required[float | None]


class UnprocessedMessage(TypedDict):
    """Raw buffered message returned by search under narrow conditions."""

    id: Required[str]
    app_id: Required[str]
    project_id: Required[str]
    session_id: Required[str]
    sender_id: Required[str]
    sender_name: Required[str | None]
    role: Required[Role]
    content: Required[str | Sequence[ContentItem]]
    timestamp: Required[str]
    tool_calls: Required[Sequence[ToolCall] | None]
    tool_call_id: Required[str | None]


class SearchData(TypedDict):
    """The ``data`` object returned by ``POST /api/v1/memory/search``."""

    unprocessed_messages: Required[Sequence[UnprocessedMessage]]


# ── /memory/get shape ────────────────────────────────────────────────────────

MemoryType = Literal["episode", "profile"]
SortBy = Literal["timestamp", "updated_at"]
SortOrder = Literal["asc", "desc"]


class GetRequest(TypedDict):
    """``POST /api/v1/memory/get`` request body.

    Corti requires exactly one of ``user_id`` or ``agent_id``; type hints
    cannot express XOR, so callers must enforce it.
    """

    memory_type: Required[MemoryType]
    user_id: NotRequired[str | None]
    agent_id: NotRequired[str | None]
    app_id: NotRequired[str]
    project_id: NotRequired[str]
    page: NotRequired[int]
    page_size: NotRequired[int]
    sort_by: NotRequired[SortBy]
    sort_order: NotRequired[SortOrder]
    filters: NotRequired[FilterNode | None]


class GetEpisodeItem(TypedDict):
    """Unranked episode listing item."""

    id: Required[str]
    user_id: Required[str | None]
    app_id: Required[str]
    project_id: Required[str]
    session_id: Required[str]
    timestamp: Required[str]
    sender_ids: Required[Sequence[str]]
    summary: Required[str]
    subject: Required[str]
    episode: Required[str]
    type: Required[str]


class GetProfileItem(TypedDict):
    """Unranked profile listing item."""

    id: Required[str]
    user_id: Required[str | None]
    app_id: Required[str]
    project_id: Required[str]
    profile_data: Required[dict[str, object]]


class GetAtomicFactItem(TypedDict):
    """Unranked atomic-fact listing item."""

    id: Required[str]
    user_id: Required[str | None]
    app_id: Required[str]
    project_id: Required[str]
    session_id: Required[str | None]
    parent_id: Required[str | None]
    content: Required[str]
    source: NotRequired[str | None]
    timestamp: Required[str]


class GetData(TypedDict):
    """The ``data`` object returned by ``POST /api/v1/memory/get``."""

    episodes: Required[Sequence[GetEpisodeItem]]
    profiles: Required[Sequence[GetProfileItem]]
    atomic_facts: Required[Sequence[GetAtomicFactItem]]
    total_count: Required[int]
    count: Required[int]


# ── runtime-interop shapes ───────────────────────────────────────────────────
# /session/start, /prefetch and /session/end return their object at the top
# level (``request_id`` rides on the same object) instead of nesting it under
# ``data``; these mirror that flat wire shape.


class RuntimeHit(TypedDict):
    """One episode hit shaping an injected interop block."""

    id: Required[str]
    subject: Required[str]
    summary: Required[str]
    timestamp: Required[str]
    score: Required[float]
    sender_ids: Required[Sequence[str]]


class SessionSummaryItem(TypedDict):
    """A finished session's digest, as stored by ``/session/end``."""

    session_id: Required[str]
    user_id: Required[str]
    app_id: Required[str]
    project_id: Required[str]
    agent_id: Required[str | None]
    first_prompt: Required[str]
    turn_count: Required[int]
    started_at: Required[str | None]
    ended_at: Required[str | None]
    reason: Required[str | None]
    recorded_at: Required[str]


class PrefetchData(TypedDict):
    """Payload of ``POST /api/v1/memory/prefetch``.

    A non-``None`` ``skipped`` means "inject nothing" and is a normal
    outcome, never an error.
    """

    request_id: Required[str]
    skipped: Required[Literal["trivial_prompt", "no_relevant_hits"] | None]
    block: Required[str]
    display: Required[str]
    hits: Required[Sequence[RuntimeHit]]
    degraded: Required[Sequence[str]]


class SessionStartData(TypedDict):
    """Payload of ``POST /api/v1/memory/session/start``."""

    request_id: Required[str]
    block: Required[str]
    display: Required[str]
    catalog: Required[Sequence[RuntimeHit]]
    total_episodes: Required[int]
    last_session: Required[SessionSummaryItem | None]
    profile_line: Required[str]
    degraded: Required[Sequence[str]]


class SessionEndData(TypedDict):
    """Payload of ``POST /api/v1/memory/session/end``."""

    request_id: Required[str]
    stored: Required[bool]
    summary: Required[SessionSummaryItem]
    display: Required[str]


# ── Envelopes and errors ─────────────────────────────────────────────────────


class ErrorDetail(TypedDict):
    """Nested ``error`` object in an Corti error response."""

    code: Required[str]
    message: Required[str]
    timestamp: Required[str]
    path: Required[str]


class ErrorEnvelope(TypedDict):
    """Corti non-2xx response envelope."""

    request_id: Required[str]
    error: Required[ErrorDetail]


class SuccessEnvelope(TypedDict):
    """Corti 2xx response envelope wrapping endpoint-specific data."""

    request_id: Required[str]
    data: Required[dict[str, object]]


# ── Internal client exceptions ───────────────────────────────────────────────


class CortiClientError(Exception):
    """Raised when the Corti server returns a non-2xx or the request fails."""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


# ── Config data shape ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScopeIds:
    """Validated Corti ``app_id`` / ``project_id`` pair."""

    app_id: str
    project_id: str
