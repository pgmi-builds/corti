# 2026-10-02 · `832d49e` — Corti memory plugin live acceptance run (iteration 2)

Second live iteration of the "memory plug-in" test series. Iteration 1 was
filed as the runtime-verification section of
[deepseek-harness-integration.md](../deepseek-harness-integration.md); this
report is the follow-up run, executed again **from inside a real DSH agent
session, through the plugin's own model-facing tools** — not through raw HTTP.

| Field | Value |
|---|---|
| Run date | 2026-10-02, 00:37–00:45 (+08:00) |
| Repository commit | `832d49eb431ded898af8c4b742daba453bfc84c7` (`832d49e`, 2026-10-02 00:32:45 +08) |
| Working tree | **dirty** — 15 modified files + 1 untracked (runtime-context refactor, Hermes refactor, DSH client) |
| Repository version | `corti 0.3.1` ([pyproject.toml](../../pyproject.toml)) |
| Plugin bundle | `corti-memory 0.2.0`; `dist/index.js` rebuilt 2026-10-02 00:36:45 from `src/index.ts` 00:31:57 |
| Plugin instance under test | `~/.dsh/profiles/web/node_modules/corti-memory`, config `~/.dsh/corti.yaml` |
| Live session id | `session-7de29e9b-e87d-41b7-a144-348c2e963d7c` |
| Scope | `app_id=test`, `project_id=default`, `user_id=default`, `agent_id=pc-dsh-4999-test` |
| Backend | Docker container `corti` (`m1research/corti:v0.3.2-slim`, `892363e4f145`), `127.0.0.1:5473`; plugin base URL `https://pc.randomhash.app/corti` (reverse proxy to the same backend) |
| Memory root | `~/.corti/test/default_project/users/default/` |

---

## 1. Results at a glance

| # | Check | Result |
|---|---|---|
| 1 | Plugin loaded and enabled (`corti-memory.enabled = true` in live DSH config) | PASS |
| 2 | `memory_add` accepts and acks | PASS (10–28 ms) |
| 3 | `memory_list` returns episodes newest-first | PASS (8 ms) |
| 4 | `memory_flush` resolves the **live session id** (iteration-1 defect) | **PASS — regression fixed** |
| 5 | Extraction → markdown truth file | PASS (canary in episode + atomic facts + foresights) |
| 6 | Extraction → retrieval index | PASS (eventually, ~1–2 min) |
| 7 | `memory_search` keyword recall of ordinary phrasing | PASS (5–16 ms) |
| 8 | `memory_search` recall of a hyphenated identifier | **FAIL** (reproducible, with control) |
| 9 | Paraphrase / semantic recall | **FAIL** — blocked by #10 |
| 10 | Embedding leg healthy (dense vectors populated) | **FAIL** — `degraded: ["embedding"]`, 0/6 rows carry a vector |
| 11 | Degradation surfaced to the model | **FAIL** — plugin drops `degraded[]`; empty and broken look identical |
| 12 | One `memory_add` ⇒ one durable episode | **PARTIAL** — 3 rapid adds ⇒ 1 episode |
| 13 | Backend availability during the run | **DEGRADED** — container crash-loop + ~1–2 min outage |

---

## 2. Tool-surface matrix (raw results)

All calls were made from the live agent session through the registered tools.
Timings are wall-clock for the tool call.

### 2.1 Writes

| Call | Text | Latency | Ack |
|---|---|---|---|
| `memory_add` A | Version-provenance canary `CORTI-ITER-20261002-7Q4M` (commit/version/scope) | 28 ms | `Stored in persistent memory: …` |
| `memory_add` B | Report-naming convention (`docs/test_reports/` + ISO date + short commit) | 10 ms | same |
| `memory_add` C | Negative-control sentinel ("Zanzibar lattice protocol") | 11 ms | same |
| `memory_add` D | Extraction probe, marker `sapphirelantern` | 25 ms | same |
| `memory_add` E | Extraction probe, marker `cobaltthistle` | 10 ms | same |
| `memory_add` F | Backend-recovery probe | 310 ms | same |

