# Prior art: task-granularity recency digests for agent memory

Survey of how existing systems decide **what one line of a "recent activity"
digest should represent**, and how they stop one session from monopolising a
fixed-size injection budget.

Read this together with
[ADR 0002](../adr/0002-recency-digest-granularity.md), which states the problem,
the measurements taken on our own corpus, and the options under consideration.

## How to read the evidence grades

Every claim carries a grade. This survey was compiled from web research, and
several items were read through search aggregators rather than primary pages —
those are leads, not settled facts, and must be re-verified before they drive a
decision.

| Grade | Meaning |
|---|---|
| **verified** | The primary source (arXiv abstract/HTML, official docs, source code) was fetched and read directly. |
| **verified (2nd reader)** | Additionally re-fetched by a second reader to guard against a fabricated citation. |
| **agent-only** | Read through a search index / summariser, not by opening the primary page. Treat as a lead. |
| **unverified** | Could not be confirmed. Explicitly not relied upon. |
| **conflict** | Two sources disagree; both values are recorded. |

## 1. The unit of memory: neither a turn nor a session

**SeCom** — *On Memory Construction and Retrieval for Personalized
Conversational Agents*, [arXiv:2502.05589](https://arxiv.org/abs/2502.05589)
(**verified (2nd reader)**, abstract quoted verbatim):

> "The granularity of memory unit matters: turn-level, session-level, and
> summarization-based methods each exhibit limitations in both memory retrieval
> accuracy and the semantic quality of the retrieved content."

The paper's proposed unit is the **topically coherent segment**: "a method that
constructs the memory bank at segment level by introducing a conversation
segmentation model that partitions long-term conversations into topically
coherent segments", with LLMLingua-2 prompt compression applied as denoising.
Evaluated on LOCOMO and Long-MT-Bench+; the segmentation model on DialSeg711,
TIAGE and SuperDialSeg.

This is the most directly applicable academic result for our problem. Our
memcells sit at the *turn* end of the axis the paper calls too fine-grained; a
whole session sits at the too-coarse end; the recommended unit —
a topically coherent segment — is what a "task" is in our vocabulary.

**MEMTIER** — *Tiered Memory Architecture and Retrieval Bottleneck Analysis for
Long-Running Autonomous AI Agents*, [arXiv:2605.03675](https://arxiv.org/abs/2605.03675)
(**verified (2nd reader)**, abstract only):

Architecture: an episodic JSONL store, a five-signal weighted retrieval engine,
an attention-attributed cognitive weight update loop, an **asynchronous
consolidation daemon promoting episodic facts to a semantic tier**, and a
PPO-based policy framework for adapting retrieval weights. The abstract reports
LongMemEval-S (500 questions) with Qwen2.5-7B on a 6 GB consumer GPU:
**Acc 0.382, F1 0.412**, a +33 percentage-point improvement over the
full-context baseline (0.050 → 0.382); single-session recall 0.686–0.714 vs a
RAG BM25 GPT-4o baseline of 0.560; temporal reasoning 0.323; multi-session
synthesis 0.173. The abstract itself flags *"infrastructure validated;
performance gains pending camera-ready"* — treat the numbers as preliminary.

The survey also surfaced a stronger claim attributed to the paper body
(**agent-only**, not present in the fetched abstract): that "the bottleneck is
injection granularity, not session recall" — reported as **90.9% session
coverage vs 4.5% fact recall@2** — and that injecting *all facts of the top-k
retrieved sessions* improved multi-session accuracy by **+0.120** and
knowledge-update accuracy by **+0.205**. If reproducible, this argues for
"pick the task well, then show all of its cells" over "mix top-k cells across
sessions". **Verify in the paper body before acting on it.**

**Mem0** — [arXiv:2504.19413](https://arxiv.org/abs/2504.19413) (**agent-only**
for the table): the stored unit is an LLM-extracted **fact** derived from a
message pair, with a periodically refreshed conversation summary used only as
extraction context and a recency window (`m = 10`); the LLM chooses
`ADD`/`UPDATE`/`DELETE`/`NOOP` against the top `s = 10` similar memories. The
survey reports LOCOMO Table 1 overall LLM-as-Judge **66.88 ± 0.15** (Mem0) and
**68.44 ± 0.17** (Mem0ᵍ), and states Zep's store at **>600k tokens per
conversation** versus Mem0's 7k. Not independently re-verified.

Worth recording from the same paper, because it is a caution about *when*
consolidation lands: the authors report that with a graph-based system,
"immediate memory retrieval attempts often failed to answer our queries
correctly. Interestingly, re-running identical searches after a delay of
**several hours** yielded considerably better results."

**Others** (**agent-only**): LightMem
([arXiv:2510.18866](https://arxiv.org/abs/2510.18866)) groups related utterances
into coherent segments by "adaptively determining segment boundaries based on
content instead of fixed window sizes"; TiMem
([arXiv:2601.02845](https://arxiv.org/html/2601.02845v1)) builds a five-level
temporal tree segment → session → day → week → profile; H-MEM
([arXiv:2507.22925](https://arxiv.org/html/2507.22925)) a four-level
domain → category → trace → episode; MemOS
([arXiv:2507.03724](https://arxiv.org/abs/2507.03724)) wraps payloads in
"MemCubes" with provenance and versioning. Memory-R1
([arXiv:2508.19828](https://arxiv.org/html/2508.19828)) reports an improvement
over Mem0 of "28% F1 / 34% BLEU-1 / 30% LLM-as-a-Judge" in the paper HTML while
its [GitHub README](https://github.com/yansikuan/memory-r1) claims "+48% F1 /
+69% BLEU-1 / +37% LLM-as-a-Judge" — recorded as **conflict**.

**Synthesis.** Three families exist: turn/segment-level (SeCom, LightMem),
fact-level (Mem0, Memory-R1), and hierarchical rollups (TiMem, H-MEM, MEMTIER).
Nobody stores a *session* as the atomic unit, and nobody presents a raw turn as
a digest line. The hierarchy family is the one that matches our intent — but
note that every hierarchical design builds its upper levels from **thematically
coherent** lower units, not from an arbitrary count.

## 2. Consolidation triggers that work without a session-end event

This matters because our runtimes resume sessions rather than ending them: a
window may be closed at 02:00 and the same `session_id` resumed at 09:00. Any
trigger that requires "the session ended" cannot fire reliably for us.

| System | Trigger | Safe for us |
|---|---|---|
| **MemGPT** ([arXiv:2310.08560](https://arxiv.org/abs/2310.08560)) | **Token pressure**: at a warning threshold (~70% of context) a memory-pressure message is inserted; at the flush threshold the queue manager evicts ~50% of the window and generates a new recursive summary from "the existing recursive summary and evicted messages" | Yes — a pressure signal, not an event |
| **Letta sleep-time compute** ([blog](https://www.letta.com/blog/sleep-time-compute), [arXiv:2504.13171](https://arxiv.org/abs/2504.13171)) | A separate sleep-time agent owns memory editing and "can run at different frequencies"; its writes are "anytime" so the primary agent can read them whenever | Yes — explicitly removes the need to wait for an end. Motivation quoted: "Memory formation in MemGPT is incremental, so memories may become messy and disorganized over time" |
| **Generative Agents** ([arXiv:2304.03442](https://arxiv.org/abs/2304.03442), [reference code](https://github.com/joonspk-research/generative_agents)) | **Accumulated importance**: `importance_trigger_max = 150`, reset after each reflection; produces 5 insights, each written back into the memory stream as a retrievable "thought" with `evidence` back-pointers and a 30-day expiry | Yes — volume/importance, not an event |
| **MemoryBank** ([arXiv:2305.10250](https://arxiv.org/abs/2305.10250)) | Ebbinghaus forgetting curve over elapsed time × significance; a global portrait and a global event summary injected alongside retrieved memories | Yes |
| **Claude Code** | Automatic compaction as the context limit approaches, or explicit `/compact` | Yes |
| **TiMem L2–L5** | Consolidation "generated automatically when their temporal windows end" | **No** — needs a defined end |
| **gantry `MEMORY.md`**, **claude-memory-kit** | Digests captured "at explicit continuation boundaries such as `/new`, `/compact`, stale-session archival" and at a `sessionEnd` event | **No** |

The safe family is consistent: **cut on how much has accumulated** (tokens,
importance, volume), never on whether a session is over. Generative Agents'
importance accumulator is the closest analogue to a trigger we can compute
locally, and it needs no model call of its own.

## 3. Digest shapes worth copying

The recurring production shape is *a curated, typed index under a hard
character budget, with detail left on disk* — not a per-session narrative.

| System | Artifact | Budget / cap rule |
|---|---|---|
| **Claude Code** auto memory ([docs](https://code.claude.com/docs/en/memory), [context window](https://code.claude.com/docs/en/context-window)) | Per-project `~/.claude/projects/<project>/memory/` with a `MEMORY.md` **index of one line per memory**, each memory a separate topic file typed `user` / `feedback` / `project` / `reference`; only the index is loaded every session | "first 200 lines or 25KB, whichever comes first"; stale entries are merged or dropped |
| **OpenHands** persistent memory ([docs](https://docs.openhands.dev/sdk/guides/persistent-memory)) | Two tiers (`~/.openhands/memory/`, `<repo>/.openhands/memory/`), each with a `MEMORY.md` index — **the only file injected**; daily logs are "never injected automatically" | combined indexes ≈ **6,000 characters**; when over budget, **whole lines are dropped from the top of each tier (oldest first) — partial lines never survive** |
| **Windsurf** rules ([docs](https://docs.windsurf.com/windsurf/cascade/memories)) | `global_rules.md` + per-workspace `.mdc` rules | global **6,000 chars**; workspace **12,000 chars** per file |
| **Letta** memory blocks ([docs](https://docs.letta.com/v1-sdk/memory/memory-blocks/)) | Labelled, always-in-context, agent-editable blocks | default `limit: 5000` chars; recommended **< 50k chars and < 20 blocks** per agent |
| **Zep** community tier ([arXiv:2501.13956](https://arxiv.org/abs/2501.13956)) | "clusters of strongly connected entities … contain high-level summarizations"; retrieval **searches the community *name*** (keywords and phrases) and injects the long summary only for selected nodes | bounded by the number of communities selected |
| **AGENTS.md** ([agents.md](https://agents.md/)) | Plain markdown, nearest-file-wins, nested per directory | none — authored by humans and agents |

Two transferable details: the **deterministic drop rule** (whole lines, oldest
first, never a partial line) and the fact that the index is **typed**, so one
kind of entry cannot silently crowd out another.

## 4. Diversity and anti-monopoly policies

**MMR** (Carbonell & Goldstein, 1998; [DOI](https://dl.acm.org/doi/10.1145/290941.291025),
[free PDF](https://www.cs.cmu.edu/~jgc/publication/The_Use_MMR_Diversity_Based_LTMIR_1998.pdf))
is the published antecedent:

```
MMR = argmax [ λ · Sim1(Di, Q) − (1 − λ) · max_{Dj ∈ S} Sim2(Di, Dj) ]
```

`λ = 1` reproduces the standard relevance ranking, `λ = 0` gives maximal
diversity; the authors suggest starting at `λ = 0.3`. They already reported
"significant repetition in content over the retrieved passages" in a plain
top-10. Zep lists MMR among its supported rerankers, so diversity reranking is
established practice in agent memory rather than a local hack.

**A per-source cap is the primitive we actually need — and it is unpublished.**
The survey found **no peer-reviewed paper that explicitly caps how many items
one session may contribute to an injection**. The clearest concrete design is a
community catalogue, the Agent Memory Atlas
"[Source-Diverse Context](https://neoneye.github.io/agent-memory-atlas/patterns/source-diverse-context/)"
page (**agent-only**, secondary source): a two-pass selector whose only tuning
constant is `per_source_cap`, where pass 1 takes the best candidate per source
and pass 2 fills the remaining budget under the cap. It reports that
`agentmemory` "caps results at three per session" inside a weighted-RRF hybrid
search (**unverified against source**), and that OpenViking keys quotas on
memory **category** instead.

The same page carries the caveat to design around: *"a quota guarantees breadth
and forfeits depth … which is why a quota needs an escape hatch."* That argues
for our two-pass form — every session gets a first slot, and additional slots
are permitted under a cap — rather than a hard one-line-per-session rule.

**Adjacent results** (**agent-only**):

- **PACMS** ([arXiv:2606.20047](https://arxiv.org/html/2606.20047v1)) frames
  context assembly as "budget-constrained submodular maximum-coverage under a
  knapsack constraint", and reports beating LangChain MMR on LongMemEval QA by
  "+8 to +12 points" at a 45% budget "despite recall parity", with ranking
  `PACMS >> top-k >> MMR ≥ last-k`.
- **xMemory** ([arXiv:2602.02007](https://arxiv.org/pdf/2602.02007v1.pdf))
  argues "fixed top-k similarity retrieval tends to return redundant context"
  and selects "a compact, diverse set" of themes, expanding to episodes only
  when that reduces the reader's uncertainty.
- **MEMTIER** uses tier multipliers `μ ∈ {1.0, 1.2, 1.4}`, "deliberately modest
  (20% and 40% boosts) to avoid tier dominance overriding relevance scores; they
  act as a **tiebreaker**, not a reranker" — a useful precedent for giving a
  task-rollup a small boost over a raw cell.

**Deterministic scorers** (no embeddings required), for reference: Generative
Agents combines `recency`, `relevance` and `importance` as
`0.5·recency + 3·relevance + 2·importance` with `recency = 0.99^i` over
chronologically sorted memories and returns `n = 30` (**verified** in
`retrieve.py` / `scratch.py`).

## 5. Evidence against using coarse session summaries

The Zep paper ([arXiv:2501.13956](https://arxiv.org/html/2501.13956v1),
**agent-only** for these figures) reports on DMR that **session summaries score
78.6%** against **full-context 94.4%** (gpt-4-turbo) and **MemGPT 93.4%**, and
characterises MemGPT's result as "a significant improvement over the **35.3%
baseline achieved through recursive summarization**". SeCom reaches the same
conclusion qualitatively.

The implication for us is direct: a digest line that is a whole-session
summary is both redundant *and* lossy. If we adopt LLM-written session
summaries at all, they should be **the label on a segment**, not the
replacement for the segment.

A second warning from the same body of work: Mem0's authors measured Zep's
memory graph at >600k tokens per conversation because "Zep's design choice to
cache a full abstractive summary at every node while also storing facts on the
connecting edges, leading to extensive redundancy." Do not attach a summary to
every node.

## 6. Traps

1. **Session-end gating.** Designs that fire on `/new`, `sessionEnd`, or "when
   the temporal window ends" cannot work for resuming runtimes. Non-traps that
   look similar: MemGPT's token flush, Claude Code's context-pressure
   compaction, Generative Agents' importance accumulation, Devin's
   retrieval-time trigger match.
2. **Per-window LLM calls.** MemGPT summarises on every flush; OpenHands'
   `LLMSummarizingCondenser` calls the model on every condensation; Mem0 runs
   extraction per message pair plus an asynchronous conversation-summary
   refresher. A rollup that fires only when volume/importance crosses a
   threshold is far cheaper, and the Mem0 "results were better hours later"
   anecdote is a reminder that eager consolidation has its own cost.
3. **Vector-store or graph assumptions.** MMR needs `Sim2`; Zep needs entity
   resolution ("embeds each entity name into a 1024-dimensional vector space");
   H-MEM needs FAISS. The subset that survives without a vector store is:
   deterministic recency/importance arithmetic, character budgets with
   whole-line drops, cheap lexical grouping, and per-source caps. (Corti *does*
   have pgvector, so MMR with an embedding `Sim2` is available here if wanted —
   but the digest should not require it to be cheap.)
4. **Coarse summaries as the substrate** (see §5).
5. **Aggregator-only numbers.** Everything graded *agent-only* or *unverified*
   in this document must be re-checked before it drives a design decision.

## 7. Open-source memory frameworks

Read from repository READMEs, source files and official docs by a survey
agent, unless a row says otherwise. Nothing in this section was independently
re-verified except EverOS and EverMemOS (**verified (2nd reader)**).

### Comparison

| System | Ingestion unit | Session id stored? | Session-start injection | Session-level rollup | License / lang |
|---|---|---|---|---|---|
| **EverOS** (EverMind-AI) | `/add` buffers per `(session_id, app_id, project_id)`; `/flush` runs one boundary LLM call → episode markdown + atomic facts + Foresight | yes, first-class and orthogonal (`user_id` / `agent_id` / `app_id` / `project_id` / `session_id`) | retrieval-time, per integration | **yes** — `reflect_episodes` cron merges an episode cluster into one narrative and deprecates the originals (`deprecated_by`); merged record carries `parent_type=cluster`, `session_id=None`; **off by default** | Apache-2.0 / Python |
| **Zep v3** (managed) | messages → episodes, facts, entities, **thread summaries**, user summary, observations | `thread_id` + `user_id`, first-class | **yes** — default *Context Block*, assembled from the 4 most recent messages as the query | **yes** — one incrementally-updated natural-language summary **per thread**, read-only to clients | Apache-2.0 repo is now examples-only / Python, Go, TS |
| **Graphiti** (the OSS core under Zep) | `add_episode` → entity/edge nodes with validity windows | `group_id` namespace only | no (library) | communities via `build_communities()` label propagation, LLM-collated summary — graph-global, not per session | Apache-2.0 / Python |
| **Mem0** | LLM-extracted facts from a `messages` payload; v3 is deliberately **ADD-only** | `user_id` / `agent_id` / `run_id` as retrieval filters (`run_id` documented as session scope) | **no** — "Your app decides which returned memories to include in the prompt" | none found (unverified negative) | Apache-2.0 / Python |
| **Letta / MemGPT** | memory blocks (git-backed MemFS) + message history + archival | agents + conversations; MemFS per agent | **yes by construction** — blocks are always in the system prompt | **yes** — "dreaming": background subagents review recent conversations and consolidate, triggered "after a set number of completed agent steps or when the context window is compacted" | Apache-2.0; active source is TypeScript |
| **LangMem + LangGraph** | extracted facts/profile/episodes into a namespace store; per-thread state in a checkpointer | `thread_id` for the checkpointer; store namespaces app-defined | **no** | **yes but in-graph** — `summarize_messages` / `SummarizationNode` maintains a `RunningSummary` in thread state above a token threshold | MIT / Python |
| **MemoryOS** | QA pairs → short-term FIFO → **segments** (one per session) → long-term profile/knowledge | `user_id` / `assistant_id`; session = mid-term segment | no | **yes** — heat-threshold promotion of a hot segment's unanalysed pages into profile/knowledge, then a **heat reset** | Apache-2.0 / Python |
| **Memobase** | chats buffered per user; flush when the buffer exceeds ~1024 tokens **or sits idle ~1 hour** | `user_id` only | **yes** — `context(max_token_size=500, prefer_topics=[...])` returns a ready prompt string | the structured **profile itself is the rollup** | Apache-2.0 / Python |
| **MemoryBank** | dialogue grouped **by date** → LLM summary + personality analysis | none explicit (keyed by user) | **yes** — history summary + user portrait | **yes, two levels** — per-date summary/personality, then `overall_history` / `overall_personality` | MIT / Python |
| **Memori** | conversation + agent-execution traces at entity / process / **session** levels | `entity_id`, `process_id`, `session_id` explicit | **yes** — auto-recall each turn | background "Advanced Augmentation"; `memori_recall_summary` exists (mechanics **unverified**) | Apache-2.0 / Python + TS |
| **MIRIX** | screen/user activity via memory tools | `session_id` first-class | unverified | **yes** — "Auto-Dream Memory Consolidation" merges duplicates and resolves stale/conflicting entries; procedural mode distils `last_n_sessions` | Apache-2.0 / Python |
| **memU** | mines host session logs (`~/.claude/.../*.jsonl`, `~/.codex/sessions/**`) into Markdown memory/skills | per-host session logs | indirectly — patches the host instruction file (`CLAUDE.md`, `AGENTS.md`, `MEMORY.md`) | **yes** — scheduled `record`/`commit` distils sessions into Markdown | non-standard license / Python |
| **Supermemory** | facts + profiles per `container_tag` | container = per user/space | **yes** — `context` injects the full profile at conversation start | **yes** — profile split into `static` (long-term facts) and `dynamic` (recent context) | MIT / TypeScript |
| **MemoryOS / A-MEM / txtai / HippoRAG / Memary** | note-per-interaction (A-MEM), document embeddings (txtai), OpenIE triples (HippoRAG) | A-MEM has **no** session/user id at all | mostly no | A-MEM: none; Memary: 50-word history summary when a threshold is crossed | MIT / Apache-2.0 |

### EverOS is the closest prior art, and it is our own lineage

EverOS is a Python, local-first, markdown-first memory runtime from
**EverMind-AI — the same organisation whose EverAlgo Corti already vendors**
(`src/everalgo/`). **Verified (2nd reader)** from the
[README](https://raw.githubusercontent.com/EverMind-AI/EverOS/main/README.md)
and the [EverMemOS paper](https://arxiv.org/abs/2601.02163):

- Same storage philosophy: "It stores conversations, files, and agent
trajectories as readable Markdown, then syncs local SQLite and LanceDB indexes"
— explicitly "no MongoDB, Elasticsearch, or Redis required".
- Same vocabulary: the paper's abstract states "**Episodic Trace Formation**
converts dialogue streams into **MemCells** … **Semantic Consolidation**
organizes MemCells into thematic **MemScenes**". Our "memcell" is their MemCell;
their MemScene is the missing consolidation layer.
- Same orthogonal scoping: search by `user_id`, `agent_id`, `app_id`,
`project_id` and `session_id`.
- The rollup we lack, as a product feature — the README's feature table lists
"**Reflection**: offline memory evolution that merges episode clusters and
refines profiles and skills between sessions".
- Integrations already exist for **DeepSeek Harness**, Hermes, OpenClaw and
Dify ([EverMind-AI/plugins](https://github.com/EverMind-AI/plugins)).
- Apache-2.0.

**Verdict: do not swap the runtime; port the idea.** Two concrete blockers:
EverOS indexes into **LanceDB**, not PostgreSQL + pgvector, and Corti's HTTP
surface is `v1` while EverOS has moved to `v2`. The valuable artefact is the
`reflect_episodes` design — merge a cluster of fragmented episodes into one
narrative, re-extract atomic facts, and mark the originals `deprecated_by`
(notably the merged record carries `session_id=None`, i.e. **the rollup is
deliberately de-sessioned**). It runs on a cron and is off by default, so it is
cheap to trial.

### Zep's thread summary is the structural fix

"There is one summary per thread, and it is persisted on the user's Context
Graph"; clients "only read summaries, they do not create them" and there is no
manual summarize call. Because there is exactly one summary per thread, **a
123-cell session can contribute exactly one line to a cross-thread view — the
flooding bug is structurally impossible.** The default Context Block also
takes "one instance of each context type", which is a per-type cap by
construction.

**Verdict: copy the invariant, cannot adopt.** Thread summaries are a managed
platform feature; the OSS `getzep/zep` repository is now described as
"Examples, Integrations, & More".

### MemoryOS is the best anti-monopoly policy

The most explicit eviction/heat design in the survey. Mid-term storage holds
**segments** (one per session); a new page is merged into an existing segment
only if a similarity score clears `similarity_threshold` (0.6 in the ChromaDB
example), otherwise a new segment is created. `compute_segment_heat(...)`
scores segments from visit frequency, interaction length and recency;
`evict_lfu()` removes the least-used when `max_capacity` is exceeded; and when
a hot segment crosses `mid_term_heat_threshold`, its unanalysed pages are
promoted into the long-term profile/knowledge and the counters are reset
(`N_visit = 0`, `L_interaction = 0`, recompute heat). **The reset is the part
that matters for us** — it is what stops one chatty session from staying hot
indefinitely.

**Verdict: port the policy, do not adopt the code** (research-grade local
JSON/ChromaDB storage, no session-start injection, and its
`short_term_capacity=7` semantics do not match a coding-agent turn stream).

### MemoryBank's two-level digest, and its idempotence

`memory_bank/summarize_memory.py` is the clearest published example of a
two-level digest: per date it writes `summary[date]` (theme and key events) and
`personality[date]` (traits, mood, reply strategy), then collapses all dates
into `overall_history` and `overall_personality`. It is also **idempotent** — a
date whose summary already exists is skipped. Retrieval injects the history
summary plus the user portrait, so this is a genuine session-start injection.

**Verdict: the summariser script is the single best artefact to read before
writing ours.**

### Adoptability summary

| Candidate | Verdict |
|---|---|
| **EverOS** | Closest fit and same lineage, but a runtime swap (LanceDB, `v2` API). **Port `reflect_episodes`; do not adopt.** |
| **Zep** | Best model, worst availability (managed-only features). **Copy the invariant, cannot adopt.** |
| **Graphiti** | Needs Neo4j / FalkorDB / Neptune + OpenSearch; no thread summaries. **Informs only.** |
| **Letta / MemGPT** | It is an agent harness, now TypeScript-first and cloud-default; memory blocks are always-in-context, a different problem. **Steal git-backed auditability and `/doctor`'s duplication/token auditing.** |
| **LangMem + LangGraph** | MIT and `AsyncPostgresStore` is real, but summarisation lives in per-thread graph state with no automatic injection. **No.** |
| **cognee** | Postgres-only operation exists, but Postgres-as-graph is explicitly "released as a demo feature … the production-ready version is available as a licensed product". **Informs only.** |
| **Memobase** | FastAPI + Postgres + **Redis**; collapses everything into a user profile — wrong granularity for a task list. **Steal the token-capped `context(max_token_size, prefer_topics)` signature.** |
| **Memori** | Apache-2.0 with BYODB and first-class sessions is attractive, but cloud-first direction and undocumented summary mechanics. **Watch, do not depend.** |
| **MemoryOS** | **Port heat/LFU/reset; do not adopt.** |
| **Mem0** | No session rollup found; needs its own vector store. **No.** |
| **A-MEM, MemoryBank, Memary, txtai, HippoRAG** | A-MEM has no session identity; txtai has no memory semantics; HippoRAG is corpus RAG. **Cite, do not adopt.** |
| **MIRIX, memU, MemOS, Supermemory** | TypeScript / cloud-first / screen-centric / writes into host prompt files. **Mine the ideas: auto-dream dedup, per-session log mining, L1-L3 skill tiers, static/dynamic profile split.** |

### The gap worth naming

**None of the surveyed systems implements an explicit per-source cap or an
MMR-style diversity constraint on an injected list.** They avoid monopoly by
making the digest source inherently one-per-scope (one thread summary per
thread, one profile per user), or by heat/LFU eviction. That is the field's
answer to our bug, and it agrees with §1: **the fix is to stop digesting at
cell granularity, not to add a round-robin knob on top of cell granularity.**

## 8. Shipped products
Read from official docs, changelogs and public pull requests by a survey
agent; no item in this section was independently re-verified. Much of it is
genuinely undocumented, and the gaps are listed at the end rather than papered
over.

### The headline: nobody ships a flat "N most recent chunks" list

Every product surveyed that ships at scale does one of two things:

- **a per-category quota** — the memory payload is assembled from several typed
  sections, each with its own budget; or
- **one line per conversation**, not one line per stored unit.

The single closest shipped analogue to what we want is ChatGPT's
**`Recent Conversation Content`** block.

### ChatGPT — closest structural precedent (**agent-only**)

Two independent switches: *Reference saved memories* and *Reference chat
history* ([Memory FAQ](https://help.openai.com/en/articles/8590148-memory-faq)).
The architecture has evolved through "dreaming" generations — OpenAI describes
Dreaming V3 as "a significantly more capable and compute-efficient memory
architecture" evaluated on carry-forward, preference-following and staying
current ([OpenAI, Dreaming](https://openai.com/index/chatgpt-memory-dreaming/)).
OpenAI publishes **no item counts, no token budget and no injection schedule**.

The only visibility into the actual payload comes from prompt-extraction
research. The system message carries memory in several blocks —
`Model Set Context`, `Assistant Response Preferences`, `Notable Past
Conversation Topic Highlights`, `Helpful User Insights`, **`Recent Conversation
Content`**, `User Interaction Metadata` — each with **its own item budget**
([embracethered, May 2025](https://embracethered.com/blog/posts/2025/chatgpt-how-does-chat-history-memory-preferences-work/)).
That account had 15 preference entries, 8 topic highlights and 14 insights.

`Recent Conversation Content` is the recency list, and its unit is **one entry
per conversation**:

```
1. 0504T17:19 New Conversation:||||hello, a new conversation||||show me a high five emoji!
10. 0503T21 Seattle Weather:||||how's the weather in seattle?||||How about Portland?
```

Format = `timestamp` + a few-word conversation title + the user's messages,
delimited by `||||`; assistant replies are excluded. About 40 conversations
were observed. A second, redacted capture reports different counts (10/10/12
and "snippets from the last 50 conversations"), so the honest reading is
"roughly ten per profile section, roughly 40-50 conversations" — **structure is
the transferable part, not the numbers**.

### Claude — the reference implementation for a capped injection

**Claude Code** is the best-documented system in this survey:

- "Each Claude Code session begins with a fresh context window." Two mechanisms
  carry knowledge across: `CLAUDE.md` (human-written) and **auto memory**
  (Claude-written, per-repository). Both load at the start of every
  conversation; auto memory is capped at **"first 200 lines or 25KB"**; the
  `CLAUDE.md` target is **under 200 lines**
  ([memory docs](https://docs.claude.com/en/docs/claude-code/memory)).
- Post-compaction, project-root `CLAUDE.md`, unscoped rules and auto memory are
  re-injected from disk, **path-scoped rules are lost** until a matching file is
  read again, and skill bodies are re-injected capped at **5,000 tokens per
  skill / 25,000 total with oldest dropped first**
  ([context window](https://code.claude.com/docs/en/context-window)).
- **`/recap` is the shipped answer to "what did I do"** — a *one-line recap of
  what happened while you were gone*, generated on demand, and it is a
  **command, not injected context**
  ([week 17 changelog](https://code.claude.com/docs/en/whats-new/2026-w17)).
  The same entry notes `/resume` now offers to summarise stale large sessions
  rather than carry a digest permanently.
- **Claude apps** take the opposite storage decision: memory is "a set of
  individual topics as you chat, rather than summarizing conversations after
  they end", per-project, with sensitive categories excluded by default
  ([chat search and memory](https://support.claude.com/en/articles/11817273-use-claude-s-chat-search-and-memory-to-build-on-previous-context)).

Anthropic's own framing — "context rot", an "attention budget", "the smallest
possible set of high-signal tokens" — is in
[Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents).

### The failure mode, found in the wild

The strongest evidence in this whole survey is not a design essay — it is a
**merged bug fix**.

**AgentScope [#2776](https://github.com/agentscope-ai/agentscope/pull/2776)
(merged September 2026)** — the issue text could be our ADR with different
nouns:

> "The filesystem memory selector can return a filename more than once. **Each
> occurrence currently adds another copy of the file to the agent's context and
> uses one of the five retrieval slots. With five copies of `profile.md`
> followed by `project.md`, the profile appears five times and the project is
> never loaded.**"

The fix is the invariant we are missing: *"Deduplicate valid filenames in their
original order before applying the existing five-file limit."* Substitute "five
copies of one session" for "five copies of profile.md" and it is our bug at a
different layer.

**Mem0 [discussion #4396](https://github.com/mem0ai/mem0/discussions/4396)** —
a user requested MMR "to make sure that there is minimal duplication in the
returned contexts". The answer confirms "**mem0 has no native MMR, and the
reranker only reorders by relevance, it doesn't diversify**", and gives a
practical recipe: over-fetch 3-4x the target k, **dedup near-identical memories
first** ("exact restatements of the same fact will otherwise eat your relevance
budget"), then diversify, with **λ ≈ 0.5-0.6 for memory recall** "since
redundant memories are especially useless there".

**NousResearch/hermes-agent [#18587](https://github.com/NousResearch/hermes-agent/pull/18587)**
— a session recap implemented deliberately as **pure local compute with no LLM
call**, because the project's prompt-caching invariants "forbid silently
reloading or mutating context", and "a free, instant recap also works offline
and does not burn reasoning tokens". Its output is close to our target shape:

```
Session recap — Fix tool_calls regression
  Recent: 2 user turns / 4 assistant replies, 4 tool results
  Tools used: patch×1, read_file×1, search_files×1, terminal×1
  Files touched: run_agent.py
```

**Gram [#4533](https://github.com/speakeasy-api/gram/pull/4533)** took the
LLM route at the session-list level: `chat.summarize` produces a persisted,
cached per-chat summary, and sessions can be **pinned** — i.e. one summary per
session, with user-curated salience.

**No authoritative engineering write-up of this failure mode was found.** The
evidence is a merged bug fix and an unanswered framework feature request, not a
design essay. It is an under-written problem.

### Product comparison (condensed)

| Product | Stored unit | Session identity / grouping | Auto-injected at start | Long-session handling | Caps and dedup |
|---|---|---|---|---|---|
| **ChatGPT** | curated saved facts + background-synthesised profile; recency list is **one entry per conversation** | conversations (titles + timestamps), not grouped in UI | **yes** — 5-6 typed sections, each with its own quota | background synthesis ("dreaming") | per-section item budgets; no published token budget |
| **Claude apps** | individual topics saved during chat, explicitly *not* post-hoc conversation summaries | project = memory space + project summary | undocumented | RAG chat-search tool call, not injected context | sensitive categories excluded by default |
| **Claude Code** | `CLAUDE.md` (human) + auto memory (agent) | none | **yes** — "first 200 lines or 25KB" | auto-compaction; `/compact`; `/recap` on demand | hard line/character budgets; oldest-first drops |
| **Gemini** | saved info + past-chat recall | none | surfaced post-hoc in "Sources and related content" (**inference, not documented**) | "relevant chats" recall | none documented |
| **M365 Copilot** | saved memories + **inferred** history details + custom instructions | none | yes (implied); format undocumented | history details are "dynamic" and may be discarded | saved memories persist; history details die 30 days after the control is turned off |
| **Recall** | local screen snapshots + semantic index | timeline | **no** — a local retrieval surface | continuous capture, no summarisation | app/site exclusions, local disk budget |
| **Perplexity Projects / Brain** | session-derived **typed** structured memories | **yes** — "delete a session from your search history" to stop it influencing answers | project Brain built from "the project's sessions and files" | Brain updates between tasks | typed buckets: Concepts / Entities / Workstreams, each with a relative freshness stamp |
| **Mem0** | extracted atomic fact (ADD-only); layers conversation / **session** / user / org | `run_id` scopes a session | framework-dependent | ADD-only, nothing overwritten | hash-based dedup, "<7,000 tokens per retrieval call", top_200 budget; **no native MMR** |
| **Supermemory** | atomic facts on a graph; relations `updates` / `extends` / `derives` | **yes at extraction** — default `dreaming: "dynamic"` groups documents so memories form from "coherent units (e.g. a real multi-turn session)" | `containerTag`-scoped search; profiles always-on | "dreaming" keeps building the graph | **typed decay**: Facts persist, Preferences strengthen, **Episodes decay unless significant** |
| **Zep** | six typed primitives incl. **thread summaries** and a user summary | yes — user graph + threads | Context Block, template-assembled from typed slots | incremental per-thread summarisation | assembly picks context "relevant to the four most recent messages"; ≤5 user-summary instructions |
| **Letta** | memory blocks with a character `limit` | per-agent, not per-thread | **always** — prepended as XML with `chars_current` / `chars_limit` | agent rewrites blocks via tools | per-block limit (5,000 chars in examples) |
| **CrewAI** | LLM-extracted record with inferred scope/categories/importance | scope tree (`/project/alpha`), not session | **yes** — "before each task, the agent recalls relevant context and injects it" | facts extracted after each task | **composite score = semantic + recency + importance**, with `recency_weight` and `recency_half_life_days` (7-day example) |
| **Memori / Personal AI** | per-upload memory block (Personal AI); entity/process/session (Memori) | no conversation grouping (Personal AI) | not documented | n/a | n/a |
| **Windsurf** | auto-generated Memories + rule files | **per-workspace**, not per-session | Memories retrieved "when it believes they're relevant" — not always-on | not documented | `global_rules.md` ≤ 6,000 chars; workspace rules ≤ 12,000 chars each; vendor recommends Rules over auto Memories |
| **Cursor** | Rules + AGENTS.md + auto Memories | none | rules "included at the start of the model context"; Memories retrieved when relevant | not documented | 4 activation modes; "keep rules under 500 lines"; Memories require user approval |
| **GitHub Copilot** | markdown instruction files | none | yes | not documented | generator guardrail: "**no longer than 2 pages**", "must not be task specific" |
| **AGENTS.md** | repo markdown, nestable | none | yes, nearest-file-wins | n/a | 60k+ projects; Linux Foundation stewardship |

### What not to copy

- **ChatGPT's background synthesis** — an always-on offline cadence plus an
evaluation harness across three objectives, sized for hundreds of millions of
users.
- **Mem0's full retrieval stack** — six-stage extraction with graph entity
linking, a temporal-reasoning pass and multi-signal fusion at a top_200 budget.
- **Zep's temporal knowledge graph, Supermemory's graph + dreaming** —
enterprise machinery for a problem that a `GROUP BY session_id` addresses.
- **Recall's continuous capture** and **Copilot's Exchange-mailbox storage** —
platform-coupled, nothing transfers.
- **Anthropic's server-side compaction / context editing** — use the lesson
(clear oldest tool results first, summarise at a threshold), not the mechanism.
- **Treating ChatGPT's observed counts as a specification.**

### Evidence gaps

- **ChatGPT**: no official counts, budget, cadence or schedule; the numbers come
  from prompt-extraction research on one or two accounts and the sources
  **disagree**. A distinct ChatGPT `memory_search` tool is **unverified**.
- **Claude apps**, **Gemini Apps and Enterprise**, **M365 Copilot**, **Cursor
  Memories**, **Windsurf Memories**: injection payload and counts undocumented.
- **Rewind / Limitless**: no longer verifiable — Meta acquired Limitless, Rewind
  capture was disabled 19 Dec 2025.
- **The failure mode itself**: no authoritative engineering write-up exists.

## Appendix: source list

| Source | Grade |
|---|---|
| SeCom — [arXiv:2502.05589](https://arxiv.org/abs/2502.05589) | verified (2nd reader), abstract |
| MEMTIER — [arXiv:2605.03675](https://arxiv.org/abs/2605.03675) | verified (2nd reader), abstract; body figures agent-only |
| MemGPT — [arXiv:2310.08560](https://arxiv.org/abs/2310.08560) | agent-only |
| Letta memory blocks / sleep-time — [docs](https://docs.letta.com/v1-sdk/memory/memory-blocks/), [blog](https://www.letta.com/blog/sleep-time-compute), [arXiv:2504.13171](https://arxiv.org/abs/2504.13171) | agent-only |
| Generative Agents — [arXiv:2304.03442](https://arxiv.org/abs/2304.03442), [code](https://github.com/joonspk-research/generative_agents) | verified, source code |
| MemoryBank — [arXiv:2305.10250](https://arxiv.org/abs/2305.10250) | agent-only |
| Zep — [arXiv:2501.13956](https://arxiv.org/abs/2501.13956) | agent-only |
| Mem0 — [arXiv:2504.19413](https://arxiv.org/abs/2504.19413) | agent-only |
| Claude Code memory — [docs](https://code.claude.com/docs/en/memory), [context window](https://code.claude.com/docs/en/context-window) | agent-only |
| Cursor self-summarisation — [blog](https://cursor.com/blog/self-summarization) | agent-only |
| Windsurf memories — [docs](https://docs.windsurf.com/windsurf/cascade/memories) | agent-only |
| OpenHands condenser / persistent memory — [docs](https://docs.openhands.dev/sdk/guides/persistent-memory) | agent-only |
| Devin knowledge — [docs](https://docs.devin.ai/product-guides/knowledge) | agent-only |
| AGENTS.md — [agents.md](https://agents.md/) | agent-only |
| EverOS — [README](https://raw.githubusercontent.com/EverMind-AI/EverOS/main/README.md) | **verified (2nd reader)** |
| EverMemOS — [arXiv:2601.02163](https://arxiv.org/abs/2601.02163) | **verified (2nd reader)**, abstract |
| Zep thread summaries / context types / assembly — [docs](https://help.getzep.com/v3/thread-summaries), [context types](https://help.getzep.com/v3/context-types) | agent-only |
| Graphiti communities / namespacing — [docs](https://help.getzep.com/v3/graphiti/core-concepts/communities) | agent-only |
| MemoryOS heat / LFU / reset — [mid_term.py](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-mcp/memoryos/mid_term.py), [memoryos.py](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-mcp/memoryos/memoryos.py) | agent-only |
| MemoryBank per-date + overall rollup — [summarize_memory.py](https://github.com/zhongwanjun/MemoryBank-SiliconFriend/blob/main/memory_bank/summarize_memory.py) | agent-only |
| Mem0 session scope — [main.py](https://raw.githubusercontent.com/mem0ai/mem0/main/mem0/memory/main.py) | agent-only |
| Memobase `context` API — [docs](https://docs.memobase.io/api-reference/prompt/get_context) | agent-only |
| Supermemory static/dynamic profile — [repo](https://github.com/supermemoryai/supermemory) | agent-only |
| cognee sessions / distillation — [docs](https://docs.cognee.ai/guides/session-distillation) | agent-only |
| Letta dreaming — [docs](https://docs.letta.com/configuration/memory) | agent-only |
| Memori / MIRIX / memU / MemOS / A-MEM / Memary / txtai / HippoRAG | agent-only, README level |
| memU license (non-standard), Memori summary mechanics, MIRIX auto-dream internals | **unverified** |
| ChatGPT memory FAQ / Dreaming — [help](https://help.openai.com/en/articles/8590148-memory-faq), [OpenAI](https://openai.com/index/chatgpt-memory-dreaming/) | agent-only |
| ChatGPT payload dissection — [embracethered](https://embracethered.com/blog/posts/2025/chatgpt-how-does-chat-history-memory-preferences-work/) | agent-only, third-party security research |
| Claude chat search and memory — [support](https://support.claude.com/en/articles/11817273-use-claude-s-chat-search-and-memory-to-build-on-previous-context) | agent-only |
| Claude Code memory / context window / week 17 changelog — [docs](https://docs.claude.com/en/docs/claude-code/memory), [context](https://code.claude.com/docs/en/context-window), [changelog](https://code.claude.com/docs/en/whats-new/2026-w17) | agent-only |
| Anthropic context engineering — [blog](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | agent-only |
| Perplexity Memory / Projects — [help](https://www.perplexity.ai/help-center/en/articles/10968016-memory) | agent-only |
| M365 Copilot memory — [Microsoft Learn](https://learn.microsoft.com/en-us/microsoft-365/copilot/copilot-personalization-memory) | agent-only |
| Windows Recall — [Microsoft Learn](https://learn.microsoft.com/en-us/windows/ai/recall/) | agent-only |
| Gemini saved info / past chats — [support](https://support.google.com/gemini/answer/16413516) | agent-only |
| CrewAI memory (recency half-life) — [docs](https://docs.crewai.com/en/concepts/memory) | agent-only |
| LangMem episodic / always-present core memory — [SDK launch](https://www.langchain.com/blog/langmem-sdk-launch) | agent-only |
| Supermemory graph memory / typed decay — [docs](https://docs.supermemory.ai/concepts/memory) | agent-only |
| Mem0 memory types / evaluation — [docs](https://docs.mem0.ai/core-concepts/memory-evaluation) | agent-only |
| **AgentScope duplicate-source retrieval fix** — [PR #2776](https://github.com/agentscope-ai/agentscope/pull/2776) | agent-only; **strongest single item in this survey** |
| Mem0 MMR feature request + over-fetch/dedup recipe — [discussion #4396](https://github.com/mem0ai/mem0/discussions/4396) | agent-only |
| hermes-agent local-only session recap — [PR #18587](https://github.com/NousResearch/hermes-agent/pull/18587) | agent-only |
| Gram persisted per-chat summary + pinning — [PR #4533](https://github.com/speakeasy-api/gram/pull/4533) | agent-only |
| TextTiling (Hearst 1997), QMSum — [ACL](https://aclanthology.org/2021.naacl-main.472/) | agent-only |
| Cursor rules / changelog 1.2, Windsurf memories, Copilot custom instructions, AGENTS.md precedence | agent-only |
| ChatGPT item counts, ChatGPT `memory_search` existence, Gemini/Copilot/Cursor/Windsurf injection payloads, Rewind-Limitless model | **unverified / undocumented** |
| Agent Memory Atlas, Source-Diverse Context — [page](https://neoneye.github.io/agent-memory-atlas/patterns/source-diverse-context/) | agent-only, secondary |
| MMR — [DOI](https://dl.acm.org/doi/10.1145/290941.291025) | agent-only |
| PACMS — [arXiv:2606.20047](https://arxiv.org/html/2606.20047v1) | agent-only |
| xMemory — [arXiv:2602.02007](https://arxiv.org/pdf/2602.02007v1.pdf) | agent-only |
| LightMem / TiMem / H-MEM / MemOS / Memory-R1 / MemAgent | agent-only; Memory-R1 figures conflict |
| MEMTIER recall@2 figures, agentmemory "three per session" cap, Cursor native Memories removal, SWE-agent recap design | **unverified** |
