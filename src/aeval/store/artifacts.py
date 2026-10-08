"""Content-addressed artifact store.

Blobs are stored by sha256 under ``objects/<aa>/<full-hash>`` — the
same blob is stored exactly once, and ``get_verified`` re-hashes on
read so tampering with the store is detectable. Raw data is permanent:
there is no delete.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Union

from pydantic import BaseModel

from aeval.contracts import ArtifactRef

__all__ = ["ArtifactStore", "ArtifactCorruptionError"]


class ArtifactCorruptionError(RuntimeError):
    """A stored blob's content no longer matches its address."""


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.jsonl"

    def _object_path(self, digest: str) -> Path:
        return self.objects / digest[:2] / digest

    def put_bytes(self, content: bytes, media_type: str) -> ArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        path = self._object_path(digest)
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: temp file + rename, same volume.
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(content)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        self._index(
            digest=digest, media_type=media_type, size_bytes=len(content),
            path=str(path.relative_to(self.root)).replace("\\", "/"),
        )
        return ArtifactRef(
            media_type=media_type,
            sha256=digest,
            size_bytes=len(content),
            path=str(path.relative_to(self.root)).replace("\\", "/"),
        )

    def put_json(self, value: Union[BaseModel, Mapping[str, Any]]) -> ArtifactRef:
        if isinstance(value, BaseModel):
            content = value.model_dump_json(exclude_none=True).encode("utf-8")
        else:
            content = json.dumps(
                value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        return self.put_bytes(content, "application/json")

    def get_verified(self, ref: ArtifactRef) -> bytes:
        """Read a blob and verify it still hashes to its address."""
        path = self._resolve_ref_path(ref)
        if not path.is_file():
            raise ArtifactCorruptionError(
                f"artifact {ref.sha256} missing at {ref.path}"
            )
        content = path.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if actual != ref.sha256:
            raise ArtifactCorruptionError(
                f"artifact {ref.path} hash mismatch: expected {ref.sha256}, "
                f"actual {actual}"
            )
        return content

    def _resolve_ref_path(self, ref: ArtifactRef) -> Path:
        # Refs recorded before a store relocation carry old roots; the
        # content address itself locates the object.
        candidate = self._object_path(ref.sha256)
        if candidate.is_file():
            return candidate
        return self.root / ref.path

    def _index(self, *, digest: str, media_type: str, size_bytes: int, path: str) -> None:
        entry = {
            "sha256": digest,
            "media_type": media_type,
            "size_bytes": size_bytes,
            "path": path,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
