"""Evidence hard-gate tests: incomplete evidence must block the verifier.

Every negative case asserts the SIDE EFFECT (verifier spy zero-call),
not just the raised error.

Additions: fixed logical-name → path mapping, empty outcomes,
never-executed outcomes, missing observables, manifest/lock binding,
missing descriptor is fatal, session ownership, resolved containment,
and no directory guessing without the owner-recorded trial dir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aeval.contracts import ArtifactRef, CollectOutcome, CollectionManifest
from aeval.hooks.context import EvaluationContext
from aeval.hooks.evidence import (
    CONDITIONAL_OUTPUTS,
    FIXED_OUTPUT_PATHS,
    EvidenceIntegrityError,
    build_required_collect_plan,
    evaluate_requirements,
    gate_verification,
    load_collection_manifest,
    output_path_for,
    validate_collect_declarations,
    verify_artifact_hashes,
    verify_evidence_bundle,
    SESSION_RECORD_OUTPUTS,
)
from aeval.suite_models import ResolvedSuite
from tests.conftest import build_complete_trial_dir


class VerifierSpy:
    """Stands in for Harbor's verifier: counts would-be invocations."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1


def _ctx(tmp_path, demo_suite, runtime_lock, *, with_trial_dir=None) -> EvaluationContext:
    ctx = EvaluationContext(
        run_id="run-test",
        runtime_lock=runtime_lock,
        suite=demo_suite,
        run_dir=tmp_path / "run",
        store_path=tmp_path / "store.sqlite3",
    )
    if with_trial_dir is not None:
        ctx.trial_state("trial-1").trial_dir = with_trial_dir
    return ctx


class _Event:
    def __init__(self, trial_id: str, trial_dir: Path):
        from datetime import datetime, timezone
        from uuid import uuid4

        self.event = None
        self.task_name = "task"
        self.timestamp = datetime.now(timezone.utc)

        class _Result:
            # deliberately present: the gate must NOT use it
            # (it forbids directory guessing from hook-event fields)
            directory = str(trial_dir)

        class _Config:
            trial_name = "trial"

        self.result = _Result()
        self.config = _Config()
        self._trial_id = trial_id
        self.id = uuid4()

    @property
    def trial_id(self):
        return self._trial_id


async def _run_gate(ctx, event, spy) -> None:
    """Invoke the gate; on success the verifier spy runs.

    Negative tests wrap this in pytest.raises(EvidenceIntegrityError)
    and then assert spy.calls == 0 — the gate must raise BEFORE the
    verifier could ever be invoked.
    """
    await gate_verification(event, ctx)
    await spy()  # gate passed: verifier runs


def _full_plan(suite) -> list[str]:
    return build_required_collect_plan(suite)


async def test_gate_passes_on_complete_evidence(tmp_path, demo_suite, runtime_lock):
    trial_dir = tmp_path / "trial"
    build_complete_trial_dir(
        trial_dir, plan=_full_plan(demo_suite), runtime_lock=runtime_lock,
    )
    ctx = _ctx(tmp_path, demo_suite, runtime_lock, with_trial_dir=trial_dir)
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    await _run_gate(ctx, event, spy)
    assert spy.calls == 1
    assert ctx.trials["trial-1"].evidence_ok is True
    assert ctx.artifacts["trial-1"].trial_id == "trial-1"


async def test_gate_blocks_on_missing_manifest(tmp_path, demo_suite, runtime_lock):
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    ctx = _ctx(tmp_path, demo_suite, runtime_lock, with_trial_dir=trial_dir)
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    with pytest.raises(EvidenceIntegrityError, match="manifest missing"):
        await _run_gate(ctx, event, spy)
    assert spy.calls == 0, "verifier must not run on missing manifest"
    assert ctx.trials["trial-1"].stop_reason == "infra_error"


async def test_gate_blocks_on_tampered_hash(tmp_path, demo_suite, runtime_lock):
    trial_dir = tmp_path / "trial"
    build_complete_trial_dir(
        trial_dir, plan=_full_plan(demo_suite), runtime_lock=runtime_lock,
        tamper="runtime_dump",
    )
    ctx = _ctx(tmp_path, demo_suite, runtime_lock, with_trial_dir=trial_dir)
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    with pytest.raises(EvidenceIntegrityError, match="hash mismatch|size mismatch"):
        await _run_gate(ctx, event, spy)
    assert spy.calls == 0


