# Cleaning the memory database properly

A procedure and an acceptance checklist for removing content from Corti — and
for proving the removal actually happened. Written after a cleanup that looked
complete but left 20,101 rows behind for two months.

**The rule that governs everything here:** markdown is the source of truth, and
the SQLite + Postgres layers are *projections* of it. Therefore cleanup is not
"delete some rows" — it is "**change markdown, then make the projections agree
with it**". Any procedure that starts in Postgres is already wrong.

## 1. Why the previous cleanups leaked

Three distinct failure modes have actually occurred in this deployment. Each one
produced residue that a superficial check would not see.

### 1.1 Deleting the queue row destroyed the deletion signal

The cascade is designed to handle removed files. `reconcile()` emits a
`deleted` decision for any path present in `md_change_state` but missing from
disk, and the worker then calls `handle_deleted(md_path)` →
`delete_by_md_path(md_path)`, which removes every index row for that path. The
comment in the reconciler describes the exact hazard:

> "A done row with `change_type='added'` / `'modified'` means the watcher missed
> the subsequent unlink — without re-emitting `deleted` here the scanner would
> never recover the stale Postgres rows."

In July 2026 a deadlocked daily log, `atomic_fact-2026-07-16.md`, was removed
from disk **and its `md_change_state` row was removed too**. Removing the state
row removed the only thing that could have noticed the divergence. The
20,101 Postgres rows for that file stayed, stayed `deprecated_by IS NULL`, and
kept answering searches for two months.

**Lesson:** never delete a `md_change_state` row. A tombstone with
`change_type='deleted'`, `status='done'` is the *correct* end state — it is the
system recording that the deletion succeeded, and it is what stops the scanner
re-emitting the same work forever.

### 1.2 Cleaning one layer only

Episode rows were removed, but the derived artefacts were not:

- 4,028 `cluster_member` rows pointing at episodes that no longer existed
  (41.5% of the table).
- 130 memberships where one episode sat in up to four different clusters,
  because the writer inserts membership rows and never evicts the old
  assignment when a re-processed episode merges somewhere else.

An index layer that is not reconciled will accumulate phantoms indefinitely.

### 1.3 Trusting a counter instead of counting

The largest cluster reported `count = 4046`. After removing orphan memberships
it held **37** — 99.1% of its reported size was phantom. This produced a wrong
diagnosis: the cluster looked like a "generic attractor" that had swallowed
unrelated topics (its stored preview showed a YouTube cron job, a PDF
conversion and a KIFRS research thread side by side), when in reality the
`count` column had simply never been reconciled against live membership.

**Lesson:** `count`-style denormalised columns must be recomputed during any
cleanup, and cluster health must be judged on live membership only.

### 1.4 Lowering `entry_count` mints duplicate entry ids

The daily-log sequence is `seq = entry_count + 1` (`EntryId.next_for`), and the
writers read `entry_count` straight from the frontmatter. So `entry_count` is
not a *count* — it is a **high-water mark**. A cleanup that rewrites it to the
number of surviving blocks hands the next append an id that a
later-but-surviving entry already owns.

That has already happened in this deployment.
`episodes/episode-2026-09-11.md`,
`.atomic_facts/atomic_fact-2026-09-11.md` and
`.foresights/foresight-2026-09-11.md` each contain entry ids that belong to two
different memories — 10 episodes, 506 atomic facts and 77 foresights, 593 ids
in total. The cascade parses a file into a dict keyed by `entry_id`, so one of
each colliding pair is silently shadowed and never indexed.

Two things changed because of this:

- `BaseDailyWriter._current_count` now returns the maximum of three sources —
  the `entry_count` frontmatter field, the highest sequence present among the
  file's entry markers, and the live block count. The sequence can no longer be
  pulled backwards by a maintenance script. Regression test:
  `test_entry_seq_is_not_reused_after_an_entry_is_deleted`.
- The procedure in §2 raises `entry_count` to the highest sequence in the file
  after a removal. It never lowers it.

The entries shadowed on 2026-09-11 were repaired by renumbering the second
occurrence of each colliding id (§6).

### 1.5 Trusting `entry_id` as a global key

`entry_id` is `"<prefix>_<YYYYMMDD>_<seq>"`, allocated **per file**, so it is
unique only within `(owner_id, md_path)`. Two owners writing the same day both
start at `seq = 1`. Postgres stays consistent because the row key is
`(md_path, entry_id)`, but any consumer that joins on the bare id silently
mixes owners — 70 episode ids, 1,910 atomic-fact ids and 503 foresight ids are
currently shared by the `default` and `business` owners.

## 2. The correct procedures

### 2.1 Removing individual entries

Delete from markdown first, then let the cascade reconcile. For a memory entry
of kind `episode`, its derived rows must go too — nothing cascades from an
episode to its atomic facts or foresights automatically, because they live in
their own daily logs.

1. Remove the `<!-- entry:<id> --> … <!-- /entry:<id> -->` block from
   `episodes/episode-<date>.md`.
