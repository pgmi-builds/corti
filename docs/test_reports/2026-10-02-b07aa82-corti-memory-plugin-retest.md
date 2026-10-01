# 2026-10-02 · `b07aa82` — Corti memory plugin retest (iteration 4)

"Test again" pass, six commits after iteration 3. Iteration 3
([8ba157c report](2026-10-02-8ba157c-corti-memory-plugin-fix-verification.md))
filed three findings — **N1** stale Hermes bundle, **N2** degraded embedding
leg, **N3** Hermes not surfacing degradation. This run re-tests at the new HEAD
and finds all three closed, plus one behaviour change worth a methodology note.

| Field | Value |
|---|---|
| Run date | 2026-10-02, 01:59–02:02 (+08:00) |
| Repository commit | `b07aa821c88605b25fc230ed78d4707e1e2a1554` (`b07aa82`, 2026-10-02 01:56:41 +08) |
| Working tree | clean (`git status --short` empty) |
| Commits since iteration 3 | `c8d15b5` `9938030` `d1526e9` `a2bb433` `743ee6d` `b07aa82` |
| Backend | Docker `corti`, image `m1research/corti:v0.3.2-slim`, `RestartCount=0`, started 2026-10-01T17:54:42Z (= 2026-10-02 01:54:42 +08), `healthy`; `127.0.0.1:5473` |
| Hermes scope | `app_id=shared-agent-memory`, `user_id=default`, `agent_id=pc-hermes-default` |
| Test scope (probes) | `app_id=test` (8 episodes) |
| Embedding leg | **healthy** — `degraded: []` on every probe (iteration 3: `["embedding"]`) |

---

## 1. Results at a glance

| # | Check | Result |
|---|---|---|
| 1 | Backend healthy | PASS |
| 2 | Prior finding **N2** — embedding leg degraded | **CLOSED** — `degraded: []`; vectors backfilled |
| 3 | Prior finding **N1** — Hermes bundle stale | **CLOSED** — installed bundle now byte-identical to repo |
| 4 | Prior finding **N3** — Hermes does not surface degradation | **CLOSED** — server renders the note; Hermes prints it as a `note` field |
| 5 | Fix `c8d15b5` — unvectorised rows skipped in vector recall | PASS — `IS NOT NULL` guard in all four recallers |
| 6 | Fix `d1526e9` — vectorless row can't fail read/write | PASS — `IS NOT NULL` in `pg_repo`; NULL score → 0.0 |
| 7 | Fix `743ee6d` — degradation note rendered once, server-side | PASS — `data.degraded_note` present; adapters print the field |
| 8 | Fix `9938030` — Hermes surfaces degraded in the tool result | PASS (structural) — `_tool_search` sets `payload["note"]` |
| 9 | Hyphenated identifier recall | PASS — `CORTI-ITER-20261002-7Q4M` → 3 hits |
| 10 | Unit suite | PASS — 1530 passed, 17 skipped |
| 11 | Hermes plugin unit suite | PASS — 118 passed |
| 12 | True-negative control under `vector`/`hybrid` | **CHANGED** — now returns nearest neighbours (observation O1) |

---

## 2. Prior findings closed

### 2.1 N1 — the Hermes bundle is no longer stale

`~/.hermes/plugins/corti/` now matches the repo byte-for-byte:

```
8c9bb72e…  ~/.hermes/plugins/corti/__init__.py   ==  src/integrations/hermes/__init__.py
6be03ae7…  ~/.hermes/plugins/corti/_formatting.py ==  src/integrations/hermes/_formatting.py
```

The installed bundle no longer imports `format_prefetch` / `format_system_prompt`
(0 occurrences); it carries the `degraded_note` handling instead. Bundle files
were written at **01:54:42**, the same instant the container restarted and the
DSH dist was rebuilt — one deploy event. The plugin `.pyc` was recompiled at
**01:57:52**, after the write, consistent with the running Hermes agent having
imported the new bundle (this session started after that).

### 2.2 N2 — the embedding leg is healthy again

Every probe returns `degraded: []` (iteration 3: `["embedding"]`). The vector
backfill has been run:

```
app_id               | total | with_vec | with_svec
 shared-agent-memory |  6449 |     6449 |      6449
 test                |     8 |        8 |         8
```

No unvectorised rows remain in either scope — which is exactly the condition
that would have produced HTTP 500s before `c8d15b5`/`d1526e9` (see §3).

### 2.3 N3 — the degradation note now reaches every tool surface

`743ee6d` moved the human-readable sentence to a single server-side renderer
(`memory/search/degradation.py::render_note`):

```
[recall degraded: <legs> unavailable — these results are partial, not the full ranking]
```

`SearchData.degraded_note` carries it; the live response includes the field
(`data.degraded_note: ""` while healthy). Each adapter now prints the field
instead of composing prose: Hermes `_tool_search` sets `payload["note"]` from
`data.get("degraded_note")` (repo `__init__.py:567-570`), DSH dropped its local
`degradationNote` helper, and Claude Code gained the note. The manual
verification could not observe a non-empty note because the provider is healthy
(`degraded == []`), so this leg is verified structurally (code + deployed
bundle + live `degraded_note` field), not behaviourally.

---

## 3. New fixes verified

