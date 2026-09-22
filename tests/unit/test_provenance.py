"""RuntimeLock / supply-chain gate tests (plan §7 row 1).

The negative tests must assert that no run bookkeeping is created by a
failing lock (the gate runs before any manifest/trial), and messages
carry expected/actual.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aeval import provenance
from aeval.contracts import ImageIdentity, NpmPackageLock
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
    assert all(
        p.version == "0.1.7-alpha.1"
        for n, p in by_name.items()
        if n.startswith("@deepseek-ai/") and n != "@deepseek-ai/cordis"
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