2. Remove every `<!-- entry:af_* -->` block whose body contains
   `**parent_id**: <entry_id>` from `.atomic_facts/atomic_fact-<date>.md`.
3. Remove every `<!-- entry:fs_* -->` block whose body contains the same
   `parent_id` from `.foresights/foresight-<date>.md`.
4. **Raise** `entry_count` to the highest sequence still present in the file.
   It is a high-water mark, not a block count: lowering it to the number of
   surviving blocks makes the next append re-mint a live entry's id (§1.4).
   Raising it leaves gaps in the sequence, which is exactly the intended end
   state.
5. Let the scanner pick the modified files up (30 s), or force-enqueue them.
   The handler diffs markdown against the index and deletes what is gone.

### 2.2 Removing a whole file

Delete the markdown file and **leave its `md_change_state` row alone**. The next
sweep emits `deleted` and the index rows are removed for you.

### 2.3 Recovering a deletion that was already missed

If the file and its state row are both gone (the §1.1 case), the fix is to
**re-create the signal**, not to `DELETE FROM` by hand:

```sql
-- insert into md_change_state a row for the vanished path, with a
-- non-'deleted' change_type so the reconciler re-emits 'deleted'.
INSERT INTO md_change_state
  (created_at, updated_at, md_path, kind, change_type, mtime,
   first_seen_at, last_changed_at, lsn, status, retryable,
   last_attempt_at, retry_count, error)
VALUES (datetime('now'), datetime('now'), '<path relative to the memory root>',
        '<kind>', 'modified', 0.0, datetime('now'), datetime('now'),
        (SELECT coalesce(max(lsn),0)+1 FROM md_change_state), 'done',
        NULL, NULL, 0, NULL);
```

Within one scan interval the row flips to `change_type='deleted'`,
`status='done'` and the orphan rows are gone. Using the cascade rather than a
manual delete means the recovery exercises the same code path that normal
operation uses.

### 2.4 Where to run the write

The memory root is owned by the container's app user, so SQLite and markdown
writes must be performed **inside the container**:

```bash
docker exec corti python3 /path/to/script.py
```

Writing from the host fails with `attempt to write a readonly database`.

## 3. The acceptance checklist

A cleanup is complete only when **every** line below is true. Checking a subset
is exactly how the previous two attempts passed review and still left residue.

| Check | Pass condition |
|---|---|
| Episode alignment | Postgres active rows == markdown entry count; **0** PG-only, **0** md-only |
| Atomic-fact alignment | same |
| Foresight alignment | same |
| Orphan index files | **0** `md_path` values present in Postgres with no file on disk |
| Cluster orphans | **0** `cluster_member` rows whose `member_id` has no live episode |
| Cluster duplicate membership | **0** `member_id` values appearing in more than one cluster |
| Cluster counters | `cluster.count` recomputed from live `cluster_member` rows |
| `md_change_state` | every row either points at a file that exists, or is a `deleted`/`done` tombstone |
| Entry-id watermark | for every daily-log file, `entry_count` >= the highest `seq` present, and no id appears twice |
| Deprecation audit | `deprecated_by IS NOT NULL` counts are understood — currently **0 everywhere**, meaning the deprecation path has never executed |
| Dead spaces | no `<app_id>/<project_id>` directory on disk with zero index rows |

The alignment scan is 40 lines of Python and should be run after every cleanup;
it is the only check that catches §1.1.

## 4. Known structural gaps

These are real defects found while auditing, listed so the next cleanup does not
have to rediscover them. None is fixed by data surgery alone.

1. **No first-class delete path.** The CLI has no `memory delete`; the HTTP API
   deletes only knowledge-base documents. Deleting a memory means editing
   markdown by hand and trusting the cascade. Every cleanup so far has been a
   bespoke script.
2. **`entry_id` is not globally unique.** It is unique only within one
   `users/<owner>/<kind>/…-<date>.md` file — 70 episode ids, 1,910 atomic-fact
   ids and 503 foresight ids are shared by two owners, and Postgres `id` is
   `<owner>_<entry_id>` precisely because the bare id is ambiguous. Joins must
   key on `(owner_id, entry_id)`. `cluster_member.member_id` still stores the
   bare id, so "which episodes are in this cluster" is not a well-defined
   query yet.
3. **Duplicate `entry_id`s inside one file are silent data loss.** On
   2026-09-11 three daily logs contained 593 ids that belonged to two different
   memories each (§1.4). They were repaired on 2026-10-01 (§6), but nothing in
   the write path *detects* a collision — the cascade simply keys its parse by
   `entry_id` and one entry disappears. A cheap guard would be a writer-side
   check ("the id I am about to mint is not already in the file"), which the
   high-water-mark fix now makes true by construction for the normal path.
4. **Nothing reconciles cluster membership on delete.** `upsert_with_members`
   now evicts a member from the cluster it previously belonged to and a
   `UNIQUE(member_id)` constraint encodes the intent, so duplicates cannot
   reappear — but deleting an *episode* still leaves its `cluster_member` row
   behind, which is why every cleanup has to prune membership by hand.