`memory_add` acks in tens of milliseconds because the server only persists the
turn into `unprocessed_buffer` and enqueues extraction — see
[how-memory-works.md](../how-memory-works.md).

### 2.2 Search

| Query | Hits | Notes |
|---|---|---|
| `CORTI-ITER-20261002-7Q4M` | **0** | canonical form of the stored canary itself |
| `CORTI ITER 20261002 7Q4M` | **1** | same tokens, hyphens replaced by spaces |
| `sapphirelantern` | 1 | single-word rare marker |
| `cobaltthistle` | 1 | single-word rare marker |
| `sapphirelantern cobaltthistle` | 1 | both markers, one merged episode |
| `Corti DSH plugin canary` | 1 | hits the 2026-10-01 episode; ordinary phrasing works |
| `capital of Peru altitude` | 0 | true negative |
| `what naming rule keeps repeated test report runs ordered` | 0 | paraphrase of B — but B was never extracted (see §4.5), so inconclusive as a semantic test |
| `how should report files be named so repeated test runs stay ordered?` | 0 | same |

Method comparison for `CORTI-ITER-20261002-7Q4M` (each `top_k=5`):
`hybrid` 0, `keyword` 0, `vector` 0. The same query space-separated returns a
hit under `hybrid`, so this is a tokenization defect, not a method defect.

### 2.3 Timing of `add` → searchable

* `add` A at 00:37:13 → extraction landed on disk by 00:37:47 (~35 s);
  first search hit for a **wildcardable** query observed at 00:39.
* `add` D/E at 00:39:19 → searchable at 00:39:55 (~36 s).
* Recall is eventually consistent; `memory_add`'s ack is not a durability
  guarantee (see §4.5).

---

## 3. Finding F1 — `memory_flush` session-id fallback is fixed (iteration-1 regression closed)

Iteration 1 reported that `memory_flush` returned the placeholder
`dsh-session` instead of the real session id (root cause: `ToolRunContext` has
no `session` field; the correct path is `exec.agent.session.id`). In this run:

```
memory_flush → "flushed session session-7de29e9b-e87d-41b7-a144-348c2e963d7c"
```

and the extracted episode carries the same id:

```yaml
# ~/.corti/test/default_project/users/default/episodes/episode-2026-10-02.md
## ep_20261002_00000002
**session_id**: session-7de29e9b-e87d-41b7-a144-348c2e963d7c
```

Attribution therefore survives `add → flush → extraction`. **PASS.**

---

## 4. Findings

### 4.1 F2 — the embedding leg is degraded; recall is keyword-only

Evidence, in order of strength:

1. `POST /api/v1/memory/search` returns a **top-level** `degraded` field:

   ```
   top keys: ['request_id', 'data', 'degraded']
   degraded: ["embedding"]
   ```

   The server substitutes keyword recall when the query cannot be embedded
   (`src/corti/memory/search/manager.py:411`,
   `vector_search_degraded_to_keyword`, `mark_degraded("embedding")`).
2. Direct index inspection — every row in the test scope has a NULL vector:

   ```sql
   select count(*) total, count(vector) with_vec, count(subject_vector) with_svec
   from episode where app_id='test';
   --  6 | 0 | 0
   ```
3. `method: "vector"` still returns a hit (score `0.2`) but reports
   `degraded: ["embedding"]` — i.e. it silently answered lexically.

Consequence: dense/paraphrase recall cannot work in this deployment. The
2026-10-01 iteration already traced this to the embedding provider account
being in arrears; it is **still true** in this iteration. This is a
configuration/accounting problem, not a plugin defect — but it is invisible
from inside the agent (§4.2).

### 4.2 F3 — the plugin discards `degraded[]`, so "empty" and "broken" are indistinguishable

`CortiClient.post()` unwraps the envelope and keeps only `data`:

```ts
// src/integrations/deepseek-harness/src/client.ts
const envelope = parsed as { data?: unknown } | null;
return { ok: true, status: res.status, data: envelope?.data as T };
```

