"""Manifest seal / tamper tests (plan §7 row 12): sealed means sealed."""

from __future__ import annotations

import json
import os

import pytest

from aeval.bundle.manifest import (
    ManifestTamperError,
    seal_run_manifest,
    validate_manifest_references,
    verify_seal,
    write_intent_manifest,
)
from aeval.contracts import ExclusionSummary, ImageIdentity, RunManifest


def _manifest(run_id: str, runtime_lock) -> RunManifest:
    from aeval.contracts import OverlayIdentity, VersionsBundle

    return RunManifest(
        run_id=run_id,
        runtime_lock=runtime_lock,
        overlay=OverlayIdentity(
            suite_id="s", suite_version="1", overlay_digest="d" * 64,
            source_commit="9" * 40,
        ),
        versions=VersionsBundle(aeval_version="0.1.0"),
    )


def test_intent_manifest_written_once_only(tmp_path, runtime_lock):
    path = write_intent_manifest(_manifest("r1", runtime_lock), tmp_path)
    assert path.name == "run_manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["sealed"] is False
    with pytest.raises(ManifestTamperError, match="never reused"):
        write_intent_manifest(_manifest("r1", runtime_lock), tmp_path)


def test_seal_appends_exclusions_and_verifies(tmp_path, runtime_lock):
    path = write_intent_manifest(_manifest("r1", runtime_lock), tmp_path)
    exclusions = ExclusionSummary(total=3, valid=2, excluded={"infra_invalid": 1})
    _, seal_digest = seal_run_manifest(path, exclusions)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["sealed"] is True
    assert data["exclusions"]["excluded"] == {"infra_invalid": 1}
    assert data["seal_digest"] == seal_digest
    assert verify_seal(path) == seal_digest


def test_double_seal_rejected(tmp_path, runtime_lock):
    path = write_intent_manifest(_manifest("r1", runtime_lock), tmp_path)
    seal_run_manifest(path, ExclusionSummary())
    with pytest.raises(ManifestTamperError, match="already sealed"):
        seal_run_manifest(path, ExclusionSummary())


def test_post_seal_intent_tamper_detected(tmp_path, runtime_lock):
    path = write_intent_manifest(_manifest("r1", runtime_lock), tmp_path)
    seal_run_manifest(path, ExclusionSummary())
    os.chmod(path, 0o666)  # undo the read-only freeze (Windows no-op anyway)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["argv_hash"] = "rewritten-after-seal"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ManifestTamperError, match="modified after sealing"):
        verify_seal(path)


def test_unsealed_manifest_rejected_by_verify(tmp_path, runtime_lock):
    path = write_intent_manifest(_manifest("r1", runtime_lock), tmp_path)
    with pytest.raises(ManifestTamperError, match="not sealed"):
        verify_seal(path)


def test_manifest_requires_full_overlay_commit(tmp_path, runtime_lock):
    manifest = _manifest("r1", runtime_lock)
    manifest.overlay.source_commit = "shortsha"
    with pytest.raises(ManifestTamperError, match="40-char SHA"):
        validate_manifest_references(manifest)


def test_manifest_rejects_unpinned_image(runtime_lock):
    manifest = _manifest("r1", runtime_lock)
    manifest.runtime_lock.images["task"] = ImageIdentity.model_construct(
        reference="repo/task:latest", digest="sha256:" + "a" * 64,
        platform="linux/amd64", pinned=False,
    )
    with pytest.raises(ManifestTamperError, match="not digest-pinned"):
        validate_manifest_references(manifest)


def test_valid_manifest_references_pass(runtime_lock):
    validate_manifest_references(_manifest("r1", runtime_lock))
