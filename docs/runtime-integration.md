# Runtime integration — writing an agent-runtime adapter

Corti is a memory **server** with a thin adapter per agent runtime. This
document is the contract an adapter implements; it is deliberately short,
because everything an adapter might be tempted to decide already lives on
the server.

Rationale and the migration that produced this split:
[adr/0004-runtime-interop-endpoints.md](adr/0004-runtime-interop-endpoints.md).

## The split

An agent runtime differs from the next one in exactly two ways:

1. **How it calls out.** DeepSeek Harness exposes a plugin API
   (`ctx.systemPrompt.section`, an `agent/pre-step` waterfall); Claude Code
   runs hook commands that read JSON on stdin and print JSON on stdout;
   Hermes calls provider methods (`prefetch`, `system_prompt_block`,
   `sync_turn`).
2. **What it calls its tools.** `memory_search`, `mem_search`, `mem_recall`.

Neither is a reason for the *memory behaviour* to differ. So the adapter
owns those two things and nothing else.

| Concern | Owner |
|---|---|
| Hook payload in / hook output out | adapter |
| Host transcript parsing (turns, timestamps) | adapter |
| Guidance that names the host's own tools | adapter |
| Recall thresholds, ordering, injected text | **server** |
| "Too trivial to search for" | **server** |
| Recency sampling and its disclaimer | **server** |
| Session digest storage | **server** |
| Degradation when a provider is down | **server** |
| Tool schemas (`add` / `search` / `flush` / `get`) | **server** |

## The five touchpoints

| Host hook | Endpoint | What comes back |
|---|---|---|
| session start | `POST /api/v1/memory/session/start` | `block` — profile, last session, recent catalog |
| user turn | `POST /api/v1/memory/prefetch` | `block` — recall for this prompt, or `skipped` |
| tool: write | `POST /api/v1/memory/add` (+ `flush`) | accepted |
| tool: read | `POST /api/v1/memory/search` | hits (plus `degraded`) |
| session end | `POST /api/v1/memory/session/end` | the stored digest |

Request and response fields are specified in
[api.md § Runtime interop endpoints](api.md#runtime-interop-endpoints).
`block` is injected verbatim. `display` is a one-line human summary the
adapter may show or drop.

## Rules

1. **Inject `block` verbatim.** Do not re-sort, re-truncate, re-score or
   re-wrap it. If it looks wrong, that is a server bug — file it.
2. **Treat `skipped` as normal.** `"trivial_prompt"` and
   `"no_relevant_hits"` are the server saying "inject nothing this turn".
   They are not errors and must not be logged as such.
3. **Fail open.** A memory outage must never block the host's turn. On any
   error: inject nothing, print the host's minimal ack, return.
4. **Never re-implement a fallback.** If recall came back keyword-only, the
   server already said so in `degraded` and already explained it inside
   `block`. Do not invent a second opinion.
5. **Send the scope.** `user_id` plus the `app_id` / `project_id` from the
   adapter's config, and `agent_id` for attributable writes. The sender tag
   is memory, not telemetry — it is what makes shared memory trustworthy.
6. **Derive a stable session id** from the host's own session key, and pass
   the same value to `session/end`. A digest stored under an id nobody can
   reproduce is a digest nobody will ever read.
7. **Keep provider errors off the model's channel.** "DashScope is in
   arrears" is an operator message; surface it through the host's log or a
   warning channel, not as injected context.

## Shipped adapters

| Runtime | Adapter | Hook mapping |
|---|---|---|
| DeepSeek Harness | `src/integrations/deepseek-harness/` | `system-prompt/assemble` → session start; `agent/pre-step` → prefetch; capture hook → add/flush + session end |
| Claude Code | `src/integrations/claude-code/` | `SessionStart` / `UserPromptSubmit` / `Stop` / `SessionEnd` hooks; MCP server for the tool surface |
| Hermes | `src/integrations/hermes/` | `system_prompt_block` / `prefetch` / `sync_turn` / `on_session_end` provider methods |

Read one of them before writing a new adapter — the shape is the same every
time, and the diff that produced them is the clearest statement of what
belongs where.
