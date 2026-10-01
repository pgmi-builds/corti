# `corti-memory` DSH plugin — live verification report

- **Date**: 2026-10-01 (UTC+08:00)
- **Tester**: the DSH agent itself, driven from a live Web GUI session
  (`http://127.0.0.1:4999`), not from the HTTP API alone
- **Subject**: `corti-memory` **0.2.0** — the Corti memory plugin for
  DeepSeek Harness, developed in this repository under
  [`src/integrations/deepseek-harness/`](../src/integrations/deepseek-harness/)
- **Verdict**: plugin logic is **sound** — 69 of 71 harness assertions pass,
  all five integration points work, and the write path lands a searchable
  episode 16.6 s after `memory_add`. **Two defects block real recall**: one
  in the plugin (session attribution, defect [#1](#1-high--tools-write-under-a-fabricated-session-id)),
  one in the deployment (embedding provider in arrears,
  [#2](#2-high--all-retrieval-is-down-embedding-provider-in-arrears)).

This report complements [deepseek-harness-integration.md](deepseek-harness-integration.md),
which documents the design and carries the 2026-08-15 verification run.
Where the two disagree, this run is the newer evidence.

## 1. Scope and method

Three independent layers were exercised, so a failure can be attributed to
the plugin, the Corti server, or the configuration:

| Layer | How it was exercised |
|---|---|
| **Live agent surface** | The four model-facing tools (`memory_search`, `memory_add`, `memory_list`, `memory_flush`) were called by the agent from inside this session; the startup catalogue was read back out of the system prompt; auto-capture was probed from a second, independent agent session |
| **Shipped artifact** | A harness (`.scratch/corti-dsh-plugin-tests/run.mjs`) drives `dist/index.js` — the exact bundle the profile loads — against a mock DSH host and a stub `fetch`: 71 assertions over config precedence, the trivial-prompt guard, the master switch, fail-open behaviour, paging, sampling and rendering |
| **Storage and server** | Container logs, the `corti` SQLite system DB, the markdown memory root, Prometheus counters, and direct `POST`s to the four `/api/v1/memory/*` endpoints |

## 2. Test environment

| Item | Value |
|---|---|
| DSH profile | `web`, `DSH_HOME=/home/u1/workspaces/dashr/.test/home/compat` |
| Plugin install | `corti-memory@0.2.0` from `file:.../corti-memory-0.2.0.tgz`, listed as a profile bundle **and** patched in `profiles/web/cordis.patch.yml` as `config: {enabled: true}` |
| Bundle freshness | `dist/index.js` (22:41) is newer than `src/index.ts` (22:37) — the installed artifact matches the current source |
| Config file | `$DSH_HOME/corti.yaml` — `api_url: https://pc.randomhash.app/corti`, `app_id: test`, `project_id: default`, `user_id: default`, `agent_id: pc-dsh-4999-test` |
| Environment overrides | none — `CORTI_*` was unset, so the file layer applied |
| Session under test | `session-e2a258f1-c7d2-4f5e-be3b-74160b014fdc` |
| Corti server | OSS container `corti` (`m1research/corti:v0.3.2-slim`), `127.0.0.1:5473`, `~/.corti` bind-mounted. The configured `https://pc.randomhash.app/corti` base URL is a reverse proxy onto **the same instance** — both return identical payloads for the same request |
| Providers | LLM `deepseek-v4-pro` @ api.deepseek.com (healthy); embedding `qwen3.7-text-embedding` and rerank `gte-rerank-v2` @ DashScope (**in arrears**) |

## 3. Results

### 3.1 Integration points

| # | Hook | Result | Evidence |
|---|---|---|---|
| 1 | `ctx.systemPrompt.section` + startup catalogue | **PASS** | The section is registered at `order: 120` with the static banner; after the first memory existed the live system prompt carried `Recent activity (1 entries drawn at random from the 200 most recent memory records)` with the canary subject and the `(pc-dsh-4999-test)` agent tag |
| 2 | `agent/pre-step` recall injection | **PASS (unit) / DEGRADED (live)** | The harness asserts the synthetic message, its `plugin:corti-memory` source kind, the `injectTopK` call shape, and the step-1-only rule. Live, the search it depends on returns 503, so nothing is injected — the documented fail-open path ([defect #2](#2-high--all-retrieval-is-down-embedding-provider-in-arrears)) |
| 3 | `ctx.tools.register` | **PASS with one defect** | All four tools are registered and callable. `memory_list` and `memory_add` work end to end; `memory_search` fails upstream; `memory_add`/`memory_flush` mis-attribute the session ([defect #1](#1-high--tools-write-under-a-fabricated-session-id)) |
| 4 | `session/event` turn-end capture | **PASS** | A separate agent session (`e6056505-…`) produced memcell `mc_3b1d5d7beada` under its **real** session id, and a flush followed the add. Plugin-sourced messages are excluded from capture |
| 5 | Client half — Settings > General switch | **PASS** | Live slot inspection shows `settings.general.item` occupant `id: corti-memory, order: 120, active: true`; the host Config projection exposes `enabled` with `x-cordis.volatile: true`, and the auto-generated settings form is suppressed |

### 3.2 Write path — measured end to end

A canary was written through `memory_add` at **15:06:10Z** and followed
through the pipeline:

| Stage | UTC time | Δ from `memory_add` |
|---|---|---|
| `memory_add` returns `Stored in persistent memory: …` | 15:06:10.186 | 0 |
| memcell `mc_47f6a664bbb0` created (boundary detection) | 15:06:14.914 | **+4.7 s** |
| episode appended to `episodes/episode-2026-10-01.md` | 15:06:26.770 | **+16.6 s** |
| 6 foresights extracted | 15:06:31.8 | +21.6 s |
| 5 atomic facts extracted | 15:06:54.7 | +44.5 s |

The extraction produced a correct subject (*"Corti DSH Plugin Regression
Canary CANARY-2306Z Memory Path Test on October 1, 2026"*), a faithful
summary, and both sender ids `[default, pc-dsh-4999-test]`. This is
materially faster than the 2026-08-15 report's "~30 s to minutes" — the
flush-after-add patch is doing its job.

### 3.3 Harness suite

```
69 passed, 2 failed, 71 total
```

Both failures are the same defect and are asserted deliberately:

```
FAIL  real ToolRunContext (exec.agent.session.id) is honoured
      <- add sent session_id=dsh-session for {"agent":{"session":{"id":"session-real-abc"}}}
FAIL  memory_flush honours exec.agent.session.id
      <- flushed session dsh-session
```

Highlights of what passes: config precedence in all four layers, empty-string
values ignored, a malformed `corti.yaml` reported on stderr without preventing
load, the trivial-prompt guard and its exact 4-character boundary, the
volatile master switch (including live re-enable and fail-open on a junk
value), every fail-open path with the server unreachable, `/get` paging at
`page_size=100`, the random-within-window draw, and `recencySample: 0`
falling back to 10.

## 4. Findings

### 1. HIGH — Tools write under a fabricated session id

**What happens.** `memory_add` and `memory_flush` do not see the calling
session. In this live session `memory_flush` returned
`flushed session dsh-session`, and the canary episode was persisted with
`session_id: dsh-session` — a conversation that does not exist. The
turn-end capture hook, running in the *same* process moments later, recorded
the *correct* id.

**Root cause is a property-path mismatch.** `resolveSessionId` reads
`exec.session.id`:

```ts
const execSession = (exec as { session?: { id?: unknown } } | undefined)?.session?.id;
```

but the host hands a tool its `ToolRunContext`, which has **no `session`
field**. The session hangs off the agent:

- `packages/core/tools/lib/types/index.d.ts:305` — `ToolRunContext extends ToolExecution`
- `.../index.d.ts:229` — `ToolExecution.agent?: Agent`
- `packages/core/agent/lib/types/runtime-types.d.ts:143` — `Agent.session: Session`
- `packages/core/session/lib/types/types.d.ts:65` — `Session.id: SessionId`
- `packages/core/tools/src/index.ts:1580` — the registry calls `tool.execute(exec.arguments, exec)`

So the first branch can never match, and every tool write falls through to
`lastSeenSessionId ?? "dsh-session"`.

**Why this is worse than a missing id.** `lastSeenSessionId` is updated on
*every* `turn/end` in the process. After any session ends a turn, the next
session's tool writes are attributed to **that other real conversation** —
silent cross-session misattribution, not just a placeholder.

**Evidence in the environment** — the synthetic rows are visible in
`conversation_status`, including today's older `dsh-tool-2026-10-01`
fabrication, which shows both generations of the bug:

```
id    app_id               session_id
6285  test                 e6056505-fd9d-4ebd-b214-f81e3d1552eb   <- capture hook: correct
6284  test                 dsh-session                            <- memory_add: fabricated
6283  shared-agent-memory  dsh-tool-2026-10-01                     <- previous scheme
```

This is exactly the defect the "Session attribution — fixed 2026-10-01"
section of [deepseek-harness-integration.md](deepseek-harness-integration.md)
declares closed. The fix landed for `memory_flush`'s *intent* but not for the
host contract, so the fallback still wins.

**Suggested fix** (keep both shapes; the legacy one keeps the harness and
older hosts working):

```ts
const resolveSessionId = (exec: unknown): string => {
  const e = exec as
    | { session?: { id?: unknown }; agent?: { session?: { id?: unknown } } }
    | undefined;
  const fromAgent = e?.agent?.session?.id;   // ToolRunContext (real host)
  if (typeof fromAgent === "string" && fromAgent !== "") return fromAgent;
  const direct = e?.session?.id;             // legacy / test shape
  if (typeof direct === "string" && direct !== "") return direct;
  return lastSeenSessionId ?? "dsh-session";
};
```

Consider also making the placeholder loud: a write that falls back to
`dsh-session` is by definition unattributable, so it is worth a one-line
`console.error` — and it is the kind of thing a harness assertion should
guard, since both the 2026-08-15 and 2026-10-01 reports missed it.

### 2. HIGH — All retrieval is down: embedding provider in arrears

Every `hybrid` and `vector` search returns **503**, from an upstream payment
failure:

```
POST /api/v1/memory/search -> 503 EXTERNAL_SERVICE_UNAVAILABLE
  Error code: 400 ... 'type': 'Arrearage' ...
  "Access denied, please make sure your account is in good standing."
  https://help.aliyun.com/zh/model-studio/error-code#overdue-payment
```

The embedding provider is DashScope (`qwen3.7-text-embedding`), and the
rerank provider (`gte-rerank-v2`) is on the same account. Consequences,
confirmed live:

- `memory_search` returns `corti search failed: {…raw provider JSON…}` to the model.
- Per-prompt recall injection is skipped on every turn (`if (!res.ok) return decision`).
- Recall is therefore **zero** for a correctly configured agent, regardless of
  what is stored.
- Episodes are still written to markdown but never vectorised — the server
  logs `cascade_embedding_unavailable_keeping_rows_unvectorised`
  (`cooldown_seconds=300`, `entries=69`, `texts=138`) and keeps the rows.
- Counters: `search` 13×503 / 2×200, `get` 263×200. The 200s were BM25
  (`method: keyword`) probes made by this test.

**This is a deployment defect, not a plugin defect** — but note the
asymmetry it exposes, which the plugin should defend against (finding #3):
the same data *is* reachable over BM25 —

```
POST search {method: "keyword", query: "canary regression"} -> 200, 1 hit
  Corti DSH Plugin Regression Canary CANARY-2306Z Memory Path Test on Oc…
```

**Action**: settle the Alibaba Cloud DashScope account (or repoint
`[embedding]` / `[rerank]` at a funded provider). Until then, treat recall
as unavailable.

### 3. MEDIUM — No fallback when the vector leg fails

`CortiClient.search()` hardcodes `method: "hybrid"` and the tools never
override it, so a single provider outage takes down recall entirely even
though BM25 answers the same query in 9 ms. A one-shot downgrade preserves
degraded-but-useful recall:

```ts
async search(query, opts = {}) {
  const first = await this.post("/api/v1/memory/search", {
    query, method: opts.method ?? "hybrid", top_k: opts.topK ?? 8, ...this.scope(),
  });
  if (first.ok || first.status !== 503) return first;
  return this.post("/api/v1/memory/search", {
    query, method: "keyword", top_k: opts.topK ?? 8, ...this.scope(),
  });
}
```

Worth exposing as a config flag (`searchFallback: "keyword" | "none"`), since
keyword-only recall changes the relevance characteristics noticeably.

### 4. LOW — Raw upstream error reaches the model

`memory_search` returns the provider's JSON verbatim, including a payment
URL, request ids, and the Chinese-language gateway text. The model will
happily relay that to a user. A short classified message — *"Corti retrieval
is unavailable (embedding provider rejected the request); stored memories are
intact and will be searchable once the provider responds"* — plus the
technical detail behind a debug flag would be kinder and less
prompt-injectable.

### 5. LOW — Config drift: the plugin points at an empty memory space

`corti.yaml` sets `app_id: test`. At the start of this run that space held
**0** episodes, while the 7,030 memcells this harness has accumulated all
live under `app_id: shared-agent-memory`. So even with a healthy embedding
provider, this agent would recall nothing from its own history. Confirm
whether the switch to `test` is deliberate; if it is, consider documenting
the break in `corti.yaml` itself, because a silent app_id change looks
exactly like total memory loss.

### 6. INFO — Older findings still hold

The 2026-08-15 report's residual note stands: ranking can favour denser older
episodes. It could not be re-measured here, since no relevance-ranked search
completed during this run.

## 5. Reproducing

```bash
# Harness: 71 assertions against the shipped bundle, no network needed
node .scratch/corti-dsh-plugin-tests/run.mjs      # expect 69 passed, 2 failed (defect #1)

# Retrieval health (expect 503 while DashScope is in arrears)
curl -s -X POST http://127.0.0.1:5473/api/v1/memory/search \
  -H 'Content-Type: application/json' \
  -d '{"app_id":"test","project_id":"default","user_id":"default","query":"canary","method":"hybrid","top_k":3}'

# Same corpus over BM25 (expect 200)
#   ... "method":"keyword" ...

# Request counters
curl -s http://127.0.0.1:5473/metrics | grep corti_http_requests_total
```

The harness lives under `.scratch/` (git-ignored) because it is verification
scaffolding, not product code. If the plugin should carry its own tests, that
suite is a ready starting point: move it into
`src/integrations/deepseek-harness/` and add an `npm test` script.

## 6. Summary

The plugin is well built: every documented guard behaved as documented, every
failure path failed open instead of throwing, the startup catalogue is
genuinely random rather than "the newest ten", and the write path is roughly
twice as fast as the previous report claimed. Two things stand between it and
usefulness — a one-line session-id lookup that reads a field the host does not
send, and an unfunded embedding account. Fix the first in the plugin, fund
the second in the deployment, and consider the keyword fallback so that the
next provider outage degrades recall instead of erasing it.

## 7. Resolution (2026-10-01, same day)

All three findings below were fixed, deployed and re-verified after this
report was written.

| Finding | Fix | Verification |
|---|---|---|
| #1 session attribution | `resolveSessionId` now reads `exec.agent.session.id` first (the real `ToolRunContext` shape), then `exec.session.id`, then the capture hook's last session | harness assertion "real ToolRunContext (exec.agent.session.id) is honoured": **PASS** (was FAIL); the "fabricated session id" failure is gone |
| #2 retrieval 503 while the embedding account is in arrears | the read path degrades instead of failing (see below) | `POST /search` with `method: hybrid` now returns **200 with BM25 hits in 33 ms** (was 503); `vector` returns 200 in 470 ms on the first call, then 3 ms inside the cooldown |
| #3 no fallback when the vector leg fails | server-side degradation **and** a one-shot `keyword` retry in the plugin | harness "falls back to keyword when hybrid returns 503": **PASS** |

### Degradation, not failure

The premise of finding #2 was wrong at the design level: recall has three
independent legs (keyword/BM25, vector, rerank) and the keyword leg needs no
provider at all. A dead embedder or cross-encoder is now a **degradation**:

- `SearchManager._embed_query` catches `EmbeddingServiceError` and returns
  `[]`, logging `search_embedding_unavailable_degrading_to_keyword` and
  opening a 300 s cooldown gate so the provider timeout is paid once, not per
  query.
- `HYBRID` runs the sparse leg alone; `VECTOR` falls back to lexical recall
  (`vector_search_degraded_to_keyword`); `AGENTIC` degrades to the HYBRID
  hierarchy when the query cannot be embedded.
- The cross-encoder callback keeps the **first-stage order** when rerank
  fails (`rerank_unavailable_keeping_first_stage_order`) — the candidates
  already carry BM25/cosine/RRF scores, so the answer is worse, not absent.
- Only a *configuration* gap stays fatal, and only for the method that exists
  for that component (`method="vector"` with no `[embedding]` configured).
- The plugin retries once as `keyword` on a 503 (`client.search`), so an
  older deployment degrades too.
- Tool output is now classified (`describeFailure`) instead of echoing the
  provider's JSON, which addresses finding #4 as a side effect.

Regression coverage: `tests/unit/test_memory/test_search/test_manager.py`
(dead embedder → keyword hits for HYBRID/VECTOR/AGENTIC, cooldown after one
attempt, rerank failure keeps first-stage order) and harness section 5b.

### Finding #5 was the mild version of a worse bug

`app_id: test` did hold zero episodes — but not because of config drift. The
markdown was there; **the rows could never be indexed**. `episode.id` is
`<owner_id>_<entry_id>`, and an `entry_id` is unique only inside one memory
space (every space starts its daily sequence at 1), yet `id` alone was the
primary key. The `test` space's rows therefore collided with
`shared-agent-memory`'s and `ON CONFLICT (id) DO UPDATE` let one space
**overwrite the other's rows** — silently, since the cascade only reports a
count.

Fixed by making the index key space-aware:

- `episode` / `atomic_fact` / `foresight` now have
  `PRIMARY KEY (app_id, project_id, id)` (live migration behind a schema
  dump);
- `PgRepoBase.upsert` accepts a composite `by` and keeps every key column out
  of the `DO UPDATE` clause;
- `BaseDailyLogHandler.db_upsert_key = ("app_id", "project_id", "id")`.

Re-indexing the `test` space produced its own 2 episodes / 39 atomic facts /
21 foresights while `shared-agent-memory` kept its 6,424 / 166,963 / 44,979,
and markdown still matches Postgres exactly (`md_only = 0`, `pg_only = 0`) in
every space. A `hybrid` search for the canary inside `test` now returns it
with no embedding provider configured.

## See also

- [deepseek-harness-integration.md](deepseek-harness-integration.md) — the plugin's design, hooks, and config reference
- [hermes-integration.md](hermes-integration.md) — the sibling integration this plugin mirrors
- [api.md](api.md) — the `/api/v1/memory/*` HTTP contract
- [how-memory-works.md](how-memory-works.md) — the write → index → read pipeline