| Commit | Claim | Evidence |
|---|---|---|
| `c8d15b5` | Vector recall skips NULL-vector rows (`<col> IS NOT NULL`) | `grep -c 'IS NOT NULL'` in the container: `pg_episode` 4, `pg_atomic_fact` 3, `pg_foresight` 2, `pg_knowledge_topic` 2 |
| `d1526e9` | `pg_repo.search()` carries the same guard; NULL score → 0.0 | container `pg_repo.py` guard present (2) |
| `743ee6d` | Degradation prose lives in one file | `render_note` in `degradation.py`; `data.degraded_note` live |
| `9938030` | Hermes surfaces degraded | `_tool_search` reads `degraded_note` → `payload["note"]`; deployed bundle matches |

These close the class of failure that iteration 3's restart exposed: once
DashScope was funded and vectors started populating, a scope containing one
unbackfilled row would 500 every search (`1 - (NULL <=> q)` → `float(None)`).
No 500 was observed in this run, and all vectors are now present, so the guard
is verified by code inspection rather than by triggering the fault.

---

## 4. Observation O1 — the true-negative control no longer holds under `vector` / `hybrid`

With the semantic leg restored, an unrelated query returns nearest neighbours
unless a floor is set. `min_score` defaults to `null` (`search/dto.py:76`;
documented in [api.md](../api.md) as "optional post-fusion relevance floor").

| Query | `keyword` | `vector` | `hybrid` | `hybrid` + `min_score=0.2` |
|---|---|---|---|---|
| `capital of Peru altitude` | 0 hits | **5 hits** (0.21–0.27) | **3 hits** (0.03–0.04) | 0 hits |
| `zzz qwerty flibbertigibbet` | — | — | **3 hits** (0.06–0.07) | 0 hits |
| `CORTI-ITER-20261002-7Q4M` | — | — | 3 hits (0.19–0.49) | — |

This is **not a defect** — vector retrieval returns top-k by construction, and
the behaviour is documented. But it invalidates the "true negative" control
used in iterations 1–2 (`capital of Peru altitude` → 0), which only read as a
true negative because the embedding leg was dead at the time. Any future test
that wants a genuine empty result under `hybrid`/`vector` must set `min_score`
(e.g. `0.2`); `keyword` remains a clean negative without one. Note the score
separation still exists (real hits ≈ 0.44–0.49 vs unrelated ≈ 0.03–0.07); it is
simply not thresholded by default.

---

## 5. Test matrix

| Suite | Result |
|---|---|
| `pytest tests/unit` (full) | **1530 passed, 17 skipped** (16.55 s) — +20 vs iteration 3 |
| `pytest tests/unit/test_integrations/test_hermes/` | **118 passed** (0.27 s) — +2 |
| `pytest tests/integration/test_hermes_plugin_install.py` | not run this pass (self-skips on the live install) |

The new tests include `test_vectorless_rows_are_skipped.py` and
`test_handler_embedding_failsafe.py` (`d1526e9`), the Hermes `degradation_note`
provider cases (`9938030`/`743ee6d`), and the recall-tokenisation cases
(`d81a5ac`, retained).

---

## 6. Storage / deployment verification

| Artifact | Location | Observed |
|---|---|---|
| Hermes bundle | `~/.hermes/plugins/corti/` | md5 == repo; written 01:54:42 |
| DSH bundle | `~/.dsh/profiles/web/node_modules/corti-memory/dist/index.js` | rebuilt 01:54:42 (29085 B); local note helper removed |
| Container | `docker inspect corti` | started 01:54:42, `RestartCount=0`, healthy |
| PG vectors | `corti.episode` | `test` 8/8, `shared-agent-memory` 6449/6449 |
| Degradation | `/api/v1/memory/search` | `degraded: []`, `degraded_note: ""` |
| Docs | `docs/runtime-integration.md` | records "needs a restart to pick the new bundle up" (N1 root cause) |

---

## 7. Recommendations

1. **No blocking item remains.** All three iteration-3 findings are closed and
   the vectorless-row crash class is guarded.
2. **Behaviourally verify the degradation note** the next time the embedder is
   down (or by pointing a staging instance at an unreachable embedder): assert
   a non-empty `data.degraded_note` and the corresponding `note` field in each
   adapter's tool result. Today only the structural path is verified.
3. **Adopt `min_score` in the test harness** for negative controls (O1), and
   consider documenting a recommended floor for agent-facing search so an
   unrelated query does not surface three plausible-looking memories.
4. **Keep the deploy discipline** now written into `docs/runtime-integration.md`
   (whole-tree copy + import smoke check + runtime restart); N1 regressed
   precisely because the repo and the live bundle could drift silently.

---

## 8. Artifacts left behind by this run

None. All probes were read-only (`mem_search` / `mem_list` + direct HTTP/SQL).
No `mem_add`, no new memories written.

---

## 9. Method notes

* Live probes via a stdlib `urllib` script against `127.0.0.1:5473`; container
  introspection via `docker exec`; storage counts via `psql` using the password
  from `docker inspect corti`.
* N1 verified by md5 comparison (installed vs repo) plus `.pyc` mtime ordering,
  not by inspecting the running process's loaded module.
* Unit counts are from a single `make test` run; skipped tests are the
  `@slow`/`@live_llm` set plus the self-skipping install-path test.
