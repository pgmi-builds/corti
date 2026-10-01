# 2026-10-02 · `8ba157c` — Corti memory plugin fix verification (iteration 3)

Third iteration of the "memory plug-in" test series. Iterations 1 and 2 were
live DSH-agent runs ([deepseek-harness-integration.md](../deepseek-harness-integration.md),
[iteration 2](2026-10-02-832d49e-corti-memory-plugin-live-test.md)). This run is
the **fix-verification pass**: it checks whether the three commits that closed
iteration-2 findings F3/F4/F5 are actually deployed and behave as claimed, and
whether the sibling Hermes integration got the same treatment. Executed from
inside a live Hermes agent session (this environment), against the running
backend, through both the Hermes plugin's own tool surface and direct HTTP/SQL.

| Field | Value |
|---|---|
| Run date | 2026-10-02, 01:05–01:10 (+08:00) |
| Repository commit | `8ba157c7008f5966a2296a6c3fd4e029e36e29c6` (`8ba157c`, 2026-10-02 01:04:20 +08) |
| Working tree | ahead of `origin/main` by 22 commits; 1 untracked path (`docs/test_reports/`) |
| Repository version | `corti 0.3.1` ([pyproject.toml](../../pyproject.toml)) |
| Backend | Docker `corti`, image `m1research/corti:v0.3.2-slim` (built 2026-08-02), `RestartCount=0`, `Up ~5 min (healthy)`; loopback `127.0.0.1:5473`; Python 3.14 at `/opt/corti/venv` |
| Deployment model | stock image + `docker cp` of the whole repo source tree into the container (F7 remediation) |
| Hermes scope | `app_id=shared-agent-memory`, `user_id=default`, `agent_id=pc-hermes-default`, `project_id=default`, `api_url=http://127.0.0.1:5473` |
| DSH scope | `app_id=shared-agent-memory`, `user_id=default`, `agent_id=pc-deepseek-default`, base URL `https://pc.randomhash.app/corti` |
| Test scope (search probes) | `app_id=test` (iteration-2 canaries still resident) |

---

## 1. Results at a glance

| # | Check | Result |
|---|---|---|
| 1 | Backend healthy (`/health` 200, container `healthy`) | PASS |
| 2 | Fix `d81a5ac` (query tokenized into index token space) deployed in container | PASS — `tsquery_text` present in `recall/base.py` |
| 3 | Fix `a8b0034` (`degraded` moved onto `data`, not envelope) deployed | PASS — `degraded` is a `SearchData` field |
| 4 | Hyphenated identifier recall (`CORTI-ITER-20261002-7Q4M`) | **PASS — regression fixed** (0 → 2 hits) |
| 5 | Space-separated control + true negative preserved | PASS (2 hits / 0 hits) |
| 6 | Fix `8ba157c` (DSH surfaces degraded in tool result) in deployed dist | PASS — `~/.dsh/.../corti-memory/dist/index.js` carries the `recall degraded` template |
| 7 | Hermes plugin unit suite | PASS — 116 passed |
| 8 | Full unit suite | PASS — 1510 passed, 17 skipped |
| 9 | Hermes plugin install-path test | SKIPPED — self-skips when the live `~/.hermes/plugins/corti/` exists |
| 10 | Embedding leg healthy | **FAIL** — `degraded: ["embedding"]` on every probe; semantic recall still untestable |
| 11 | Hermes plugin bundle matches the repo | **FAIL** — live install is a pre-refactor copy (finding N1) |
| 12 | Hermes plugin surfaces degradation as a first-class signal | **FAIL** — buried under `results.degraded`, no human-readable text (finding N3) |

---

## 2. Fix verification (live evidence)

### 2.1 `d81a5ac` — query tokenization (F4 closed)

