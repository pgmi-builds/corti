# DeepSeek Harness Integration (corti-memory)

Wire [DeepSeek Harness](https://github.com/deepseek-ai/dsh) (DSH) to a
Corti server as its persistent cross-session memory — startup recall
injection, per-prompt retrieval, model-facing memory tools, and rolling
turn capture.

The integration ships **inside the Corti repo** as a DSH plugin bundle
at [`src/integrations/deepseek-harness/`](../src/integrations/deepseek-harness/).
It has two small runtime dependencies (`@deepseek-ai/schemastery` and
`yaml`; TypeScript + `@types/node` are dev-only). It ships a commented
`corti.yaml.template` and talks HTTP to the Corti `/api/v1/memory/*`
endpoints. It never touches Corti core source — the same pattern as the
Hermes and Claude Code integrations.

This doc doubles as the **live runtime verification report**
(2026-08-15): the plugin was tested from inside a real DSH agent session
by the agent itself, not only against the HTTP API.

## How it hooks into DSH

Four integration points (`src/index.ts`):

| # | Hook | What it does |
|---|---|---|
| 1 | `ctx.systemPrompt.section` | Adds a `corti:memory` guidance section to the system prompt (order 120), refreshed per assembly with a **random sample** of recent memory subjects (see [Startup recency sample](#startup-recency-sample)) |
| 2 | `ctx.on("agent/pre-step")` | On step 1, searches Corti with the latest user message and splices a synthetic user message `[corti memory — recalled context, not a user message]` into the decision; trivial prompts (greetings, < 4 chars) are skipped |
| 3 | `ctx.tools.register` | Registers `memory_search` / `memory_add` / `memory_list` / `memory_flush` model-facing tools |
| 4 | `ctx.on("session/event")` | Buffers `user/message` + `assistant/message` events per session and submits them to Corti on `turn/end`; synthetic plugin injections (`source.kind === "plugin"`) are skipped to avoid a recall feedback loop |

## Live runtime verification — 2026-08-15

Test environment: DSH agent session (Web GUI), Corti OSS server at
`127.0.0.1:5473`, default scope (`app_id=dsh`, `project_id=default`,
`user_id=default`, `agent_id=pc-deepseek-default`). All checks below
were performed **by the runtime agent through the plugin's own
surfaces**, not by manual HTTP calls.

| Capability | Evidence | Result |
|---|---|---|
| System prompt section | The `corti:memory` guidance text appears verbatim in the live agent's system prompt | PASS |
| Per-prompt recall injection | A `[corti memory — recalled context, not a user message]` block with 2 relevance-ranked episodes was injected before the agent's first reply | PASS |
| `memory_search` | Two distinct queries returned correctly ranked results (relevance-first, not recency-first) | PASS |
| `memory_list` | Returned episodes newest-first | PASS |
| `memory_add` | Accepted immediately; after async extraction completed, a round-trip `memory_search` ranked the new memory #1 with a correctly generated subject | PASS (eventual) |
| `memory_flush` | Returned `flushed` | PASS |
| Turn-end auto-capture | Handler active in-session; messages buffered under the real session id and submitted on `turn/end` | PASS (code-path) |

Supporting evidence from the storage side: episodes land as markdown at
`~/.corti/dsh/default_project/users/default/episodes/episode-YYYY-MM-DD.md`
(entry-marker format), with atomic facts under `.atomic_facts/` and
foresights under `.foresights/`. The two pre-existing memories used for
the recall test (dated 2026-08-14, `session_id=dsh-tool-2026-08-14`)
were themselves created through this plugin's `memory_add` tool on the
previous day — confirming the write path end-to-end across sessions.

## Behavior notes

- **`add` / `flush` ack before extraction runs.** The Corti API
  persists messages into `unprocessed_buffer`, enqueues a per-session
  FIFO work item, and returns `status: "accepted"` immediately (see
  `entrypoints/api/routes/memorize.py`). Extraction (boundary detection
  + episode/atomic-fact pipelines) is LLM work and completes later.
  Consequently `memory_add` reporting `Stored in persistent memory`
  means *accepted into the pipeline*, not *already searchable*. In the
  verification run the new memory was not findable ~30 s after the add
  and was returned (rank #1) a few minutes later. Treat recall as
  eventually consistent.
- **Recall injection is relevance-ranked with a char budget** — the
  pre-step injection renders at most `injectTopK` episodes within
  `maxInjectChars`; the tools render within 6 000 chars.
- **Trivial-prompt guard** — short/greeting prompts skip retrieval
  entirely, so chatty turns don't burn a search.

## Known gaps — fixed 2026-08-15 (post-report patch)

Both gaps observed during the first verification run were patched in
`src/index.ts` and re-verified:

- `memory_flush` now resolves the **live session id** (execution
  context's `session.id`, falling back to the last captured id) instead
  of the hardcoded `dsh-session`.
- The `turn/end` handler now calls `flush` **after** a successful `add`,
  forcing a final extraction boundary per turn. Re-verification: a
  memory added mid-turn became searchable within the same turn
  (previously ~30 s to minutes).

Residual note: retrieval ranking may favor denser older episodes over a
short exact match — a Corti-side retrieval property, not a plugin
behavior.

## Session attribution — fixed 2026-10-01 (twice)

`memory_flush` already resolved a live session id, but `memory_add` wrote
under a fabricated per-day id (`dsh-tool-<date>`): every manual memory of a
day landed in one synthetic conversation that no real session could recall.
Both tools now share one `resolveSessionId(exec)`.

**The first attempt read the wrong field.** It looked for
`exec.session.id`, but dsh hands a tool a `ToolRunContext` whose session
hangs off the *agent* — `ToolRunContext.agent.session.id`
(`packages/core/tools/lib/types/index.d.ts` → `ToolExecution.agent` →
`Agent.session` → `Session.id`). The branch therefore never matched and every
write fell through to the fallback. Worse, the first fallback was the last
session the capture hook had seen, which after any `turn/end` is *another*
conversation's id — silent misattribution rather than a missing id.

The resolution order is now, most specific first:

1. `exec.agent.session.id` — the real host contract,
2. `exec.session.id` — legacy hosts and the plugin's own test harness,
3. the last session the capture hook saw,
4. `dsh-session`, reported on stderr because by definition no session can
   ever recall such a write.

The shipped harness asserts the real `ToolRunContext` shape, which is what
caught the miss.

## Config

Environment variables (read from the DSH launch environment;
`~/.dsh/.env` or `<cwd>/.env`):

| Env var | Default | Purpose |
|---|---|---|
| `CORTI_BASE_URL` | `http://127.0.0.1:5473` | Corti server base URL |
| `CORTI_APP_ID` | `shared-agent-memory` | App scope |
| `CORTI_PROJECT_ID` | `default` | Project scope |
| `CORTI_USER_ID` | `default` | User scope |
| `CORTI_AGENT_ID` | `pc-deepseek-default` | This agent's name in the shared space |
A config file supplies the same five connection keys when the matching
environment variable is absent. YAML is the preferred shape — each key can
carry an inline comment — with the legacy JSON file still read as a
fallback. The directory is `$DSH_HOME` (the harness data directory the
plugin runs under), falling back to `~/.dsh` when `DSH_HOME` is unset, so
several DSH instances on one machine keep **independent** memory identities
instead of silently sharing one file:

| File (first one that exists wins) | Format |
|---|---|
| `$DSH_HOME/corti.yaml` | YAML |
| `$DSH_HOME/corti.yml` | YAML |
| `$DSH_HOME/corti.json` | JSON (legacy) |

`corti.yaml.template` in this package is the commented starting point (its
header explains how the four ids partition memory); copy it to
`$DSH_HOME/corti.yaml` and fill in the five values.

Per-key precedence: **environment variable → config file → plugin config →
built-in default**. Keys accept both spellings (`api_url` or `baseUrl`,
`app_id` or `appId`, ...), and an empty-string value is ignored rather than
clearing a default. An absent file is silent; a file that exists but cannot
be parsed is reported on stderr and skipped — a malformed config can never
stop the plugin from loading.

DSH-side config keys (Schemastery-style, with defaults):
`recallTopK` (8), `injectTopK` (5), `recencySample` (10),
`recencyWindow` (200), `maxInjectChars` (3500), `autoCapture` (true).

## Startup recency sample

The startup catalog is deliberately **not** "the N most recent episodes".
One long session can span dozens of turns, and one agent can run many
sessions, so reading the newest N entries would let a single task occupy
every slot with fragments of itself.

Instead the plugin fetches the newest `recencyWindow` records (200) and
draws `recencySample` (10) of them **at random**, then renders one subject
line each. The block is an awareness cue, not a ranking, and says so in the
prompt: it is a random fetch over stored memory, not a summary of any
complete task, and the entries are neither relevance-ranked nor
necessarily recent. `memory_search` remains the path to a real answer.

## Build

```bash
cd src/integrations/deepseek-harness
npm install
npm run build   # tsc → dist/
```

The bundle manifest (`package.json → dsh.bundle.patch`) points at
`cordis.patch.yml`, which registers the `corti-memory` plugin id.

## See also

- [hermes-integration.md](hermes-integration.md) — the Hermes Agent
  integration this plugin mirrors
- [api.md](api.md) — the `/api/v1/memory/*` HTTP contract
- [how-memory-works.md](how-memory-works.md) — write → index → read
  pipeline and consistency model
