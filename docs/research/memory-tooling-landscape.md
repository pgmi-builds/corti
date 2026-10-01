# Memory tooling landscape: what else is out there, and what is usable

Companion to [recency-digest-prior-art.md](recency-digest-prior-art.md) (which
answers a narrower question: how systems built their digests). This document
answers a different, wider set of questions asked by the operator:

1. Is there anything we could adopt as-is?
2. How do the candidates differ in features, complexity, completeness and
   maturity?
3. What mechanisms exist **beyond vector / semantic / keyword search**?
4. What are the deployment requirements — embedded SQLite, or a Postgres-grade
   dependency like ours?
5. We run **shared memory for an agent swarm** — how do others model that?

Deliberate scoping note: candidates are **not** filtered by whether they
implement a "recency" feature. Corti has both recency and relevance; most
memory systems have only relevance, and recency may be dropped anyway.

Evidence grades are the same as in the companion document: **verified**
(primary source fetched and read directly), **agent-only** (read by the survey
agent), **unverified**. Everything below marked *agent-only* needs re-checking
before it drives a decision.

## 0. Baseline: who we are and what we actually need

Measured on the live deployment, 2026-10-01. This section is verified.

### Deployment shape

| Property | Value |
|---|---|
| Image | `m1research/corti:v0.3.2-slim` — **753 MB** |
| Variant | **slim**: the container does **not** bundle PostgreSQL (no `psql` in its `PATH`); it talks to an external server |
| External dependency | **PostgreSQL 18.6** on the host (`/var/lib/postgresql/18/main`) |
| Runtime | 284.8 MiB RSS, 0.4% CPU while idle |
| Host bind mount | `/home/u1/.corti` → `/home/app/.corti` (single directory, 12-Factor style) |

### Storage

| Layer | Size | Notes |
|---|---|---|
| **PostgreSQL database `corti`** | **3,721 MB** | the index; fully rebuildable from markdown |
| ├ `atomic_fact` | **2,774 MB** | **75% of the database** |
| ├ `foresight` | 757 MB | |
| └ `episode` | 181 MB | |
| Markdown (source of truth) | 101 MB | `~/.corti/shared-agent-memory` |
| SQLite state | 143 MB | `~/.corti/.index/sqlite/system.db` — state, queue, memcell ledger |
| Total on disk | ≈ 5.9 GB | for ~6,600 episodes |

**The single most important number here is that `atomic_fact` is 75% of the
database.** The extraction pipeline emits roughly 28 atomic facts per memcell,
and each fact carries a 1024-dimension vector. So the storage cost — and, since
embeddings are billed per token, a large share of the Aliyun bill — is
concentrated in a derived artefact that the digest never reads and that search
reaches only through a max-pool reduction over parent memcells. Any plan to
slim the deployment should start here, not at the search layer.

### The swarm model we are actually running

This is not a single-agent store. Measured over active episodes:

| Scoping key | Distinct values |
|---|---|
| distinct agents writing into one space | **15** |
| distinct `(app_id, project_id)` spaces | 1 (`shared-agent-memory` / `default`) |
| distinct owners in that space | 2 (`default` 6,512; `business` 112) |

| Agent | Episodes |
|---|---:|
| `pc-deepseek-default` | 3,022 |
| `pc-hermes-code` | 702 |
| `pc-hermes-default` | 672 |
| `pc-hermes-research` | 352 |
| `pc-claude-code` | 251 |
| `mac-hermes-default` | 128 |
| `business` | 94 |
| `dev3-deepseek-default` | 83 |
| `mac-claude-code` | 47 |
| `dev3-hermes-default` | 21 |
| `pc-hermes-business` | 18 |
| `pc-hermes-junior` | 16 |
| `dev2-dashr-default` | 13 |
| `dev3-hermes-research` | 9 |
| `pc-hermes-docker` | 5 |

So the design constraints that actually bind are:

- **One shared memory space, many agents.** Every agent can read every other
  agent's memories by default. There is no per-agent private partition.
- **Attribution matters.** A memory is attributed to its producer through
  `sender_ids` (a JSON list), not through a dedicated `agent_id` foreign key.
- **The swarm writes concurrently** — several runtimes and machines at once —
  which is why a single-writer embedded store is a real risk.
- **One user's facts sit alongside every agent's**: the `user/default` space and
  the 15 agents share the same tables, distinguished only by `owner_id` /
  `owner_type` / `sender_ids`.

### Mechanisms Corti already implements

Discovered while measuring the baseline, and important context for question 1:
the retrieval and consolidation stack is **already more sophisticated than most
of the systems surveyed**, and the pieces we were about to design from scratch
already exist and are wired end to end. Verified by reading the source and the
live tables.

| Mechanism | State | Evidence |
|---|---|---|
| **Lexical search — cover density, NOT BM25** | running | `tsvector` + `ts_rank_cd`, jieba tokenisation. **Correction:** `ts_rank_cd` computes cover density (Clarke/Cormack/Tudhope), which is *not* BM25. True BM25 inside Postgres needs an extension (`pg_search` / VectorChord-bm25). This matters because RRF and hybrid fusion behave differently when the lexical leg is cover density rather than BM25 — swapping the lexical ranker is itself a cheap, measurable win |
| Vector ANN search | running (embedder currently unpaid) | pgvector HNSW, `vector_cosine_ops` |
| Hybrid fusion | running | RRF and LR-calibrated scoring, hidden under `method=hybrid` |
| Agentic multi-round retrieval | implemented | `search/agentic.py` — fact-MaxSim → hybrid → cluster-scoped → round-2 agentic |
| Hierarchy (episode ↔ atomic facts) | implemented | `search/hierarchy.py` — heap-driven lazy expansion, global top-N competition between episodes and their facts |
| MaxSim fact pooling | running | fact pool sized at `top_k × 20`, capped at 2,000 |
| **Semantic clustering of episodes** | **running** | `strategies/trigger_profile_clustering.py`; **936 clusters over 9,658 episode members**, last updated 2026-09-30 05:06 |
| **Offline LLM consolidation of a cluster into one narrative episode** | **implemented but disabled** | `strategies/reflect_episodes.py`, `enabled=False`, cron `0 2 * * 1`; `reflection_report` table is **empty — it has never run** |
| Retrieval that understands merged cluster episodes | implemented | `parent_type=cluster` handling in `search/agentic.py` and `search/hierarchy.py` |
| User profile extraction | running | `strategies/extract_user_profile.py` |
| Foresight extraction | running | `strategies/extract_foresight.py` |

Three consequences.

**1. The "task" unit we were going to build with a time-gap heuristic already
exists, and it is semantic rather than temporal.** A cluster is a set of
episodes grouped by embedding similarity — i.e. a topic/task — with a centroid,
a member count, a `last_ts_ms`, and a preview. Rendering a digest from clusters
instead of from raw memcells gives one line per task *by construction*, and a
single session cannot flood it because clusters partition by meaning, not by
time.

