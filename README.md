# 🧠 Corti

**Cognitive Substrate for AI Agent Swarms**

*Markdown Source of Truth · Sub-second Cascade Sync · Postgres Vector Search · Multi-Agent Adaptive*

[Quick Start](#-quick-start) · [Install & Integration](#-installation--plugin-integration) · [Storage Stack](#-storage-stack--path-layout) · [Lifecycle](#-memory-lifecycle-pipeline) · [Design Philosophy](#-design-philosophy-feature-removals)

---

Corti (corti): persistent, self-evolving memory layer for AI agents. Decouples runtime state from static weights. Stores state as diffable Markdown. Rebuilds high-performance Postgres/SQLite vector/BM25 indexes. Fully self-hosted.

```
┌─────────────────────────────────────────────────────────────────────────┐
|                          AI AGENT COGNITIVE ARCHITECTURE                 |
|                                                                         |
| 1. LLM Backend (API)     --> Brainstem / Basal Ganglia (Stateless Core) |
| 2. Agent Framework       --> Sensorimotor Exoskeleton (Hands & Feet)   |
| 3. Memory ───────────────--> Agent Cortex (Persistent State Core)       |
└─────────────────────────────────────────────────────────────────────────┘
```

1. **Persona Agent vs. CLI Agent**: Target audience = Persona Agents (e.g., Hermes Agent, OpenClaw Agent) ≠ CLI task-runners. Persona Agents require persistent background identity, preference awareness, context stability.
2. **State-Weight Decoupling**: Trillion-parameter static model weights cannot mutate for local projects. Runtime state must be decoupled from static weights.
3. **Random Pre-Injection of Memory Fragments**: Random pre-injection of memory fragments ➔ context. Matches human long-term memory: constant background context + targeted activation weights. Widens activation surface, prevents recall omission.
4. **Runtime Epistemic Limits**: Map vector spaces directly to context. Resolves runtime bottleneck: LLMs "do not know what they do not know". Resident memory ≠ On-demand query ("document lookup"). Memory requires residency to define identity.

---

## ⚡ Key Features

* **Simplicity over Complexity (Why Not Knowledge Graphs?)**:
  * *Open-World Semantic Discrepancy*: Open-world semantic parsing fails to converge. Code parsing has strict AST syntax, but natural language has systemic ambiguity. LLM parsing under AST/Safeguards generates naming collisions (e.g., "ABC Project" parsed sometimes as "ABC", other times as "ABC Project"), stalling graph construction. Formal ontology engineering (KBpedia, Wikipedia scale) is cost-prohibitive.
  * *Diminishing Search Utility*: Graph traversal provides minimal marginal gain over raw keyword/semantic search. Open-world information lacks strict dependency chains; it relies on loose causality/inclusion. LLMs possess massive pre-trained associative spaces; passing raw keywords + semantic vectors triggers the correct associative touchpoints instantaneously without explicit graph edges.
  * *Memory-Knowledge Isomorphism & Limitation*: Memory and knowledge are isomorphic at the root [1] [2]. Limitation: Corti does not currently distill memory into structured academic knowledge. Resolution: Semantic keywords/vectors are the most cost-effective trigger mechanism to activate LLM's pre-trained latent space.
  * *Theoretical Grounding*: "Memory and knowledge share an isomorphic graph base $G = (V, E)$. The distinction is operational (meta-attribute variance), not structural (data format divergence)." [1]
* **Dual Ingestion Paths**:
  * *Silent Background Digestion*: Automatic, silent background ingestion of conversation logs from active agent sessions.
  * *Explicit Agent PUT*: Direct, programmatically triggered memory writes committed by the agent.
* **Retrieval & Context Injection**:
  * *Startup Fragment Pre-injection*: Pre-loads random memory fragments at session startup ➔ resident context. Prepares cognitive triggers for keyword activation.
  * *Targeted Prompt Enrichment*: Per-prompt targeted semantic search. Directly appends retrieved memory context to user input.
* **Flexible Sharing & Isolation**:
  * Multi-dimensional namespace isolation across 5 axes: `app_id`, `user_id`, `agent_id`, `project_id`, `session_id`.
  * Namespace mapping: identical setting values across axes ➔ memory sharing; distinct setting values ➔ absolute memory isolation.
* **Markdown Source of Truth** — Human-readable, diffable, Git-versioned `.md` files (`~/.corti/`). Rebuilds 100% from flat files.
* **Sub-Second Cascade Watcher** — Direct edits (VS Code, Obsidian, Neovim) ➔ automatic SQLite metadata & Postgres vector synchronization.
* **Self-Evolving Offline Memory Engine (OME)** — Background reflection loops compress raw conversation logs into atomic facts and dynamic profiles.
* **Hybrid Postgres / PGLite Vector Search** — Merges `pgvector` semantic similarity, BM25 keyword matching, and scalar SQL filters in single-query execution.
* **Seamless Agent Integrations** — Plugins for Hermes Agent, Claude Code, DeepSeek Harness (`dsh`), FastAPI HTTP endpoints, and CLI.

---

## 💾 Storage Stack & Path Layout

Three-piece embedded engine. Markdown = Single Source of Truth. SQLite + Postgres = Derived, 100% Rebuildable.

### 1. SQLite State Manager
* **Role**: Primary state engine, audit logger, and raw conversation buffer.
* **Storage Mechanism**: Collects raw conversation stream inputs. Stores them as structured JSON blobs / block objects representing unprocessed message arrays. Serves as canonical source for all downstream compiled memory artifacts.

### 2. PostgreSQL (PG) Memory Core
* **Role**: High-performance carrier substrate of persistent, query-ready memories.
* **Storage Mechanism**: Evaluates raw Markdown files, "chews" (digests) semantic structures, and compiles them into hierarchical Postgres table schemas:
  * **Episode**: Individual Agent Session / Conversation segment (conversation streams chunked logically by topic boundaries).
  * **Facts**: Refined and distilled semantic outputs extracted from Episodes. Carries structured attributes, `subject` indexing, and concise keyword-triggered `title` metadata.

| Layer | Technology | Function / Assets | Rebuildability |
|---|---|---|---|
| **Truth** | Plain Markdown (`.md`) | Canonical memory content, human-editable, portable. | — |
| **State** | SQLite (`aiosqlite`) | Raw transaction buffer (JSON blobs), audit log, OME state. | ✅ Rebuildable from `.md` |
| **Index** | Postgres + pgvector | Epised/Facts vector + BM25 hierarchical search index. | ✅ Rebuildable from `.md` |

### Path Namespace Mapping:
```
~/.corti/                                ← Memory Root (CORTI_ROOT)
├── <app_id>/                                  ← App isolation boundary
│   └── <project_id>/                          ← Project isolation boundary
│       ├── users/
│       │   └── <user_id>/
│       │       ├── user.md                    ← Single-file rewrite (User Profile)
│       │       ├── episodes/                  ← Daily-log append (Raw conversations)
│       │       └── .atomic_facts/             ← Daily-log append (OME extracted facts)
│       └── agents/
│           └── <agent_id>/
│               └── .foresights/               ← Daily-log append (OME extracted foresight)
└── .index/                                    ← Ignored system cache (SQLite + Postgres)
```

---

## 🔄 Memory Lifecycle Pipeline

### Phase 1: Stream Accumulation
* `/add` endpoint buffers raw messages per `(session, app, project)` in SQLite.
* Real-time boundary detector evaluates message transitions ➔ triggers extraction.

### Phase 2: Boundary Ingestion (Sync)
* `/flush` endpoint triggers manual/forced boundary extraction.
* Extracted `MemCell` written synchronously to `episodes/episode-<date>.md` on disk.
* HTTP response returns immediately after Markdown commit.

### Phase 3: Offline Synthesis (Async OME)
* Offline Memory Engine (OME) schedules asynchronous synthesis strategies.
* Evaluates raw episodes ➔ generates derived Markdown assets:
  * `atomic_facts` (individual facts)
  * `foresights` (predictive context)
  * `user.md` (user profiles)

### Phase 4: Cascade Sync (FS Watcher)
* Native file system events (`watchdog` on Linux/macOS) intercept Markdown writes.
* Pending changes queued to durable SQLite table `md_change_state` (prevents crash loss).
* Multi-threaded workers pull from queue ➔ compute incremental SHA256 diffs ➔ re-embed only mutated entries ➔ upsert Postgres (Episodes / Facts schemas).

---

## 🚀 Quick Start

### 1. One-Command Install

```bash
curl -fsSL https://raw.githubusercontent.com/pgmi-builds/corti/main/install.sh | bash
```

What this does: checks Docker → pulls the pre-built image from Docker Hub → seeds `~/.corti/` with default config → auto-installs agent plugins (**Hermes, Claude Code, DeepSeek Harness**) if detected.

**Wire a single agent only** (Corti server already running elsewhere):

```bash
curl -fsSL https://raw.githubusercontent.com/pgmi-builds/corti/main/install.sh | bash -s -- --only-dsh      # DeepSeek Harness
curl -fsSL https://raw.githubusercontent.com/pgmi-builds/corti/main/install.sh | bash -s -- --only-hermes   # Hermes Agent
curl -fsSL https://raw.githubusercontent.com/pgmi-builds/corti/main/install.sh | bash -s -- --only-claude   # Claude Code
```

**Requirements**: Docker 24+.

> **Slim variant** — if you already have PostgreSQL 18+ with pgvector:
> ```bash
> export DB_HOST=... DB_PORT=5432 DB_NAME=corti DB_USER=corti DB_PASSWORD=...
> curl -fsSL https://raw.githubusercontent.com/pgmi-builds/corti/main/install.sh | bash -s slim
> ```
> Slim image: ~400 MB vs 1.2 GB all-in-one. Pinned version: `bash -s v0.3.4-slim`.

### 2. Start the Server

For upgrades or transfers of an existing external-PG service, use the
[verified local deployment workflow](docs/local-deployment.md). It builds committed
source, pins the image ID, backs up external data, and verifies installed source.
Copying code into a running container does not update its underlying image.

```bash
docker run -d --name corti \
  -p 5473:5473 \
  -v ~/.corti:/home/app/.corti \
  m1research/corti:latest
# Slim variant (external PG):
# docker run -d --name corti -p 5473:5473 -v ~/.corti:/home/app/.corti \
#   -e DB_HOST=... -e DB_NAME=corti -e DB_USER=corti -e DB_PASSWORD=... \
#   m1research/corti:slim
# Verify
curl http://localhost:5473/health
# → {"status":"ok"}
```

### 3. Store & Search (HTTP API)

```bash
# Store preference
curl -s -X POST http://localhost:5473/api/v1/memory/add \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"default_user","messages":[{"role":"user","content":"Prefer strictly typed Python, dark mode UI","timestamp":'"$(date +%s)000"'}]}'

# Search memory store
curl -s -X POST http://localhost:5473/api/v1/memory/search \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"default_user","query":"dev styling preference"}'
```

---

## ⚙️ Installation & Plugin Integration

### 1. One-Command Docker Install

The install script (`install.sh`) handles everything automatically:

1. **Docker pull** — fetches pre-built `m1research/corti:latest` from Docker Hub (no clone, no build)
2. **Data seeding** — extracts default config from the image into `~/.corti/corti.toml`
3. **Agent plugin installation** — extracts plugin source from the image into the agent's local plugins directory:

| Agent | Target | Source |
|---|---|---|
| Hermes Agent | `~/.hermes/plugins/corti/` | `src/integrations/hermes/` |
| Claude Code | `~/.claude/skills/corti/` | `src/integrations/claude-code/` |
| DeepSeek Harness | every `~/.dsh/profiles/*` (via `dsh plugin add`) | `src/integrations/deepseek-harness/` |

Plugins are **copied** (not symlinked), so they survive repo updates and work
independently. The dsh plugin instead installs through dsh's own pnpm-backed
profile manager — from the GitHub repo directly, so it works even without the
Docker image on the same host.

### 2. Manual Plugin Install

If the install script's auto-detection didn't pick up your agent, or you installed a new agent
after Corti — pull the plugins directly from the GitHub repo (no Docker required):

```bash
# Hermes Agent
curl -fsSL https://github.com/pgmi-builds/corti/archive/refs/heads/main.tar.gz | \
  tar -xz --strip-components=3 -C ~/.hermes/plugins/ corti-main/src/integrations/hermes
mv ~/.hermes/plugins/hermes ~/.hermes/plugins/corti
hermes plugins enable corti

# Claude Code — dropped into ~/.claude/skills/ for auto-discovery
# (hooks/hooks.json + MCP auto-loaded on next session, zero CLI)
curl -fsSL https://github.com/pgmi-builds/corti/archive/refs/heads/main.tar.gz | \
  tar -xz --strip-components=3 -C ~/.claude/skills/ corti-main/src/integrations/claude-code
mv ~/.claude/skills/claude-code ~/.claude/skills/corti

# DeepSeek Harness (dsh) — installs into every existing dsh profile
dsh plugin --profile web add https://github.com/pgmi-builds/corti#src/integrations/deepseek-harness
# then point it at your Corti server (once):
#   echo "CORTI_BASE_URL=http://<corti-host>:5473" >> ~/.dsh/.env
```

Pin to a specific version by replacing `main` with a tag (e.g. `v0.3.4`).

---

## 🛠 Design Philosophy: Feature Removals

Corti actively removes legacy **Agent Case** and **Agent Skill** modules from the execution loop. 

### 1. Removal of Agent Case (Trajectory Summaries)
* *Original Feature*: Reviewed memory histories to summarize individual Agent trajectories and task scenarios for future replays.
* *Removal Reason*: Trajectory contexts are already cleanly digested into the base hierarchical schemas (`episode`, `facts`, `subject`, `title`). High-parameter LLMs contain massive native pre-trained intelligence. When a semantic cue is matched, passing raw facts and conversation logs directly into the context is mathematically sufficient. The LLM can generalize and resolve local tasks dynamically. Artificial pre-summarization is redundant.

### 2. Removal of Agent Skill (Custom Capability Stores)
* *Original Feature*: Provided execution environments for registered Agent capability scripts.
* *Removal Reason*: Legitimate Agent platforms (Hermes, Claude Code, OpenClaw) possess robust, native, open skill registries and plugins. Repeating these capabilities inside the memory substrate violates minimalism.

### 3. Core Architectural Philosophies
* **Linux Single-Tool Philosophy**: Adherence to radical minimalism. Focus entirely on optimizing the memory state layer to the absolute theoretical limit. Never duplicate services handled by surrounding execution chassis.
* **Mitigating Cognitive Overlap**: The primary pain point of autonomous agents is "not knowing what they do not know". Memory exists to guide prompt context to the correct coordinate in the model's pre-trained latent space. In the 2026 AI-driven development era, top-level architectural blueprints and prompt context selection are far more valuable than local code generation scripts. Selecting and attaching correct facts/episodes to prompt context is fully sufficient for high-parameter LLM reasoning.

---

## 🔬 Theoretical Foundations & Cognitive Grounding

### 1. Updated Complementary Learning Systems (CLS)
* *Citation*: Kumaran, D., Hassabis, D., & McClelland, J. L. (2016). What learning systems do intelligent agents need? Complementary learning systems theory updated. *Trends in Cognitive Sciences*, 20(7), 350-375. [1]
* *Finding*: Neocortical learning rates scale with consistency of new input against existing schema topology.
* *Relevance*: Verifies memory consolidation processes scale on mutual compatibility; hippocampal replay trains neocortical generative structures. Empirically anchors the **Memory-Knowledge Isomorphism Hypothesis (MKIH)**.

### 2. Tolman-Eichenbaum Machine (TEM)
* *Citation*: Whittington, J. C., Muller, T. H., Mark, S., Chen, G., Barry, C., Burgess, N., & Behrens, T. E. (2020). The Tolman-Eichenbaum Machine: Unifying space and relational memory through generalization in the hippocampal formation. *Cell*, 183(5), 1249-1263. [2]
* *Finding*: Medial entorhinal cortex (MEC) forms abstract structural knowledge basis ↔ hippocampus binds sensory episodic memories.
* *Relevance*: Proves memory and abstract schema share identical spatial basis representations; distinction is topological conjunction, not data format.

### 3. Clone-Structured Cognitive Graph (CSCG)
* *Citation*: George, D., Rikhye, R. V., Gothoskar, N., Guntupalli, J. S., Dedieu, A., & Lázaro-Gredilla, J. (2021). Clone-structured graph representations enable flexible learning and vicarious evaluation of cognitive maps. *Nature Communications*, 12(1), 2392.
* *Finding*: Space navigation, relational reasoning, and episodic maps consolidate within identical unified graph structures.
* *Relevance*: Mathematical validation of multi-format memory-schema convergence under unified graph topologies.

### 4. Empirical Validation: LoCoMo Dataset
* *Evaluation baseline*: LoCoMo benchmark (Maharana et al., 2024), 1,540 multi-session evaluation queries.
* *Results Matrix*:
  * Single-hop Fact Retrieval: **94.0%**
  * Multi-hop Reason Ingestion: **91.0%**
  * Dialogue-Grounded Open-Domain: **80.2%**
  * Temporal Sequence Reasoning: **95.5%**
  * Weighted Average: **93.3%**
* *Theoretical Interpretation*:
  * Simple semantic vector retrieval + temporal metadata-filtering resolves **95.5%** of complex chronological reasoning.
  * Confirms "Keyword + Semantic Similarity + Temporal Metadata" is mathematically sufficient to trigger context-appropriate recall.
  * Bypasses the high parsing latency, cost, and parse-divergence bottlenecks of formal knowledge graph construction.

---

## ⚖️ EverOS Provenance & Minimalist Production Overhaul

Corti originally borrowed code structures and core concepts from the upstream [EverOS](https://github.com/EverMind-AI/EverOS) codebase (Apache-2.0). However, the system has undergone deep local redevelopment, custom architecture optimization, and design philosophy shifts.

### Minimalist Engineering & Feature Removal:
* Applied strict UNIX-style minimalism to optimize execution paths and reduce cognitive bloat.
* Removed `agent_case` (trajectory summaries) and `agent_skill` (capability stores) entirely. Upstream EverOS retains these components; Corti prunes them because high-parameter LLM latent spaces need only raw semantic triggers (`episode` and `facts` schemas) rather than redundant, pre-summarized local structures and custom execution registries.

### Upstream Open-Source Limitations:
* **The LanceDB Bottleneck**: Upstream EverOS relies on LanceDB—an embedded columnar store designed for flat, high-read research data. In persistent daemon workloads, LanceDB loads full tables into memory and copies during mutation. This results in severe, unrecoverable memory leaks and thread-locking, rendering it completely unsuitable for multi-agent self-hosted production.
* **SaaS Commercial Split**: EverOS open-source core acts as a funnel for their proprietary cloud SaaS. Consequently, its "out-of-the-box" local self-hosted readiness is low, lacking multi-tenant transactional safety, concurrent execution pipelines, and robust local persistence guarantees.

### Custom Corti Enhancements:
* **Transactional Postgres Overhaul**: Replaced the entire retrieval chassis with a robust, production-grade PostgreSQL / pgvector backend (offering embedded PGLite support for lightweight instances), resolving storage leaks and securing transactional safety.
* **Persistent API Daemon**: Overhauled execution-loop CLI overhead, replacing it with an active, concurrent FastAPI daemon to serve concurrent Agent swarms safely.
* **Lightweight Memory Fragments**: Dumped prompt-bloating full-episode ingestion, replacing it with title-based fragment retrieval. Injecting 20 concise metadata fragments widens the recall trigger surface while preserving token context boundaries.
* **Plugin Overhaul**: Fully rewrote both the Hermes and Claude Code plugins to communicate seamlessly with the self-hosted local daemon (decoupling them from cloud API bindings).

---

## 📄 Footnotes & Citations

[1] Kumaran, D., Hassabis, D., & McClelland, J. L. (2016). What learning systems do intelligent agents need? Complementary learning systems theory updated. *Trends in Cognitive Sciences*, 20(7), 350-375.

[2] Whittington, J. C., Muller, T. H., Mark, S., Chen, G., Barry, C., Burgess, N., & Behrens, T. E. (2020). The Tolman-Eichenbaum Machine: Unifying space and relational memory through generalization in the hippocampal formation. *Cell*, 183(5), 1249-1263.

---

## 📄 License

[Apache-2.0 License](legal/LICENSE).
