"""Deployment safety: detect source drift and preserve service on backup failure."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "deploy-local.py"
SPEC = importlib.util.spec_from_file_location("deploy_local", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


def test_source_gate_detects_a_container_only_patch(tmp_path: Path) -> None:
    manifest = {}
    for name in ("corti", "everalgo"):
        folder = tmp_path / name
        folder.mkdir()
        module = folder / "__init__.py"
        module.write_text("VERSION = 'committed'\n")
        manifest[name + "/__init__.py"] = hashlib.sha256(
            module.read_bytes()
        ).hexdigest()
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    args = [sys.executable, "-c", deploy.VERIFY_CODE]
    result = subprocess.run(
        args,
        input=json.dumps(manifest),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    (tmp_path / "corti" / "__init__.py").write_text("VERSION = 'hot-patched'\n")
    result = subprocess.run(
        args,
        input=json.dumps(manifest),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert "corti/__init__.py" in result.stderr


def test_image_mismatch_fails_before_source_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deploy,
        "inspect_container",
        lambda _: {
            "State": {"Running": True},
            "Image": "sha256:old",
        },
    )
    with pytest.raises(RuntimeError, match="image differs"):
        deploy.check({"container": "corti", "image_id": "sha256:fixed"})


def test_backup_failure_restarts_old_service_without_replacing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands = []
    monkeypatch.setattr(deploy, "HISTORY", tmp_path)
    monkeypatch.setattr(
        deploy, "inspect_container", lambda _: {"State": {"Running": True}}
    )
    monkeypatch.setattr(deploy, "run", lambda command, **_: commands.append(command))

    def fail_backup(*_: object) -> None:
        raise RuntimeError("backup unavailable")

    monkeypatch.setattr(deploy, "backup", fail_backup)
    with pytest.raises(RuntimeError, match="backup unavailable"):
        deploy.replace({"container": "corti", "revision": "abcdef123456"})
    assert commands == [
        ["docker", "stop", "--time", "60", "corti"],
        ["docker", "start", "corti"],
    ]


def test_credentials_are_private_on_creation_and_replacement(tmp_path: Path) -> None:
    target = tmp_path / "private" / "runtime.env"
    deploy.private_write(target, "DB_PASSWORD=first\n")
    assert target.stat().st_mode & 0o777 == 0o600
    deploy.private_write(target, "DB_PASSWORD=second\n")
    assert target.stat().st_mode & 0o777 == 0o600