**2. The label problem is exactly what the disabled strategy solves.** Cluster
`preview_json` is a raw excerpt of a member, not a synthesized title, so a
cluster-derived line currently reads as a verbose opening sentence. The
reflection strategy generates a real narrative per cluster.

**3. Enabling reflection naively is not safe.** Of the 936 clusters, **583
(62%) are singletons**, and the largest holds **4,046 of the 9,658 members
(42%)** — a degenerate blob that swallowed the recurring scheduled-job
episodes. Running consolidation over that distribution means ~936 LLM calls,
including one merge over 4,046 episodes, and the project's own documentation
warns that "each run is a lossy LLM re-merge" and "repeatedly re-consolidating
the same memories can make the narrative *worse*." Any plan to use this path
has to deal with cluster quality first. The same clustering is, however,
good enough to *read* — the 20 most recent clusters by `last_ts_ms` cover ~2,000
episodes in 20 lines, with member counts of 1, 2, 3, 7, 13, 39, 43, 66, 80, 99,
102, 119, 123, 135, 384 and 638.

## 1. Drop-in candidates

**Nothing is a drop-in replacement for Corti.** The honest ranking below is
of things that could be *installed* this week, not things that could *replace*
us. One candidate is genuinely interesting on storage compatibility; everything
else is either a design to port or a system that would require a second
datastore.

### Installable this week (with caveats)

| Rank | Candidate | Why it is installable | What it costs us |
|---|---|---|---|
| 1 | **LightRAG** (`lightrag-hku` 1.5.7, MIT) | **The only system whose entire storage stack runs on the Postgres+pgvector image we already run**: `PGKVStorage` + `PGVectorStorage` + `PGTableGraphStorage` + `PGDocStatusStorage`, **with no Apache AGE**. Project-reported p50 for `get_knowledge_graph` on an 8k-node graph: **39 ms vs 1,099 ms (~28×)** for the AGE path | It is a **corpus-RAG engine, not an agent-memory API.** Isolation is a `WORKSPACE` env var — no per-user or per-agent ACL. Storage backends are immutable after first ingest. Default file backends are explicitly "not for production" |
| 2 | **Apache AGE 1.8.0 (PG18)** — a substrate, not a memory system | If we want Cypher and graph traversal *inside* the Postgres we already have, this is the extension | **Pin carefully:** LightRAG documents that AGE ≥ 1.8.0 changed `id()` to return `graphid`, breaking `get_knowledge_graph` and capable of SIGSEGV — **1.7.0 is the verified-good version**. No native vector index; pgvector stays separate |
| 3 | **Supermemory local** (MIT, single binary) | Genuinely self-contained: *"No Docker. No database to provision. No config files."* Embeds its own graph engine **and** a local embedding model (`Xenova/bge-base-en-v1.5`, 768d), serves the full Memory API on `localhost:6767`, all state in `./.supermemory`, runs **fully offline** | Docs state the scale limit plainly: *"**One machine, one process**"*, single auto-generated API key, "Single org on one machine". **MCP is platform-only** for self-hosted. Single-tenant by design |
| 4 | **cognee 1.6.2** (Apache-2.0) | The **best tenancy model surveyed**: users / datasets / ACL grants / tenants / roles / `(user_id, session_id)` sessions, with child agents visible to a parent | **Blocker:** its Postgres *graph* store is self-described as *"demo, not production-ready"*; the production PG graph adapter is a licensed product. Its embedded default (SQLite+LanceDB+Kùzu) *"is not suitable for concurrent use from different agents or processes"* |
| 5 | **A-MEM's retriever seam** | Not a dependency — a **half-day port**. One `ChromaRetriever`-shaped class with four methods sits between the system and its store; swap it for a pgvector table and keep the link-expansion design | The repo itself is not installable as a dependency: not on PyPI, no CI, default in-memory storage, and its advertised hybrid retrieval **does not exist in code** (`rank_bm25` is imported but never queried) |
### Agent-native SDKs — evaluated as *libraries*, not replacements

This cluster is where the philosophies diverge most, and where two systems are
worth reading closely.

