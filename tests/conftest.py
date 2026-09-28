"""Shared pytest fixtures for aeval tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aeval.contracts import (
    ArtifactRef,
    CollectOutcome,
    CollectionManifest,
    RuntimeLock,
)
from aeval.provenance import build_runtime_lock
from aeval.suite_loader.loader import load_suite

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def demo_suite_dir() -> Path:
    return FIXTURES / "suites" / "demo"


@pytest.fixture()
def demo_suite(demo_suite_dir):
    return load_suite(demo_suite_dir)


@pytest.fixture()
def runtime_lock() -> RuntimeLock:
    return build_runtime_lock()


@pytest.fixture()
def native_suite_dir(tmp_path) -> Path:
    import yaml

    root = tmp_path / "native-suite"
    task = root / "tasks" / "example"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (root / "graders").mkdir()
    (root / "datasets").mkdir()
    data = yaml.safe_load((FIXTURES / "suites/demo/suite.yaml").read_text(encoding="utf-8"))
    data.update(id="native-example", version="1.0.0")
    data["harbor"] = {"dataset": "datasets/local.yaml", "job": "job.yaml"}
    data["clock"] = {"mode": "real"}
    data["driver"] = {"require": []}
    data["baselines"] = [{"id": "ready", "probe": "file:/workspace/ready", "equals": True}]
    data["observables"] = [{"name": "result", "type": "string", "source": "file:/workspace/result"}]
    (root / "suite.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    (root / "datasets/local.yaml").write_text("path: tasks\n", encoding="utf-8")
    (root / "job.yaml").write_text(
        "job_name: synthetic\nn_attempts: 2\nn_concurrent_trials: 1\nagents: [{name: nop}]\n",
        encoding="utf-8",
    )
    (root / "graders/outcome.py").write_text('VERSION = "v7"\n', encoding="utf-8")
    (task / "task.toml").write_text(
        'version = "1.0"\n'
        "[[verifier.collect]]\n"
        'command = "aeval-collect runtime_dump mock_call_log dsh_session canonical_transcript"\n',
        encoding="utf-8",
    )
    (task / "instruction.md").write_text("Synthetic authored fixture: write hello to /workspace/result.\n", encoding="utf-8")
    (task / "environment/Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (task / "tests/test.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    return root


def write_artifact(root: Path, name: str, content: bytes) -> ArtifactRef:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return ArtifactRef(
        media_type="application/octet-stream",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        path=name,
    )


def build_complete_trial_dir(
    root: Path,
    *,
    trial_id: str = "trial-1",
    plan: Sequence[str] | None = None,
    runtime_lock: RuntimeLock | None = None,
    tamper: str | None = None,
    omit: str | None = None,
    descriptor: bool = True,
    session_root: str = "dsh-home",
    manifest_overrides: Mapping[str, object] | None = None,
) -> tuple[Path, CollectionManifest]:
    """Create a trial dir whose evidence bundle verifies cleanly.

    Files land at their FIXED paths (P0-6); the manifest is bound to
    the runtime lock and every outcome records a successful execution.
    ``tamper`` rewrites one output after hashing; ``omit`` drops one
    logical output entirely; ``descriptor=False`` skips the bundle
    descriptor. All produce gate failures for negative tests.
    """
    if runtime_lock is None:
        raise TypeError("runtime_lock is required since P0-6 (manifest binding)")
    from aeval.hooks.evidence import output_path_for

    if plan is None:
        plan = (
            "runtime_dump", "mock_call_log", "dsh_session", "canonical_transcript",
        )
    root.mkdir(parents=True, exist_ok=True)
    manifest = CollectionManifest(
        schema_version=1,
        trial_id=trial_id,
        outcomes=[],
        artifacts=[],
        runtime_lock_digest=runtime_lock.digest(),
    )
    now = datetime.now(timezone.utc)
    for name in plan:
        if name == omit:
            continue
        rel = output_path_for(name)
        content = json.dumps({"name": name, "payload": 1}).encode()
        ref = write_artifact(root, rel, content)
        manifest.outcomes.append(
            CollectOutcome(
                name=name,
                command=f"snapshot {name}",
                exit_code=0,
                started_at=now,
                finished_at=now,
                output_path=rel,
                sha256=ref.sha256,
                atomic=True,
            )
        )
        manifest.artifacts.append(ref)
    if manifest_overrides:
        for key, value in manifest_overrides.items():
            setattr(manifest, key, value)
    (root / "collection_manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    if descriptor:
        # The official session record the descriptor's session_root must
        # hold: collection copies it to the fixed logical path, and the
        # gate checks ownership by CONTENT (the artifact must BE this
        # record), not by where the copy sits.
        session_id = "s-1"
        record = root / session_root / session_id / "session.v4.jsonl.zstd"
        record.parent.mkdir(parents=True, exist_ok=True)
        collected = root / output_path_for("dsh_session")
        record.write_bytes(
            collected.read_bytes() if collected.is_file() else b"session-record"
        )
        (root / "bundle_descriptor.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "run": {
                        "run_id": "run-test",
                        "job_config_hash": "a" * 64,
                        "config_file_sha256": "b" * 64,
                        "runtime_lock_digest": runtime_lock.digest(),
                    },
                    "trial_id": trial_id,
                    "session_id": "s-1",
                    "session_root": session_root,
                    "stop_reason": "agent_exit_0",
                    "config_digest": "d" * 64,
                }
            ),
            encoding="utf-8",
        )
    if tamper is not None:
        (root / output_path_for(tamper)).write_bytes(b"tampered")
    return root, manifest