async def test_gate_blocks_on_missing_required_output(tmp_path, demo_suite, runtime_lock):
    trial_dir = tmp_path / "trial"
    build_complete_trial_dir(
        trial_dir, plan=_full_plan(demo_suite), runtime_lock=runtime_lock,
        omit="dsh_session",
    )
    ctx = _ctx(tmp_path, demo_suite, runtime_lock, with_trial_dir=trial_dir)
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    with pytest.raises(EvidenceIntegrityError, match="missing at fixed path|outcomes missing"):
        await _run_gate(ctx, event, spy)
    assert spy.calls == 0


async def test_gate_blocks_on_collect_failure(tmp_path, demo_suite, runtime_lock):
    trial_dir, manifest = build_complete_trial_dir(
        trial_dir := tmp_path / "trial", plan=_full_plan(demo_suite),
        runtime_lock=runtime_lock,
    )
    manifest.outcomes[0].exit_code = 3
    (trial_dir / "collection_manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, demo_suite, runtime_lock, with_trial_dir=trial_dir)
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    with pytest.raises(EvidenceIntegrityError, match="exited 3"):
        await _run_gate(ctx, event, spy)
    assert spy.calls == 0


async def test_gate_blocks_on_never_executed_outcome(tmp_path, demo_suite, runtime_lock):
    """An outcome with no exit code and no exception never ran."""
    trial_dir, manifest = build_complete_trial_dir(
        trial_dir := tmp_path / "trial", plan=_full_plan(demo_suite),
        runtime_lock=runtime_lock,
    )
    manifest.outcomes[0].exit_code = None
    (trial_dir / "collection_manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, demo_suite, runtime_lock, with_trial_dir=trial_dir)
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    with pytest.raises(EvidenceIntegrityError, match="never executed"):
        await _run_gate(ctx, event, spy)
    assert spy.calls == 0


def test_bundle_rejects_empty_outcomes(tmp_path, demo_suite, runtime_lock):
    trial_dir, manifest = build_complete_trial_dir(
        tmp_path / "trial", plan=_full_plan(demo_suite), runtime_lock=runtime_lock,
    )
    manifest.outcomes = []
    (trial_dir / "collection_manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    with pytest.raises(EvidenceIntegrityError, match="no outcomes at all"):
        verify_evidence_bundle(trial_dir, runtime_lock, _full_plan(demo_suite))


def test_bundle_rejects_missing_observable(tmp_path, demo_suite, runtime_lock):
    """The demo suite's observable must be collected like any output."""
    plan = _full_plan(demo_suite)
    assert "observable:order_status" in plan
    trial_dir, _ = build_complete_trial_dir(
        tmp_path / "trial", plan=plan, runtime_lock=runtime_lock,
        omit="observable:order_status",
    )
    with pytest.raises(EvidenceIntegrityError, match="order_status"):
        verify_evidence_bundle(trial_dir, runtime_lock, plan)


def test_bundle_rejects_manifest_lock_mismatch(tmp_path, demo_suite, runtime_lock):
    trial_dir, manifest = build_complete_trial_dir(
        tmp_path / "trial", plan=_full_plan(demo_suite), runtime_lock=runtime_lock,
    )
    manifest.runtime_lock_digest = "f" * 64
    (trial_dir / "collection_manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    with pytest.raises(EvidenceIntegrityError, match="differs from the run's lock"):
        verify_evidence_bundle(trial_dir, runtime_lock, _full_plan(demo_suite))


def test_bundle_rejects_unbound_manifest(tmp_path, demo_suite, runtime_lock):
    """A manifest with an empty lock digest came from outside the pipeline."""
    trial_dir, manifest = build_complete_trial_dir(
        tmp_path / "trial", plan=_full_plan(demo_suite), runtime_lock=runtime_lock,
    )
    manifest.runtime_lock_digest = ""
    (trial_dir / "collection_manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    with pytest.raises(EvidenceIntegrityError, match="not bound.*runtime lock"):
        verify_evidence_bundle(trial_dir, runtime_lock, _full_plan(demo_suite))


def test_bundle_rejects_missing_descriptor(tmp_path, demo_suite, runtime_lock):
    """A missing bundle descriptor is fatal, not a recorded issue."""
    trial_dir, _ = build_complete_trial_dir(
        tmp_path / "trial", plan=_full_plan(demo_suite), runtime_lock=runtime_lock,
        descriptor=False,
    )
    with pytest.raises(EvidenceIntegrityError, match="bundle descriptor missing"):
        verify_evidence_bundle(trial_dir, runtime_lock, _full_plan(demo_suite))


def test_bundle_rejects_a_session_root_without_the_official_record(
    tmp_path, demo_suite, runtime_lock
):
    """Session ownership is by content: the descriptor's session_root
    must hold the official record for its own session id."""
    trial_dir, _ = build_complete_trial_dir(
        tmp_path / "trial", plan=_full_plan(demo_suite), runtime_lock=runtime_lock,
    )
    # the descriptor's session root exists but holds no record for this session
    (trial_dir / "dsh-home" / "s-1" / "session.v4.jsonl.zstd").unlink()
    with pytest.raises(EvidenceIntegrityError, match="holds no official record"):
        verify_evidence_bundle(trial_dir, runtime_lock, _full_plan(demo_suite))


def test_bundle_rejects_a_session_artifact_that_is_not_the_record(
    tmp_path, demo_suite, runtime_lock
):
    """A dsh_session artifact whose bytes differ from the official record
    must be refused (content ownership, not location)."""
    trial_dir, _ = build_complete_trial_dir(
        tmp_path / "trial", plan=_full_plan(demo_suite), runtime_lock=runtime_lock,
    )
    (trial_dir / "dsh-home" / "s-1" / "session.v4.jsonl.zstd").write_bytes(b"someone else")
    with pytest.raises(EvidenceIntegrityError, match="not this trial's official session"):
        verify_evidence_bundle(trial_dir, runtime_lock, _full_plan(demo_suite))


def test_bundle_rejects_artifact_outside_fixed_mapping(tmp_path, demo_suite, runtime_lock):
    trial_dir, manifest = build_complete_trial_dir(
        tmp_path / "trial", plan=_full_plan(demo_suite), runtime_lock=runtime_lock,
    )
    import hashlib as _h

    smuggled = b"renamed evidence"
    (trial_dir / "freeform.bin").write_bytes(smuggled)
    manifest.artifacts.append(ArtifactRef(
        media_type="application/octet-stream",
        sha256=_h.sha256(smuggled).hexdigest(),
        size_bytes=len(smuggled),
        path="freeform.bin",
    ))
    (trial_dir / "collection_manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    with pytest.raises(EvidenceIntegrityError, match="outside the fixed mapping"):
        verify_evidence_bundle(trial_dir, runtime_lock, _full_plan(demo_suite))


def test_bundle_rejects_symlink_escape(tmp_path, demo_suite, runtime_lock):
    """A benign-looking relative path that resolves outside the trial dir."""
    trial_dir, manifest = build_complete_trial_dir(
        tmp_path / "trial", plan=_full_plan(demo_suite), runtime_lock=runtime_lock,
    )
    import hashlib as _h
    import os

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"stolen")
    link = trial_dir / "sessions" / "linked.zstd"
    link.symlink_to(outside)
    content = outside.read_bytes()
    manifest.artifacts.append(ArtifactRef(
        media_type="application/octet-stream",
        sha256=_h.sha256(content).hexdigest(),
        size_bytes=len(content),
        path="sessions/linked.zstd",
    ))
    (trial_dir / "collection_manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    # the extra artifact is outside the mapping AND escapes via symlink
    with pytest.raises(EvidenceIntegrityError, match="escapes the bundle|outside the fixed mapping"):
        verify_evidence_bundle(trial_dir, runtime_lock, _full_plan(demo_suite))


async def test_gate_blocks_without_trusted_trial_dir(tmp_path, demo_suite, runtime_lock):
    """No owner-recorded trial dir → fail closed, no directory guessing.

    The event carries result.directory, but the gate forbids using it.
    """
    trial_dir = tmp_path / "trial"
    build_complete_trial_dir(
        trial_dir, plan=_full_plan(demo_suite), runtime_lock=runtime_lock,
    )
    ctx = _ctx(tmp_path, demo_suite, runtime_lock)  # no trial_dir recorded
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    with pytest.raises(EvidenceIntegrityError, match="no trusted trial directory"):
        await _run_gate(ctx, event, spy)
    assert spy.calls == 0
    assert ctx.trials["trial-1"].infra_invalid_reasons


async def test_gate_blocks_on_baseline_failure_state(tmp_path, demo_suite, runtime_lock):
    trial_dir = tmp_path / "trial"
    build_complete_trial_dir(
        trial_dir, plan=_full_plan(demo_suite), runtime_lock=runtime_lock,
    )
    ctx = _ctx(tmp_path, demo_suite, runtime_lock, with_trial_dir=trial_dir)
    state = ctx.trial_state("trial-1")
    state.mark_infra_invalid("baseline_arrival: seed mismatch")
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    with pytest.raises(EvidenceIntegrityError, match="baseline"):
        await _run_gate(ctx, event, spy)
    assert spy.calls == 0


async def test_gate_blocks_on_artifact_path_escape(tmp_path, demo_suite, runtime_lock):
    trial_dir, manifest = build_complete_trial_dir(
        tmp_path / "trial", plan=_full_plan(demo_suite), runtime_lock=runtime_lock,
    )
    import hashlib

    import pydantic

    # A path-escaping artifact is unrepresentable at the model level —
    # the strongest form of "the escape never reaches the verifier".
    with pytest.raises(pydantic.ValidationError, match="must not escape"):
        manifest.artifacts.append(
            ArtifactRef(
                media_type="application/octet-stream",
                sha256=hashlib.sha256(b"stolen").hexdigest(),
                size_bytes=6,
                path="../outside.bin",
            )
        )
    # And the clean bundle still gates fine.
    (trial_dir / "collection_manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, demo_suite, runtime_lock, with_trial_dir=trial_dir)
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    await _run_gate(ctx, event, spy)
    assert spy.calls == 1


def test_collect_plan_lists_atomic_outputs(demo_suite):
    plan = build_required_collect_plan(demo_suite)
    # Every FIXED output except the session-record slot's other flavor; the
    # slot itself takes the suite's declared flavor (demo: dsh_session).
    # Conditionally-gated outputs (the sealed anchors channel) appear only
    # when a suite declares them — the demo suite does not.
    for name in FIXED_OUTPUT_PATHS:
        if name in SESSION_RECORD_OUTPUTS and name != "dsh_session":
            continue
        if name in CONDITIONAL_OUTPUTS:
            assert name not in plan
            continue
        assert name in plan
    # the manifest itself is NOT a collect output: its identity
    # is bound by the outer bundle attestation, never self-hashed
    assert "collection_manifest" not in plan
    assert "canonical_transcript" in plan
    assert any(p.startswith("observable:") for p in plan)


def test_anchors_channel_is_opt_in_and_gates_the_plan(demo_suite):
    """The sealed rubric-anchors channel (integration): a suite that
    does not declare it keeps a byte-identical plan; a suite that does
    gets ``task_anchors`` in its plan — and its tasks' collect commands
    must then name it like any other required output."""
    class Cmd:
        def __init__(self, command):
            self.command = command

    base_plan = build_required_collect_plan(demo_suite)
    assert "task_anchors" not in base_plan

    declaring = demo_suite.model_copy(deep=True)
    declaring.overlay.verdict.anchors = "task_anchors"
    plan = build_required_collect_plan(declaring)
    assert "task_anchors" in plan
    assert output_path_for("task_anchors") == "rubric/task_anchors.json"

    # declaration validation now demands the anchors output by name
    validate_collect_declarations(
        [Cmd("snapshot runtime_dump mock_call_log dsh_session "
             "canonical_transcript task_anchors")],
        ["runtime_dump", "mock_call_log", "dsh_session",
         "canonical_transcript", "task_anchors"],
    )
    with pytest.raises(EvidenceIntegrityError, match="task_anchors"):
        validate_collect_declarations(
            [Cmd("snapshot runtime_dump mock_call_log dsh_session "
                 "canonical_transcript")],
            plan,
        )


def test_output_path_mapping_is_fixed():
    assert output_path_for("dsh_session") == "sessions/session.v4.jsonl.zstd"
    assert output_path_for("observable:x") == "observables/x.json"
    assert output_path_for("task_anchors") == "rubric/task_anchors.json"
    with pytest.raises(EvidenceIntegrityError, match="unknown collect output"):
        output_path_for("freeform")


def test_collect_declarations_require_every_output():
    class Cmd:
        def __init__(self, command):
            self.command = command

    dsh_flavor_plan = [
        n for n in FIXED_OUTPUT_PATHS
        if n not in SESSION_RECORD_OUTPUTS or n == "dsh_session"
        # the anchors output joins a plan only on declaration
        if n not in CONDITIONAL_OUTPUTS
    ]

    with pytest.raises(EvidenceIntegrityError, match="not produced"):
        validate_collect_declarations(
            [Cmd("snapshot runtime_dump")],
            ["runtime_dump", "mock_call_log"],
        )
    # the session-record slot is flavor-exclusive: only the plan's flavor is
    # required of the declared commands, the other slot name never is
    validate_collect_declarations(
        [Cmd("snapshot runtime_dump mock_call_log dsh_session canonical_transcript")],
        ["runtime_dump", "mock_call_log", "dsh_session", "canonical_transcript"],
    )
    validate_collect_declarations(
        [
            Cmd("snapshot runtime_dump && snapshot mock_call_log"),
            Cmd("cp sessions/session.v4.jsonl.zstd dsh_session"),
            Cmd("build canonical_transcript"),
        ],
        dsh_flavor_plan,
    )
    with pytest.raises(EvidenceIntegrityError, match="no \\[\\[verifier.collect\\]\\]"):
        validate_collect_declarations([], dsh_flavor_plan)


def test_bundle_descriptor_rejected_on_escape(tmp_path, runtime_lock, demo_suite):
    trial_dir, _ = build_complete_trial_dir(
        tmp_path / "trial", plan=_full_plan(demo_suite), runtime_lock=runtime_lock,
    )
    (trial_dir / "bundle_descriptor.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run": {
                    "run_id": "run-test",
                    "job_config_hash": "a" * 64,
                    "config_file_sha256": "b" * 64,
                    "runtime_lock_digest": "c" * 64,
                },
                "trial_id": "trial-1",
                "session_id": "s",
                "session_root": "../../escape",
                "stop_reason": "agent_exit_0",
                "config_digest": "d" * 64,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceIntegrityError, match="bundle descriptor"):
        verify_evidence_bundle(
            trial_dir, runtime_lock, _full_plan(demo_suite)
        )


def test_bundle_descriptor_is_found_where_harbor_downloads_it(tmp_path):
    """Harbor downloads the sandbox agent logs tree into <trial>/agent/,
    so the descriptor lands there — gate it at that path (the trial root
    is still accepted for a deployment that copies it)."""
    from aeval.hooks.evidence import find_bundle_descriptor

    trial = tmp_path / "trial"
    (trial / "agent").mkdir(parents=True)
    assert find_bundle_descriptor(trial) is None
    (trial / "agent" / "bundle_descriptor.json").write_text("{}")
    assert find_bundle_descriptor(trial) == trial / "agent" / "bundle_descriptor.json"
    (trial / "bundle_descriptor.json").write_text("{}")
    # the downloaded location wins when both exist
    assert find_bundle_descriptor(trial) == trial / "agent" / "bundle_descriptor.json"


async def test_gate_fails_closed_when_no_adapter_is_recorded(tmp_path, demo_suite):
    """G6: without a live handle, the record owner comes from the runtime
    lock — never from a hardcoded first agent. A lock recording no agent is a
    refusal, not a silent default."""
    from aeval.provenance import build_runtime_lock

    lock = build_runtime_lock()
    trial_dir = tmp_path / "trial"
    build_complete_trial_dir(
        trial_dir, plan=_full_plan(demo_suite), runtime_lock=lock,
    )
    ctx = _ctx(tmp_path, demo_suite, lock, with_trial_dir=trial_dir)
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    with pytest.raises(EvidenceIntegrityError, match="cannot be determined"):
        await _run_gate(ctx, event, spy)
    assert spy.calls == 0, "the gate must refuse before any verifier runs"


async def test_gate_fails_closed_on_an_ambiguous_lock(tmp_path, demo_suite):
    """Two recorded adapters and no live handle: which session-record layout
    applies is genuinely unknown, so the gate refuses instead of guessing."""
    from aeval.contracts import AgentReleaseLock
    from aeval.provenance import build_runtime_lock

    lock = build_runtime_lock(
        agents={
            "dsh": AgentReleaseLock(id="dsh", version="1"),
            "deepagent": AgentReleaseLock(id="deepagent", version="1"),
        },
    )
    trial_dir = tmp_path / "trial"
    build_complete_trial_dir(
        trial_dir, plan=_full_plan(demo_suite), runtime_lock=lock,
    )
    ctx = _ctx(tmp_path, demo_suite, lock, with_trial_dir=trial_dir)
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    with pytest.raises(EvidenceIntegrityError, match="ambiguous"):
        await _run_gate(ctx, event, spy)
    assert spy.calls == 0


async def test_gate_resolves_the_record_owner_from_the_lock(tmp_path, demo_suite):
    """The offline/replay fallback: a lock recording exactly one agent
    resolves its declaration to the adapter class — a dsh run's lock (the
    adapter hook contributed its pin) finds the DSH-shaped session record
    with no live handle."""
    from aeval.agents.dsh.agent import DshAgent
    from aeval.hooks.evidence import _record_owner

    from aeval.agents.dsh.release import build_official_dsh_lock
    from aeval.provenance import build_runtime_lock

    dsh_run = build_runtime_lock(release_locks={"dsh": build_official_dsh_lock()})
    assert _record_owner(None, dsh_run) is DshAgent
    # a live handle always wins over the lock
    sentinel = object()
    assert _record_owner(sentinel, dsh_run) is sentinel


async def test_a_declared_slot_passes_the_gate_end_to_end(tmp_path):
    """Acceptance: an adapter's OWN slot name and path — plan,
    collection, gate — without the framework's table knowing either.

    The third slot kind beyond dsh_session/agent_session_record: a declared
    slug plus a declared path, mirrored in the adapter's declaration."""
    from types import SimpleNamespace

    from aeval.provenance import build_runtime_lock
    from aeval.hooks.evidence import build_required_collect_plan

    class GptAgent:
        SESSION_RECORD_OUTPUT = "gpt_session"
        SESSION_RECORD_OUTPUT_PATH = "gpt/summary.v2.json"

        @staticmethod
        def locate_session_record(record_root, session_id):
            candidate = Path(record_root) / session_id / "summary.v2.json"
            return candidate if candidate.is_file() else None

    # the suite declares the same slot; the plan carries it instead of the
    # built-ins
    suite = SimpleNamespace(overlay=SimpleNamespace(
        driver=SimpleNamespace(session_record="gpt_session"), observables=[],
    ))
    plan = build_required_collect_plan(suite)
    assert "gpt_session" in plan
    assert "dsh_session" not in plan and "agent_session_record" not in plan
    assert "runtime_dump" in plan and "canonical_transcript" in plan

    lock = build_runtime_lock()
    trial_dir = tmp_path / "trial"
    build_complete_trial_dir(
        trial_dir, plan=plan, runtime_lock=lock, session_root="sessions",
        output_paths={"gpt_session": "gpt/summary.v2.json"},
    )
    # the official record the descriptor's session_root holds (same bytes as
    # the collected artifact — ownership is by content)
    record = trial_dir / "sessions" / "s-1" / "summary.v2.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_bytes((trial_dir / "gpt" / "summary.v2.json").read_bytes())

    bundle = verify_evidence_bundle(trial_dir, lock, plan, adapter=GptAgent)
    assert bundle.trial_id == "trial-1"
    assert "gpt/summary.v2.json" in bundle.artifacts


async def test_the_gate_refuses_a_declared_slot_without_a_path(tmp_path):
    """A slot nobody can locate is fail-closed, not passed with a guess."""
    from aeval.provenance import build_runtime_lock

    class Pathless:
        SESSION_RECORD_OUTPUT = "gpt_session"
        # no SESSION_RECORD_OUTPUT_PATH, and no built-in path exists

        @staticmethod
        def locate_session_record(record_root, session_id):
            return None

    lock = build_runtime_lock()
    trial_dir = tmp_path / "trial"
    plan = ["runtime_dump", "mock_call_log", "gpt_session", "canonical_transcript"]
    build_complete_trial_dir(
        trial_dir, plan=plan, runtime_lock=lock, session_root="sessions",
        output_paths={"gpt_session": "gpt/summary.v2.json"},
    )
    with pytest.raises(EvidenceIntegrityError, match="no fixed path"):
        verify_evidence_bundle(trial_dir, lock, plan, adapter=Pathless)
