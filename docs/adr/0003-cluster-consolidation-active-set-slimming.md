---
status: proposed
---

# Consolidate episode clusters into merged narratives to shrink the active set

Search ranks a **deprecated-excluded view** of the corpus, so the set of rows
that competes for every recall is exactly the set that is *not* deprecated.
That active set only ever grows: every turn appends an episode and its atomic
facts, and nothing ever leaves. The proposal is to run the already-built
Reflection pipeline as an **active-set slimmer** — periodically merge the
episodes of one similarity cluster into a single consolidated narrative
episode and mark the fragments it replaces `deprecated`, which removes them
from vector, semantic and keyword ranking while leaving them in markdown and
in Postgres for direct lookup.

Nothing in the merge path is new. What is missing is a rollout: the strategy
is disabled by default, its candidate selection is ordered for a different
goal, and nothing measures the effect it has on the active set.

## Context

### What "active" means here

`deprecated_by` is already a first-class column on `episode` and
`atomic_fact`, with a partial index (`WHERE deprecated_by IS NULL`,
`infra/persistence/pg/ddl.py`) built for active-only scans. Retrieval
compiles **one** `where` string per request
(`memory/search/manager.py`, via `search/filters.py::compile_filters`) and
threads it through every path — `KEYWORD` sparse recall, `VECTOR` dense
recall, `HYBRID` (both recallers), `AGENTIC` multi-round, and the MaxSim
atomic-fact recall. The clause that matters is a single line:

```python
if owner_type == "user":
    base.append("deprecated_by IS NULL")
```

`/api/v1/memory/get` (the listing endpoint that feeds the startup recency
sample) shares the same compile path, so a deprecated entry disappears from
the recency catalogue too. Deprecation is therefore a **retrieval-level**
operation, not a delete: the row, the markdown block and the primary key all
stay, and `deprecated_by` records *what replaced it*.

That is the property the proposal depends on: memory stays append-only, the
old entries remain addressable by `entry_id`, and only the searchable
projection shrinks.

### What the corpus looks like today

Measured 2026-10-01 on the `shared-agent-memory/default` space:

| Metric | Value |
|---|---|
| Postgres database | 3,751 MB |
| `atomic_fact` | 2,791 MB (74% of the database) |
| `foresight` | 763 MB |
| `episode` | 187 MB |
| Episodes | 6,404 |
| Atomic facts | 166,072 |
| Foresights | 44,830 |
| Deprecated rows | **0** |
| Clusters | 857 (539 singletons, 318 with >= 2 members) |
| Cluster members | 5,284 episodes (83% of all episodes) |
| Markdown corpus | 101 MB |
| SQLite state | 98 MB |

Two conclusions follow. First, the volume is overwhelmingly in the *facts*
extracted from episodes, so a slimmed episode must take its atomic facts with
it or the database will not move. Second, clusters already cover 83% of
episodes — the partition this feature needs is already being maintained on
every extraction.

### Why the fragments are worth replacing

The fragments that Reflection consumes are the output of a boundary detector
that, under the DeepSeek Harness integration, fires **once per agent turn**
(the plugin force-flushes at `turn/end`). A single multi-hour session
therefore lands as dozens of near-adjacent memcells: median episode block is
about 2.3 KB, and one session can own dozens of them. Individually they are
low-value search targets — none of them is the task, each is a slice of it —
and collectively they crowd out everything else.

## What already exists

`corti.memory.reflection.ReflectionOrchestrator` implements the whole flow,
in four steps:

1. **Select** — `_select_candidates` takes this owner's clusters, keeps those
   with `count >= 2` (or `count > 1` if the cluster was already reflected),
   drops cluster ids that already have a `reflection_report`, sorts by member
   count descending, and truncates to `_MAX_CLUSTERS_PER_RUN = 10`.
2. **Merge** — `_load_cluster_episodes` reads the member episodes, the algo
   reflector (`_call_reflector`) produces one narrative, and
   `_write_merged_episode` appends it to the owner's episode daily log with
   `parent_type: cluster` / `parent_id: <cluster_id>` (ordinary episodes are
   `parent_type: memcell`).
