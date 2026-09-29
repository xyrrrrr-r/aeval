"""RuntimeLock / supply-chain gate tests (plan §7 row 1).

The negative tests must assert that no run bookkeeping is created by a
failing lock (the gate runs before any manifest/trial), and messages
carry expected/actual.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from aeval import provenance
from aeval.contracts import ControlDistLock, ImageIdentity, NpmPackageLock
from aeval.provenance import (
    DSH_NODE_VERSIONS,
    LockMismatchError,
    assert_clean_harbor_source,
    build_official_dsh_lock,
    build_runtime_lock,
    verify_runtime_lock,
)


def _pinned_image() -> ImageIdentity:
    return ImageIdentity(
        reference="registry.example.com/task@sha256:" + "a" * 64,
        digest="sha256:" + "a" * 64,
        platform="linux/amd64",
    )


def test_build_and_verify_roundtrip_passes_on_live_env():
    lock = build_runtime_lock(images={"task": _pinned_image()})
    assert lock.harbor.version == provenance.OFFICIAL_HARBOR_VERSION
    assert lock.harbor.commit == provenance.OFFICIAL_HARBOR_COMMIT
    assert lock.dsh.official_tag == provenance.OFFICIAL_DSH_TAG
    verify_runtime_lock(lock)


def test_installed_harbor_must_match_official_version(monkeypatch):
    monkeypatch.setattr(provenance, "OFFICIAL_HARBOR_VERSION", "0.99.0")
    with pytest.raises(LockMismatchError, match="expected 0.99.0, actual 0.23.0"):
        build_runtime_lock()


def test_verify_rejects_harbor_version_drift():
    lock = build_runtime_lock()
    lock.harbor.version = "0.24.0"
    with pytest.raises(LockMismatchError, match="harbor version: expected '0.24.0'"):
        verify_runtime_lock(lock)


def test_verify_rejects_python_version_drift():
    lock = build_runtime_lock()
    lock.python_env.python_version = "3.13.0"
    with pytest.raises(LockMismatchError, match="python version: expected '3.13.0'"):
        verify_runtime_lock(lock)


def test_verify_rejects_unpinned_image_even_past_model_layer():
    # The pydantic model already refuses mutable tags; the lock layer must
    # still catch one if it ever slips through (defense in depth).
    lock = build_runtime_lock()
    lock.images["task"] = ImageIdentity.model_construct(
        reference="registry.example.com/task:latest",
        digest="sha256:" + "a" * 64,
        platform="linux/amd64",
        pinned=False,
    )
    with pytest.raises(LockMismatchError, match="not digest-pinned"):
        verify_runtime_lock(lock)


def test_verify_rejects_dsh_lock_without_integrity():
    lock = build_runtime_lock()
    lock.dsh.packages = [
        NpmPackageLock(name="@deepseek-ai/dsh", version="0.1.7-alpha.1", integrity=None),
        *[p for p in lock.dsh.packages if p.name != "@deepseek-ai/dsh"],
    ]
    with pytest.raises(LockMismatchError, match="missing its npm integrity hash"):
        verify_runtime_lock(lock)


def test_verify_rejects_empty_dsh_package_set():
    lock = build_runtime_lock()
    lock.dsh.packages = []
    with pytest.raises(LockMismatchError, match="declares no npm packages"):
        verify_runtime_lock(lock)


def test_verify_rejects_plugin_version_drift():
    lock = build_runtime_lock()
    lock.plugin.version = "9.9.9"
    with pytest.raises(LockMismatchError, match="aeval plugin version: expected '9.9.9'"):
        verify_runtime_lock(lock)


def test_official_dsh_slice_is_fully_pinned():
    dsh = build_official_dsh_lock()
    by_name = {p.name: p for p in dsh.packages}
    assert by_name["@deepseek-ai/dsh"].integrity is not None
    assert by_name["@deepseek-ai/cordis"].version == "4.0.3"
    assert by_name["@agentclientprotocol/sdk"].version == "1.4.0"
    # schemastery is a standalone library (like cordis), not part of the
    # DSH release train; it carries its own version line.
    assert by_name["@deepseek-ai/schemastery"].version == "3.18.3"
    assert all(
        p.version == "0.1.7-alpha.1"
        for n, p in by_name.items()
        if n.startswith("@deepseek-ai/")
        and n not in ("@deepseek-ai/cordis", "@deepseek-ai/schemastery")
    )
    assert dsh.experimental is True
    assert dsh.node_versions == list(DSH_NODE_VERSIONS)


def _make_git_repo(tmp_path: Path, dirty: bool = False) -> Path:
    repo = tmp_path / "harbor-src"
    repo.mkdir()
    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "pyproject.toml").write_text("name = 'harbor'\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if dirty:
        (repo / "local-edit.txt").write_text("uncommitted\n", encoding="utf-8")
    return repo, head  # type: ignore[return-value]


def test_clean_source_at_expected_commit_passes(tmp_path):
    repo, head = _make_git_repo(tmp_path)
    assert_clean_harbor_source(repo, head)


def test_dirty_source_rejected(tmp_path):
    repo, head = _make_git_repo(tmp_path, dirty=True)
    with pytest.raises(LockMismatchError, match="dirty"):
        assert_clean_harbor_source(repo, head)


def test_wrong_commit_rejected(tmp_path):
    repo, _head = _make_git_repo(tmp_path)
    with pytest.raises(LockMismatchError, match="expected " + "0" * 40):
        assert_clean_harbor_source(repo, "0" * 40)


def test_non_repository_rejected(tmp_path):
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    with pytest.raises(LockMismatchError, match="not a git repository"):
        assert_clean_harbor_source(empty, "0" * 40)


# --- Control-stack defenses (split plan: lock before move) ------------------
# Defense 1: the control plugin's direct import surface is pinned first-class
# in the npm slice, not trusted transitively through the DSH parent package.
# Defense 2: the control dist an operator supplies is fingerprinted into the
# lock, binding the control build to the trial.


def test_slice_pins_the_control_plugins_direct_imports():
    slice_names = {name for name, _, _ in provenance.DSH_NPM_SLICE}
    missing = set(provenance.DSH_CONTROL_DIRECT_IMPORTS) - slice_names
    assert not missing, (
        f"control plugin direct imports missing from DSH_NPM_SLICE: {sorted(missing)}"
    )


def test_control_package_pins_match_the_dsh_slice():
    """Defense 3: aeval/control's build pins and the trial lock live in ONE
    repo — every @deepseek-ai/* version in the control package's manifest
    must equal the DSH slice the lock records, or the build drifts from what
    trials attest."""
    import json

    manifest_path = Path(__file__).resolve().parents[2] / "control" / "package.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    slice_versions = {name: version for name, version, _ in provenance.DSH_NPM_SLICE}
    deps = {**manifest.get("dependencies", {}), **manifest.get("devDependencies", {})}
    deepseek_deps = {n: v for n, v in deps.items() if n.startswith("@deepseek-ai/")}
    assert deepseek_deps, "aeval/control lost its @deepseek-ai/* pins"
    for name, pinned in deepseek_deps.items():
        assert slice_versions.get(name) == pinned, (
            f"aeval/control pins {name}@{pinned} but DSH_NPM_SLICE records "
            f"{slice_versions.get(name)!r} — build pin and trial lock drifted"
        )


def test_control_plugin_import_surface_matches_recorded_list():
    """Re-measure the sibling checkout against the recorded import surface.

    Skipped (never passed) when the sibling checkout is absent: the defense
    is only checkable where the control sources live.
    """
    sibling = Path(__file__).resolve().parents[2].parent / "dsh-eval-control" / "src"
    if not sibling.is_dir():
        pytest.skip("dsh-eval-control sibling checkout not present (dev layout)")
    measured: set[str] = set()
    for source in sorted(sibling.glob("*.ts")):
        measured.update(
            re.findall(r"from '(@[^']+)'", source.read_text(encoding="utf-8"))
        )
    assert measured == set(provenance.DSH_CONTROL_DIRECT_IMPORTS), (
        "dsh-eval-control/src direct import surface drifted from "
        "DSH_CONTROL_DIRECT_IMPORTS — update the constant AND the slice"
    )


def test_lock_digest_excludes_control_dist_while_none(tmp_path):
    """A lock without a control dist digests exactly as it did before the
    field existed (the I1 invariant, extended)."""
    lock = build_runtime_lock(images={"task": _pinned_image()})
    before = lock.digest()

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.js").write_text("export {}", encoding="utf-8")
    lock.control_dist = provenance.fingerprint_control_dist(dist)

    assert lock.digest() != before
    lock.control_dist = None
    assert lock.digest() == before


def test_fingerprint_control_dist_is_deterministic_and_content_sensitive(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "b.js").write_text("// b", encoding="utf-8")
    (dist / "a.js").write_text("// a", encoding="utf-8")

    first = provenance.fingerprint_control_dist(dist)
    # Sorted by name, not creation order.
    assert first.files == ["a.js", "b.js"]
    assert provenance.fingerprint_control_dist(dist).sha256 == first.sha256

    (dist / "a.js").write_text("// a changed", encoding="utf-8")
    assert provenance.fingerprint_control_dist(dist).sha256 != first.sha256


def test_fingerprint_control_dist_requires_built_files(tmp_path):
    with pytest.raises(LockMismatchError, match="no built .js files"):
        provenance.fingerprint_control_dist(tmp_path)


def test_build_runtime_lock_records_control_dist(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "broker_main.js").write_text("// broker", encoding="utf-8")

    lock = build_runtime_lock(images={"task": _pinned_image()}, control_dist=dist)
    assert lock.control_dist is not None
    assert lock.control_dist.files == ["broker_main.js"]
    verify_runtime_lock(lock)


def test_verify_rejects_control_dist_without_files():
    lock = build_runtime_lock(images={"task": _pinned_image()})
    lock.control_dist = ControlDistLock(files=[], sha256="a" * 64)
    with pytest.raises(LockMismatchError, match="control dist lock declares no files"):
        verify_runtime_lock(lock)


def test_verify_rejects_control_dist_with_malformed_digest():
    lock = build_runtime_lock(images={"task": _pinned_image()})
    lock.control_dist = ControlDistLock(files=["index.js"], sha256="not-a-digest")
    with pytest.raises(LockMismatchError, match="sha256 hex digest"):
        verify_runtime_lock(lock)
