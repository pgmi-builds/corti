#!/usr/bin/env python3
"""Deploy committed Corti code by immutable image ID, retaining external data."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = Path.home() / ".config" / "corti"
STATE = CONFIG / "deployment.json"
ENV_FILE = CONFIG / "runtime.env"
HISTORY = Path.home() / ".local" / "state" / "corti" / "deployments"

# Runs without credentials or a database connection, in the image/container.
VERIFY_CODE = """
import hashlib, importlib.util, json, pathlib, sys
expected = json.load(sys.stdin)
roots = {name: pathlib.Path(importlib.util.find_spec(name).origin).parent
         for name in ('corti', 'everalgo')}
wrong = []
for name, digest in expected.items():
    package, relative = name.split('/', 1)
    file = roots[package] / relative
    if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
        wrong.append(name)
actual = {package + '/' + str(p.relative_to(root))
          for package, root in roots.items() for p in root.rglob('*.py')}
extra = sorted(actual - expected.keys())
if wrong or extra:
    raise SystemExit('Source verification failed: changed/missing=' + str(wrong)
                     + ' unexpected=' + str(extra))
print('PASS: installed source matches committed revision ('
      + str(len(expected)) + ' files)')
"""


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, **kwargs)


def capture(args: list[str]) -> str:
    return run(args, capture_output=True, text=True).stdout.strip()


def inspect_container(name: str) -> dict | None:
    result = subprocess.run(
        ["docker", "inspect", name], capture_output=True, text=True, check=False
    )
    if result.returncode:
        if "No such" in result.stderr:
            return None
        raise RuntimeError("Cannot inspect Docker container: " + result.stderr.strip())
    return json.loads(result.stdout)[0]


def private_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(value)
    temporary.replace(path)


def import_existing(name: str) -> dict:
    """Capture only service configuration; never print credential values."""
    existing = inspect_container(name)
    if not existing:
        raise RuntimeError("First deployment requires an existing container to import.")
    mounts = existing["Mounts"]
    if len(mounts) != 1 or mounts[0]["Destination"] != "/home/app/.corti":
        raise RuntimeError(
            "Unexpected mounts; inspect the deployment before proceeding."
        )
    if mounts[0]["Type"] != "bind" or not mounts[0]["RW"]:
        raise RuntimeError("Expected one writable memory-root bind mount.")
    host = existing["HostConfig"]
    if host["NetworkMode"] != "bridge" or host["Privileged"]:
        raise RuntimeError(
            "Only the standard unprivileged bridge deployment is supported."
        )
    bindings = host["PortBindings"]
    if set(bindings) != {"5473/tcp"} or len(bindings["5473/tcp"]) != 1:
        raise RuntimeError("Unexpected port bindings.")
    port = bindings["5473/tcp"][0]
    env = {}
    for item in existing["Config"]["Env"]:
        key, value = item.split("=", 1)
        if key.startswith(("DB_", "CORTI_")) or key == "TZ":
            if "\n" in value or "\r" in value:
                raise RuntimeError("Docker env-file cannot represent multiline values.")
            env[key] = value
    required = {"DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"}
    if not required <= env.keys():
        raise RuntimeError(
            "Existing container does not define all required DB variables."
        )
    if env.get("CORTI_ROOT", "/home/app/.corti") != "/home/app/.corti":
        raise RuntimeError("Unexpected container memory root.")
    if ENV_FILE.exists():
        if read_env(str(ENV_FILE)) != env:
            raise RuntimeError("runtime.env differs; refuse to overwrite credentials.")
    else:
        private_write(ENV_FILE, "".join(f"{k}={v}\n" for k, v in sorted(env.items())))
    return {
        "container": name,
        "memory_root": mounts[0]["Source"],
        "container_root": mounts[0]["Destination"],
        "host_ip": port["HostIp"],
        "port": port["HostPort"],
        "extra_hosts": host["ExtraHosts"] or [],
        "env_file": str(ENV_FILE),
    }


def verify_source(target: str, manifest: dict, *, image: bool = False) -> None:
    prefix = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--entrypoint",
        "/opt/corti/venv/bin/python",
    ]
    if not image:
        prefix = ["docker", "exec", "-i"]
    args = [*prefix, target]
    if not image:
        args.append("/opt/corti/venv/bin/python")
    run([*args, "-c", VERIFY_CODE], input=json.dumps(manifest), text=True)


def build() -> tuple[str, str, dict]:
    run(["git", "fetch", "origin"], cwd=REPO)
    run(["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"], cwd=REPO)
    if capture(
        ["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"]
    ):
        raise RuntimeError(
            "Commit tracked changes before building; images use git archive HEAD."
        )
    revision = capture(["git", "-C", str(REPO), "rev-parse", "HEAD"])
    tag = "corti-local:git-" + revision
    archive = run(
        ["git", "archive", "--format=tar", revision], cwd=REPO, capture_output=True
    ).stdout
    with tempfile.TemporaryDirectory(prefix="corti-build-") as folder:
        context = Path(folder)
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(context, filter="data")
        manifest = {
            str(p.relative_to(context / "src")): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for name in ("corti", "everalgo")
            for p in (context / "src" / name).rglob("*.py")
        }
        build_env = os.environ.copy()
        buildx = subprocess.run(
            ["docker", "buildx", "version"], capture_output=True, check=False
        )
        if buildx.returncode:
            build_env["DOCKER_BUILDKIT"] = "0"
        args = [
            "docker",
            "build",
            "--pull",
            "-f",
            "Dockerfile.slim",
            "-t",
            tag,
            "--label",
            "org.opencontainers.image.revision=" + revision,
            "--label",
            "org.opencontainers.image.source=https://github.com/pgmi-builds/corti",
        ]
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
            if os.environ.get(key):
                args.extend(["--build-arg", key])
        run([*args, "."], cwd=context, env=build_env)
    image_id = capture(["docker", "image", "inspect", tag, "--format", "{{.Id}}"])
    verify_source(image_id, manifest, image=True)
    return image_id, revision, manifest


def read_env(path: str) -> dict:
    return dict(
        line.split("=", 1) for line in Path(path).read_text().splitlines() if line
    )


def backup(state: dict, folder: Path) -> None:
    folder.mkdir(parents=True, mode=0o700)
    run(
        [
            "tar",
            "-C",
            state["memory_root"],
            "--exclude=./.tmp",
            "-czf",
            str(folder / "memory-root.tar.gz"),
            ".",
        ]
    )
    env = os.environ.copy()
    credentials = read_env(state["env_file"])
    for key, target in (
        ("DB_HOST", "PGHOST"),
        ("DB_PORT", "PGPORT"),
        ("DB_USER", "PGUSER"),
        ("DB_NAME", "PGDATABASE"),
        ("DB_PASSWORD", "PGPASSWORD"),
    ):
        env[target] = credentials[key]
    if env["PGHOST"] == "host.docker.internal":
        env["PGHOST"] = "127.0.0.1"
    run(
        ["pg_dump", "--format=custom", "--file", str(folder / "postgres.dump")], env=env
    )
    shutil.copyfile(state["env_file"], folder / "runtime.env")
    private_write(folder / "deployment.json", json.dumps(state, indent=2) + "\n")


def check(state: dict) -> None:
    existing = inspect_container(state["container"])
    if not existing or not existing["State"]["Running"]:
        raise RuntimeError("Recorded container is not running.")
    if existing["Image"] != state["image_id"]:
        raise RuntimeError(
            "Running image differs from deployment.json; do not trust its tag."
        )
    revision = (
        existing["Config"].get("Labels", {}).get("org.opencontainers.image.revision")
    )
    if revision != state["revision"]:
        raise RuntimeError("Running revision label differs from deployment.json.")
    mounted = {(m["Source"], m["Destination"]) for m in existing["Mounts"]}
    if mounted != {(state["memory_root"], state["container_root"])}:
        raise RuntimeError("Running memory mount differs from deployment.json.")
    verify_source(state["container"], state["source_manifest"])
    print("Revision:", state["revision"], "\nImage ID:", state["image_id"])


def replace(state: dict) -> None:
    name = state["container"]
    previous = inspect_container(name)
    deployment_id = state["revision"][:12] + "-" + uuid.uuid4().hex[:8]
    folder = HISTORY / deployment_id
    old_name = name + "-previous-" + deployment_id
    stopped = False
    renamed = False
    created = False
    try:
        if previous:
            run(["docker", "stop", "--time", "60", name])
            stopped = True
        backup(state, folder)
        if previous:
            run(["docker", "rename", name, old_name])
            renamed = True
            run(["docker", "update", "--restart=no", old_name])
        host_ip = state["host_ip"]
        port = (host_ip + ":" if host_ip else "") + state["port"] + ":5473"
        args = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--restart=unless-stopped",
            "--env-file",
            state["env_file"],
            "-p",
            port,
            "--mount",
            f"type=bind,src={state['memory_root']},dst={state['container_root']}",
        ]
        for host in state["extra_hosts"]:
            args.extend(["--add-host", host])
        run([*args, state["image_id"]])
        created = True
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            current = inspect_container(name)
            if (
                current
                and current["State"].get("Health", {}).get("Status") == "healthy"
            ):
                check(state)
                break
            if current and current["State"]["Status"] in {"exited", "dead"}:
                raise RuntimeError("New container exited during startup.")
            time.sleep(2)
        else:
            raise RuntimeError(
                "New container did not become healthy within 180 seconds."
            )
    except BaseException:
        if created:
            subprocess.run(["docker", "stop", name], check=False)
            subprocess.run(
                ["docker", "rename", name, name + "-failed-" + deployment_id],
                check=False,
            )
        if renamed:
            run(["docker", "rename", old_name, name])
            run(["docker", "update", "--restart=unless-stopped", name])
        if stopped:
            run(["docker", "start", name])
        raise
    state["backup"] = str(folder)
    state["previous_container"] = old_name if previous else None
    private_write(STATE, json.dumps(state, indent=2) + "\n")
    print("Deployment verified. Backup:", folder)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["deploy", "recreate", "verify", "export-image"]
    )
    parser.add_argument("--container", default="corti")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "deploy":
        image_id, revision, manifest = build()
        state = (
            json.loads(STATE.read_text())
            if STATE.exists()
            else import_existing(args.container)
        )
        state.update(image_id=image_id, revision=revision, source_manifest=manifest)
        replace(state)
    else:
        state = json.loads(STATE.read_text())
        if args.command == "recreate":
            verify_source(state["image_id"], state["source_manifest"], image=True)
            replace(state)
        elif args.command == "verify":
            check(state)
        else:
            check(state)
            if not args.output:
                parser.error("export-image requires --output PATH.tar.gz")
            with (
                args.output.open("xb") as destination,
                gzip.GzipFile(fileobj=destination, mode="wb") as zipped,
            ):
                process = subprocess.Popen(
                    ["docker", "image", "save", state["image_id"]],
                    stdout=subprocess.PIPE,
                )
                assert process.stdout is not None
                shutil.copyfileobj(process.stdout, zipped)
                if process.wait():
                    raise RuntimeError("Docker image export failed.")
            private_write(
                args.output.with_suffix(args.output.suffix + ".json"),
                json.dumps(state, indent=2) + "\n",
            )
            print("Exported the verified running image:", args.output)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print("Deployment failed:", error, file=sys.stderr)
        sys.exit(1)
