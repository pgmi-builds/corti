---
status: proposed
---

# The recency digest is aggregated per task, not per extracted memory cell

The "recent activity" block that every integration injects into the system
prompt at session startup must be built at **task granularity** — one line per
unit of work, newest first — and a single agent session must never be able to
monopolise the list. Today it is built from the N newest extracted *memcells*,
so a long session that produced a hundred cells occupies a hundred slots. This
record captures the measurements behind that statement, why the *ingestion*
pipeline is this fine **on purpose**, and the options for fixing **only the
read path**.

## Context

### What the ingestion pipeline actually stores

A message does not become memory directly. The path is:

```
client turn end ──▶ POST /add ──▶ unprocessed_buffer (SQLite)
                                        │
                 POST /flush ───────────┘
                      │
                      ▼
             LLM boundary detector  →  N × MemCell
                      │
                      ├─▶ memcell ledger (SQLite)
                      ├─▶ episode / atomic_fact / foresight  (PostgreSQL)
                      └─▶ markdown daily log  (source of truth)
```

The unit that lands in the database is the **memcell**: a slice of the
conversation chosen by an LLM boundary detector
(`corti.memory.extract.pipeline`, `everalgo.boundary.detect_boundaries`), not a
raw message and not a session.

The DeepSeek Harness integration **forces a boundary on every turn** — it calls
`/flush` immediately after each `add`, with the comment *"Force a final
extraction boundary for the turn so memories become searchable without waiting
for Corti's idle timeout"*
(`src/integrations/deepseek-harness/src/index.ts`). So in practice one memcell
covers roughly one agent turn.

### Measurements (live instance, 2026-10-01, app `shared-agent-memory`, project `default`)

Memcell ledger (`~/.corti/.index/sqlite/system.db`, table `memcell`):

| Metric | Value |
|---|---|
| memcells | 7,195 |
| distinct `session_id` | 2,196 |
| average memcells per session | 3.28 |
| memcell size, median | 3 messages |
| memcell size, mean | 5.12 messages |
| memcell size, p90 / max | 13 / 49 messages |
| share of memcells with ≤ 2 messages | 49.5% |
| cells inside sessions holding > 20 cells | 1,594 (22.2%) |
| largest session | 123 cells over 5 days (`session-9ef55897…`, 2026-09-24 → 09-29) |

Episode index (PostgreSQL, active rows only):

| Metric | Value |
|---|---|
| episodes | 6,605 |
| distinct `session_id` | 2,168 |
| average episodes per session | 3.05 |
| episodes under the synthetic `dsh-tool-*` session namespace | 728 (11.0%) in 44 buckets |

### How the digest is really built

Both integrations call `POST /api/v1/memory/get` with `memory_type=episode`,
`sort_by=timestamp`, `sort_order=desc`, `page_size=20`, then render one line per
returned episode. The DeepSeek Harness plugin labels the result
*"20 most recent sessions"* (`src/integrations/deepseek-harness/src/index.ts`);
the Hermes integration does the same (`src/integrations/hermes/_formatting.py`).
**That label is wrong — the rows are memcells, not sessions.**

Two consequences, both measured:

**1. One session can take almost the whole list.** Scanning every 20-row
sliding window over the episode table, the worst window contains **19 of 20
rows from a single session, with only 2 distinct sessions present**
(2026-09-15 05:07). 22.2% of all memcells already live inside sessions that
hold more than 20 cells, so this is the steady state, not an edge case.

**2. The window collapses to a few hours, not a few days.** The 20-row window
as of 2026-10-01 01:50 spans only 7 hours (2026-09-30 18:25 → 2026-10-01 01:50)
across 6 distinct sessions, with this agent distribution:
`pc-deepseek-default` 10, `pc-hermes-default` 6, `pc-hermes-code` 2,
`pc-hermes-research` 1, unnamed 1. A digest labelled "recent activity" that in
fact describes a single evening is not usable as recency context.