| System | What it is | Retrieval mechanisms | Deployment | Swarm scoping | Verdict |
|---|---|---|---|---|---|
| **memU** (Apache-2.0, 14.5k★) | **"Personal memory, stored as Wiki"** — Markdown files + agent-distilled skills. `MemoryService` **makes no LLM or chat calls**: *"the judgment and synthesis stay inside the agent"* | **embedding-only** — SQLite brute-force cosine, or pgvector `<=>` + index. **No BM25 / FTS / keyword / graph.** Progressive: segments → files (path + summary) → resources, and it returns **file handles, not text** | **SQLite default**, **Postgres+pgvector** (`memu-cli[postgres]`), or in-memory. No server | **"All hosts share one configured memory backend… What one host's sessions taught memU, another host retrieves."** Adapters already exist for Codex, Claude Code, Cursor, OpenClaw, **Hermes**, and more | **The most philosophically aligned system surveyed.** It is our premise (markdown truth, agent decides what to write, shared store across heterogeneous runtimes) implemented independently, and it already speaks Hermes. Against it: CLI/sidecar not a service, 0.x version skew, and **zero keyword retrieval** |
| **Memori** (Apache-2.0, 17.0k★) | "Memory from what agents do, not just what they say" — captures **tool calls, decisions and outcomes**, not just conversation | **A logical knowledge graph over plain SQL tables** (entity/process/relation — not a graph DB) + entity attribution + `rank_score` fusion; embeddings via local FAISS shipped in the wheel | **BYODB**: SQLite, **PostgreSQL**, MySQL, TiDB, CockroachDB, Oracle, MongoDB, … **Runs with no external service.** A **library** | `attribution(entity_id, process_id, project_id)` — a clean two-axis split mapping onto our `users/` vs `agents/` | **The closest architectural cousin** and installable against our own Postgres. Cost: you run their writer/collector pipeline and schema instead of our `memory/` layer |
| **MemOS 2.0 "Stardust"** (Apache-2.0, 11.7k★) | Composable **MemCube** namespaces; tiered skill evolution (L1 traces / L2 policies / L3 world model) | Graph (Neo4j) + hybrid **FTS5 + vector** in the local plugin | Self-host = **Neo4j + Qdrant**; local plugin = **SQLite only, "100% local, zero cloud dependency"** | `owner_id` + `cube_id` with **separate `readable_cube_ids` / `writable_cube_ids`** — **read scope ≠ write scope**, which we have no equivalent for | **Ships a DeepSeek Harness plugin** — the cheapest thing in this survey to actually trial on this fleet. Careful: PyPI name is `MemoryOS`, colliding with the unrelated BAI-LAB project |
| **LangGraph `PostgresStore`** (MIT) | Not a product — a **storage pattern** | Verified SQL: `store(prefix, key, jsonb, created_at, updated_at, expires_at, ttl_minutes)` + a `store_vectors(prefix, key, field_name, embedding vector(dims))` side table with hnsw/ivfflat. Filters are **exactly** `$eq $ne $gt $gte $lt $lte`; namespace matching is a `LIKE` prefix. **No `tsvector`, no GIN, no BM25, no recency, no importance, no rerank** | `InMemoryStore` by default; `PostgresStore` requires pgvector | Tuple namespaces, prefix search spans the subtree; **`aput` is store-or-overwrite — no merge** | **The single best drop-in *reference* in the survey**: it is essentially our storage model already. Copy the pattern; do **not** add `langmem` (PyPI release is ~10 months stale and it drags LangGraph in) |
| **Mem0 OSS** (Apache-2.0, 66.4k★) | Extracted facts, single-pass **ADD-only** — no UPDATE/DELETE, one LLM call; `history.db` gives versioning | **BM25 keyword boost + entity-match boost + pluggable rerank**, but its own migration doc is explicit: *"**BM25 is a boost signal, not a recall expander. Only semantic search results are candidates.**"* **Graph memory was deleted from OSS** — ~4,000 lines, every external graph driver removed | Default Qdrant; 25 providers incl. **pgvector**; real self-hosted server | `user_id` / `agent_id` / `run_id` / `actor_id` **only via `filters`**; no conflict resolution — ADD-only accumulation is the design | Adopting it means adopting their extractor and accepting a **deliberately shrinking OSS ceiling** (their own README says benchmark numbers use platform-only optimizations) |
| **MIRIX** (Apache-2.0, 3.4k★) | **Six typed components** — Core / Episodic / Semantic / Procedural / Resource / Knowledge Vault — each with dedicated agents | **"PostgreSQL-native BM25 full-text search with vector similarity support"** + multi-modal; **`auto_dream`** merges duplicates and resolves stale/conflicting entries, with scopeable modes (`experience`, `dry_run`) | Postgres+pgvector **+ Redis Stack** + server + dashboard; **manual SQL migrations on upgrade** or the server refuses to start | `user_id` + `session_id` + `filter_tags['scope']` | **Not adoptable** (two datastores + migrations). But it preserves `tool_calls` / `role="tool"` turns because *"tool errors, retries, and the fix that finally worked are **the distiller's strongest signals**"* — **for a coding swarm this argues against "turn" as the memory unit** |
| **Memobase** (Apache-2.0, 2.9k★) | **User profile + event timeline** — *"Memory for User, not Agent"* | **Vector is optional**: `enable_event_embedding: false` serves quality from **profile + timeline + SQL alone**, sub-100 ms | Postgres+pgvector **+ Redis 7.4**; server | `user_id` only; **no agent dimension** | The valuable idea is the **vector-optional mode** — proof that a production memory system can drop vector search entirely. Repo is in maintenance mode (last push 2026-01-11) |
| **Acontext** (Apache-2.0, 3.7k★) | **Agent skill = a Markdown file.** *"Skill is Memory, Memory is Skill."* | **Deliberately non-vector**: *"Progressive disclosure, not search"*; *"No embeddings, no API lock-in"*; retrieval is by tool use and reasoning | `acontext server up` → Docker + dashboard; **requires an OpenAI key** for distillation | Skills shareable across agents/LLMs/frameworks | Not a drop-in. But it is **external validation of markdown-as-truth**, with a concrete "hand back a path, not content" contract |
| **Letta / MemGPT** (Apache-2.0) | Agent-editable blocks; current product is **TypeScript** (`letta-code`) | pgvector cosine + **SQL substring fallback** + tag filters + datetime filters; **BM25 only on the optional Turbopuffer backend**, reranking only on Pinecone. **No recency or importance scoring** | **SQLite is the default**; Postgres via `LETTA_PG_URI` | Shared blocks via `block_ids`; **no agent-vs-agent conflict resolution — `session.merge` last-write-wins** | **Mid-migration and the wrong shape for us**: the Python V1 memory server now lives on an `archive` branch, the memory layer is ORM-coupled to **167 Alembic migrations** with no standalone sub-package, and Docker `latest` no longer means the server |
| **Personal AI** | Closed-source SaaS | not disclosed | **cloud only**, no self-host path in the docs | workspaces/personas/roles | Excluded: no inspectable internals, no self-host |



