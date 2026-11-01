# Reproducible local Docker deployment

Git stores source code. A Docker image stores the installed application and its
dependencies. A running container adds a writable layer on top of an image.
Pushing source to GitHub does not build an image or replace a running container.
Copying source into a container changes only its writable layer: recreating that
container from the original image loses those changes.

Use the deployment script instead of copying application files into containers:

```bash
python3 scripts/deploy-local.py deploy
python3 scripts/deploy-local.py verify
```

The script fetches `origin`, refuses a checkout behind/diverged from `origin/main`
or with tracked uncommitted changes, and builds `Dockerfile.slim` from
`git archive HEAD`. Untracked files, credentials, and local scratch work never
enter the build context. Dependencies use `uv.lock`. The resulting image carries
the full Git revision label; its immutable local SHA256 image ID is recorded.
The script compares every installed Corti and EverAlgo Python file with the
committed source before deploying and again inside the running container.

The first deployment imports the existing standard `corti` container's memory
mount, port, host alias, and service environment. It preserves the environment
in `~/.config/corti/runtime.env` (mode 0600), never in Git or command-line
arguments. Nonstandard mounts, networking, or ports require an explicit review.

After the image passes verification, the script stops the old service, archives
the existing memory root and takes a PostgreSQL custom-format dump. It then
retains the old container under `corti-previous-...` with automatic restart
disabled and starts the new one by **image ID**, using the same external data.
It saves `~/.config/corti/deployment.json` only after health and source checks
pass. Backups are under `~/.local/state/corti/deployments/` in private directories.
If replacement fails, it restores the old container name/start policy without
rewinding the database. Review any database migration before using an old image.

## Recreate or transfer the deployed version

```bash
# Recreate exactly the recorded image, not whatever a mutable tag now means.
python3 scripts/deploy-local.py recreate
python3 scripts/deploy-local.py verify

# Export the verified running image by its ID, including the deployment manifest.
python3 scripts/deploy-local.py export-image --output /secure/path/corti-image.tar.gz
```

On a new machine, load the exported image with `docker image load -i`, restore
`deployment.json` and the private `runtime.env`, and restore both the memory root
and the external PostgreSQL database from a consistent snapshot. Adjust the
manifest's host paths and DB settings when host locations change. Run `recreate`
and `verify`. The image export contains application code, **not memory data**.
The accompanying manifest contains source hashes and paths, not API keys; the
memory-root backup and `runtime.env` contain credentials and must remain private.

## Data and acceptance checks

The slim container does not store the external PostgreSQL server's data:

- Markdown under `~/.corti/` is the memory source of truth.
- SQLite under `~/.corti/.index/sqlite/` holds queues, extraction ledgers, and
  evolution state.
- The host PostgreSQL `corti` database contains vectors and searchable indexes.

Replacing the application image does not restore any of those layers from Git.
When moving a PostgreSQL data directory, stop the old server for a physical copy
and use a compatible PostgreSQL major version and extension binaries. A running
server's directory copied without a consistent snapshot is not a valid backup.
For deployment backups this script uses `pg_dump`; it does not copy live PG files.

`/health` alone is not a cost or indexing acceptance check. After replacement,
also inspect the cascade queue, assert unchanged large files make zero embedding
requests, check that failed work is not automatically rescheduled every 30 seconds,
and observe actual successful provider token usage. Account arrearage can still
degrade semantic search even when the application and keyword search are healthy.

Never perform an unbounded reindex/backfill to validate deployment. Never use
`docker cp` as the final application deployment step. Keep the committed revision,
image ID, source verification, and external data locations together.