**3. The memory-write channel feeds itself.** The plugin mirrors `memory_add`
into Corti under a **fabricated** session id, `dsh-tool-<YYYY-MM-DD>`
(`src/integrations/deepseek-harness/src/index.ts`), because `/flush` requires a
session id. Every `memory_add` therefore becomes an episode under a bucket that
belongs to no conversation, and that episode is injected back as "recent
activity" in the next session. 44 such buckets hold 728 episodes — 11.0% of the
entire episode corpus. Its cells are exactly two messages (one user, one
assistant) on average, i.e. the shape of a memory write, not of a conversation.

### Session identity is already recorded, everywhere

The aggregation problem is *not* an identity problem. `session_id` is a
first-class column at every layer:

| Layer | `session_id` | Notes |
|---|---|---|
| SQLite `unprocessed_buffer` | yes | buffer is keyed by session |
| SQLite `memcell` | yes | plus `message_ids_json`, `sender_ids_json` |
| SQLite `conversation_status` | yes | **plus `last_message_ts` / `last_memcell_ts`** — 2,209 rows, a ready-made per-session activity ledger |
| PostgreSQL `episode` / `atomic_fact` / `foresight` | yes | plain columns |
| markdown entry body | yes | `**session_id**: …` on every entry |

So grouping is a plain `GROUP BY session_id`. No heuristics ("has an hour
passed?", "are the last 100 rows from another session?") are needed, and no
upstream change is required to obtain the identity.

## Problem

Stated as the operator put it: recency should reflect **what tasks an agent did
today and in the preceding days** — one line per task. Running two or three long
tasks in a day is expected; what must not happen is that *the dozens of
fragments of one task fill the whole recency list*.

Failure mode to eliminate: a single session (or a single fabricated bucket)
supplying a large fraction of the injected lines, and the digest covering hours
instead of days.

## Why ingestion must stay fine-grained

The per-turn flush exists so that memory is searchable immediately rather than
after an idle timeout. Fine granularity is **correct for retrieval**: small
cells make `memory_search` precise and high-recall. It is **wrong for a recency
digest**, whose whole purpose is to compress.

Therefore: do not coarsen the write path. Ingestion granularity and digest
granularity are separate concerns. The fix belongs in the read path that builds
the recency block.

## Constraints

1. **No "final turn" is available.** A user may close the window, or return
   hours later and resume the *same* session id. Any design that depends on a
   session-end event is unreliable. A digest computed at session startup from
   whatever is already indexed is fine; a digest that waits for "the session to
   end" is not.
2. **The read path is on the startup critical path** and must stay cheap — no
   per-startup LLM call.
3. **Three independent clients** consume this (DeepSeek Harness / TypeScript,
   Hermes / Python, Claude Code / hooks), plus the CLI and the `memory_list`
   tool. Aggregation duplicated in each client will drift; a server-side
   solution fixes all consumers at once.
4. **`POST /api/v1/memory/get` keeps its contract.** It is a paginated listing
   with `extra="forbid"` and `page_size ≤ 100`, documented as a mirror of
   `/search` minus `score`. Widening it into an aggregation endpoint would
   muddy a published contract.
5. **A session is not a task.** `session-9ef55897…` spans 5 days and 123 cells.
   "One line per session" is therefore also wrong; a session must be able to
   yield more than one line when it genuinely contains more than one task.

## Options

| # | Approach | LLM cost | Effect | Risk |
|---|---|---|---|---|
| ① | **Representative cell per session** — pick one cell (newest, or longest) as the session's label | none | One line per session; prevents monopoly; but a multi-day or multi-topic session collapses to a single stale-sounding label | Low |
| ② | **Local task clustering** — group cells by `session_id`, split each session into runs at a gap threshold (e.g. 90 min), one line per run; cap runs per session and per agent | none | Task-level granularity with no model call; deterministic and unit-testable | Gap threshold is a heuristic; a run's label is still one cell's subject |
| ③ | **LLM-generated session summary** — a per-session markdown artifact (like `user.md`), regenerated incrementally every N cells | one LLM call per regeneration, per session | Genuinely "one concept per session"; best labels | New markdown type + cascade handler + regeneration policy; needs a fallback for the not-yet-summarised tail |

② and ③ compose: ② establishes the grouping that bounds monopoly, ③ only
upgrades the label quality. ② is a prerequisite for ③ because a session that
spans several days and several topics still needs to be split.

An initial simulation of ② on the live corpus (90-minute gap), taking the
newest 20 runs: the window reaches back 27.5 hours instead of 7, covers 13
distinct sessions instead of 6, and no run merges more than 7 cells. Adding the
caps (≤ 2 runs per session, ≤ 8 runs per agent) yields 20 lines drawn from
**15 distinct sessions across 7 agents, spanning 3 calendar days** — against 6
sessions and 7 hours today.

Implementation note: the SQLite `conversation_status` table already maintains
`last_message_ts` / `last_memcell_ts` per `(app, project, session, track)`, so a
digest endpoint has a cheap, already-maintained per-session activity index
available without walking the markdown tree.

## Existing precedent inside this repository

The Claude Code integration already carries a client-side session digest, and
it is worth recording because it shows both what works and what cannot be
reused.

- `src/integrations/claude-code/hooks/scripts/session-summary.js` — a
  `SessionEnd` hook that appends a row to `data/sessions.jsonl`:
  `{sessionId, groupId, summary, turnCount, reason, startTime, endTime,
  timestamp}`. The file header is explicit: *"No AI summarization - just
  extracts key info from transcript"*. The `summary` field is the session's
  **first user prompt**, truncated to 200 characters.
- `src/integrations/claude-code/hooks/scripts/session-context.js` — a
  `SessionStart` hook that renders **the single most recent** summary for the
  current group, plus `RECENT_MEMORY_COUNT = 5` memories fetched with
  `PAGE_SIZE = 100`.

What this gets right, and is worth keeping: the digest is keyed by session,
it is cheap (no model call), and it is rendered once at startup rather than on
every prompt.

What cannot be reused as-is:

1. It depends on a `SessionEnd` event, which constraint 1 rules out for the
   DeepSeek Harness and Hermes runtimes — their sessions resume instead of
   ending, so the hook would never fire, or would fire on a session that is
   still alive.
2. It shows exactly one prior session, so it cannot answer "what did this agent
   do today and yesterday".
3. The label quality is bounded by "first user prompt": it is a client-side
   stand-in for a real task title, not a summary.

A server-side digest would generalise this precedent to every integration at
once, keep the no-model-call property, and drop the `SessionEnd` dependency.

## Prior art

Full survey: [recency-digest-prior-art.md](../research/recency-digest-prior-art.md).
That document grades every claim (verified / agent-only / unverified) and must
be read before any of the numbers below are relied on.

The five findings that bear on the decision:

1. **The field's unit is a thematically coherent segment, not a turn and not a
   session.** SeCom ([arXiv:2502.05589](https://arxiv.org/abs/2502.05589))
   states it directly: "The granularity of memory unit matters: turn-level,
   session-level, and summarization-based methods each exhibit limitations".
   Our memcells are at the turn end of that axis; a whole session is at the
   too-coarse end. This is independent support for option ② (segment by
   cohesion) over option ① (collapse to one line per session).

2. **The structural fix used in production is one rollup per session/thread, so
   that flooding is impossible by construction.** Zep keeps exactly one
   incrementally-updated summary per thread, and clients read it rather than
   constructing anything; EverOS merges a cluster of fragmented episodes into a
   single narrative and marks the originals `deprecated_by`, deliberately
   setting the merged record's `session_id` to `None`. Both make the digest
   source one-per-scope, which is a stronger guarantee than a cap.

3. **Coarse session summaries are measurably lossy, so a rollup must label a
   segment rather than replace it.** The Zep paper reports session summaries at
   78.6% against full-context 94.4% on DMR, and characterises recursive
   summarization's baseline as 35.3%. This is the main argument against making
   option ③ the whole answer.

4. **Per-source caps are the primitive we want, and they are unpublished.** No
   peer-reviewed system was found that caps per-session contribution to an
   injection; the closest concrete design is a two-pass selector (best item per
   source, then fill the remaining budget under a cap) in a community
   catalogue. Neighbouring techniques with published numbers are MMR and
   budgeted maximum-coverage selection. The cap is therefore justified by our
   own measurements rather than by precedent.

5. **Nothing in the space is droppable into Corti.** Every candidate needs a
   storage swap (LanceDB, Neo4j, Redis, its own vector store), a language
   swap, or is a managed-only feature. The nearest relative, **EverOS**, is
   from the same organisation as the EverAlgo we already vendor and is
   markdown-first + SQLite — but it indexes into LanceDB, so it is a runtime
   swap rather than a library adoption. Its `reflect_episodes` consolidation
   is the design worth porting.

6. **The bug is not unique to us, and it has a published fix.** AgentScope
   [PR #2776](https://github.com/agentscope-ai/agentscope/pull/2776) (merged
   September 2026) fixed a memory selector whose duplicate source could occupy
   every retrieval slot: "With five copies of `profile.md` followed by
   `project.md`, the profile appears five times and the project is never
   loaded." The fix was to **deduplicate by source before applying the limit**.
   In shipped products the corresponding invariant is that the recency unit is
   the **conversation**, not the stored chunk: ChatGPT's `Recent Conversation
   Content` block carries one entry per conversation, and its memory payload is
   split into typed sections each with its own quota. The cheapest ranking
   improvement found anywhere is CrewAI's tunable
   `recency_half_life_days` (7-day example) on a `semantic + recency +
   importance` score.

What this changes about the options in this ADR: ② remains the right
foundation, but the prior art suggests its output should eventually be a
**persisted one-line-per-task rollup with a stable task identity** (so that the
"one per scope" invariant, not just a cap, prevents flooding), and that the
rollup should be produced by a **volume/importance-triggered background pass**
rather than at session end — the trigger family used by MemGPT (token
pressure), Generative Agents (accumulated importance), Claude Code (context
pressure) and Letta's sleep-time agent.

## Open questions

1. Which option (or combination) to implement, and in which order.
2. Gap threshold for ② if adopted.
3. How to treat the `dsh-tool-*` write-log namespace: exclude it from the
   digest, cap it, or give it an honest identity (`memwrite-<agent>-<date>`)
   instead of a fake conversation id.
4. Whether the digest should be scoped to the reading agent (each agent sees
   its own recent tasks) or remain global across agents, and whether per-agent
   caps are needed on top of per-session caps.

## Reproducing the measurements

```sql
-- memory/get as the clients call it: 20 newest episodes
SELECT row_number() OVER (ORDER BY timestamp DESC) AS rn, timestamp, session_id, subject
FROM episode WHERE deprecated_by IS NULL ORDER BY timestamp DESC LIMIT 20;

-- worst-case monopoly over every 20-row sliding window
WITH e AS (SELECT timestamp, session_id, row_number() OVER (ORDER BY timestamp) rn
           FROM episode WHERE deprecated_by IS NULL),
     win AS (SELECT a.rn, b.session_id, count(*) AS cnt
             FROM e a JOIN e b ON b.rn BETWEEN a.rn-19 AND a.rn
             GROUP BY a.rn, b.session_id)
SELECT max(cnt) AS max_same_session, count(*) AS distinct_sessions
FROM win GROUP BY rn ORDER BY max_same_session DESC LIMIT 5;
```

```python
# cell size distribution (SQLite system.db)
sizes = [r[0] for r in con.execute("select json_array_length(message_ids_json) from memcell")]
```

## Outcome

The shipped fix is narrower than the options above: the integrations stop
ranking by recency at all. The DeepSeek Harness plugin fetches the newest 200
records (`/api/v1/memory/get`, paged 100 at a time) and draws **10 of them at
random**, then renders them with an explicit disclaimer that the block is a
random fetch over stored memory — not a summary of any complete task, not
ranked, not necessarily recent. That removes the monopoly by construction
rather than by aggregation, at the cost of the "one line per unit of work"
reading.

The per-task digest remains the option to revisit once the active set is
smaller: [0003](0003-cluster-consolidation-active-set-slimming.md) proposes
removing consolidated fragments from ranking, which shrinks the corpus this
sample is drawn from.
