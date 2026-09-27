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


def _entry_failures(bundle_dir: Path, entry: dict[str, Any]) -> list[str]:
    """Validate one attestation entry against the bundle on disk."""
    problems: list[str] = []
    rel = entry.get("path")
    if not isinstance(rel, str) or not rel:
        return [f"attestation entry without a path: {entry!r}"]
    if rel == "attestation.json":
        return [f"attestation must not attest itself: {rel}"]
    candidate = bundle_dir / rel
    # Containment by resolved components, never string prefixes: a
    # symlinked or ``..``-laden path cannot escape the bundle unnoticed.
    resolved_root = bundle_dir.resolve()
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return [f"cannot resolve {rel!r}: {exc}"]
    if not resolved.is_relative_to(resolved_root):
        return [f"path escapes the bundle: {rel}"]
    if not resolved.is_file():
        return [f"missing: {rel}"]
    try:
        content = resolved.read_bytes()
    except OSError as exc:
        return [f"unreadable: {rel}: {exc}"]
    actual = hashlib.sha256(content).hexdigest()
    if actual != entry.get("sha256"):
        problems.append(f"hash mismatch: {rel}")
    if entry.get("size") != resolved.stat().st_size:
        problems.append(
            f"size mismatch: {rel} (attested {entry.get('size')}, "
            f"actual {resolved.stat().st_size})"
        )
    return problems


def recompute_bundle(bundle_dir: Path, *, verify_signature: bool = True) -> RecomputeReport:
    """Independently verify a sealed bundle (plan §6, P0-8 strict).

    Steps: seal integrity → attestation presence and completeness →
    per-entry containment/size/digest → required and manifest-referenced
    files → unattested-file detection. Recompute NEVER re-runs agents
    or collectors; it verifies the sealed artifact set.

    Strictness (P0-8): a missing or empty attestation, a deleted
    manifest entry, a missing required file, an unattested file, a
    size/digest mismatch, or a path escape each FAIL the recompute —
    none of them downgrade to a warning.
    """
    bundle_dir = Path(bundle_dir)
    report = RecomputeReport()

    manifest_path = bundle_dir / "run_manifest.json"
    if not manifest_path.is_file():
        raise ManifestTamperError(f"no run manifest in {bundle_dir}")
    seal = verify_seal(manifest_path)
    report["seal"] = seal
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    attestation_path = bundle_dir / "attestation.json"
    if not attestation_path.is_file():
        raise ManifestTamperError(
            f"no attestation.json in bundle — integrity is unverifiable "
            f"(this is a hard failure, not a warning)"
        )
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    entries = attestation.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ManifestTamperError("attestation carries no entries")

    attested: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append(f"malformed attestation entry: {entry!r}")
            continue
        rel = entry.get("path")
        if isinstance(rel, str):
            if rel in attested:
                problems.append(f"duplicate attested path: {rel}")
            attested[rel] = entry
        problems.extend(_entry_failures(bundle_dir, entry))

    # Required files: the manifest itself must be attested, and every
    # other regular file in the bundle (except the attestation) must be
    # attested — a deleted file whose entry was also removed from the
    # attestation is caught by the manifest-referenced checks below, a
    # swapped-in extra file is caught here.
    if "run_manifest.json" not in attested:
        problems.append("run_manifest.json is not attested")
    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(bundle_dir).as_posix()
        if rel == "attestation.json":
            continue
        if rel not in attested:
            problems.append(f"unattested file in bundle: {rel}")

    # Manifest-referenced files: the config file digest recorded at
    # intent time must still match the on-disk config, and the runtime
    # lock file must still parse to the locked digest. These bindings
    # are what make "delete a file AND remove its attestation entry"
    # detectable for everything the manifest references.
    config_digest = manifest.get("config_file_sha256")
    if config_digest:
        config_path = bundle_dir / "harbor-job.json"
        if not config_path.is_file():
            problems.append("manifest references harbor-job.json but it is missing")
        else:
            actual = hashlib.sha256(config_path.read_bytes()).hexdigest()
            if actual != config_digest:
                problems.append("harbor-job.json digest differs from the manifest")
    elif verify_signature:
        report["note"] = "manifest carries no config_file_sha256 (pre-P0-8 manifest)"

    lock_digest = manifest.get("runtime_lock_digest")
    lock_path = bundle_dir / "runtime_lock.json"
    if lock_digest:
        if not lock_path.is_file():
            problems.append("manifest references runtime_lock.json but it is missing")
        else:
            from aeval.contracts import RuntimeLock

            try:
                actual_lock = RuntimeLock.model_validate_json(
                    lock_path.read_text(encoding="utf-8")
                )
            except Exception as exc:
                problems.append(f"runtime_lock.json is unreadable: {exc}")
            else:
                if actual_lock.digest() != lock_digest:
                    problems.append("runtime_lock.json digest differs from the manifest")

    if problems:
        raise ManifestTamperError(
            "bundle verification failed: " + "; ".join(problems[:8])
            + (f" (+{len(problems) - 8} more)" if len(problems) > 8 else "")
        )
    report["attested_files"] = len(entries)
    report["bundle_dir"] = bundle_dir.name
    return report
