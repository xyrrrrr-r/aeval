"""P0-6 collector producer tests.

The producers are the trustworthy source behind the hard gate: every
output lands at its FIXED path, atomically, with its execution status
recorded — and a collector-built evidence set passes the very gate
that tampered hand-made sets fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aeval.hooks.collectors import (
    atomic_write_bytes,
    produce_canonical_transcript,
    produce_mock_call_log,
    produce_observable,
    produce_runtime_dump,
    produce_session_record,
    record_outcome,
    write_collection_manifest,
)
from aeval.hooks.evidence import (
    FIXED_OUTPUT_PATHS,
    EvidenceIntegrityError,
    build_required_collect_plan,
    output_path_for,
    verify_evidence_bundle,
)
from aeval.contracts import BundleDescriptor, RuntimeLock


def _descriptor(trial_dir: Path, runtime_lock: RuntimeLock) -> None:
    (trial_dir / "bundle_descriptor.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run": {
                    "run_id": "run-test",
                    "job_config_hash": "a" * 64,
                    "config_file_sha256": "b" * 64,
                    "runtime_lock_digest": runtime_lock.digest(),
                },
                "trial_id": "trial-1",
                "session_id": "s-1",
                "session_root": "sessions",
                "stop_reason": "agent_exit_0",
                "config_digest": "d" * 64,
            }
        ),
        encoding="utf-8",
    )
    # the descriptor's session root must hold the official record whose
    # bytes the collected dsh_session artifact copies (content ownership)
    official = trial_dir / "sessions" / "s-1" / "session.v4.jsonl.zstd"
    official.parent.mkdir(parents=True, exist_ok=True)
    official.write_bytes(produce_session_record(trial_dir, b"binary session bytes")[1]
                         and (trial_dir / "sessions" / "session.v4.jsonl.zstd").read_bytes())


class _FakeTranscript:
    """Minimal pydantic-like stand-in for CanonicalTranscript."""

    def model_dump_json(self, indent=None):
        return json.dumps({"events": [], "stop_reason": "agent_exit_0"}, indent=indent)


def _collect_everything(trial_dir: Path, runtime_lock: RuntimeLock, suite):
    outcomes, artifacts = [], []
    for producer, payload in (
        (produce_runtime_dump, {"env": "e2b", "arch": "arm64"}),
        (produce_mock_call_log, [{"call": "model", "tokens": 12}]),
        (produce_session_record, b"binary session bytes"),
        (produce_canonical_transcript, _FakeTranscript()),
    ):
        outcome, ref = producer(trial_dir, payload)
        outcomes.append(outcome)
        artifacts.append(ref)
    for obs in suite.overlay.observables:
        outcome, ref = produce_observable(trial_dir, obs.name, "probed")
        outcomes.append(outcome)
        artifacts.append(ref)
    write_collection_manifest(
        trial_dir,
        trial_id="trial-1",
        outcomes=outcomes,
        artifacts=artifacts,
        runtime_lock=runtime_lock,
    )
    _descriptor(trial_dir, runtime_lock)


def test_producers_write_at_fixed_paths(tmp_path, runtime_lock, demo_suite):
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    _collect_everything(trial_dir, runtime_lock, demo_suite)
    for name in FIXED_OUTPUT_PATHS:
        assert (trial_dir / output_path_for(name)).is_file(), name
    for obs in demo_suite.overlay.observables:
        assert (trial_dir / f"observables/{obs.name}.json").is_file()
    # the manifest is never an artifact of itself
    manifest = json.loads(
        (trial_dir / "collection_manifest.json").read_text(encoding="utf-8")
    )
    assert all(a["path"] != "collection_manifest.json" for a in manifest["artifacts"])
    assert manifest["runtime_lock_digest"] == runtime_lock.digest()


def test_collector_built_evidence_passes_the_hard_gate(tmp_path, runtime_lock, demo_suite):
    """End-to-end: producers → manifest → descriptor → verify passes."""
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    _collect_everything(trial_dir, runtime_lock, demo_suite)
    plan = build_required_collect_plan(demo_suite)
    bundle = verify_evidence_bundle(trial_dir, runtime_lock, plan)
    assert bundle.trial_id == "trial-1"
    assert bundle.stop_reason == "agent_exit_0"
    assert bundle.bundle_descriptor is not None
    assert set(bundle.artifacts) == {output_path_for(n) for n in plan}


def test_outcomes_record_execution_facts(tmp_path, runtime_lock, demo_suite):
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    outcome, ref = produce_runtime_dump(trial_dir, {"a": 1})
    assert outcome.exit_code == 0
    assert outcome.exception is None
    assert outcome.atomic is True
    assert outcome.started_at <= outcome.finished_at
    assert outcome.sha256 == ref.sha256
    assert outcome.output_path == FIXED_OUTPUT_PATHS["runtime_dump"]


def test_record_outcome_marks_never_executed():
    """exit_code=None with no exception means the step never ran —
    the gate treats that as missing evidence, not success."""
    from datetime import datetime, timezone

    never = record_outcome(
        name="y", command="c",
        started_at=datetime.now(timezone.utc),
        exit_code=None,
    )
    assert never.exit_code is None and never.exception is None


def test_atomic_write_leaves_no_temp_files(tmp_path):
    path = tmp_path / "nested" / "out.bin"
    digest, size = atomic_write_bytes(path, b"payload")
    assert size == 7 and len(digest) == 64
    assert path.read_bytes() == b"payload"
    assert list(tmp_path.rglob("*.tmp")) == []
    # rewrite is atomic too
    atomic_write_bytes(path, b"second")
    assert path.read_bytes() == b"second"
    assert list(tmp_path.rglob("*.tmp")) == []


def test_collector_evidence_with_wrong_lock_fails_gate(tmp_path, runtime_lock, demo_suite):
    """The manifest binds to the lock it ran under — a different run's
    lock must refuse the evidence."""
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    _collect_everything(trial_dir, runtime_lock, demo_suite)
    other_lock: RuntimeLock = runtime_lock.model_copy(
        update={"harbor_lock_ref": "another-run"}
    )
    plan = build_required_collect_plan(demo_suite)
    with pytest.raises(EvidenceIntegrityError, match="differs from the run's lock"):
        verify_evidence_bundle(trial_dir, other_lock, plan)


def test_tampered_collect_output_fails_gate(tmp_path, runtime_lock, demo_suite):
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    _collect_everything(trial_dir, runtime_lock, demo_suite)
    session = trial_dir / FIXED_OUTPUT_PATHS["dsh_session"]
    session.write_bytes(b"rewritten session")
    plan = build_required_collect_plan(demo_suite)
    with pytest.raises(EvidenceIntegrityError, match="hash mismatch"):
        verify_evidence_bundle(trial_dir, runtime_lock, plan)


def test_failed_producer_outcome_blocks_gate(tmp_path, runtime_lock, demo_suite):
    """A producer that raised is recorded honestly and blocks the gate."""
    from aeval.hooks.evidence import load_collection_manifest

    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    _collect_everything(trial_dir, runtime_lock, demo_suite)
    manifest = load_collection_manifest(trial_dir)
    manifest.outcomes[0].exception = "OSError: disk full"
    manifest.outcomes[0].exit_code = None
    (trial_dir / "collection_manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    plan = build_required_collect_plan(demo_suite)
    with pytest.raises(EvidenceIntegrityError, match="raised: OSError"):
        verify_evidence_bundle(trial_dir, runtime_lock, plan)