The container's installed `recall/base.py` exposes `tsquery_text` (`hasattr
True`), and the four `pg_*` recallers now route the query through it before
`plainto_tsquery`. Live search against `app_id=test`:

| Query | Iteration 2 | This run | Expected |
|---|---|---|---|
| `CORTI-ITER-20261002-7Q4M` (hyphenated) | 0 | **2** | 2 |
| `CORTI ITER 20261002 7Q4M` (space control) | 2 | **2** | 2 |
| `capital of Peru altitude` (true negative) | 0 | **0** | 0 |

Both hits are the 2026-10-01 episodes that store the canary verbatim. The
hyphenated identifier — the exact string an agent greps for later — is now
recallable, and the true negative stayed at zero. **PASS.**

### 2.2 `a8b0034` — `degraded` on the payload (F3/F5 server side)

`POST /api/v1/memory/search` now returns:

```
top-level keys: ['request_id', 'data']
data keys:      ['episodes', 'profiles', 'unprocessed_messages', 'degraded']
degraded:       ['embedding']
```

`degraded` lives inside `data`, beside the results, and no longer on the
envelope. Container `SearchData.model_fields` contains `degraded`. **PASS.**

### 2.3 `8ba157c` — DSH surfaces degraded (F3/F5 plugin side)

Deployed bundle `~/.dsh/profiles/web/node_modules/corti-memory/dist/index.js`
(29548 bytes, rebuilt 01:03:44) contains the template:

```
recall degraded: ${degraded.join(", ")} unavailable — these results are partial,
not the full ranking
```

appended on hits and empty results alike (the empty case being where the
iteration-2 ambiguity was harmful). The prior backup
(`corti-memory.bak-20261002-003258/dist/index.js`, 30913 bytes) is the pre-fix
build. **PASS.**

---

## 3. New findings

### 3.1 N1 — the live Hermes plugin bundle is a pre-refactor copy

The Hermes plugin installed at `~/.hermes/plugins/corti/` does **not** match
the repo's `src/integrations/hermes/`. The installed files are the pre-Oct-1/2
"holds memory policy" design:

| File | Installed (`~/.hermes/plugins/corti/`) | Repo (`src/integrations/hermes/`) |
|---|---|---|
| `_formatting.py` | 5186 B — has `format_prefetch` (2×), `format_system_prompt` (1×) | 1085 B — two helpers only, 0 occurrences |
| `_client.py` | 11958 B — **0** interop methods (`session_start`/`prefetch`/`session_end`) | 16223 B — all three present |
| `__init__.py` | imports + calls `format_prefetch` (L409), `format_system_prompt` (L699) | imports only `format_tool_result` / `format_memory_write_message` |

md5 all differ. The repo refactor (commits `555fd28`…`9451b43`, "make the plugin
a transport adapter", zero memory policy, interop endpoints) was **never
deployed to the live Hermes instance**. The DSH and Claude Code plugins were
pushed, but the Hermes bundle directory was not refreshed.

Consequence: the Hermes runtime still composes the prefetch/system-prompt
blocks locally and never calls the server's runtime-interop endpoints
(`/session/start`, `/prefetch`, `/session/end`). The memory episodes record the
refactor as "done in repo"; this run shows the live Hermes process is not
running it.

The install-path integration test (`tests/integration/test_hermes_plugin_install.py`,
4 skipped) self-skips with "real ~/.hermes/plugins/corti exists — refusing to
touch the live install", so the install path is never exercised here either —
the stale bundle and the test that would catch it are mutually exclusive.

### 3.2 N2 — the embedding leg is still degraded; semantic recall remains untestable

Every probe in this run returns `degraded: ["embedding"]`. The server
substitutes keyword recall for the query-embedding leg. This is the same
environmental condition iteration 2 traced to the embedding-provider account
being in arrears (DashScope). It is **not** a plugin defect, but it means
dense/paraphrase recall still cannot be verified end-to-end. The degradation is
now correctly *reported* (§2.2/§2.3); the underlying provider is unchanged.

### 3.3 N3 — the Hermes plugin does not surface degradation as a first-class signal

`8ba157c` applied the human-readable degradation notice to the DSH plugin only.
The Hermes sibling was not given the equivalent. Its `_tool_search` returns:

```json
{"results": {"episodes": [], ..., "degraded": ["embedding"]}, "count": 0}
```

`degraded` reaches the model only incidentally — as a nested field inside the
raw `data` blob — with no human-readable line, so "empty store" and
"keyword-only answer" remain textually indistinguishable to the model. (The
Hermes client is not *dropping* the field the way the pre-fix DSH client did,
because the server now puts it inside `data`; but it is not surfaced either.)

---

## 4. Test matrix

| Suite | Result |
|---|---|
| `pytest tests/unit/test_integrations/test_hermes/` | **116 passed** (0.32 s) |
| `pytest tests/unit` (full) | **1510 passed, 17 skipped** (17.14 s) |
| `pytest tests/integration/test_hermes_plugin_install.py` | **4 skipped** (live install present) |

The new `test_recall_tsquery.py` cases (added by `d81a5ac`) are inside the 1510.

---

## 5. Storage-side verification

| Artifact | Location | Observed |
|---|---|---|
| Retrieval index | Postgres `corti.episode` (`app_id=test`) | hyphenated canary now resolves to 2 episodes |
| Degradation report | `/api/v1/memory/search` | `data.degraded: ["embedding"]` on every probe |
| DSH plugin bundle | `~/.dsh/profiles/web/node_modules/corti-memory/dist/index.js` | `recall degraded` template present (01:03:44) |
| Hermes plugin bundle | `~/.hermes/plugins/corti/` | stale (Aug 1 – Sep 11), pre-refactor |
| Container | `docker inspect corti` | image `v0.3.2-slim` (2026-08-02), `RestartCount=0`, healthy |

---

## 6. Recommendations for iteration 4

1. **Deploy the refactored Hermes bundle** to `~/.hermes/plugins/corti/` (copy
   the whole `src/integrations/hermes/` tree, then smoke-check the import and
   `hermes corti status`). This closes N1. Mirror the DSH deployment discipline:
   whole-tree `cp`, import smoke check, restart, `/health`.
2. **Restore the embedding provider** (DashScope arrearage) and assert
   `degraded == []` plus `count(vector) > 0` as a test precondition. N2 is the
   sole blocker on semantic-recall verification.
3. **Apply the `8ba157c` equivalent to the Hermes plugin**: append a
   human-readable `[recall degraded: …]` line in `_tool_search`/`_tool_list`
   when `data.degraded` is non-empty, instead of relying on the nested field.
   This closes N3 and matches the sibling DSH behaviour (blueprint-first rule).
4. **Pin the Hermes bundle build** alongside the repo commit: the live bundle
   carries no build stamp, so staleness (N1) is invisible until probed. Add a
   version/commit marker to `plugin.yaml` or the bundle dir.
5. **Consider an install-path test that runs against a staging dir** rather than
   self-skipping on the live install, so N1-class drift is caught by CI.

---

## 7. Artifacts left behind by this run

None. This run was read-only against the store: `mem_search` / `mem_list` probes
and direct HTTP/SQL queries. No `mem_add`, no new memories written.

---

## 8. Method notes

- §2 evidence was produced by direct HTTP (`curl` to `127.0.0.1:5473`) and
  container introspection (`docker exec`, `hasattr` on the installed modules),
  independent of any plugin.
- §3.1 staleness was established by `grep`/`md5` comparing the installed bundle
  against the repo tree, not by observing the running process's loaded module
  path; the inference that the Hermes runtime loads this bundle rests on
  `memory.provider: corti` + `plugins.entries.corti` in `~/.hermes/config.yaml`
  with no `external_dirs` override, plus the install-path test's own
  "live install exists" guard.
- Latencies in iteration 2 were single-sample wall-clock; this run is a
  correctness pass and reports no timing.
