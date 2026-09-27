"""P0-7 grading pipeline integration tests.

Real grader files, a real store, real suites: the production path from
sealed evidence to an atomically persisted, classified TrialRecord.
Every failure mode must leave either a clean store or an honestly
classified infra_invalid record — never a lucky pass, never a
half-written trial.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aeval.contracts import (
    EvidenceBundle,
    OverlayIdentity,
    RunManifest,
    TrialCoordinates,
    VersionsBundle,
)
from aeval.store.sqlite import StoreConflictError, TrialStore
from aeval.suite_loader.loader import load_suite
from aeval.verdict.loader import GraderLoadError
from aeval.verdict.pipeline import grade_and_record, load_suite_graders
from aeval.verdict.progress import RequirementProgress

from tests.conftest import FIXTURES, write_artifact

_PASSING_GRADER = '''
GRADER_ID = "outcome"
GRADER_VERSION = "v7"
LAYER = "outcome"
REQUIRED_FIELDS = ["events"]

async def grade(record):
    from aeval.contracts import GradeResult, Score
    return GradeResult(
        grader_id=GRADER_ID,
        grader_version=GRADER_VERSION,
        layer="outcome",
        score=Score(value=1.0),
        status="pass",
        reasons=["fixture grader pass"],
    )
'''

_FAILING_GRADER = '''
GRADER_ID = "outcome"
GRADER_VERSION = "v7"

async def grade(record):
    from aeval.contracts import GradeResult, Score
    return GradeResult(
        grader_id=GRADER_ID,
        grader_version=GRADER_VERSION,
        layer="outcome",
        score=Score(value=0.0),
        status="fail",
        reasons=["fixture grader fail"],
    )
'''

_CRASHING_GRADER = '''
GRADER_ID = "outcome"
GRADER_VERSION = "v7"

async def grade(record):
    raise RuntimeError("grader bug")
'''

_LYING_GRADER = '''
GRADER_ID = "outcome"
GRADER_VERSION = "v7"

async def grade(record):
    from aeval.contracts import GradeResult, Score
    return GradeResult(
        grader_id="someone-else",
        grader_version="v99",
        layer="outcome",
        score=Score(value=1.0),
        status="pass",
        reasons=["borrowed identity"],
    )
'''

_MISSING_FILE_GRADER = "GRADER_ID = 'x'\nGRADER_VERSION = 'v1'\n"  # no grade()


def _suite(tmp_path: Path, grader_body: str, *, veto: bool = False) -> Path:
    root = tmp_path / "suite"
    (root / "graders").mkdir(parents=True)
    (root / "datasets").mkdir()
    (root / "tasks" / "answer" / "environment").mkdir(parents=True)
    suite = yaml.safe_load(
        (FIXTURES / "suites" / "demo" / "suite.yaml").read_text(encoding="utf-8")
    )
    suite.update(id="pipeline-fixture", version="1.0.0")
    suite["harbor"] = {"dataset": "datasets/local.yaml", "job": "job.yaml"}
    suite["clock"] = {"mode": "real"}
    suite["baselines"] = [
        {"id": "ready", "probe": "file:/workspace/ready", "equals": True},
    ]
    suite["observables"] = [
        {"name": "result", "type": "string", "source": "file:/workspace/result"},
    ]
    suite["verdict"]["graders"] = {
        "default": {"impl": "graders/outcome.py@v7", "layer": "outcome", "veto": veto},
    }
    (root / "suite.yaml").write_text(yaml.safe_dump(suite), encoding="utf-8")
    (root / "datasets" / "local.yaml").write_text("path: tasks\n", encoding="utf-8")
    (root / "job.yaml").write_text(
        "job_name: pipeline\nn_attempts: 1\nn_concurrent_trials: 1\n"
        "agents: [{name: nop}]\n",
        encoding="utf-8",
    )
    (root / "graders" / "outcome.py").write_text(grader_body, encoding="utf-8")
    task = root / "tasks" / "answer"
    (task / "task.toml").write_text(
        'schema_version = "1.4"\n[environment]\nnetwork_mode = "no-network"\n',
        encoding="utf-8",
    )
    (task / "instruction.md").write_text("fixture\n", encoding="utf-8")
    (task / "environment" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    return root


def _evidence(tmp_path: Path, trial_id: str) -> EvidenceBundle:
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(exist_ok=True)
    ref = write_artifact(artifacts_dir, "runtime_dump", b"{}")
    return EvidenceBundle(
        trial_id=trial_id,
        stop_reason="agent_exit_0",
        artifacts={"runtime_dump": ref},
    )


_COORDS = TrialCoordinates(
    run_id="run-1", suite_id="pipeline-fixture", suite_version="1.0.0",
    task_id="answer", trial_index=0,
)


def _completed_progress() -> RequirementProgress:
    p = RequirementProgress()
    p.mark("input_complete")
    p.mark("artifact_schema_ok")
    p.mark("agent_finished")
    p.mark("integration_valid")
    p.mark("render_valid")
    return p


def _open_store(tmp_path: Path, lock) -> TrialStore:
    """Store with the run already created (trials reference runs)."""
    store = TrialStore(tmp_path / "store.sqlite3")
    store.create_run(RunManifest(
        run_id="run-1",
        runtime_lock=lock,
        overlay=OverlayIdentity(
            suite_id="pipeline-fixture", suite_version="1.0.0",
            overlay_digest="d" * 64, source_commit="9" * 40,
        ),
        versions=VersionsBundle(aeval_version="0.1.0"),
    ))
    return store


async def test_grade_and_record_happy_path(tmp_path, runtime_lock):
    suite = load_suite(_suite(tmp_path, _PASSING_GRADER))
    store = _open_store(tmp_path, runtime_lock)
    try:
        record = await grade_and_record(
            suite=suite,
            trial_id="trial-1",
            coordinates=_COORDS,
            stop_reason="agent_exit_0",
            baseline_ok=True,
            progress=_completed_progress(),
            evidence=_evidence(tmp_path, "trial-1"),
            transcript_extra={"aeval": {"completeness": {
                "fields": [{"field": "events", "status": "ok"}],
            }}},
            store=store,
        )
        assert record.verdict == "pass"
        assert record.requirements.judge_finished is True
        assert record.requirements.all_satisfied
        assert record.grades[0].grader_id == "outcome"
        # persisted atomically with grades
        loaded = store.load_trial("trial-1")
        assert loaded.verdict == "pass"
        assert [g.grader_id for g in loaded.grades] == ["outcome"]
        assert loaded.requirements.judge_finished is True
    finally:
        store.close()


async def test_grade_and_record_cannot_judge_when_fields_missing(tmp_path, runtime_lock):
    suite = load_suite(_suite(tmp_path, _PASSING_GRADER))
    store = _open_store(tmp_path, runtime_lock)
    try:
        record = await grade_and_record(
            suite=suite,
            trial_id="trial-2",
            coordinates=_COORDS,
            stop_reason="agent_exit_0",
            baseline_ok=True,
            progress=_completed_progress(),
            evidence=_evidence(tmp_path, "trial-2"),
            transcript_extra=None,  # completeness unavailable
            store=store,
        )
        # grading completed (judge_finished) but the verdict is cannot_judge
        assert record.verdict == "cannot_judge"
        assert record.requirements.judge_finished is True
        assert record.grades[0].status == "cannot_judge"
    finally:
        store.close()


async def test_grade_and_record_crashing_grader_is_infra_invalid(tmp_path, runtime_lock):
    suite = load_suite(_suite(tmp_path, _CRASHING_GRADER))
    store = _open_store(tmp_path, runtime_lock)
    try:
        from aeval.verdict.pipeline import GradingPipelineError

        with pytest.raises(GradingPipelineError, match="grader execution failed"):
            await grade_and_record(
                suite=suite,
                trial_id="trial-3",
                coordinates=_COORDS,
                stop_reason="agent_exit_0",
                baseline_ok=True,
                progress=_completed_progress(),
                evidence=_evidence(tmp_path, "trial-3"),
                transcript_extra=None,
                store=store,
            )
        # the failure is recorded honestly: infra verdict, no judge bit
        loaded = store.load_trial("trial-3")
        assert loaded.verdict == "infra_invalid"
        assert loaded.stop_reason == "infra_error"
        assert loaded.requirements.judge_finished is False
        assert loaded.grades == []
    finally:
        store.close()


async def test_grade_and_record_lying_grader_is_infra_invalid(tmp_path, runtime_lock):
    suite = load_suite(_suite(tmp_path, _LYING_GRADER))
    store = _open_store(tmp_path, runtime_lock)
    try:
        from aeval.verdict.pipeline import GradingPipelineError

        with pytest.raises(GradingPipelineError, match="grader execution failed"):
            await grade_and_record(
                suite=suite,
                trial_id="trial-4",
                coordinates=_COORDS,
                stop_reason="agent_exit_0",
                baseline_ok=True,
                progress=_completed_progress(),
                evidence=_evidence(tmp_path, "trial-4"),
                transcript_extra={"aeval": {"completeness": {
                    "fields": [{"field": "events", "status": "ok"}],
                }}},
                store=store,
            )
        loaded = store.load_trial("trial-4")
        assert loaded.verdict == "infra_invalid"
        assert loaded.requirements.judge_finished is False
    finally:
        store.close()


async def test_grade_and_record_unloadable_grader_is_infra_invalid(tmp_path, runtime_lock):
    suite = load_suite(_suite(tmp_path, _MISSING_FILE_GRADER))
    store = _open_store(tmp_path, runtime_lock)
    try:
        from aeval.verdict.pipeline import GradingPipelineError

        with pytest.raises(GradingPipelineError, match="grader loading failed"):
            await grade_and_record(
                suite=suite,
                trial_id="trial-5",
                coordinates=_COORDS,
                stop_reason="agent_exit_0",
                baseline_ok=True,
                progress=_completed_progress(),
                evidence=_evidence(tmp_path, "trial-5"),
                transcript_extra=None,
                store=store,
            )
        loaded = store.load_trial("trial-5")
        assert loaded.verdict == "infra_invalid"
        assert loaded.requirements.judge_finished is False
    finally:
        store.close()


async def test_grade_and_record_fail_verdict_persists(tmp_path, runtime_lock):
    suite = load_suite(_suite(tmp_path, _FAILING_GRADER))
    store = _open_store(tmp_path, runtime_lock)
    try:
        record = await grade_and_record(
            suite=suite,
            trial_id="trial-6",
            coordinates=_COORDS,
            stop_reason="agent_exit_0",
            baseline_ok=True,
            progress=_completed_progress(),
            evidence=_evidence(tmp_path, "trial-6"),
            transcript_extra=None,  # no required fields declared → executes
            store=store,
        )
        assert record.verdict == "fail"
        assert record.requirements.judge_finished is True
    finally:
        store.close()


async def test_grade_and_record_store_conflict_propagates_cleanly(tmp_path, runtime_lock):
    suite = load_suite(_suite(tmp_path, _PASSING_GRADER))
    store = _open_store(tmp_path, runtime_lock)
    try:
        args = dict(
            suite=suite,
            trial_id="trial-7",
            coordinates=_COORDS,
            stop_reason="agent_exit_0",
            baseline_ok=True,
            progress=_completed_progress(),
            evidence=_evidence(tmp_path, "trial-7"),
            transcript_extra={"aeval": {"completeness": {
                "fields": [{"field": "events", "status": "ok"}],
            }}},
            store=store,
        )
        await grade_and_record(**args)
        # a second write of the same trial must conflict, not overwrite;
        # the retry re-tracks its own progress from scratch
        with pytest.raises(StoreConflictError):
            await grade_and_record(**{**args, "progress": _completed_progress()})
        loaded = store.load_trial("trial-7")
        assert loaded.verdict == "pass"
        assert len(loaded.grades) == 1
    finally:
        store.close()


def test_load_suite_graders_veto_from_declaration(tmp_path):
    suite = load_suite(_suite(tmp_path, _PASSING_GRADER, veto=True))
    graders = load_suite_graders(suite)
    assert len(graders) == 1
    assert graders[0].veto is True
    assert graders[0].grader.id == "outcome"


def test_load_suite_graders_missing_reference_fails(tmp_path):
    root = _suite(tmp_path, _PASSING_GRADER)
    (root / "graders" / "outcome.py").unlink()
    suite = load_suite(root)
    with pytest.raises(GraderLoadError, match="does not exist"):
        load_suite_graders(suite)