| Rejected | Reason |
|---|---|
| **Zep (platform)** | SaaS / BYOC with closed storage; the `getzep/zep` repo states it is **not** the product. Nothing to self-host |
| **Graphiti (as a dependency)** | No Postgres driver (open FR [#779](https://github.com/getzep/graphiti/issues/779)); base install hard-requires Neo4j; adding a driver requires `rfc-approved` and ~12 operation interfaces. **Steal the model, not the package** |
| **Microsoft GraphRAG** | Self-declared **maintenance mode**: *"won't be accepting new PRs or implementing new features"* and *"not an officially supported Microsoft offering"*. No pgvector or SQL storage; project-root/CLI/Parquet architecture; no tenancy |
| **HippoRAG (as a dependency)** | PyPI is **alpha-only** (`2.0.0a4`), no CI, no Docker; PPR needs an in-process `igraph` that pgvector cannot serve; no tenancy at all |
| **Kùzu** | **Archived 2025-10-10**; Apple acquired the company (EU filing). Graphiti already emits a `DeprecationWarning` for its driver. Live successor: **LadybugDB** (MIT, PyPI `ladybug` 0.19.0, "formerly known as Kuzu") |
| **Mem0 (for graph)** | OSS **V3 removed external graph-DB support**; graph memory is now a hosted-platform feature only |
| **Memary** | Dormant ~18 months (last push 2024-10-22), PyPI three releases behind its tag, `python<=3.11.9`, Neo4j/FalkorDB mandatory |
| **nano-graphrag** | Frozen since its 2024 release; communities fully recomputed on every insert; read it as a textbook, do not ship it |
| **OpenSPG / KAG** | Needs a full Docker Compose stack (MySQL + Neo4j + Elasticsearch); stale since Jan 2026 |
| **OpenMemory (mem0's self-hosted MCP server)** | **Retired, and the repo name reused.** `openmemory/README.md` and `openmemory/api/main.py` both 404 on mem0's `main`; what survives at `mem0ai/openmemory` is now *"OpenMemory — Sync Your Sessions Across Any Coding Harness"*, a CLI/TUI session porter between Claude Code / Codex / OpenCode. mem0's current story is the hosted platform plus first-party agent plugins |

### Ideas to steal, ranked by value to us

1. **Graphiti's bi-temporal fact model + automatic invalidation.** `valid_at`/`invalid_at`/`created_at`/`expired_at` on facts; old facts preserved, never deleted. The best answer surveyed to "how do two agents' contradicting memories coexist?" and it maps cleanly onto markdown truth + SQLite state. **See §3 for the cost.**
2. **Graphiti's reranker menu as a shared retrieval API** — `rrf`, `mmr`, `cross_encoder`, `node_distance`, `episode_mentions`, applied over four object types. `episode_mentions` (rank a fact by how much provenance supports it) and `node_distance` (rank by graph proximity to a focal entity) are directly reusable on top of pgvector + SQL.
3. **HippoRAG 2's Personalized PageRank as a multi-hop primitive.** Replace an LLM loop over neighbours with one seeded random walk whose passage mass *is* the ranking — deterministic, no LLM in the retrieval loop. Pair with its **passage nodes + synonymy edges** and its **recognition filter** that can fall back to vector-only.
4. **GraphRAG's claim/covariate status field** (`TRUE` / `FALSE` / `SUSPECTED`). A first-class truth status for extracted statements is a stronger primitive than a score when several agents write to one store.
5. **GraphRAG's DRIFT loop** — primer → LLM follow-up questions → iterative local refinement with a depth cap. The one agentic retrieval pattern worth copying, with the knobs that bound its cost.
6. **LightRAG's dual-level keyword split** — one LLM call produces low-level (entity) and high-level (theme) keywords routed to different indexes.
7. **cognee's tenancy vocabulary** — datasets as the isolation-and-sharing unit, ACL grants, tenants/roles, `(user_id, session_id)` sessions, parent/child agent visibility. A ready-made vocabulary for the swarm model in §5.
8. **G-Memory's three-tier graph** (insight / query / interaction) and **MAGMA's four orthogonal graphs** (semantic / temporal / causal / entity) — the most interesting recent multi-agent *schema* ideas; research-grade, use as design input.
9. **Memary/K-LaMP's frequency × recency entity ranking** and **nano-graphrag's node2vec + degree ranking** — cheap heuristic scorers that need no LLM.

## 2. Features, complexity, completeness, maturity
Verdicts below are from the surveys; star counts and dates are as fetched on
2026-10-01 and carry a **±2% caveat** (a same-day cached pass disagreed by that
much). "Production library" vs "research artifact" is the column that matters
most for us.

| System | Stars | Latest release | Last push | Packaging / CI | Verdict |
|---|---:|---|---|---|---|
| **Mem0** | 64.9k | — | active | PyPI, Docker, MCP | Most-adopted layer; **OSS V3 removed external graph support and the self-hosted OpenMemory server is gone** — it is a hosted platform with OSS loss-leader |
| **LightRAG** | 39,942 | v1.5.7 (2026-09-02) | 2026-09-30 | PyPI + Cosign-signed GHCR + CI | **Production-shaped**, Beta classifier; README itself recommends the REST server over the SDK |
| **Microsoft GraphRAG** | 36,180 | v3.2.0 (2026-09-24) | 2026-09-28 | PyPI, docs CI | **Maintenance mode**, explicitly not an officially supported MS offering |
| **Graphiti** | 31,332 | v0.30.2 (2026-09-08) | 2026-09-30 | PyPI, CI, Docker Compose, MCP+REST | Production-grade OSS library; Zep is the commercial half |
| **cognee** | 31,246 | v1.6.2 (2026-09-29) | 2026-10-01 | PyPI + Docker + CI | Funded company (~$7.5M seed), Beta classifier; best tenancy model |
| **nano-graphrag** | 3,990 | v0.0.8 (**2024-10-01**) | 2026-01-27 | PyPI | Frozen research artifact, ~1,100 LOC |
| **HippoRAG** | 4,034 | v1.0.0 (2025-02-27); PyPI alpha `2.0.0a4` | 2026-09-29 | **no CI, no Docker** | Research library; alpha-only on PyPI |
| **Kùzu** | 4,024 | v0.11.3 (2025-10-10) | 2025-10-10 | archived | **Archived**; company acquired by Apple |
| **Memary** | 2,653 | v0.1.5 (2024-10-22) | **2024-10-22** | no CI | **Dormant ~18 months** |
| **A-MEM** | 1,188 (+976 +398 siblings) | none | 2025-12-12 | **not on PyPI** | Research artifact; three competing repos |
| **Supermemory** | — | active | active | npm + PyPI + single binary | MIT; **only self-contained single-binary option** — see §1 |
| **EverOS** | — | active | active | PyPI | Same lineage as Corti; md + SQLite + LanceDB |

**Complexity is not the differentiator — deployment shape is.** The three
systems with the strongest retrieval stacks (GraphRAG, Graphiti, HippoRAG) are
also the three that cannot run on our storage. The systems that *can* run on our
storage (LightRAG's PG backends, cognee's PG vector tier) are corpus-RAG engines
with weak agent-identity models.

## 3. Mechanisms beyond vector / semantic / keyword search

*Compiled by a survey agent; the arXiv IDs were each checked against the arXiv
API and all resolved to the claimed titles. Grades apply per claim.*

### A. Organisation, tiering and consolidation

| Mechanism | What it actually does | Cost on our stack | Needs |
|---|---|---|---|
| **Heat-based tiering** — MemoryOS ([arXiv:2506.06326](https://arxiv.org/abs/2506.06326)) | Segment heat = retrieval count (`N_visit`) × pages (`L_interaction`) × time decay (`R_recency`); lowest-heat segments evicted at capacity; heat > τ=5 promotes to long-term; counters reset after promotion | **cheap** — three counter columns + a nightly job | embeddings (have), LLM for trait extraction (have) |
| **Topic-aware segmentation** — LightMem ([arXiv:2510.18866](https://arxiv.org/abs/2510.18866)) | Boundaries = intersection of attention-based and similarity-based boundaries; segments become the memory unit | **cheap–medium** — the similarity half is cosine between adjacent turns | similarity-only variant needs nothing new; full variant needs LLMLingua-2 resident |
| **Trained segmentation** — SeCom ([arXiv:2502.05589](https://arxiv.org/abs/2502.05589)) | A dedicated conversation-segmentation model; segment-level beats turn-, session-, and summary-level banks | **needs a new model**; the *finding* is free | trained segmentation net |
| **Sleep-time consolidation** — LightMem; EverOS `reflect_episodes`; MIRIX auto-dream | Offline pass builds an update queue of nearest neighbours constrained by `t_j ≥ t_i`, then parallel merges. Stated motivation is safety: an LLM may "incorrectly interpret [two related facts] as a conflict and delete the older memory entry, leading to irreversible information loss" | **cheap–medium** — **we already implement this** (`reflect_episodes`, disabled) | embeddings + LLM |
| **Temporal memory tree** — TiMem ([arXiv:2601.02845](https://arxiv.org/abs/2601.02845)) | segment → session → day → week → profile, plus a complexity-aware router that picks which levels to consult | **medium** — tables over data we have; work is the consolidation prompts + router | embeddings + LLM, no fine-tuning |
| **Hierarchical index pointers** — H-MEM ([arXiv:2507.22925](https://arxiv.org/abs/2507.22925)) | Each memory vector carries a positional index pointing at its sub-memories; retrieval descends layer by layer instead of scanning | **medium** — the stated benefit is ANN latency, which is weak at our scale (HNSW already answers in ms) | embeddings + LLM |
| **MemCell → MemScene** — EverMemOS ([arXiv:2601.02163](https://arxiv.org/abs/2601.02163)) | Thematic consolidation of cells into scenes, then scene-guided agentic retrieval | **cheap–medium** — **we already have the clustering half** (936 clusters) | embeddings + LLM |
| **MemCube provenance/versioning** — MemOS ([arXiv:2507.03724](https://arxiv.org/abs/2507.03724)) | Content + metadata (provenance, versioning) as the unit; cubes compose/migrate/fuse | **cheap for the metadata discipline** — it is the precondition for every invalidation mechanism in section C | none extra |
| **Hypergraph memory** — HyperMem ([arXiv:2604.08256](https://arxiv.org/abs/2604.08256)) | Hyperedges group related episodes + facts into coherent units; pairwise relations cannot capture "high-order associations" | **medium** — a hyperedge is one row + one join table | embeddings + LLM |
| **Typed memory stores** — MIRIX ([arXiv:2507.07957](https://arxiv.org/abs/2507.07957)) | Six types (Core / Episodic / Semantic / Procedural / Resource / Knowledge Vault) with **per-type search method** (`bm25` \| `embedding` \| `string_match`) | **cheap** for the typing; the multimodal half is out of scope | LLM |

### B. Retrieval beyond top-k similarity

| Mechanism | What it does | Cost on our stack |
|---|---|---|
| **Associative retrieval via Personalized PageRank** — HippoRAG / HippoRAG 2 ([2405.14831](https://arxiv.org/abs/2405.14831), [2502.14802](https://arxiv.org/abs/2502.14802)) | Query entities seed a PPR diffusion over an LLM-built KG; "up to **20%**" better multi-hop QA; single-step PPR ≈ iterative retrieval at **10–30× cheaper**. **Its own warning:** structure-augmented RAG "drops considerably below standard RAG" on basic factual memory — that regression is why HippoRAG 2 exists | **medium** — edges are ordinary rows, PPR runs in-process; the cost is LLM graph construction |
| **Community summaries / global search** — Microsoft GraphRAG ([2404.16130](https://arxiv.org/abs/2404.16130)) | Leiden communities + pregenerated summaries; local vs global search | **medium–high** — the most expensive mechanism per unit of corpus; needs periodic re-indexing. Secondary-source win rates are **unverified** |
| **Dual-level retrieval + incremental update** — LightRAG ([2410.05779](https://arxiv.org/abs/2410.05779)) | Low-level entities and high-level themes, with incremental graph updates instead of full re-communities | **medium** — the incremental update is why it beats GraphRAG for a *live* store. Win rates are LLM-judged, not accuracy: vs NaiveRAG on Legal, Overall **15.2% → 84.8%** |
| **Bi-temporal validity + edge invalidation** — Zep / Graphiti ([2501.13956](https://arxiv.org/abs/2501.13956)) | Two time axes per edge: *valid time* (`valid_at`/`invalid_at`) and *transaction time* (`created_at`/`expired_at`). On contradiction the old edge is closed, never deleted — "preserving the complete history of what the system knew and when" | **cheap–medium** — four `timestamptz` columns, or two `tstzrange`s with an exclusion constraint. **The highest value-per-line-of-schema mechanism in the report** |
| **RRF fusion** — Cormack 2009 ([PDF](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf)) | `Σ 1/(k + rank)`, `k = 60` fixed after a pilot | **cheap** — we already use RRF; worth confirming the constant and that it is applied to cover density (see §0 correction) |
| **MMR** — Carbonell & Goldstein 1998 ([PDF](https://aclanthology.org/X98-1025.pdf)) | Greedy relevance-minus-redundancy selection | **cheap** — embeddings already present |
| **Budgeted submodular coverage** — PACMS ([2606.20047](https://arxiv.org/abs/2606.20047)) | Context assembly as budgeted maximum-coverage (facility-location objective, CELF lazy-greedy); explicitly attacks recency truncation as "topic-blind" | **cheap–medium** — application code over a candidate pool we already produce |
| **Cross-encoder rerank** — monoBERT ([1910.14424](https://arxiv.org/abs/1910.14424)) | Joint (query, passage) scoring; 36.5 MRR@10 on MS MARCO dev | **medium** — a local reranker model |
| **HyDE** ([2212.10496](https://arxiv.org/abs/2212.10496)) | Generate a hypothetical answer, embed it, search with that — fixes query/passage asymmetry | **cheap** — one LLM call |
| **Memory links (Zettelkasten)** — A-MEM ([2502.12110](https://arxiv.org/abs/2502.12110)) | At write time, generate keywords and link the new note to neighbours; new memories can also update neighbours' context | **cheap–medium** — one `memory_link` table + an LLM pass |
| **RL-trained memory policies** — Memory-R1 ([2508.19828](https://arxiv.org/abs/2508.19828)), MemAgent ([2507.02259](https://arxiv.org/abs/2507.02259)) | Learned `ADD/UPDATE/DELETE/NOOP` policies; MemAgent overwrites fixed-length memory per chunk | **needs new infrastructure** — PPO/GRPO fine-tuning |

### C. Write-side mechanisms

- **Explicit contradiction invalidation.** Supermemory exposes it as typed relations — `updates` / `extends` / `derives` — with the rule that with `updates` "the model knows the history", and a typed forgetting policy. **cheap–medium**: candidate pairs from a pgvector neighbourhood query, verdict from one LLM call.
- **Typed decay / forgetting classes.** MemoryBank ([2305.10250](https://arxiv.org/abs/2305.10250)) is Ebbinghaus-inspired ("forget and reinforce memory based on time elapsed and the relative significance"); Supermemory's production form is per-type: **Facts persist until updated, Preferences strengthen with repetition, Episodes decay unless significant**, plus noise filtering. **cheap** — a `decay_class` column plus two counters. The *typed* part is what matters: uniform decay is actively harmful in a coding store, where "the deploy command is X" must never decay but "the build is running" must.
- **Reflection with an importance trigger.** Generative Agents ([2304.03442](https://arxiv.org/abs/2304.03442)) fires reflection when accumulated importance crosses `importance_trigger_max = 150` (verified in `scratch.py`); `recency_decay = 0.99`. **cheap** — a counter column plus an LLM synthesis call. Reflexion ([2303.11366](https://arxiv.org/abs/2303.11366)) reports "91% pass@1 on HumanEval".
- **Fleet-memory governance.** MemClaw ([2606.24535](https://arxiv.org/abs/2606.24535)) formalises the *swarm* problem and names four failure modes: **unauthorized leakage, stale propagation, contradiction persistence, provenance collapse**, with four primitives (scoped retrieval, temporal supersession, provenance tracking, policy-governed propagation). It also documents an ordering bug we are about to walk into:

  > "a synchronous near-duplicate gate can **prematurely reject contradictory writes before the asynchronous contradiction detector can evaluate them**"

  **medium** — mostly schema and queue discipline.

### Ranked: cheap, with real gain on our stack

1. **Bi-temporal invalidation of facts** (Zep/Graphiti) — turns the store from "what is similar" into "what is *currently true*".
2. **Typed relationship edges** (`updates`/`extends`/`derives`, A-MEM links) — turns the memcell bag into a graph and gives invalidation something to traverse.
3. **Typed decay classes** (Supermemory) — bounds growth without discarding durable facts.
4. **Offline consolidation** (EverOS `reflect_episodes`) — *already implemented and disabled*; see §0.
5. **Budgeted submodular selection** (PACMS) — replaces "top-k then truncate".
6. **Heat-based tiering** (MemoryOS) — bounds the hot set.
7. **RRF with k=60** + a real BM25 lexical leg — see the §0 correction.
8. **Cross-encoder rerank** of the top ~50.
9. **Reflection with an importance trigger** (Generative Agents, 150).

### Ranked: exciting, but needs infrastructure we do not have

MSA latent memory ([2603.23516](https://arxiv.org/abs/2603.23516), trained sparse attention, 2×A800 for 100M tokens) · Titans ([2501.00663](https://arxiv.org/abs/2501.00663)) · LoRA fast-weight memory (TMEM, [2606.04536](https://arxiv.org/abs/2606.04536)) · Larimar ([2403.11901](https://arxiv.org/abs/2403.11901)) · Memorizing Transformers ([2203.08913](https://arxiv.org/abs/2203.08913)) · KV-cache reuse ([Prompt Cache 2311.04934](https://arxiv.org/abs/2311.04934), [CacheBlend 2405.16444](https://arxiv.org/abs/2405.16444)) · RL memory policies · full GraphRAG community hierarchy · SeCom's trained segmentation model.

All of these require model internals, a GPU fleet, or a control plane we do not own. **None of them can be bolted onto a hosted-API model plus Postgres.**

### Is top-k similarity even the right primitive?

**Top-k similarity is a correct *recall* primitive and an incorrect *sufficiency* primitive.** The evidence:

- **It is cheap and competitive — keep it.** HippoRAG single-step ≈ iterative retrieval at "**10–30 times cheaper and 6–13 times faster**"; the RAG-vs-long-context study finds "RAG's significantly lower cost remains a distinct advantage".
- **More context is not monotonically better.** Quality "initially improves first, but then subsequently declines as the number of retrieved passages increases", with retrieved hard negatives a key contributor; and models degrade on information in the **middle** of a long context ([2410.05983](https://arxiv.org/abs/2410.05983), [Lost in the Middle 2307.03172](https://arxiv.org/abs/2307.03172)).
- **The assembly objective matters as much as the retrieval objective.** PACMS holds recall constant and still moves accuracy: **+8 to +12 points over MMR at recall parity**, with the ordering **PACMS > top-k > MMR ≥ last-k**. *If our pipeline is "hybrid search → take top-k → pack prompt", we are running the third-best of four strategies by this measurement.*
- **Similarity has no answer for multi-hop, global, temporal, update or abstention questions.** LongMemEval ([2410.10813](https://arxiv.org/abs/2410.10813)) is built around exactly those five abilities and reports a **30% accuracy drop** for commercial assistants across sustained interactions. Zep's DMR margin is trivial (94.8% vs 93.4%) while its LongMemEval gain is large (up to 18.5%) — **the win is temporal, not similarity**.

A defensible target: keep vector + lexical + hybrid as the **candidate generator**, then add (a) an explicit **selection** step with a coverage objective, (b) a **validity filter** excluding superseded facts, and (c) **traversal** over typed edges for multi-hop. That is PACMS + Zep + A-MEM, in that order, and all three are cheap-to-medium here.


## 4. Deployment requirements

All entries in this section are **agent-only** unless marked otherwise: read
from official docs, `docker-compose` files and source by a survey agent, with
verbatim quotes. Re-check before acting.

### The question that actually matters for us

"Lightest footprint" is not the right frame for a 15-agent swarm. The binding
constraint is **eliminating a service while keeping concurrent writers**.

> With the single exception of LanceDB's documented transaction model, **no
> system in this survey documents multi-*process* concurrent writes as a
> supported configuration.** LightRAG explicitly forbids it and cognee
> explicitly forbids it; the rest are silent.

That reframes our Postgres dependency: it is not accidental bloat. It is the
thing that makes a shared memory space safe for many concurrent agent writers.

### Embedded substrates — can anything replace Postgres+pgvector?

| Substrate | ANN | Keyword | Multi-writer | Verdict |
|---|---|---|---|---|
| **LanceDB** | IVF_PQ / HNSW | **native BM25**, plus pre/post filtering | **✅ MVCC + optimistic concurrency**; `rename-if-not-exists` / `put-if-not-exists` "guarantee that exactly one writer succeeds"; appends designed to be compatible "to ensure that multiple writers can append without worry about conflicts" | **The only embedded engine with a documented multi-writer story.** Caveat: Overwrite/Restore conflict with Append — a swarm must only ever append |
| **sqlite-vec** | brute-force by default; `rescore`/`ivf`/DiskANN only in **alpha** pre-releases | FTS5 in the same file | ❌ one writer at a time (SQLite semantics, WAL does not change this) | Viable at our scale, but pre-v1 and the author says the docs are stale |
| **libSQL / Turso** | native `F32_BLOB` + `libsql_vector_idx`, LM-DiskANN (not HNSW) | SQLite FTS5 | ⚠️ embedded replica writes go to the remote primary | **Index bloat is a documented, severe problem** — one report: 30,000 × 1024-dim vectors ≈ 117 MiB of data produced a **~5 GiB index** |
| **DuckDB VSS** | real HNSW (`USING HNSW`) | separate FTS extension | ❌ in-process single-writer engine | **No** for a multi-process swarm |
| **Chroma (embedded)** | real HNSW | FTS5 inside `chroma.sqlite3` | ❌ file-locked, single process | **No** — and it is a directory, not one portable file |
| **Kùzu** (embedded graph) | vector support | Cypher | ❌ file-locked | ❌ **Upstream abandoned — do not adopt.** This also disqualifies cognee's default embedded graph |
| **Qdrant local mode** | real HNSW | sparse | ❌ single process | ❌ **the vendor says "Do not use for production" and the local format is incompatible with the server.** Note this is Mem0 OSS's *default* store |
| **SurrealDB** | `HNSW` + `DISKANN` | `FULLTEXT ANALYZER ... BM25` | ⚠️ embedded multi-process unverified | Promising on paper — verify embedded concurrency first |
| **PostgreSQL + pgvector** (ours) | HNSW, `vector_cosine_ops` | `tsvector` + GIN, `ts_rank_cd` | ✅ MVCC, advisory locks, multi-process | Mature, boring, and the only one already proven in *our* load |

### Memory systems — external services required

| System | Default | Minimum | Zero-service? | Verdict |
|---|---|---|---|---|
| **EverOS** | md + SQLite + **LanceDB** | same | ✅ | **Strongest direct alternative.** BM25 + vector ANN + scalar filter in **one LanceDB query**; publishes its consistency contract (writes strong, reads eventual ~10-15 s under load). Uses multiple SQLite files on purpose so a sync and an async writer never contend for one file lock |
| **txtai** | content storage **off** by default; SQLite when enabled; ANN = **Faiss** | same, or `content=sqlite` + ANN backend `sqlite` (sqlite-vec) + a sparse terms index | ✅ | Most configuration-flexible of the group. Headless single-library candidate; trap is that SQLite `wal` defaults to **false** |
| **LightRAG** | JSON KV + NanoVectorDB (one JSON file per namespace) + NetworkX (one GraphML file) | same, or Postgres/Mongo/OpenSearch for all four tiers | ✅ | Default is explicitly "not recommended for production"; in-source: *"Single writer per workspace"*, the file being "the **only** cross-process synchronization surface". Readers do a full file reload |
| **cognee** | **Kùzu** graph + **LanceDB** vectors + SQLite | Neo4j/FalkorDB for graph, external vector DB | ✅ on paper | ❌ *"Default Kuzu graph store uses file-based locking and is **not suitable for concurrent access from multiple agents**"* — and Kùzu is abandoned. Also: Postgres-as-graph is a **demo** feature with the production adapter under a commercial licence |
| **Mem0 OSS (library)** | embedded **Qdrant at `/tmp/qdrant`** + SQLite history | LLM/embedder key only | ✅ | Genuinely local, but its default vector store is the one its vendor disowns for production |
| **Mem0 OSS (server)** | **Postgres + pgvector** | same | ❌ | It *is* our architecture |
| **Letta** | pip → **SQLite**; Docker → **Postgres + pgvector** | SQLite via pip | ⚠️ | Maintainer, verbatim: *"we maintain migration scripts for postgres, but not for sqlite… if you want to freely upgrade and keep your data make sure to use postgres"* |
| **Memobase** | **Postgres + Redis** | same | ❌ | Two services |
| **Acontext** | Go API (Postgres, Redis, RabbitMQ, S3) + Python core (Postgres+pgvector, Redis, RabbitMQ, S3) | same | ❌ | Heaviest dependency graph surveyed. Deliberately **no embeddings** — retrieval is progressive disclosure over Markdown skill files |
| **Zep / Graphiti** | Graphiti: Neo4j / FalkorDB / Neptune+OpenSearch | embedded **FalkorDB Lite** (`graphiti-core[falkordblite]`, Python 3.12+) | ⚠️ | Kùzu support is deprecated. Hosted Zep uses a proprietary engine and needs no third-party graph DB; the OSS side always does |
| **Basic Memory** | Markdown + **SQLite FTS5** | same | ✅ | Local-first, MCP-native; a separate syncable vector layer exists, embedded ANN implementation unverified |
| **marm-memory** | SQLite WAL + serialised write queue | same | ✅ | Ships `--profile swarm` for *"Multiple agents sharing memory"* |
| **Engram (TAIPANBOX)** | one `.engram` file | same | ✅ | BM25 + vector, bitemporal facts, multi-agent (private observations + shared facts). **2 stars at fetch — unproven** |

### Cloud/API-first, ruled out on posture

Memobase's successor **Acontext**, **Supermemory**, **MemOS Cloud**, **MemU** self-host
("Single-device"), and any product whose self-hosting story is a locked-down
docker compose are not candidates for a fully local single-user swarm.

### The honest options

1. **Keep Postgres.** It is the only tier we have already proven under our own
   concurrency, and nothing in this survey offers a like-for-like replacement.
2. **Move to LanceDB** — the single embedded engine with a real multi-writer
   model, and the architecture EverOS actually ships. Cost: re-doing the
   LanceDB→Postgres migration we deliberately performed in July 2026 (see
   [ADR 0001](../adr/0001-postgresql-all-in-one-container.md) and the memory
   cost that drove it: 1.2-1.5 GB RSS spikes and 1.3 GB of disk waste under the
   cascade write stream). **Not recommended** without a new reason.
3. **Slim Postgres instead of replacing it.** The 3.72 GB database is **75%
   `atomic_fact`**. That is where a footprint reduction should be attempted
   first, and it requires no architectural change at all.


## 5. Shared memory for an agent swarm

Read from official docs and source by a survey agent. **agent-only**
throughout.

### How systems with an agent model scope memory

| System | Scoping keys | Cross-agent read by default? | Private vs shared | Conflict handling |
|---|---|---|---|---|
| **Letta** | `memory_blocks` per agent; `block_ids` shared across agents | **yes** — attach a block to N agents and "all others see the change immediately" | ✅ **structural**: per-agent blocks vs shared blocks, plus a read-only flag | ❌ `update()` replaces the whole block — last writer wins |
| **Letta (current direction)** | a **Git repository** cloned beside each agent's MemFS, synced by commit/push/pull | yes, via `git pull` | ✅ own MemFS vs shared repo | ✅ git merge semantics; explicit `sync` |
| **Zep** | `user_id` → a *user graph* fusing all threads; `group_id` / standalone graphs | ❌ across users ("Other users' data is never accessible"); ✅ across threads of one user | ✅ user graph vs group/standalone graph; **UserGroups with policy-based access control** | graphs are isolated, no merge |
| **Graphiti** | `group_id` on every node and edge | ❌ isolated; cross-namespace query is the caller's job | ✅ namespace isolation | not documented |
| **Mem0** | `user_id`, `agent_id`, `app_id`, `run_id` (+ org/project via API key) | ❌ by default | ⚠️ not first-class — achieved by a separate `add()` per participant | none; separate records |
| **Memori** | `entity_id` + `process_id` + `session_id` | ❌; the triple **is** the scope | ⚠️ `process_id` separates agents; no shared tier | none documented |
| **EverOS** | **orthogonal** `user_id`, `agent_id`, `app_id`, `project_id`, `session_id` | ⚠️ scoped, but `knowledge/` is global shared | ✅ strongest: `users/` vs `agents/` vs `knowledge/` | ✅ **supersession** via `deprecated_by` + a `reflection_report` audit trail |
| **LangGraph Store** | namespace **tuple**, matched **by prefix** | ⚠️ caller-defined; prefix matching can over-read | ❌ not first-class | ❌ no CAS or merge |
| **CrewAI** | hierarchical scope **path** under `root_scope` | ⚠️ scope-dependent; `include_private` defaults to false | ✅ `private` flag + `source` identity — only the producer sees its private rows | ⚠️ serialised single-worker save pool |
| **Supermemory** | a single `containerTag` string (hierarchical via `:`) | ❌ strict — "one user can never see another user's memories" | ⚠️ convention only, but enforced at the **vector-namespace** level | none documented |
| **MIRIX** | `user_id` + `session_id`; six memory *types* in one shared DB | ✅ across its eight internal agents | ❌ | `auto_dream` merges duplicates and resolves stale conflicts |
| **MemOS** | memory cubes — "isolation, controlled sharing, and dynamic composition across users, projects, and agents" | ⚠️ configurable per cube | ✅ explicitly | NL feedback to correct or replace |
| **Acontext** | projects + learning spaces + sessions | ✅ "share across agents, LLMs, and frameworks" | ⚠️ by project/space, not by agent role | distillation rewrites skill Markdown |
| **memU** | per-user wiki; agents are interchangeable clients | ✅ by design — "Across Agents" | ❌ | not documented |
| **mcp-memory-service** | `X-Agent-ID` header auto-tags as `agent:<id>`; `crew:` / `proj:` / `user:` namespaces | ✅ **yes** — memory is shared across all agents and runs | ❌ no private tier; tags are filters, not boundaries | dedup by `content_hash` |
| **marm-memory** | runtime profiles (incl. `swarm`, `swarm-max`) | ✅ by design | ⚠️ profile-level | ✅ **serialised write queue** + WAL |
| **Engram (TAIPANBOX)** | one `.engram` file | ✅ facts and the graph are shared | ✅ **explicit**: "Each has its own private observations; extracted facts and the relationship graph are shared" | bitemporal facts, closed not deleted |
| **open-multi-agent** | `<agent>/<key>` namespaces + a mandatory `{agent: name}` marker | ✅ namespaced but readable | ⚠️ reserved `__oma_checkpoint__/` and `__oma_approval__/` hidden from agents | `compareAndSet` **in-process only** — no cross-process lock in `FileStore` |

### What our own model looks like in comparison

We are at the "one flat space, attributed by `sender_ids`" end of this spectrum:
15 agents, 1 `(app, project)` space, 2 owners, no private tier, attribution via a
JSON list rather than a first-class column. Every system above that has thought
about this deliberately has some notion of *private vs shared*, and the two with
the strongest isolation (Zep's UserGroups, Supermemory's per-container API-key
scopes) enforce it at infrastructure rather than application level.

### Design patterns worth copying

1. **Tenancy key in the path, owner key in the record.** EverOS partitions by
   `<app_id>/<project_id>` **in the filesystem path** and keeps only the
   file-level owner in frontmatter — `app_id`/`project_id` are explicitly *not*
   frontmatter fields. A filesystem guarantee costs nothing at query time and
   survives a human editing the file.
2. **Make the shared tier a directory, not a flag.** EverOS's `users/` /
   `agents/` / `knowledge/` split, and Dakera's rule worth quoting verbatim:
   *"You might consider using a single namespace with metadata tags to separate
   private vs. shared memories. **Don't.** … Tags are application-level
   conventions; namespaces are infrastructure-level guarantees."*
3. **Attribution must be structural, never inferred from content.** Three
   independent systems converge: Mem0 — *"Attribution follows the
   `user_id`/`agent_id`/`run_id` you pass to `add()`, never the `name`"*;
   open-multi-agent forces an `{ agent: agentName }` marker on **every** write;
   mcp-memory-service auto-tags from an `X-Agent-ID` header.
4. **Never key joins on a per-file sequence alone.** EverOS's entry IDs are
   per-file, so the same `ep_20260601_00000001` recurs across users — joins
   must key on **`(scope_id, entry_id)`**.
5. **Resolve supersession by deprecation, not deletion, and filter it at the
   search layer.** This is exactly what our `deprecated_by` already does; the
   EverOS docs confirm search "filters automatically exclude rows where
   `deprecated_by IS NOT NULL`".
6. **Make retrieval mode a per-kind choice.** MIRIX exposes
   `search_method ∈ {bm25, embedding, string_match}` per memory type. Right for
   us: an exact symbol name wants lexical match, a paraphrased preference wants
   vectors.
7. **Publish the consistency contract.** EverOS states writes are strong and
   reads eventual. A swarm reads its own writes constantly; without a stated
   contract every agent author assumes read-your-writes.
8. **Split lock domains on purpose.** EverOS runs three SQLite files
   (`system.db`, `ome.db`, `ome.aps.db`) explicitly so a sync and an async
   writer never contend for one file lock.
9. **Two-tier memory: always-in-context vs retrieved.** Matches Letta's blocks
   and the swarm research note's prescription of "`MEMORY.md` always-in-context
   + `memory/*.md` semantically searchable". This is precisely the recency-block
   question from [ADR 0002](../adr/0002-recency-digest-granularity.md): a small
   curated always-injected tier, plus a large retrieval tier.
10. **Convergence signal.** Letta deprecated shared memory *blocks* in favour of
    **Git repositories**; EverOS, Basic Memory, Acontext, memU and A-MEM all
    store memory as human-readable files. **The industry trend is toward
    file-based memory, not away from it** — which is an argument for keeping our
    markdown-as-truth invariant, not for replacing it.

### Conflict strategy is a spectrum; pick per tier

The realistic menu, all of which we could adopt incrementally:
**append-only + optimistic retry** (Lance: appends never conflict, overwrites
can); **serialise the writer** (CrewAI's single-worker pool, marm-memory's
serialised queue, EverOS's portalocker guard); **supersede** (`deprecated_by`,
Engram's bitemporal close-out); **merge with audit** (MIRIX auto-dream, our own
`reflect_episodes`). What must not happen is leaving it implicit.

### The gap nobody fills

No system was found whose **primary** positioning is "shared memory for agent
swarms". Searching that phrasing returns small projects — `marm-memory`'s
`--profile swarm`, `desplega-ai/agent-swarm`'s research note, AgentField's
four-scope fabric (`global`/`session`/`actor`/`workflow` with hierarchical
lookup), `open-multi-agent`, SEGYR — none established. The common design in the
wild is a **blackboard plus a scope chain**, which is close to what we already
run. Separately, mcp-memory-service's own guide documents that one of its
search endpoints **silently ignores** a `tags` filter — a concrete reminder that
documented scoping and enforced scoping are different things.
