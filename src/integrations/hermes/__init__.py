"""Corti memory provider for the Hermes Agent.

Wires the Phase 1 plugin modules (``_client`` / ``_config`` / ``_formatting``
/ ``_setup`` / ``_types`` / ``_constants``) into the Hermes ``MemoryProvider``
ABC. This is the only module in the bundle that imports Hermes symbols
(``agent.memory_provider``, ``hermes_constants``, ``tools.registry``,
``utils``); everything else is Hermes-agnostic so it can be unit-tested in
isolation.

The provider mirrors each turn into Corti via a background sync thread,
prefetches relevant memory before the agent answers, exposes four tools
(``mem_search`` / ``mem_list`` / ``mem_add`` / ``mem_flush``),
and trips a circuit breaker after repeated transient failures (mem0 parity).

It holds no memory *policy*: the once-per-session and per-turn blocks are
composed server-side by the runtime-interop endpoints and injected verbatim,
so this module keeps only transport, the host's session handling, the cron
guard and the breaker.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from agent.memory_provider import MemoryProvider
from hermes_constants import get_hermes_home
from tools.registry import tool_error
from utils import atomic_json_write

from ._client import CortiApiClient
from ._config import (
    get_scope_ids,
    is_configured,
    load_config,
    resolve_agent_id,
    resolve_user_id,
)
from ._constants import (
    _BREAKER_COOLDOWN_SECS,
    _BREAKER_THRESHOLD,
    _DEFAULT_AGENT_ID,
    _DEFAULT_API_URL,
    _DEFAULT_SEARCH_METHOD,
    _DEFAULT_TOOL_SEARCH_TOP_K,
    _MIRROR_TARGETS,
    _PREFETCH_WAIT_SECS,
    TOOL_ADD,
    TOOL_FLUSH,
    TOOL_LIST,
    TOOL_SEARCH,
)
from ._formatting import (
    format_memory_write_message,
    format_tool_result,
)
from ._setup import post_setup as _run_setup
from ._types import CortiClientError, MessageItem, ScopeIds

logger = logging.getLogger(__name__)

# Corti error codes that warrant circuit-breaker trips (transient). Client
# errors (INVALID_INPUT / NOT_FOUND / ...) are not transient and do not count.
_TRANSIENT_CODES: frozenset[str] = frozenset(
    {
        "",
        "CLIENT_CLOSED",
        "CONFIGURATION_ERROR",
        "EXTERNAL_SERVICE_UNAVAILABLE",
        "INTERNAL_ERROR",
    }
)

# ── mem_add retry dedup ──────────────────────────────────────────────────────
# Corti derives message ids deterministically (session + timestamp + index),
# so a blind retry of a timed-out mem_add only produces a duplicate if it
# carries a NEW timestamp. This cache reuses the timestamp for the same
# (session, content) within a short window, making the retried tool call a
# byte-identical payload — the server's INSERT OR IGNORE absorbs it silently.
_ADD_TS_CACHE: dict[tuple[str, str], tuple[int, float]] = {}
_ADD_TS_TTL_SECS = 300.0
_ADD_TS_MAX_ENTRIES = 512


def _stable_add_timestamp(session_id: str, content: str, now_ms: int) -> int:
    """Return a stable timestamp for repeated (session, content) tool calls."""
    now = time.monotonic()
    key = (session_id, content)
    cached = _ADD_TS_CACHE.get(key)
    if cached is not None and now - cached[1] < _ADD_TS_TTL_SECS:
        return cached[0]
    if len(_ADD_TS_CACHE) >= _ADD_TS_MAX_ENTRIES:
        expired = [
            k for k, (_, exp) in _ADD_TS_CACHE.items() if now - exp >= _ADD_TS_TTL_SECS
        ]
        for k in expired:
            del _ADD_TS_CACHE[k]
        if len(_ADD_TS_CACHE) >= _ADD_TS_MAX_ENTRIES:
            oldest_key = min(_ADD_TS_CACHE, key=lambda k: _ADD_TS_CACHE[k][1])
            del _ADD_TS_CACHE[oldest_key]
    _ADD_TS_CACHE[key] = (now_ms, now)
    return now_ms


# ── OpenAI function-calling tool schemas ────────────────────────────────────

_SEARCH_SCHEMA: dict[str, Any] = {
    "name": TOOL_SEARCH,
    "description": (
        "Search the Corti memory store for relevant episodes, atomic "
        "facts, and the user profile."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language search query.",
            },
            "top_k": {
                "type": "integer",
                "default": _DEFAULT_TOOL_SEARCH_TOP_K,
                "description": "Maximum number of results (1-100).",
            },
            "method": {
                "type": "string",
                "enum": ["keyword", "vector", "hybrid", "agentic"],
                "default": "hybrid",
            },
        },
        "required": ["query"],
    },
}

_LIST_SCHEMA: dict[str, Any] = {
    "name": TOOL_LIST,
    "description": "List memories of a given type from the Corti store.",
    "parameters": {
        "type": "object",
        "properties": {
            "memory_type": {
                "type": "string",
                "enum": ["episode", "profile"],
                "default": "episode",
            },
            "page": {
                "type": "integer",
                "default": 1,
                "minimum": 1,
            },
            "page_size": {
                "type": "integer",
                "default": 20,
                "minimum": 1,
                "maximum": 100,
            },
        },
        "required": [],
    },
}

_ADD_SCHEMA: dict[str, Any] = {
    "name": TOOL_ADD,
    "description": "Store a fact in the Corti memory store for the user.",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact to remember.",
            },
        },
        "required": ["content"],
    },
}

_FLUSH_SCHEMA: dict[str, Any] = {
    "name": TOOL_FLUSH,
    "description": "Flush the buffered session messages for extraction.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


class CortiMemoryProvider(MemoryProvider):
    """Hermes ``MemoryProvider`` backed by an Corti server."""

    def __init__(self) -> None:
        self._config: dict[str, Any] | None = None
        self._client: CortiApiClient | None = None
        self._session_id: str | None = None
        self._user_id: str | None = None
        self._agent_id: str | None = None
        self._scope: ScopeIds | None = None
        self._sync_thread: threading.Thread | None = None
        self._mirror_thread: threading.Thread | None = None
        self._prefetch_thread: threading.Thread | None = None
        self._prefetch_query: str | None = None
        self._prefetch_result: str | None = None
        self._prefetch_done: bool = False
        self._consecutive_failures: int = 0
        self._breaker_open_until: float = 0.0
        self._breaker_lock: threading.Lock = threading.Lock()
        self._sync_lock: threading.Lock = threading.Lock()
        self._prefetch_lock: threading.Lock = threading.Lock()
        self._atexit_registered: bool = False
        self._system_prompt_cached: str = ""
        self._init_error: str = ""
        self._platform: str = ""
        self._cron_disabled: bool = False

    # ── identity / availability ───────────────────────────────────────────

    @property
    def name(self) -> str:
        return "corti"

    def is_available(self) -> bool:
        cfg = load_config(get_hermes_home())
        return is_configured(cfg)

    # ── lifecycle ─────────────────────────────────────────────────────────

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._config = load_config(get_hermes_home())
        self._user_id = resolve_user_id(
            self._config, kwargs.get("user_id"), kwargs.get("user_id_alt")
        )
        self._agent_id = resolve_agent_id(self._config)
        self._scope = get_scope_ids(self._config)
        try:
            self._client = CortiApiClient(
                self._config.get("api_url") or _DEFAULT_API_URL
            )
        except Exception as exc:
            self._init_error = str(exc)
            self._client = None
            logger.warning("Corti client init failed: %s", exc)
        self._session_id = session_id
        self._platform = kwargs.get("platform") or ""
        # Cron/scheduled agents must be fully memory-silent: Hermes
        # forces sync_turn + on_session_end on EVERY turn regardless of
        # model intent, so a high-frequency watcher floods the shared
        # store with near-identical episodes. Detect the cron platform
        # (plus the cron_ session-id prefix as a fallback) and no-op
        # every read/write hook below.
        self._cron_disabled = self._platform == "cron" or (
            (session_id or "").startswith("cron_")
        )
        if not self._atexit_registered:
            atexit.register(self._shutdown_client)
            self._atexit_registered = True

    # ── circuit breaker ───────────────────────────────────────────────────

    def _is_breaker_open(self) -> bool:
        with self._breaker_lock:
            if self._consecutive_failures < _BREAKER_THRESHOLD:
                return False
            if time.monotonic() >= self._breaker_open_until:
                self._consecutive_failures = 0
                self._breaker_open_until = 0.0
                return False
            return True

    def _record_success(self) -> None:
        with self._breaker_lock:
            self._consecutive_failures = 0

    def _record_failure(self) -> None:
        with self._breaker_lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= _BREAKER_THRESHOLD:
                self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
                logger.warning(
                    "Corti circuit breaker opened after %d failures",
                    self._consecutive_failures,
                )

    @staticmethod
    def _is_transient(code: str) -> bool:
        return code in _TRANSIENT_CODES

    # ── turn sync ─────────────────────────────────────────────────────────

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        if self._cron_disabled:
            return
        if self._client is None or self._is_breaker_open():
            return
        now_ms = time.time_ns() // 1_000_000
        user_msg: MessageItem = {
            "sender_id": self._user_id,
            "role": "user",
            "timestamp": now_ms,
            "content": user_content,
        }
        asst_msg: MessageItem = {
            "sender_id": self._agent_id,
            "role": "assistant",
            "timestamp": now_ms + 1,
            "content": assistant_content,
        }
        sid = session_id or self._session_id
        with self._sync_lock:
            prev = self._sync_thread
            if prev is not None:
                prev.join(timeout=5.0)
                if prev.is_alive():
                    return  # avoid duplicate writes
            thread = threading.Thread(
                target=self._sync_worker,
                args=(sid, [user_msg, asst_msg]),
                name="corti-sync",
                daemon=True,
            )
            self._sync_thread = thread
            thread.start()

    def _sync_worker(self, session_id: str, messages: list[MessageItem]) -> None:
        client = self._client
        scope = self._scope
        if client is None or scope is None:
            return
        try:
            client.add_messages(session_id, scope.app_id, scope.project_id, messages)
            self._record_success()
        except CortiClientError as exc:
            if self._is_transient(exc.code):
                self._record_failure()
            logger.warning("Corti sync_turn add failed: %s", exc)
        except Exception:
            logger.warning("Corti sync_worker error", exc_info=True)

    # ── prefetch ──────────────────────────────────────────────────────────

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._cron_disabled:
            return ""
        if self._client is None or self._is_breaker_open():
            return ""
        cached = self._consume_prefetch_result(query)
        if cached is not None:
            return cached
        self._start_prefetch(query)
        thread = self._prefetch_thread
        if thread is not None:
            thread.join(timeout=_PREFETCH_WAIT_SECS)
        return self._consume_prefetch_result(query) or ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._cron_disabled:
            return
        self._start_prefetch(query)

    def _start_prefetch(self, query: str) -> None:
        with self._prefetch_lock:
            self._prefetch_query = query
            self._prefetch_result = None
            self._prefetch_done = False
            thread = threading.Thread(
                target=self._prefetch_worker,
                args=(query,),
                name="corti-prefetch",
                daemon=True,
            )
            self._prefetch_thread = thread
            thread.start()

    def _prefetch_worker(self, query: str) -> None:
        client = self._client
        scope = self._scope
        user_id = self._user_id
        if client is None or scope is None or user_id is None:
            return
        try:
            resp = client.prefetch(
                user_id,
                scope.app_id,
                scope.project_id,
                query,
                self._session_id,
                agent_id=self._agent_id,
            )
            body = "" if resp.get("skipped") else (resp.get("block") or "")
            with self._prefetch_lock:
                if self._prefetch_query == query:
                    self._prefetch_result = body
                    self._prefetch_done = True
            self._record_success()
        except CortiClientError as exc:
            if self._is_transient(exc.code):
                self._record_failure()
            else:
                logger.warning("Corti prefetch failed: %s", exc)
        except Exception:
            logger.warning("Corti prefetch error", exc_info=True)

    def _consume_prefetch_result(self, query: str) -> str | None:
        with self._prefetch_lock:
            if self._prefetch_query == query and self._prefetch_done:
                result = self._prefetch_result
                self._prefetch_result = None
                self._prefetch_done = False
                self._prefetch_query = None
                return result
            return None

    # ── session / memory-write hooks ──────────────────────────────────────

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        if self._cron_disabled:
            return
        if self._sync_thread is not None:
            self._sync_thread.join(timeout=10.0)
        client = self._client
        scope = self._scope
        if client is not None and not self._is_breaker_open() and scope is not None:
            try:
                client.flush_session(self._session_id, scope.app_id, scope.project_id)
            except CortiClientError as exc:
                logger.warning("Corti flush on session end failed: %s", exc)
            except Exception:
                logger.warning("Corti on_session_end error", exc_info=True)
            # The server owns the session digest. This host knows only its
            # session id and scope; it has no parsed transcript to offer.
            if self._session_id and self._user_id is not None:
                try:
                    client.session_end(
                        self._user_id,
                        scope.app_id,
                        scope.project_id,
                        self._session_id,
                        agent_id=self._agent_id,
                    )
                except CortiClientError as exc:
                    logger.warning("Corti session/end failed: %s", exc)
                except Exception:
                    logger.warning("Corti session/end error", exc_info=True)
        self._shutdown_client()

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._cron_disabled:
            return
        effective = "add" if action in ("add", "replace") else action
        if (
            effective != "add"
            or target not in _MIRROR_TARGETS
            or not content
            or self._client is None
            or self._is_breaker_open()
            or self._scope is None
        ):
            return
        msg = format_memory_write_message(
            content, self._user_id, time.time_ns() // 1_000_000
        )
        sid = self._session_id
        scope = self._scope
        client = self._client

        def _mirror() -> None:
            try:
                client.add_messages(sid, scope.app_id, scope.project_id, [msg])
                client.flush_session(sid, scope.app_id, scope.project_id)
                self._record_success()
            except CortiClientError as exc:
                if self._is_transient(exc.code):
                    self._record_failure()
                logger.warning("Corti memory-write mirror failed: %s", exc)
            except Exception:
                logger.warning("Corti mirror error", exc_info=True)

        self._mirror_thread = threading.Thread(
            target=_mirror, name="corti-mirror", daemon=True
        )
        self._mirror_thread.start()

    # ── tools ─────────────────────────────────────────────────────────────

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        if self._cron_disabled:
            return []
        return [_SEARCH_SCHEMA, _LIST_SCHEMA, _ADD_SCHEMA, _FLUSH_SCHEMA]

    def handle_tool_call(
        self, tool_name: str, args: dict[str, Any], **kwargs: Any
    ) -> str:
        if self._cron_disabled:
            return tool_error("Corti memory is disabled for scheduled (cron) sessions")
        if self._client is None:
            return tool_error(f"Corti backend not initialized: {self._init_error}")
        if self._is_breaker_open():
            return tool_error(
                "Corti temporarily unavailable (circuit breaker open). "
                "Will retry automatically."
            )
        scope = self._scope
        client = self._client
        if scope is None:
            return tool_error("Corti scope not initialized")

        if tool_name == TOOL_SEARCH:
            return self._tool_search(client, scope, args)
        if tool_name == TOOL_LIST:
            return self._tool_list(client, scope, args)
        if tool_name == TOOL_ADD:
            return self._tool_add(client, scope, args)
        if tool_name == TOOL_FLUSH:
            return self._tool_flush(client, scope)
        return tool_error(f"Unknown tool: {tool_name}")

    def _tool_search(
        self,
        client: CortiApiClient,
        scope: ScopeIds,
        args: dict[str, Any],
    ) -> str:
        query = args.get("query")
        if not isinstance(query, str) or not query:
            return tool_error("Missing required parameter: query")
        top_k = args.get("top_k") or _DEFAULT_TOOL_SEARCH_TOP_K
        if not isinstance(top_k, int):
            top_k = _DEFAULT_TOOL_SEARCH_TOP_K
        top_k = max(1, min(100, top_k))
        method = args.get("method") or _DEFAULT_SEARCH_METHOD
        try:
            data = client.search(
                self._user_id,
                None,
                scope.app_id,
                scope.project_id,
                query,
                top_k=top_k,
                method=method,
                include_profile=True,
            )
        except CortiClientError as exc:
            if self._is_transient(exc.code):
                self._record_failure()
            return tool_error(f"Corti search failed: {exc}")
        payload: dict[str, Any] = {
            "results": data,
            "count": len(data.get("episodes") or []),
        }
        # The server renders the sentence; the adapter only prints it.
        note = data.get("degraded_note")
        if note:
            payload["note"] = note
        return format_tool_result(payload)

    def _tool_list(
        self,
        client: CortiApiClient,
        scope: ScopeIds,
        args: dict[str, Any],
    ) -> str:
        memory_type = args.get("memory_type") or "episode"
        page = args.get("page") or 1
        if not isinstance(page, int) or page < 1:
            page = 1
        page_size = args.get("page_size") or 20
        if not isinstance(page_size, int):
            page_size = 20
        page_size = max(1, min(100, page_size))
        try:
            data = client.get(
                self._user_id,
                None,
                scope.app_id,
                scope.project_id,
                memory_type,
                page=page,
                page_size=page_size,
            )
        except CortiClientError as exc:
            if self._is_transient(exc.code):
                self._record_failure()
            return tool_error(f"Corti list failed: {exc}")
        items = data.get(f"{memory_type}s") or []
        return format_tool_result(
            {
                "results": items,
                "total_count": data.get("total_count", 0),
                "count": len(items),
            }
        )

    def _tool_add(
        self,
        client: CortiApiClient,
        scope: ScopeIds,
        args: dict[str, Any],
    ) -> str:
        content = args.get("content")
        if not isinstance(content, str) or not content:
            return tool_error("Missing required parameter: content")
        # Stable timestamp for repeated calls with the same content: a
        # retried mem_add (after a client-side timeout) then carries the
        # same deterministic message_id, and the server dedupes it.
        now_ms = time.time_ns() // 1_000_000
        ts = _stable_add_timestamp(self._session_id, content, now_ms)
        msg = format_memory_write_message(content, self._user_id, ts)
        try:
            # Both calls are fast-ack now: /add durably persists the raw
            # message and /flush queues the boundary pass behind it, so
            # the tool returns before any LLM latency.
            client.add_messages(self._session_id, scope.app_id, scope.project_id, [msg])
            client.flush_session(self._session_id, scope.app_id, scope.project_id)
        except CortiClientError as exc:
            if self._is_transient(exc.code):
                self._record_failure()
            return tool_error(f"Corti add failed: {exc}")
        return format_tool_result({"result": "Fact stored."})

    def _tool_flush(self, client: CortiApiClient, scope: ScopeIds) -> str:
        try:
            resp = client.flush_session(
                self._session_id, scope.app_id, scope.project_id
            )
        except CortiClientError as exc:
            if self._is_transient(exc.code):
                self._record_failure()
            return tool_error(f"Corti flush failed: {exc}")
        return format_tool_result({"status": resp.get("status")})

    # ── prompt / config ───────────────────────────────────────────────────

    def system_prompt_block(self) -> str:
        """Once-per-session block from the server, plus the local tool banner.

        The server composes every memory-bearing line (profile, last session,
        recent catalog); this adapter only appends the sentence naming its own
        tool surface. Falls back to the static banner if the Corti API is
        unreachable or the provider hasn't been initialised yet.
        """
        if self._cron_disabled:
            return ""
        if self._system_prompt_cached:
            return self._system_prompt_cached
        mode = (self._config or {}).get("mode", "oss")
        user = self._user_id or "unknown"
        banner = (
            "## Corti Memory Active\n"
            f"- Mode: {mode}\n"
            f"- User: {user}\n"
            "- Call `mem_search` before answering context-dependent "
            "questions about the user or prior conversations.\n"
        )
        client = self._client
        scope = self._scope
        user_id = self._user_id
        if client is None or scope is None or user_id is None:
            self._system_prompt_cached = banner
            return banner
        try:
            resp = client.session_start(
                user_id,
                scope.app_id,
                scope.project_id,
                self._session_id,
                agent_id=self._agent_id,
            )
            block = resp.get("block") or ""
            # The server's block never names a tool; the banner carries the
            # sentence pointing at Hermes' own tool surface.
            self._system_prompt_cached = f"{block}\n\n{banner}" if block else banner
            return self._system_prompt_cached
        except Exception:
            logger.warning("Corti system_prompt_block fetch failed", exc_info=True)
            self._system_prompt_cached = banner
            return banner

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "api_url",
                "description": "Corti API base URL.",
                "default": _DEFAULT_API_URL,
            },
            {
                "key": "mode",
                "description": "Corti deployment mode.",
                "choices": ["platform", "oss"],
                "default": "oss",
            },
            {
                "key": "user_id",
                "description": "User identifier for memory scoping.",
                "default": "hermes-user",
            },
            {
                "key": "agent_id",
                "description": "Agent identifier for memory scoping.",
                "default": _DEFAULT_AGENT_ID,
            },
            {
                "key": "app_id",
                "description": "Application identifier for memory scoping.",
                "default": "default",
            },
            {
                "key": "project_id",
                "description": "Project identifier for memory scoping.",
                "default": "default",
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        path = Path(hermes_home) / "corti.json"
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    existing = raw
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Failed to read existing %s: %s", path, exc)
        merged = {**existing, **values}
        atomic_json_write(path, merged, mode=0o600)

    def post_setup(self, hermes_home: str, config: dict[str, Any]) -> None:
        _run_setup(Path(hermes_home), config, interactive=True)

    def backup_paths(self) -> list[str]:
        root = Path.home() / ".corti"
        return [str(root)] if root.exists() else []

    # ── teardown ──────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        if self._prefetch_thread is not None:
            self._prefetch_thread.join(timeout=5.0)
        if self._sync_thread is not None:
            self._sync_thread.join(timeout=5.0)
        if self._mirror_thread is not None and self._mirror_thread.is_alive():
            self._mirror_thread.join(timeout=5.0)
        self._shutdown_client()

    def _shutdown_client(self) -> None:
        client = self._client
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.warning("Error closing Corti client", exc_info=True)
        self._client = None