3. **Re-extract** — `_emit_and_wait_extraction` emits `EpisodeExtracted` for
   the merged narrative and waits up to 120 s, so the merged episode gets its
   own atomic facts and takes part in normal clustering. Markdown is the only
   write target; the cascade indexes it like any other entry.
4. **Deprecate** — `_resolve_deprecation_targets` re-reads cluster membership
   and intersects it with the snapshot taken at selection time (a member that
   left mid-run is left alone), `_apply_deprecation_writes` patches the
   `deprecated_entries` map in the affected markdown frontmatter **and**
   writes `deprecated_by = <merged_entry_id>` onto the member episodes and
   every atomic fact whose `parent_id` is one of them, then
   `_update_cluster_after_merge` removes the deprecated members from the
   cluster, adds the merged episode, and rewrites the centroid from the
   merged narrative with `count = 1`.

The write is idempotent in the direction that matters: because the markdown
frontmatter carries `deprecated_entries`, the cascade re-derives the
`deprecated_by` column on every sync, so a rebuild from markdown reproduces
the same active set rather than resurrecting the fragments.

`_create_reflection_report` records each run (`reflection_report` table,
currently empty), and `_detect_orphans` logs when a cluster already has a
live merged episode — one safeguard against merging the same cluster twice.

The strategy wrapper is `strategies/reflect_episodes.py`: an offline `Cron`
strategy (`0 2 * * 1`, weekly) that enumerates the distinct owner scopes from
the cluster table and runs the orchestrator per owner. It ships
`enabled=False`; `~/.corti/ome.toml` has the matching block commented out.

## Decision

Adopt Reflection as the active-set slimmer, with the following changes to
make it a controlled, measurable rollout rather than a switch.

### 1. Order candidates by payoff, not by size

`_select_candidates` sorts by member count descending, so a run spends its
ten slots on the biggest clusters first. That is the right order if the goal
is "consolidate the most fragments"; it is the wrong order if the goal is
"shrink the active set per LLM token spent", because it front-loads the
expensive merges. Sort by *estimated reclaim* instead: members removed minus
one (the merged entry), weighted by the cluster's atomic-fact count, and skip
clusters below a floor (proposal: 3 members) so single-pair merges do not
consume a slot.

### 2. Make the per-run budget explicit and configurable

`_MAX_CLUSTERS_PER_RUN` is a module constant. Move it, the minimum cluster
size, and the target owner scope into `[reflection]` in
`config/default.toml`, so an operator can run a bounded dry pass (one
cluster) before turning on the full weekly job.

### 3. Give the run a report-first mode

Add a `dry_run` flag that performs Select and Merge planning but stops before
`_apply_deprecation_writes`, and emits the reclaim estimate per cluster. This
is what makes the feature reviewable: the operator sees which clusters would
collapse and how many facts would leave the active set before anything is
deprecated.

### 4. Fix `entry_id` joins before they matter more

`cluster_member.member_id` stores a bare `entry_id`, and `entry_id` is only
unique per `(owner_id, md_path)` — the daily-log sequence is allocated per
file. Today's corpus already has 70 episode ids, 1,910 atomic-fact ids and
503 foresight ids that appear under two owners, plus 593 ids duplicated
*within* the 2026-09-11 files. Reflection joins on bare `entry_id`
throughout (`_resolve_deprecation_targets`, `_deprecate_db_episodes`,
`_update_cluster_after_merge`), so a collision means deprecating the wrong
owner's row. Two acceptable fixes:

- store `(owner_id, entry_id)` in `cluster_member.member_id`, or
- add an `owner_id` column to `cluster_member` and scope every lookup by it.

Either way this is a schema migration and must land before the feature is
enabled for more than one owner.

### 5. Keep the operator escape hatch

Deprecation is reversible by construction (`deprecated_by = NULL` restores
search) but there is no surface for it. Ship a `corti memory deprecate` /
`corti memory restore` CLI pair, or at minimum document the two SQL updates,
so an over-eager merge can be undone without hand-editing markdown.

## Phased plan

