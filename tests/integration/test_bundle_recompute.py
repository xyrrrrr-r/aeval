"""Bundle recompute integration tests (plan §7 row 12).

Recompute must verify a sealed bundle's integrity without re-running
anything, and detect post-attestation tampering.
"""

from __future__ import annotations

import json
import os

import pytest

from aeval.bundle.attestation import create_test_result_attestation, recompute_bundle
from aeval.bundle.manifest import (
    ManifestTamperError,
    seal_run_manifest,
    write_intent_manifest,
)
from aeval.contracts import ExclusionSummary, OverlayIdentity, RunManifest, VersionsBundle


def _manifest(runtime_lock, run_id="r1") -> RunManifest:
    return RunManifest(
        run_id=run_id,
        runtime_lock=runtime_lock,
        overlay=OverlayIdentity(
            suite_id="s", suite_version="1", overlay_digest="d" * 64,
            source_commit="9" * 40,
        ),
        versions=VersionsBundle(aeval_version="0.1.0"),
    )


def _build_bundle(run_dir, runtime_lock, with_artifact=True):
    manifest = _manifest(runtime_lock)
    manifest_path = write_intent_manifest(manifest, run_dir)
    if with_artifact:
        (run_dir / "trial-1.jsonl").write_text('{"trial": 1}\n', encoding="utf-8")
    seal_run_manifest(manifest_path, ExclusionSummary(total=1, valid=1))
    attestation_digest = create_test_result_attestation(run_dir)
    return manifest_path, attestation_digest


def test_recompute_verifies_clean_bundle(tmp_path, runtime_lock):
    run_dir = tmp_path / "run"
    manifest_path, attestation_digest = _build_bundle(run_dir, runtime_lock)
    report = recompute_bundle(run_dir)
    assert report["seal"] == json.loads(
        manifest_path.read_text(encoding="utf-8"))["seal_digest"]
    assert report["attested_files"] >= 2  # manifest + artifact
    assert "warning" not in report
    assert len(attestation_digest) == 64


def test_recompute_detects_post_attestation_tamper(tmp_path, runtime_lock):
    run_dir = tmp_path / "run"
    _build_bundle(run_dir, runtime_lock)
    (run_dir / "trial-1.jsonl").write_text('{"trial": "rewritten"}\n', encoding="utf-8")
    with pytest.raises(ManifestTamperError, match="hash mismatch"):
        recompute_bundle(run_dir)


def test_recompute_detects_missing_attested_file(tmp_path, runtime_lock):
    run_dir = tmp_path / "run"
    _build_bundle(run_dir, runtime_lock)
    (run_dir / "trial-1.jsonl").unlink()
    with pytest.raises(ManifestTamperError, match="missing"):
        recompute_bundle(run_dir)


def test_recompute_requires_manifest(tmp_path):
    with pytest.raises(ManifestTamperError, match="no run manifest"):
        recompute_bundle(tmp_path)


def test_recompute_rejects_unsealed_manifest(tmp_path, runtime_lock):
    run_dir = tmp_path / "run"
    write_intent_manifest(_manifest(runtime_lock), run_dir)
    with pytest.raises(ManifestTamperError, match="not sealed"):
        recompute_bundle(run_dir)


def test_recompute_without_attestation_warns(tmp_path, runtime_lock):
    run_dir = tmp_path / "run"
    manifest = _manifest(runtime_lock)
    manifest_path = write_intent_manifest(manifest, run_dir)
    seal_run_manifest(manifest_path, ExclusionSummary())
    report = recompute_bundle(run_dir)
    assert report["attested_files"] == 0
    assert "no attestation.json" in report["warning"]
