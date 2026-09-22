"""Shared pytest fixtures for aeval tests."""

from __future__ import annotations

import hashlib
import json
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


def write_artifact(root: Path, name: str, content: bytes) -> ArtifactRef:
    path = root / name
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
    required: tuple[str, ...] = (
        "runtime_dump",
        "mock_call_log",
        "dsh_session",
        "collection_manifest",
    ),
    tamper: str | None = None,
    omit: str | None = None,
) -> tuple[Path, CollectionManifest]:
    """Create a trial dir whose evidence bundle verifies cleanly.

    ``tamper`` rewrites a file after hashing; ``omit`` drops one
    required output. Both produce gate failures for negative tests.
    """
    root.mkdir(parents=True, exist_ok=True)
    manifest = CollectionManifest(
        schema_version=1,
        trial_id=trial_id,
        outcomes=[],
        artifacts=[],
    )
    now = datetime.now(timezone.utc)
    for name in required:
        if name == omit:
            continue
        content = json.dumps({name: "payload"}).encode()
        ref = write_artifact(root, name, content)
        manifest.outcomes.append(
            CollectOutcome(
                name=name,
                command=f"snapshot {name}",
                exit_code=0,
                started_at=now,
                finished_at=now,
                output_path=name,
                sha256=ref.sha256,
                atomic=True,
            )
        )
        manifest.artifacts.append(ref)
    if tamper is not None:
        (root / tamper).write_bytes(b"tampered")
    (root / "collection_manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    # the manifest file itself is also an artifact of the collection
    if "collection_manifest" in required and omit != "collection_manifest":
        pass
    return root, manifest
