"""Evidence collectors (P0-6): the producers behind the hard gate.

Every required evidence output is produced HERE, atomically, with its
exit status, timestamps, digest and size recorded in a
``CollectionManifest``. The manifest itself is written last and is
never an artifact of itself: its identity is bound by the outer bundle
attestation (P0-8).

The producers are pure host-side code (no sandbox interaction): the
sandbox-side ``[[verifier.collect]]`` commands are validated at suite
time (``validate_collect_declarations``); this module records what the
aeval owner actually collected and from where, so the verification
gate (``verify_evidence_bundle``) has something trustworthy to check.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from aeval.contracts import (
    ArtifactRef,
    CollectOutcome,
    CollectionManifest,
    RuntimeLock,
)
from aeval.hooks.evidence import FIXED_OUTPUT_PATHS, output_path_for

__all__ = [
    "CollectionProducerError",
    "atomic_write_bytes",
    "record_outcome",
    "produce_runtime_dump",
    "produce_mock_call_log",
    "produce_session_record",
    "produce_canonical_transcript",
    "produce_declared_artifact",
    "produce_observable",
    "write_collection_manifest",
]


class CollectionProducerError(RuntimeError):
    """A producer could not write its output atomically."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def atomic_write_bytes(path: Path, content: bytes) -> tuple[str, int]:
    """Write bytes via temp file + rename; return (sha256, size).

    The rename is same-directory, so it is atomic on POSIX and Windows
    alike. A crash between write and rename leaves no partial output.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return sha256(content).hexdigest(), len(content)


def _ref(path_rel: str, digest: str, size: int, media_type: str) -> ArtifactRef:
    return ArtifactRef(
        media_type=media_type,
        sha256=digest,
        size_bytes=size,
        path=path_rel,
    )


def record_outcome(
    *,
    name: str,
    command: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    exit_code: int | None = 0,
    exception: str | None = None,
    output_path: str | None = None,
    sha256: str | None = None,
    atomic: bool = True,
) -> CollectOutcome:
    """Record what one collection step actually did.

    ``exit_code=None`` with no exception means the step never ran — the
    verification gate treats that as missing evidence, not success.
    """
    return CollectOutcome(
        name=name,
        command=command,
        exit_code=exit_code,
        exception=exception,
        started_at=started_at,
        finished_at=finished_at,
        output_path=output_path,
        sha256=sha256,
        atomic=atomic,
    )


def _produce_bytes(
    trial_dir: Path,
    name: str,
    content: bytes,
    media_type: str,
    command: str,
) -> tuple[CollectOutcome, ArtifactRef]:
    rel = output_path_for(name)
    started = _now()
    digest, size = atomic_write_bytes(trial_dir / rel, content)
    return (
        record_outcome(
            name=name, command=command, started_at=started, finished_at=_now(),
            exit_code=0, output_path=rel, sha256=digest,
        ),
        _ref(rel, digest, size, media_type),
    )


def produce_runtime_dump(
    trial_dir: Path, dump: Mapping[str, Any]
) -> tuple[CollectOutcome, ArtifactRef]:
    """Snapshot of the sandbox runtime state as JSON."""
    content = json.dumps(dump, sort_keys=True, ensure_ascii=False, indent=2).encode()
    return _produce_bytes(
        trial_dir, "runtime_dump", content, "application/json",
        command="aeval: runtime_dump snapshot",
    )


def produce_mock_call_log(
    trial_dir: Path, calls: Sequence[Mapping[str, Any]]
) -> tuple[CollectOutcome, ArtifactRef]:
    """JSONL log of mock (model/tool) calls observed during the trial."""
    lines = "\n".join(
        json.dumps(c, sort_keys=True, ensure_ascii=False) for c in calls
    )
    content = (lines + "\n").encode("utf-8") if lines else b""
    return _produce_bytes(
        trial_dir, "mock_call_log", content, "application/x-ndjson",
        command="aeval: mock_call_log snapshot",
    )


def produce_session_record(
    trial_dir: Path, session_bytes: bytes, flavor: str = "dsh_session"
) -> tuple[CollectOutcome, ArtifactRef]:
    """The agent's official session record, in the slot the suite declared.

    The slot is agent-flavored (``driver.session_record``): ``dsh_session`` for
    DSH, ``agent_session_record`` for the generalized flavor, whose fixed path is
    ``agent_session/record``. Writing the historical DSH slot for a non-DSH
    adapter leaves the trial's required output missing even though the read
    succeeded — example-lab caught exactly that: the ACP record was read host-side,
    landed in the DSH path, and the trial was refused for "collect outcomes
    missing for required outputs: ['agent_session_record']". The default keeps
    the historical DSH plan byte-identical (name, path and command).
    """
    from aeval.agents.contract import SESSION_RECORD_OUTPUTS

    if flavor not in SESSION_RECORD_OUTPUTS:
        raise CollectionProducerError(
            f"unknown session-record flavor {flavor!r} — expected one of "
            f"{sorted(SESSION_RECORD_OUTPUTS)}"
        )
    command = (
        "aeval: dsh_session download"
        if flavor == "dsh_session"
        else f"aeval: {flavor} read"
    )
    return _produce_bytes(
        trial_dir, flavor, session_bytes, "application/octet-stream",
        command=command,
    )


def produce_canonical_transcript(
    trial_dir: Path, transcript: Any
) -> tuple[CollectOutcome, ArtifactRef]:
    """The canonical transcript built from the synced session.

    ``transcript`` is a ``CanonicalTranscript`` model (or any pydantic
    model with ``model_dump_json``).
    """
    content = transcript.model_dump_json(indent=2).encode("utf-8")
    return _produce_bytes(
        trial_dir, "canonical_transcript", content, "application/json",
        command="aeval: canonical_transcript build",
    )


def produce_declared_artifact(
    trial_dir: Path, logical_name: str, content: bytes
) -> tuple[CollectOutcome, ArtifactRef]:
    """One adapter-declared evidence artifact, at its namespaced fixed path.

    ``logical_name`` carries the ``agent_artifact:`` prefix; its path comes from
    the same fixed table discipline as every other output, so a declaration names
    evidence without being able to relocate or rename reserved slots.
    """
    return _produce_bytes(
        trial_dir, logical_name, content, "application/octet-stream",
        command=f"aeval: {logical_name} adapter artifact",
    )


def produce_observable(
    trial_dir: Path, name: str, value: Any
) -> tuple[CollectOutcome, ArtifactRef]:
    """One probed observable, at ``observables/<name>.json``."""
    logical = f"observable:{name}"
    content = json.dumps(
        {"name": name, "value": value}, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return _produce_bytes(
        trial_dir, logical, content, "application/json",
        command=f"aeval: observable {name} probe",
    )


def write_collection_manifest(
    trial_dir: Path,
    *,
    trial_id: str,
    outcomes: Sequence[CollectOutcome],
    artifacts: Sequence[ArtifactRef],
    runtime_lock: RuntimeLock,
) -> Path:
    """Write ``collection_manifest.json`` bound to the runtime lock.

    The manifest is written AFTER all outputs exist and is not part of
    the artifact list: its identity is bound by the outer bundle
    attestation, never by hashing itself.
    """
    manifest = CollectionManifest(
        trial_id=trial_id,
        outcomes=list(outcomes),
        artifacts=list(artifacts),
        runtime_lock_digest=runtime_lock.digest(),
    )
    path = Path(trial_dir) / "collection_manifest.json"
    atomic_write_bytes(path, manifest.model_dump_json(indent=2).encode("utf-8"))
    return path
