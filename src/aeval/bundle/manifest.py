"""Run manifests: intent at start, sealed with exclusions at end (plan §6)."""

from __future__ import annotations

import json
import os
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any

from aeval.contracts import ExclusionSummary, OverlayIdentity, RuntimeLock, RunManifest

__all__ = [
    "ManifestTamperError",
    "write_intent_manifest",
    "seal_run_manifest",
    "validate_manifest_references",
]


class ManifestTamperError(RuntimeError):
    """The sealed manifest changed outside the allowed append path."""


def write_intent_manifest(manifest: RunManifest, run_dir: Path) -> Path:
    """Write the intent manifest at run start (before any trial).

    The file records the full intent: runtime lock, overlay identity,
    versions, hashes. After sealing, only `exclusions` may be appended.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "run_manifest.json"
    if path.exists():
        raise ManifestTamperError(
            f"intent manifest already exists: {path} — run ids are never reused"
        )
    data = manifest.model_dump(mode="json", exclude_none=True)
    data["sealed"] = False
    _atomic_write_json(path, data)
    return path


def seal_run_manifest(path: Path, exclusions: ExclusionSummary) -> tuple[Path, str]:
    """Seal: append exclusions, write the final digest, freeze the file.

    Returns (path, seal_digest). Any later modification of the manifest
    is detectable by comparing the file's content digest against the
    recorded seal.
    """
    path = Path(path)
    data = _read_json(path)
    if data.get("sealed"):
        raise ManifestTamperError(f"manifest already sealed: {path}")
    # Everything except `exclusions` must be untouched from intent time.
    intent_copy = {k: v for k, v in data.items() if k not in ("exclusions", "sealed")}
    data["exclusions"] = exclusions.model_dump(mode="json", exclude_none=True)
    data["sealed"] = True
    seal_digest = sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    data["seal_digest"] = seal_digest
    _atomic_write_json(path, data)
    # Freeze: read-only from here on (best-effort on Windows).
    try:
        os.chmod(path, 0o444)
    except OSError:
        pass
    return path, seal_digest


def validate_manifest_references(manifest: RunManifest) -> None:
    """Structural self-check: identity fields must be present and pinned."""
    if not manifest.run_id:
        raise ManifestTamperError("run manifest has no run_id")
    lock: RuntimeLock = manifest.runtime_lock
    if not lock.harbor.version:
        raise ManifestTamperError("run manifest has no harbor version")
    for name, image in lock.images.items():
        if not image.pinned or "@sha256:" not in image.reference:
            raise ManifestTamperError(
                f"run manifest image {name!r} is not digest-pinned"
            )
    overlay: OverlayIdentity = manifest.overlay
    if not overlay.overlay_digest or not overlay.source_commit:
        raise ManifestTamperError(
            "run manifest overlay identity incomplete "
            "(overlay_digest/source_commit required)"
        )
    if len(overlay.source_commit) != 40:
        raise ManifestTamperError(
            f"overlay source_commit must be a full 40-char SHA: "
            f"{overlay.source_commit!r}"
        )


def verify_seal(path: Path) -> str:
    """Recompute the seal digest; raise on any tampering."""
    path = Path(path)
    data = _read_json(path)
    if not data.get("sealed"):
        raise ManifestTamperError(f"manifest not sealed: {path}")
    recorded = data.get("seal_digest")
    if not recorded:
        raise ManifestTamperError(f"sealed manifest carries no seal digest: {path}")
    content = {k: v for k, v in data.items() if k != "seal_digest"}
    actual = sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if actual != recorded:
        raise ManifestTamperError(
            f"manifest seal digest mismatch: expected {recorded}, actual {actual} — "
            "the sealed manifest was modified after sealing"
        )
    return actual


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestTamperError(f"cannot read manifest {path}: {exc}") from exc


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
