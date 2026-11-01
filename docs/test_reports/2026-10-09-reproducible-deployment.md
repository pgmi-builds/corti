# 2026-10-09 reproducible deployment and memory integrity verification

This follows the cost investigation and the user's request to repair deployment
and check whether the migrated memory database had undone the previous cleanup.
Times are Hong Kong time (UTC+08).

## What was previously fixed, and where it disappeared

The cost fix (`2b49150`, October 1) was committed to the repository. The October 2
reports document copying the complete updated source tree into an existing
container made from `m1research/corti:v0.3.2-slim`. They also explicitly record
that the underlying August image was unchanged.

During the October 6 migration, the saved Docker image was loaded and a new
container was created using the saved `Config.Image`. The restore script is
`/usr/local/sbin/migration-corti-restore`, with its private image/definition under
`/data/migration-private`. Saving an image does not save a running container's
writable-layer patches. The new container consequently imported the old code,
including the 10,000-row limit and repeated requeue behavior.

Fetching GitHub on October 9 confirmed local `main` and `origin/main` both at
`19ce94a`. The source was current; the installed application image was not.

## Repair actually deployed

- Repository deployment commit: `d8ebd00cf436327ab8db9a2247dd5509d1ee0a1a`.
- Installed Corti: **0.3.4**, Python 3.14.8.
- Image ID: `sha256:8ce7df14414ee254fefbb9bec668caf6f6e9af3c10267894ebaa571ac9d0ddc8`.
- Container: `corti`, created fresh from that image ID; healthy after startup.
- All **424** installed Corti/EverAlgo Python source files matched the committed
  source, both in a disposable image-verification container and the live service.
- External data mount retained: `/home/u1/.corti` to `/home/app/.corti`.
- Existing external PostgreSQL DB settings and host alias retained.
- Previous container retained stopped as `corti-previous-d8ebd00cf436-2f116247`,
  with restart policy `no`.
- The obsolete `migration-corti-restore.service` was disabled after the replacement
  passed verification. The current Docker container uses `unless-stopped`.

The script is [scripts/deploy-local.py](../../scripts/deploy-local.py); the
operator procedure is [local-deployment.md](../local-deployment.md). Its build
input is `git archive HEAD`, with locked Python dependencies. It records the
image ID/revision/source manifest and privately preserves service configuration.
It fails when the running image or installed source differs from that record.

Backups, made while the old application was stopped:

```text
/home/u1/.local/state/corti/deployments/d8ebd00cf436-2f116247/
  memory-root.tar.gz    # complete memory root, including SQLite/config
  postgres.dump        # PostgreSQL custom-format dump; pg_restore --list verified
  runtime.env          # credentials, private
  deployment.json      # source manifest, image ID, data location
```

A verified application-image export was generated separately:

```text
/home/u1/.local/state/corti/corti-verified-d8ebd00.tar.gz
/home/u1/.local/state/corti/corti-verified-d8ebd00.tar.gz.json
```

That export was saved by the active image's immutable ID after source verification.
It contains application code, not the external memory databases. The initial
`/data/migration-private/corti-image.tar` is an obsolete migration artifact and
must not be used as the current application image.

## Memory database: no evidence the cleanup was undone

The October 1 cleanup record reports **166,072 facts remained** after removing
1,259 fact blocks, alongside 6,404 episodes and 44,830 foresights. A six-digit
fact count is therefore consistent with that cleanup, rather than evidence that
the removed content returned.

The current and old SSD PostgreSQL cluster identifiers differ. The migration
handoff documents a **logical backup/restore**, not a raw PG-directory copy.
The old cluster is shut down; the active host runs PostgreSQL 18.6 under
`/var/lib/postgresql/18/main`. The application uses database `corti` on that host,
not a database inside the slim application container.

Cleanup markers remain present:

- The removed July 16 fact file remains absent on both old and new SSD memory
  roots; its formerly orphaned 20,101 PG rows remain **0**.
- The repaired September 11 episode IDs through sequence 328 remain in PG.
- A complete daily-log scan found **0 duplicate IDs**, **0 unsafe sequence
  watermarks**, **0 orphan PG file paths**, and **0 PG-only entries** in all
  three memory kinds before replacement.

Before repair, the old code had failed to index newer Markdown records:

| Kind | PostgreSQL before | Markdown before | Missing from PG |
|---|---:|---:|---:|
| Episode | 6,604 | 6,724 | 120 |
| Atomic fact | 171,794 | 174,634 | 2,840 |
| Foresight | 46,275 | 47,120 | 845 |

All missing records were in October 6-8 files. They were recovered by the new
service's cascade. Ordinary incoming activity continued during verification, so
the final snapshot also includes additional current-day records:

| Kind | PostgreSQL after | Markdown after | PG-only | MD-only | Duplicate IDs |
|---|---:|---:|---:|---:|---:|
| Episode | 6,730 | 6,730 | 0 | 0 | 0 |
| Atomic fact | 174,644 | 174,644 | 0 | 0 | 0 |
| Foresight | 47,159 | 47,159 | 0 | 0 | 0 |

The queue snapshot showed **411 done**, with **0 pending/processing/failed**.
The new service recorded no worker failures in the later recovery observation
window. Historical October 6-8 files were not being repeatedly reprocessed.
No memories were deleted as part of this deployment.

A subsequent full content-hash audit also found **0 content mismatches** in all
three kinds, in addition to zero missing/orphan/duplicate entries. That snapshot
had 6,730 episodes, 174,694 facts and 47,159 foresights; the additional 50 facts
were normal incoming writes after the earlier alignment snapshot.

## Functional and cost verification

- Health: HTTP 200.
- Keyword search against the existing shared memory scope: HTTP 200.
- The deployed handler was exercised against unchanged real fact files with
  15,624 and 10,628 entries. A stub that raises on any embedding/tokenizer call
  was used, and the PG connections were read-only. Results: every entry skipped,
  **0 upserts, 0 deletes, 0 embedding provider calls**.
- The previous 10,000-entry cutoff would have embedded 5,624 and 628 records
  respectively in that scenario.
- The ordinary restart recovered the unindexed records through the supported
  queue; no full reindex or vector backfill was scheduled.

## Checks and limitations

Deployment safety tests passed: source tampering detection, image-ID mismatch
rejection, old-service recovery on backup failure, and private credential writes.
Full `UV_PYTHON=3.12 make ci` passed lint, unit tests, integration tests, packaging,
and package import smoke verification: **1,534 unit tests passed** (17 skipped),
and **51 integration tests passed** (4 skipped, 6 deselected).
The initial default Python 3.14 run found
21 failures caused by the pre-existing jieba SyntaxWarning allowlist not matching
Python 3.14's changed warning wording (1,513 tests passed). No application code
or unrelated test configuration was changed to address that issue; the full gate
was run under the repository-supported Python 3.12 instead. The live Python 3.14
application passed startup, indexing recovery, and keyword retrieval checks.

DashScope still reports account **Arrearage**. The fixed service stores new rows
with NULL vectors and retains keyword access, instead of failing the file and
requeueing it every scan. Newly recovered rows therefore still need a bounded
manual vector backfill after provider access is restored. The existing historical
vectors were preserved. Successful live semantic embedding/retrieval and the
post-fix daily bill cannot yet be validated while the account is in arrears.