5. **`_detect_orphans` only logs.** The reflection orchestrator has a method
   named for the problem and it does nothing but warn.
6. **No scheduled reconciliation.** By operator decision, cleanup is on-demand.
   The consequence is that divergence between markdown and the index is
   invisible until someone runs the scan in §3.

## 5. Applying this to a whole class of content

When the target is "remove every memory of kind X" (test sessions, a cron
job's output, an abandoned deployment), the safe order is:

1. **Enumerate first, delete second.** Write the predicate, print the exact
   match set, and read it. A predicate built from a word like `smoke test` or a
   tool name like `run_cell` will match legitimate work — the first version of
   the test-session cleanup matched real engineering sessions and was discarded.
   A usable predicate matches something with exactly one interpretation
   (`x=42`, `INCLUDE-TEST-OK`, a `/tmp/...-test/` path).
2. **Snapshot before deleting.** Markdown entries can be restored from the
   memory-root backup; index-only rows cannot, because their markdown is already
   gone. Export those separately before touching them.
3. **Edit markdown, then reconcile.** Never delete index rows directly.
4. **Recompute every denormalised counter** (`cluster.count`) — but **raise,**
   never lower, `entry_count`: it is the sequence's high-water mark, and
   lowering it duplicates a live entry's id (§1.4).
5. **Run the §3 checklist and record the numbers.** "It looks clean" is not a
   result; `PG-only = 0, md-only = 0, orphans = 0` is.

## 6. Cleanup records

### 2026-10-01 — test and scheduled-run records

Target: short automated and test-shaped exchanges — greetings (`hihi`,
`hello`), arithmetic probes (`2+2`), single-token compliance tests (`PONG`,
`DSH_WEB_OK`, `PROBE-OK`), codeword-recall probes, model-identity questions,
single-character messages, filler-token exchanges, and the records written by
cron-driven agent runs. The predicate matched on subject + summary **with a
size ceiling**, not on length alone, so short but real work survived; the
first draft predicate (matching a bare `test` keyword) matched 1,058 real
episodes and was discarded.

| Removed | Count |
|---|---|
| Episode entry blocks | 237 (231 distinct ids) |
| Atomic-fact entry blocks | 1,259 (1,073 ids) |
| Foresight entry blocks | 1,339 (1,293 ids) |
| `memcell` ledger rows | 237 |
| `cluster_member` rows | 216 |
| Clusters dropped (emptied, or belonging to a space with no markdown root) | 18 |

Result: 6,641 → 6,404 episodes, 167,330 → 166,072 atomic facts, 46,169 →
44,830 foresights, 875 → 857 clusters, 5,500 → 5,284 members, 7,272 → 7,035
memcells. The §3 checklist passed with `PG-only = 0`, `md-only = 0`, orphan
files `= 0`, orphan cluster members `= 0`, duplicate members `= 0`.

Two repairs were needed along the way:

- **Entry-id watermark (§1.4).** The first pass lowered `entry_count` to the
  surviving block count and left 71 files unsafe; 58 more were already unsafe
  before it. All 129 were repaired by raising `entry_count` to the highest
  sequence present, and the writer hook was fixed so it cannot regress.
- **Cluster centroids.** 22 clusters lost members. `count`, `preview_json`
  and `last_ts_ms` were recomputed, and each centroid was rebuilt as the mean
  of the surviving members' vectors read back from Postgres.

### 2026-10-01 (follow-up) — duplicate `entry_id` repair

The 593 colliding ids in the three 2026-09-11 daily logs (§1.4) were
renumbered. The rule: within a file, keep the **first** occurrence of a
repeated id and give every later occurrence a fresh id allocated above the
file's `entry_count` watermark.

| File | Renumbered | New watermark |
|---|---|---|
| `episodes/episode-2026-09-11.md` | 10 | 318 → 328 |
| `.atomic_facts/atomic_fact-2026-09-11.md` | 506 | 7,225 → 7,731 |
| `.foresights/foresight-2026-09-11.md` | 77 | 2,380 → 2,457 |

Cross-references were repaired rather than guessed: an episode's atomic facts
carry both `parent_id` and `session_id`, so the 452 facts belonging to a
renumbered variant were identified by `(parent_id, session_id)` and re-pointed;
the 365 facts belonging to the retained variant were left alone. Foresights
track memcells, not episodes, so they needed no reference fix. The ten
`cluster_member` rows that referenced a colliding episode id were checked
against the cluster's `added_ts` (each sat ~1 minute after the *retained*
variant's timestamp) and left pointing at it.

Postgres needed no manual edit: renumbering in markdown made the cascade see
new entry ids and insert them, while the retained entries hashed unchanged and
were skipped. Result: `md_only = 0`, `pg_only = 0`, and zero duplicate ids in
any daily log.

Backup: `~/corti-cleanup-backup-<timestamp>/` — full markdown corpus, the
SQLite tables to be mutated (as JSON), and the Postgres rows to be deleted
(as TSV).