`Envelope<T>` has no `degraded` member, so the top-level `degraded: ["embedding"]`
never reaches the tool. `memory_search` renders `"(no memories found)"` for
"the store has nothing relevant" and for "recall is running degraded" alike.
A model reading that string has no signal that recall quality is impaired.

### 4.3 F4 — hyphenated identifiers are not recallable (reproducible)

The episode's own index tokens are normalized to space-separated words
(`corti iter 20261002 7q4m`), while the query tokenizer keeps the hyphen form
and adds a fragment:

```sql
select to_tsvector('simple','CORTI-ITER-20261002-7Q4M'),
       plainto_tsquery('simple','CORTI-ITER-20261002-7Q4M');
-- '-7':5 '20261002':4 'corti':2 'corti-iter':1 'iter':3 'q4m':6
-- 'corti-iter' & 'corti' & 'iter' & '20261002' & '-7' & 'q4m'
```

`plainto_tsquery` requires **all** of those tokens, including `corti-iter` and
`-7`, neither of which exists in `episode_tokens_tsv`. The BM25 leg therefore
returns nothing for the exact identifier that is stored, and — with the
embedding leg also down (§4.1) — the hybrid fusion has no second chance. The
control (`CORTI ITER 20261002 7Q4M`) returns the episode, proving the data is
present and indexed.

Impact: commit hashes, ticket ids, nonces, and versioned filenames are exactly
the kind of durable fact an agent stores and later greps for. They are the
worst case for this tokenizer mismatch.

### 4.4 F5 — the model cannot tell "no results" from "degraded recall"

See §4.2. Suggested fix belongs to the plugin (surface `degraded[]` in the tool
result) and optionally to the server (document the field in
[api.md](../api.md) as part of the search contract).

### 4.5 F6 — `memory_add` acks are not episode-granular

| Adds | Issued | Materialized in markdown |
|---|---|---|
| A + B + C | 00:37:13–00:37:13 (≈9 s window, one flush each) | **A only** (`ep_20261002_00000002`) |
| D + E | 00:39:19, ~1 s apart | both, **merged into one episode** whose subject names both markers |
| F | recovery window | one episode |

`grep` for B's marker (`docs/test_reports`) and C's marker (`Zanzibar`) across
`~/.corti/test/` returns nothing; neither ever became an episode, an atomic
fact, or a foresight. Meanwhile D+E were coalesced into a single episode. So
per-call `Stored in persistent memory` is an ack of *acceptance*, and the
episode granularity downstream is decided by boundary detection, not by the
number of calls. This matches the documented "eventually consistent" contract,
but the three-calls-one-episode outcome is worth a deliberate probe next
iteration (adds 30 s apart vs. batched) to separate "coalesced" from "dropped".

### 4.6 F7 — backend instability during the run (environmental)

Timeline, reconstructed from `docker logs -t` and host probes:

| Time (+08) | Event |
|---|---|
| 00:38:25 | Container server log: `ImportError: cannot import name 'PrefetchResponse' from 'corti.memory.runtime_context.dto'` — a **mixed install**: the installed package's `__init__.py` imported `PrefetchResponse` while its `dto.py` already exposed `PrefetchData`/`PrefetchRequest` (the host tree is consistent) |
| 00:38:27 – 00:38:52 | Entrypoint restarts; first inspect showed `Restarting (1)`, `RestartCount=7` |
| ~00:47 | `127.0.0.1:5473` refused connections (`http=000`); `https://pc.randomhash.app/corti/health` returned an empty body (the proxy fronts the same backend) |
| 00:52 | Container recreated: `RestartCount=0`, code inside the container consistent, port `5473/tcp -> 0.0.0.0:5473` serving normally |

The published image tag itself is unchanged (`m1research/corti:v0.3.2-slim`,
created 2026-08-03, same image id before and after), so what varied is the
**install layer inside the running container**, not the image. The working tree
being refactored at 00:23–00:35 (runtime-context files, `dist/*.js` rebuilt at
00:36:45) makes a mid-refactor install the likely cause. Note that the
`corti` container's installed package was **newer than the published image**,
so the deployment is not reproducible from the tag alone.

