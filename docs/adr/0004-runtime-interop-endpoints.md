---
status: accepted
---

# Serve the agent-runtime contract from the server, not from each plugin

Corti reaches agents through plugins (`integrations/deepseek-harness`,
`integrations/hermes`, `integrations/claude-code`). Each plugin was written
as if it owned the memory *product*: its own recall threshold, its own
trivial-prompt rule, its own injected-block format, its own session
bookkeeping, its own degradation story. This ADR moves that policy into the
server and reduces every plugin to transport for its host's hook protocol.

## Context

### What a plugin actually has to do

An agent runtime differs from the next one in exactly two ways:

1. **How it calls out.** DeepSeek Harness exposes a plugin API
   (`ctx.systemPrompt.section`, `agent/pre-step`); Claude Code runs hook
   commands that read JSON on stdin and print JSON on stdout; Hermes calls
   provider methods (`prefetch`, `system_prompt_block`, `sync_turn`).
2. **What it calls its tools.** `memory_search`, `mem_search`, `mem_recall`.

Neither of those is a reason for the *memory behaviour* to differ. The agent
should get the same recall, the same relevance floor, and the same text
whether it is running under DSH, Hermes or Claude Code.

### What was actually duplicated

| Concern | DSH | Hermes | Claude Code |
|---|---|---|---|
| "too trivial to search" | `isTrivialPrompt` in `src/index.ts` | `_is_trivial_prompt` in `__init__.py` | `countWords` in `inject-memories.js` |
| per-turn block | `renderEpisodeStubs` | `format_prefetch` | `buildContext` |
| session-start block | subject catalog + `ctx.systemPrompt.section` | `system_prompt_block` | `session-context.js` |
| session bookkeeping | capture hook | `on_session_end` | `data/sessions.jsonl` |
| degradation | client retries `keyword` on 503 | `_is_breaker_open` circuit breaker | swallow and `exit(0)` |

Three runtimes, three answers to every question, and a fourth runtime would
have meant a fourth copy. The drift was already visible: three different
"trivial prompt" definitions, three block skeletons, three failure
policies — and the session summaries Claude Code stored in its own JSONL
file were invisible to every other runtime.

The fail-safe work that motivated this ADR (degrading a dead embedder or
cross-encoder instead of failing the request) landed **server-side**, in
`memory/search/manager.py` and `memory/search/callbacks.py`, precisely
because that is where it covers every caller at once. The same reasoning
applies to the rest of the runtime-facing policy.

## Decision

Expose three server-side *interop* endpoints that return finished text, and
keep the primitives (`add` / `search` / `flush` / `get`) as the tool
surface.

| Endpoint | Home of the policy |
|---|---|
| `POST /api/v1/memory/session/start` | `memory/runtime_context/render.py` + `service/runtime_context.py` |
| `POST /api/v1/memory/prefetch` | same |
| `POST /api/v1/memory/session/end` | same, persisted through `session_summary_repo` |

Supporting pieces:

- `memory/runtime_context/policy.py` — one `is_trivial_prompt`, one set of
  injection budgets, one sampler, one truncator.
- `memory/runtime_context/render.py` — every injected string, in one file.
  The blocks deliberately never name a tool: tool names are per-runtime, so
  the adapter owns the sentence that points at its own tool.
- `memory/runtime_context/dto.py` — `skipped` and `degraded` are first-class
  response fields, so "nothing worth recalling" and "recall was partial" stop
  being things an adapter has to guess.
- `infra/persistence/sqlite/tables/session_summary.py` — where a session
  digest lives now, so `session/start` reports the same last session in
  every runtime.

### What stays in the adapter

Exactly the two host-specific things, plus one more:

1. Read the host's hook payload; write the host's hook output.
2. Parse the host's transcript into `session/end`'s fields (transcript shapes
   are host-specific and the server must not learn three of them).
3. Add the guidance sentence that names its own tools.

That is a transport shim of roughly fifty lines, and it holds no memory
policy.

## Consequences

- A fourth agent runtime costs one adapter, not one re-implementation.
- Recall thresholds, block text and degradation reporting change once, for
  the whole fleet, without touching a plugin.
- The server owns state that used to be plugin-local (`sessions.jsonl`);
  that state is now visible to every runtime and to operators.
- Adapters get simpler to test: they can be exercised against a recorded
  server response with no policy assertions.
- Cost: a runtime now needs a reachable server for session start and
  per-turn recall. That is already true for writes, and both endpoints must
  fail open (`skipped`) rather than block the host's turn.

## Alternatives considered

- **Ship one shared client library to all plugins.** Reduces duplication of
  *code* but not of *policy*: each plugin still decides thresholds, formats
  and degradation, and the library cannot be fixed in place — three vendored
  copies drift.
- **Make MCP the only surface.** MCP covers the tool surface but not
  session-start injection or per-turn prefetch on runtimes that do not expose
  those hooks; Claude Code's own hooks exist because MCP cannot reach them.
- **Fold the interop endpoints into `/search`.** Would force every adapter to
  know the rendering rules and the thresholds, which is the problem being
  solved.
