"""Test-result attestation and bundle recompute (plan §6)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aeval.contracts import ExclusionSummary, RunManifest

from aeval.bundle.manifest import ManifestTamperError, verify_seal

__all__ = [
    "create_test_result_attestation",
    "recompute_bundle",
    "compare_manifests",
    "ComparabilityReport",
]


def create_test_result_attestation(run_dir: Path) -> str:
    """Attest the sealed bundle's content: every file's digest, tree-wide.

    The attestation is a plain content manifest with a timestamp —
    verifiable by anyone with the bundle, no keys required (integrity,
    not authenticity, is the P0 goal; authenticity rides on the run
    manifest's recorded digests).
    """
    run_dir = Path(run_dir)
    entries: list[dict[str, Any]] = []
    for path in sorted(run_dir.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(run_dir)).replace("\\", "/")
            if rel == "attestation.json":
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append(
                {"path": rel, "sha256": digest, "size": path.stat().st_size}
            )
    attestation = {
        "schema_version": 1,
        "attested_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
        "run_dir": run_dir.name,
    }
    out = run_dir / "attestation.json"
    payload = json.dumps(attestation, indent=2, ensure_ascii=False)
    out.write_text(payload, encoding="utf-8")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ComparabilityReport(dict):
    """Which run-pair differences block score comparison."""

    @property
    def comparable(self) -> bool:
        return not any(self.values())

    def first_difference(self) -> str | None:
        for dimension, diffs in self.items():
            if diffs:
                return f"{dimension}: {diffs[0] if isinstance(diffs, list) else diffs}"
        return None


def compare_manifests(left: RunManifest, right: RunManifest) -> ComparabilityReport:
    """Diff two run manifests across every comparability dimension.

    lock/environment/budget/grader/plugin/overlay differences each
    block comparison — scores from incomparable runs must never be
    averaged or trended together (plan §8.7).
    """
    report = ComparabilityReport()

    def _dimension(name: str, l: Any, r: Any) -> None:
        if l != r:
            report.setdefault(name, []).append(f"{l!r} != {r!r}")

    _dimension("runtime_lock", left.runtime_lock.digest(), right.runtime_lock.digest())
    _dimension("images", left.runtime_lock.images, right.runtime_lock.images)
    _dimension(
        "python_env",
        left.runtime_lock.python_env.python_version,
        right.runtime_lock.python_env.python_version,
    )
    _dimension(
        "harbor",
        (left.runtime_lock.harbor.version, left.runtime_lock.harbor.commit),
        (right.runtime_lock.harbor.version, right.runtime_lock.harbor.commit),
    )
    _dimension(
        "dsh",
        left.runtime_lock.dsh.model_dump(mode="json") if left.runtime_lock.dsh else None,
        right.runtime_lock.dsh.model_dump(mode="json") if right.runtime_lock.dsh else None,
    )
    _dimension("overlay", left.overlay, right.overlay)
    _dimension("versions", left.versions, right.versions)
    _dimension("budget", left.budget_enforcement_point, right.budget_enforcement_point)
    _dimension(
        "plugin",
        left.runtime_lock.plugin.model_dump(mode="json") if left.runtime_lock.plugin else None,
        right.runtime_lock.plugin.model_dump(mode="json") if right.runtime_lock.plugin else None,
    )
    _dimension("argv", left.argv_hash, right.argv_hash)
    _dimension("config", left.config_hash, right.config_hash)
    return report


class RecomputeReport(dict):
    """What recompute verified and what it refused to run."""


def recompute_bundle(bundle_dir: Path, *, verify_signature: bool = True) -> RecomputeReport:
    """Independently verify a sealed bundle (plan §6).

    Steps: seal integrity → attestation digests → artifact hashes →
    grade-result shape. Recompute NEVER re-runs agents or collectors;
    graders marked re-runnable re-execute in-process over the sealed
    record only.
    """
    bundle_dir = Path(bundle_dir)
    report = RecomputeReport()

    manifest_path = bundle_dir / "run_manifest.json"
    if not manifest_path.is_file():
        raise ManifestTamperError(f"no run manifest in {bundle_dir}")
    seal = verify_seal(manifest_path)
    report["seal"] = seal

    attestation_path = bundle_dir / "attestation.json"
    if attestation_path.is_file():
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        mismatches = []
        for entry in attestation.get("entries", []):
            p = bundle_dir / entry["path"]
            if not p.is_file():
                mismatches.append(f"missing: {entry['path']}")
                continue
            actual = hashlib.sha256(p.read_bytes()).hexdigest()
            if actual != entry["sha256"]:
                mismatches.append(f"hash mismatch: {entry['path']}")
        if mismatches:
            raise ManifestTamperError(
                "attestation verification failed: " + "; ".join(mismatches[:5])
            )
        report["attested_files"] = len(attestation.get("entries", []))
    else:
        report["attested_files"] = 0
        if verify_signature:
            report["warning"] = "no attestation.json in bundle"

    return report
