"""Artifact store tests: content addressing + tamper proof."""

from __future__ import annotations

import json

import pytest

from aeval.contracts import ArtifactRef
from aeval.store.artifacts import ArtifactCorruptionError, ArtifactStore


def test_put_bytes_is_content_addressed(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_bytes(b"hello", "text/plain")
    digest = ref.sha256
    assert (tmp_path / "artifacts" / "objects" / digest[:2] / digest).is_file()
    assert ref.path == f"objects/{digest[:2]}/{digest}"
    assert ref.size_bytes == 5
    assert ref.media_type == "text/plain"


def test_same_content_dedups_to_one_object(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    a = store.put_bytes(b"same", "text/plain")
    b = store.put_bytes(b"same", "text/plain")
    assert a.sha256 == b.sha256
    objects = list((tmp_path / "artifacts" / "objects").rglob("*"))
    files = [p for p in objects if p.is_file()]
    assert len(files) == 1  # stored exactly once


def test_put_json_is_deterministic(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    a = store.put_json({"b": 1, "a": [1, 2]})
    b = store.put_json({"a": [1, 2], "b": 1})
    assert a.sha256 == b.sha256


def test_get_verified_roundtrip(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_bytes(b"payload", "application/octet-stream")
    assert store.get_verified(ref) == b"payload"


def test_get_verified_rejects_tampered_blob(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_bytes(b"original", "application/octet-stream")
    object_path = tmp_path / "artifacts" / "objects" / ref.sha256[:2] / ref.sha256
    object_path.write_bytes(b"tampered")
    with pytest.raises(ArtifactCorruptionError, match="hash mismatch"):
        store.get_verified(ref)


def test_get_verified_rejects_missing_blob(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_bytes(b"x", "application/octet-stream")
    object_path = tmp_path / "artifacts" / "objects" / ref.sha256[:2] / ref.sha256
    object_path.unlink()
    with pytest.raises(ArtifactCorruptionError, match="missing"):
        store.get_verified(ref)


def test_raw_is_permanent_no_delete_api(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    assert not any(
        callable(getattr(store, name, None)) and "delete" in name.lower()
        for name in dir(store)
    ), "the artifact store must never expose a delete path"


def test_index_records_every_put(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    store.put_bytes(b"a", "text/plain")
    store.put_bytes(b"b", "text/plain")
    lines = (tmp_path / "artifacts" / "index.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    entries = [json.loads(line) for line in lines]
    assert len(entries) == 2
    assert all({"sha256", "media_type", "size_bytes", "path"} <= set(e) for e in entries)
