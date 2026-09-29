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


def test_the_session_record_lands_in_the_declared_flavor_slot(tmp_path):
    """A non-DSH adapter's record must go to ITS slot.

    example-lab: the host-side read of the ACP record succeeded, but the producer
    wrote the historical ``dsh_session`` slot, so the trial was refused for
    "collect outcomes missing for required outputs: ['agent_session_record']".
    """
    trial_dir = tmp_path / "declared"
    outcome, ref = produce_session_record(
        trial_dir, b"acp summary bytes", flavor="agent_session_record"
    )
    assert outcome.name == "agent_session_record"
    assert outcome.output_path == output_path_for("agent_session_record")
    assert outcome.output_path == "agent_session/record"
    assert (trial_dir / "agent_session" / "record").read_bytes() == b"acp summary bytes"
    assert ref.path == "agent_session/record"
    # and the historical DSH slot was NOT written
    assert not (trial_dir / FIXED_OUTPUT_PATHS["dsh_session"]).exists()


def test_the_dsh_default_keeps_the_historical_plan_byte_identical(tmp_path):
    """Sealed DSH runs must not move: same name, same path, same command."""
    trial_dir = tmp_path / "dsh"
    outcome, _ = produce_session_record(trial_dir, b"dsh bytes")
    assert outcome.name == "dsh_session"
    assert outcome.output_path == FIXED_OUTPUT_PATHS["dsh_session"]
    assert outcome.command == "aeval: dsh_session download"


def test_an_unknown_session_record_flavor_is_refused(tmp_path):
    from aeval.hooks.collectors import CollectionProducerError

    with pytest.raises(CollectionProducerError, match="unknown session-record flavor"):
        produce_session_record(tmp_path, b"x", flavor="gpt_session")


class _CannedExec:
    """Harbor-shaped results for the runtime-dump probes."""

    async def exec(self, command: str):
        from types import SimpleNamespace

        for key, value in (
            ("uname -m", "aarch64"),
            ("uname -sr", "Linux 6.6.0"),
            ("node --version", "v24.20.0"),
        ):
            if key in command:
                return SimpleNamespace(return_code=0, stdout=value, stderr="")
        if command.startswith("cat /workspace/"):
            name = command.rsplit("/", 1)[-1]
            return SimpleNamespace(return_code=0, stdout=f"observed:{name}", stderr="")
        return SimpleNamespace(return_code=127, stdout="", stderr="not found")


class _FlavoredEnv:
    async def exec(self, command: str):
        return await _CannedExec().exec(command)


class _FlavoredAgent:
    """A non-DSH adapter: its own slot, its own record reader."""

    SESSION_RECORD_OUTPUT = "agent_session_record"

    def read_session_record(self) -> bytes:
        return b"acp summary bytes"

    def read_trial_session(self):
        return _FakeTranscript()


class _FlavoredDriver:
    session_record = "agent_session_record"
    workspace_dir = "/workspace"


async def test_a_declared_flavor_is_what_the_collect_plan_collects(
    tmp_path, runtime_lock
):
    """The collect plan follows the suite's declared flavor end to end.

    The producer honors the flavor and ``collect_trial_evidence`` passes the
    suite's declaration into it. example-lab found the two halves disagreeing only on
    a real trial: the record was read host-side, landed in the DSH slot, and the
    trial was refused for a missing required output.
    """
    from types import SimpleNamespace

    from aeval.hooks.collection import collect_trial_evidence

    trial_dir = tmp_path / "declared"
    suite = SimpleNamespace(
        overlay=SimpleNamespace(observables=[], driver=_FlavoredDriver())
    )
    manifest = await collect_trial_evidence(
        trial_dir=trial_dir,
        trial_id="t1",
        suite=suite,
        environment=_FlavoredEnv(),
        agent=_FlavoredAgent(),
        runtime_lock=runtime_lock,
        session_id="sess-1",
    )
    names = {o.name for o in manifest.outcomes}
    assert "agent_session_record" in names
    assert "dsh_session" not in names
    assert (trial_dir / output_path_for("agent_session_record")).is_file()
    assert not (trial_dir / FIXED_OUTPUT_PATHS["dsh_session"]).exists()


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
    plan = build_required_collect_plan(demo_suite)
    for name in plan:
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
