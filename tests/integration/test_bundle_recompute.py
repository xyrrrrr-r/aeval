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


def test_recompute_without_attestation_is_a_hard_failure(tmp_path, runtime_lock):
    """P0-8: a bundle without attestation is unverifiable, not a warning."""
    run_dir = tmp_path / "run"
    manifest = _manifest(runtime_lock)
    manifest_path = write_intent_manifest(manifest, run_dir)
    seal_run_manifest(manifest_path, ExclusionSummary())
    with pytest.raises(ManifestTamperError, match="no attestation.json in bundle"):
        recompute_bundle(run_dir)


def test_recompute_rejects_empty_attestation(tmp_path, runtime_lock):
    """P0-8: an attestation with no entries seals nothing."""
    run_dir = tmp_path / "run"
    _build_bundle(run_dir, runtime_lock)
    attestation = json.loads((run_dir / "attestation.json").read_text(encoding="utf-8"))
    attestation["entries"] = []
    (run_dir / "attestation.json").write_text(
        json.dumps(attestation, indent=2), encoding="utf-8"
    )
    with pytest.raises(ManifestTamperError, match="no entries"):
        recompute_bundle(run_dir)


def test_recompute_rejects_deleted_manifest_entry(tmp_path, runtime_lock):
    """P0-8: deleting a manifest-referenced file AND trimming its entry fails.

    harbor-job.json is bound by the manifest's config_file_sha256, so
    removing both the file and its attestation entry is still detected.
    (Deleting an UNREFERENCED file and trimming its entry is not
    bundle-internally detectable — that is why the finalize gate
    cross-checks store records; see test_bundle_finalize.py.)
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    manifest = _manifest(runtime_lock)
    config = b'{"job_name": "j"}\n'
    (run_dir / "harbor-job.json").write_bytes(config)
    manifest.config_file_sha256 = __import__("hashlib").sha256(config).hexdigest()
    manifest_path = write_intent_manifest(manifest, run_dir)
    (run_dir / "trial-1.jsonl").write_text('{"trial": 1}\n', encoding="utf-8")
    seal_run_manifest(manifest_path, ExclusionSummary(total=1, valid=1))
    create_test_result_attestation(run_dir)
    recompute_bundle(run_dir)  # intact bundle passes
    # delete the referenced file and trim the attestation to match
    (run_dir / "harbor-job.json").unlink()
    attestation = json.loads((run_dir / "attestation.json").read_text(encoding="utf-8"))
    attestation["entries"] = [
        e for e in attestation["entries"] if e["path"] != "harbor-job.json"
    ]
    (run_dir / "attestation.json").write_text(
        json.dumps(attestation, indent=2), encoding="utf-8"
    )
    with pytest.raises(ManifestTamperError, match="harbor-job.json but it is missing"):
        recompute_bundle(run_dir)


def test_recompute_rejects_runtime_lock_rewrite(tmp_path, runtime_lock):
    """P0-8: rewriting runtime_lock.json breaks the manifest's lock digest."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    manifest = _manifest(runtime_lock)
    manifest.runtime_lock_digest = runtime_lock.digest()
    lock_path = run_dir / "runtime_lock.json"
    lock_path.write_text(runtime_lock.model_dump_json(), encoding="utf-8")
    manifest_path = write_intent_manifest(manifest, run_dir)
    (run_dir / "trial-1.jsonl").write_text('{"trial": 1}\n', encoding="utf-8")
    seal_run_manifest(manifest_path, ExclusionSummary(total=1, valid=1))
    create_test_result_attestation(run_dir)
    recompute_bundle(run_dir)  # intact bundle passes
    # rewrite the lock with a mutated harbor_lock_ref → different digest
    # (digest() excludes created_at, so that field would not change it)
    mutated = runtime_lock.model_copy(update={"harbor_lock_ref": "tampered"})
    lock_path.write_text(mutated.model_dump_json(), encoding="utf-8")
    attestation = json.loads((run_dir / "attestation.json").read_text(encoding="utf-8"))
    content = lock_path.read_bytes()
    import hashlib

    for entry in attestation["entries"]:
        if entry["path"] == "runtime_lock.json":
            entry["sha256"] = hashlib.sha256(content).hexdigest()
            entry["size"] = len(content)
    (run_dir / "attestation.json").write_text(
        json.dumps(attestation, indent=2), encoding="utf-8"
    )
    with pytest.raises(ManifestTamperError, match="runtime_lock.json digest differs"):
        recompute_bundle(run_dir)