| Phase | Deliverable | Gate |
|---|---|---|
| 0 | `entry_id` join scoping (migration + callsite updates) | existing tests green; cluster lookups scoped by owner |
| 1 | `dry_run` mode + reclaim estimate in the reflection report | a dry run over the live corpus produces a reviewable table, no writes |
| 2 | Config plumbing (`max_clusters_per_run`, `min_cluster_size`, owner scope) | config round-trip test |
| 3 | Bounded live run: one owner, 1 cluster, manual trigger | cluster shrinks to 1 member, fragments deprecated, merged episode searchable, facts deprecated |
| 4 | Candidate ordering by reclaim; weekly cron enabled for the main owner | active-set size and DB size trend down run over run |
| 5 | Default on for new installs; CLI restore | documented rollback exercised once |

Phase 3 is the real decision point: it is the first irreversible-looking
write on live memory, and it should be observed end-to-end (markdown →
cascade → Postgres → search) before the cron is enabled.

## Risks

- **Lossy merge.** The merged narrative is one LLM pass over many fragments;
  a detail that appears once can be dropped. Mitigation: fragments stay in
  markdown and Postgres, addressable by `entry_id`; `deprecated_by` records
  the replacement so the relationship is auditable after the fact. The
  documentation already warns that re-merging an already-merged cluster is
  lossy — hence `count > 1` for reflected clusters and a weekly (not hourly)
  cadence.
- **Cost.** Each cluster costs one reflection LLM call plus one embedding,
  and the merged episode then costs a full extraction pass. Ten clusters per
  week is a bounded, predictable spend; raising the budget needs the reclaim
  estimate to justify it.
- **Provider dependency.** The merge needs a working embedder
  (`_update_cluster_after_merge` re-embeds the merged narrative) and a
  working LLM. Both fail closed — clusters are skipped and logged, the run
  continues — so an outage delays consolidation rather than corrupting it.
- **Recall regression.** Deprecation removes rows from ranking, so a query
  that only matched a fragment now matches the merged narrative or nothing.
  Mitigation: a fixed query set replayed before and after each phase, and the
  merged episode's own atomic facts re-enter the index as active rows.
- **Deprecating the wrong owner's row.** This is the `entry_id` collision in
  the current schema — the reason phase 0 exists.

## Verification

Each phase must be able to answer:

1. **Active set** — `SELECT count(*) FROM episode WHERE deprecated_by IS NULL`
   and the same for `atomic_fact`, before and after.
2. **Reversibility** — a deprecated entry is still in markdown, still in
   Postgres, and returns to ranking after `deprecated_by = NULL`.
3. **Consistency** — markdown and Postgres still agree on
   `(md_path, entry_id)` for all three kinds (the check used by
   [memory-db-cleanup.md](../memory-db-cleanup.md)).
4. **Cluster integrity** — every cluster member exists in markdown,
   `cluster.count` equals its member rows, and every member belongs to a
   space that still has a markdown root.
5. **Search behaviour** — the fixed query set returns the merged narrative
   for queries that previously returned fragments.

## Appendix: reproduction

Cluster inventory and coverage:

```sql
-- clusters, members, deprecated rows (SQLite + Postgres)
SELECT count(*) FROM cluster;                          -- 857
SELECT count(*) FROM cluster_member;                   -- 5284
SELECT count(*) FROM episode WHERE deprecated_by IS NOT NULL;      -- 0
SELECT count(*) FROM atomic_fact WHERE deprecated_by IS NOT NULL;  -- 0
```

Database composition:

```sql
SELECT pg_size_pretty(pg_total_relation_size('atomic_fact')),  -- 2791 MB
       pg_size_pretty(pg_total_relation_size('foresight')),    -- 763 MB
       pg_size_pretty(pg_total_relation_size('episode'));      -- 187 MB
```

`entry_id` collisions (bare id is not unique):

```bash
# count blocks vs distinct ids per kind, per day file
grep -ho '<!-- entry:ep_' .../episodes/*.md | wc -l   # 6414 blocks
grep -ho '<!-- entry:ep_' .../episodes/*.md | sort -u | wc -l  # 6334 ids
```

## See also

- [reflection.md](../reflection.md) — user-facing description of the feature
- [memory-db-cleanup.md](../memory-db-cleanup.md) — the maintenance procedures
  and consistency checks this plan reuses
- [adr/0002-recency-digest-granularity.md](0002-recency-digest-granularity.md)
  — the read-path companion: what the startup injection should sample
  *after* the active set is smaller
