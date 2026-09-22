"""Evidence hard-gate tests: incomplete evidence must block the verifier.

Every negative case asserts the SIDE EFFECT (verifier spy zero-call),
not just the raised error, per the plan's verification matrix.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aeval.contracts import ArtifactRef, CollectOutcome, CollectionManifest
from aeval.hooks.context import EvaluationContext
from aeval.hooks.evidence import (
    EvidenceIntegrityError,
    build_required_collect_plan,
    evaluate_requirements,
    gate_verification,
    load_collection_manifest,
    validate_collect_declarations,
    verify_artifact_hashes,
    verify_evidence_bundle,
)
from aeval.suite_models import ResolvedSuite
from tests.conftest import build_complete_trial_dir, write_artifact


class VerifierSpy:
    """Stands in for Harbor's verifier: counts would-be invocations."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1


def _ctx(tmp_path, demo_suite, runtime_lock) -> EvaluationContext:
    return EvaluationContext(
        run_id="run-test",
        runtime_lock=runtime_lock,
        suite=demo_suite,
        run_dir=tmp_path / "run",
        store_path=tmp_path / "store.sqlite3",
    )


class _Event:
    def __init__(self, trial_id: str, trial_dir: Path):
        from datetime import datetime, timezone

        self.event = None
        self.task_name = "task"
        self.timestamp = datetime.now(timezone.utc)

        class _Result:
            directory = str(trial_dir)

        class _Config:
            trial_name = "trial"

        self.result = _Result()
        self.config = _Config()

        from uuid import uuid4

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


REQUIRED = ("runtime_dump", "mock_call_log", "dsh_session", "collection_manifest")


async def test_gate_passes_on_complete_evidence(tmp_path, demo_suite, runtime_lock):
    trial_dir, _ = build_complete_trial_dir(tmp_path / "trial", required=REQUIRED)
    ctx = _ctx(tmp_path, demo_suite, runtime_lock)
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    await _run_gate(ctx, event, spy)
    assert spy.calls == 1
    assert ctx.trials["trial-1"].evidence_ok is True
    assert ctx.artifacts["trial-1"].trial_id == "trial-1"


async def test_gate_blocks_on_missing_manifest(tmp_path, demo_suite, runtime_lock):
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    ctx = _ctx(tmp_path, demo_suite, runtime_lock)
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    with pytest.raises(EvidenceIntegrityError, match="manifest missing"):
        await _run_gate(ctx, event, spy)
    assert spy.calls == 0, "verifier must not run on missing manifest"
    assert ctx.trials["trial-1"].stop_reason == "infra_error"


async def test_gate_blocks_on_tampered_hash(tmp_path, demo_suite, runtime_lock):
    trial_dir, _ = build_complete_trial_dir(
        tmp_path / "trial", required=REQUIRED, tamper="runtime_dump"
    )
    ctx = _ctx(tmp_path, demo_suite, runtime_lock)
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    with pytest.raises(EvidenceIntegrityError, match="hash mismatch|size mismatch"):
        await _run_gate(ctx, event, spy)
    assert spy.calls == 0


async def test_gate_blocks_on_missing_required_output(tmp_path, demo_suite, runtime_lock):
    trial_dir, _ = build_complete_trial_dir(
        tmp_path / "trial", required=REQUIRED, omit="dsh_session"
    )
    ctx = _ctx(tmp_path, demo_suite, runtime_lock)
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    with pytest.raises(EvidenceIntegrityError, match="missing"):
        await _run_gate(ctx, event, spy)
    assert spy.calls == 0


async def test_gate_blocks_on_collect_failure(tmp_path, demo_suite, runtime_lock):
    trial_dir, manifest = build_complete_trial_dir(tmp_path / "trial", required=REQUIRED)
    manifest.outcomes[0].exit_code = 3
    (trial_dir / "collection_manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, demo_suite, runtime_lock)
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    with pytest.raises(EvidenceIntegrityError, match="exited 3"):
        await _run_gate(ctx, event, spy)
    assert spy.calls == 0


async def test_gate_blocks_on_baseline_failure_state(tmp_path, demo_suite, runtime_lock):
    trial_dir, _ = build_complete_trial_dir(tmp_path / "trial", required=REQUIRED)
    ctx = _ctx(tmp_path, demo_suite, runtime_lock)
    state = ctx.trial_state("trial-1")
    state.mark_infra_invalid("baseline_arrival: seed mismatch")
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    with pytest.raises(EvidenceIntegrityError, match="baseline"):
        await _run_gate(ctx, event, spy)
    assert spy.calls == 0


async def test_gate_blocks_on_artifact_path_escape(tmp_path, demo_suite, runtime_lock):
    trial_dir, manifest = build_complete_trial_dir(tmp_path / "trial", required=REQUIRED)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"stolen")
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
    ctx = _ctx(tmp_path, demo_suite, runtime_lock)
    event = _Event("trial-1", trial_dir)
    spy = VerifierSpy()
    await _run_gate(ctx, event, spy)
    assert spy.calls == 1


def test_collect_plan_lists_atomic_outputs(demo_suite):
    plan = build_required_collect_plan(demo_suite)
    for name in ("runtime_dump", "mock_call_log", "dsh_session", "collection_manifest"):
        assert name in plan
    assert any(p.startswith("observable:") for p in plan)


def test_collect_declarations_require_every_output():
    class Cmd:
        def __init__(self, command):
            self.command = command

    with pytest.raises(EvidenceIntegrityError, match="not produced"):
        validate_collect_declarations(
            [Cmd("snapshot runtime_dump")],
            ["runtime_dump", "mock_call_log"],
        )
    validate_collect_declarations(
        [
            Cmd("snapshot runtime_dump && snapshot mock_call_log"),
            Cmd("cp session.jsonl dsh_session"),
            Cmd("write collection_manifest"),
        ],
        ["runtime_dump", "mock_call_log", "dsh_session", "collection_manifest"],
    )


def test_bundle_descriptor_rejected_on_escape(tmp_path, runtime_lock, demo_suite):
    trial_dir, _ = build_complete_trial_dir(tmp_path / "trial", required=REQUIRED)
    (trial_dir / "bundle_descriptor.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
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
            trial_dir, runtime_lock, list(REQUIRED)
        )