def test_recompute_rejects_size_mismatch(tmp_path, runtime_lock):
    """P0-8: same digest claim but wrong recorded size fails."""
    run_dir = tmp_path / "run"
    _build_bundle(run_dir, runtime_lock)
    attestation = json.loads((run_dir / "attestation.json").read_text(encoding="utf-8"))
    for entry in attestation["entries"]:
        if entry["path"] == "trial-1.jsonl":
            entry["size"] = entry["size"] + 1
    (run_dir / "attestation.json").write_text(
        json.dumps(attestation, indent=2), encoding="utf-8"
    )
    with pytest.raises(ManifestTamperError, match="size mismatch"):
        recompute_bundle(run_dir)


def test_recompute_rejects_path_escape(tmp_path, runtime_lock):
    """P0-8: an entry pointing outside the bundle is refused."""
    run_dir = tmp_path / "run"
    _build_bundle(run_dir, runtime_lock)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    attestation = json.loads((run_dir / "attestation.json").read_text(encoding="utf-8"))
    import hashlib

    attestation["entries"].append({
        "path": "../outside.txt",
        "sha256": hashlib.sha256(b"outside\n").hexdigest(),
        "size": len("outside\n"),
    })
    (run_dir / "attestation.json").write_text(
        json.dumps(attestation, indent=2), encoding="utf-8"
    )
    with pytest.raises(ManifestTamperError, match="escapes the bundle"):
        recompute_bundle(run_dir)


def test_recompute_rejects_unattested_extra_file(tmp_path, runtime_lock):
    """P0-8: a file swapped into the bundle after attestation fails."""
    run_dir = tmp_path / "run"
    _build_bundle(run_dir, runtime_lock)
    (run_dir / "smuggled.txt").write_text("not attested\n", encoding="utf-8")
    with pytest.raises(ManifestTamperError, match="unattested file"):
        recompute_bundle(run_dir)


def test_recompute_rejects_config_digest_mismatch(tmp_path, runtime_lock):
    """P0-8: harbor-job.json rewritten after intent time fails the manifest binding."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    manifest = _manifest(runtime_lock)
    config = b'{"job_name": "j"}\n'
    (run_dir / "harbor-job.json").write_bytes(config)
    manifest.config_file_sha256 = __import__("hashlib").sha256(config).hexdigest()
    manifest_path = write_intent_manifest(manifest, run_dir)
    (run_dir / "trial-1.jsonl").write_text('{"trial": 1}\n', encoding="utf-8")
    seal_run_manifest(manifest_path, ExclusionSummary(total=1, valid=1))
    create_test_result_attestation(run_dir)
    # recompute passes on the intact bundle
    recompute_bundle(run_dir)
    # rewriting the config breaks the recorded digest
    (run_dir / "harbor-job.json").write_bytes(b'{"job_name": "rewritten"}\n')
    attestation = json.loads((run_dir / "attestation.json").read_text(encoding="utf-8"))
    import hashlib as _h

    rewritten = b'{"job_name": "rewritten"}\n'
    for entry in attestation["entries"]:
        if entry["path"] == "harbor-job.json":
            entry["sha256"] = _h.sha256(rewritten).hexdigest()
            entry["size"] = len(rewritten)
    (run_dir / "attestation.json").write_text(
        json.dumps(attestation, indent=2), encoding="utf-8"
    )
    with pytest.raises(ManifestTamperError, match="digest differs from the manifest"):
        recompute_bundle(run_dir)