The outage window was short enough that the tool surface recovered before a
dedicated "backend down" probe could be captured: `memory_add` F — issued while
host `curl` was still failing — succeeded in 310 ms (≈10× the healthy latency)
and became a durable memory. That is a good sign for retry behaviour, but it
means resilience-under-outage is **not yet verified**; it should be a
first-class scenario next iteration.

---

## 5. Storage-side verification

| Artifact | Location | Observed |
|---|---|---|
| Episode (truth) | `~/.corti/test/default_project/users/default/episodes/episode-2026-10-02.md` | `ep_20261002_00000001…4`, marker-comment entry format |
| Atomic facts | `.atomic_facts/atomic_fact-2026-10-02.md` | canary + markers present |
| Foresights | `.foresights/foresight-2026-10-02.md` | canary + markers present |
| Retrieval index | Postgres `corti.episode` | 6 rows for `app_id=test`; **0 with vectors** |
| Session state | SQLite `~/.corti/.index/sqlite/system.db` | `unprocessed_buffer` drained to 0 after the last flush |

---

## 6. Recommendations for iteration 3

1. **Restore the embedding provider** (accounting/quota) and assert
   `degraded == []` plus `count(vector) > 0` as a test precondition. Without
   this, every semantic-recall claim is untestable.
2. **Make degradation visible**: propagate the envelope's `degraded[]` through
   `CortiClient.post()` into `memory_search` output (e.g.
   `recall degraded: embedding`) — an empty result must not be silently
   indistinguishable from an impaired one.
3. **Fix identifier recall**: either normalize queries the same way the
   extractor normalizes content (strip hyphens / index the concatenated form),
   or make the query an OR/`websearch_to_tsquery` rather than an all-tokens-AND,
   or lower `min_score` for pure-identifier queries. Add a regression fixture
   whose query is identical to a stored identifier.
4. **Make the add contract explicit** in the plugin's tool description and the
   integration doc: acks are acceptance-only; episode granularity is decided
   downstream. Add a probe that distinguishes coalescing from dropping.
5. **Test under outage**: stop the container mid-run and record the
   `describeFailure` text of all four tools, plus recovery behaviour; today's
   run only produced accidental evidence.
6. **Pin the test environment**: the container's installed package differed
   from the published image tag during this run (and crash-looped on an
   `ImportError` from a mid-refactor install). A run report should record the
   installed package build, not just the image tag.
7. **Consider pinning the plugin bundle build** alongside the repo commit: the
   plugin under test was `dist/index.js` built at 00:36:45 from a dirty tree,
   while the repo commit reported is `832d49e` (00:32:45). Date+commit alone is
   not enough to reproduce a run in this iteration model.

---

## 7. Artifacts left behind by this run

Scoped to `app_id=test`, `user_id=default` (all are ordinary memories, no test
scaffolding outside Corti's own store):

* `CORTI-ITER-20261002-7Q4M` — version-provenance canary
* `sapphirelantern` / `cobaltthistle` — extraction-coalescing markers
* "Backend-down probe…" — recovery-window probe
* Report-naming convention and "Zanzibar" sentinel — **accepted but never
  extracted** (see §4.5)

To remove them, use the store's normal cleanup path
([memory-db-cleanup.md](../memory-db-cleanup.md)); markdown remains the source
of truth, so deleting the entries in `episode-2026-10-02.md` and its sibling
fact/foresight files is what actually removes them.

---

## 8. Method notes

* Every tool result in §2 was produced by calling the live registered tool
  (`memory_add` / `memory_search` / `memory_list` / `memory_flush`) from inside
  the agent session; SQL, `curl`, and `docker` were used only to *verify* the
  claims independently of the plugin.
* Latencies are single-sample wall-clock measurements, not benchmarks.
* The paraphrase probes in §2.2/§4.1 are reported as **inconclusive where the
  target memory was never extracted**; the embedding-leg failure stands on the
  `degraded[]` + `count(vector)=0` evidence, which does not depend on them.
