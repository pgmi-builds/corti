"""Find and re-embed rows whose vector is still ``NULL``.

Corti does **not** backfill on a schedule, on purpose: embedding is triggered
by a memory arriving, and re-scanning the corpus on every write to hunt for a
backlog would spend provider calls without producing a new memory. A row left
vectorless by an embedder outage therefore catches up only when its own md
file happens to be reprocessed for another reason — and a file whose mtime has
not changed is never reprocessed. That is how 6,745 rows sat without vectors
after one arrears period.

This script is the manual escape hatch, and the only supported way to clear a
backlog. It is idempotent and cheap to re-run: it force-enqueues only the md
files that actually contain a vectorless row, and the cascade then re-embeds
only the rows whose ``vector`` is still ``NULL`` (rows that already have one
are skipped on a content-hash match, so no provider call is wasted on them).

Usage::

    python src/scripts/backfill_vectors.py             # report only
    python src/scripts/backfill_vectors.py --apply     # enqueue + drain

Run it against the same memory root and Postgres the server uses. Inside the
container::

    docker exec -u app corti /opt/corti/venv/bin/python /tmp/backfill_vectors.py --apply

Exit codes: ``0`` on success (including "nothing to do"), ``1`` on failure.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from corti.infra.persistence.pg import atomic_fact_repo, episode_repo, foresight_repo

TABLES = (
    (episode_repo, "episode"),
    (atomic_fact_repo, "atomic_fact"),
    (foresight_repo, "foresight"),
)

_MAX_DRAIN_PASSES = 400


async def _vectorless_rows() -> dict[str, int]:
    """``md_path -> number of vectorless rows`` across every daily-log table."""
    counts: dict[str, int] = {}
    for repo, table in TABLES:
        pool = await repo._pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                f"select md_path as p, count(*) as n from {table} "
                "where vector is null group by 1 order by 2 desc"
            )
            for row in await cur.fetchall():
                counts[row["p"]] = counts.get(row["p"], 0) + row["n"]
    return counts


async def _apply(counts: dict[str, int]) -> int:
    """Force-enqueue each affected file, then drain the cascade queue."""
    from corti.entrypoints.cli.commands.cascade import _build_orchestrator, _runtime
    from corti.infra.persistence.sqlite import md_change_state_repo
    from corti.memory.cascade.registry import match_kind

    async with _runtime():
        for path in sorted(counts):
            spec = match_kind(path)
            await md_change_state_repo.force_enqueue(
                path, spec.name if spec else "episode"
            )
        print(f"force-enqueued {len(counts)} md file(s)")
        orchestrator = _build_orchestrator()
        processed = 0
        for _ in range(_MAX_DRAIN_PASSES):
            n = await orchestrator.sync_once()
            processed += n
            if n == 0:
                break
        print(f"cascade processed {processed} row(s)")
    return processed


def _print_report(counts: dict[str, int]) -> None:
    total = sum(counts.values())
    print(f"{total} vectorless row(s) across {len(counts)} md file(s):")
    for path, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>6}  {path}")


async def _run(apply: bool) -> int:
    """One event loop for the whole run.

    The reporter and the drain share the process-wide Postgres / SQLite
    singletons, which bind to whichever loop first used them — two
    ``asyncio.run`` calls leave the teardown holding a future from the other
    loop.
    """
    counts = await _vectorless_rows()
    if not counts:
        print("no vectorless rows — nothing to backfill")
        return 0

    _print_report(counts)
    if not apply:
        print("\nreport only — re-run with --apply to backfill")
        return 0

    await _apply(counts)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="force-enqueue the affected files and drain the queue "
        "(default: report only)",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args.apply))
    except Exception as exc:  # pragma: no cover - operational failure
        print(f"error: backfill failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
