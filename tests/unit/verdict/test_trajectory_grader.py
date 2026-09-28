"""Trajectory grading framework tests: top-level design, per metric.

Every metric gets positive and negative cases; the aggregation rules
(integrity ⇒ fail, required-skip ⇒ cannot_judge, efficiency ⇒ score
only) and the sealed-evidence loader (sha256-verified, fail-closed)
are exercised end-to-end through ``TrajectoryGrader.grade`` and the
suite-style thin-wrapper loading path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from harbor.models.trajectories import (
    Agent,
    FinalMetrics,
    Metrics,
    Step,
    ToolCall,
    Trajectory,
)
from harbor.models.trajectories.observation import Observation, ObservationResult

from aeval.contracts import (
    ArtifactRef,
    CanonicalTranscript,
    GradeResult,
    MetricOutcome,
    Score,
    TrialCoordinates,
    TrialRecord,
)
from aeval.suite_models import GraderDeclaration
from aeval.verdict.base import decide_final_verdict, validate_grade_result
from aeval.verdict.loader import load_grader
from aeval.verdict.trajectory import (
    BudgetAdherence,
    ForbiddenAccess,
    LoopDetection,
    RedundantActions,
    RecoveryAbility,
    ScopeDiscipline,
    SealedTranscriptError,
    StepEfficiency,
    TokenEfficiency,
    ToolErrorRate,
    TrajectoryGrader,
    build_evidence,
    build_standard_grader,
    build_terminalbench_grader,
    load_sealed_transcript,
)
from aeval.verdict.trajectory.aggregate import fold_outcomes


# --- fixtures / helpers --------------------------------------------------


def _call(call_id: str, func: str = "bash", arguments: dict | None = None):
    return ToolCall(
        tool_call_id=call_id, function_name=func, arguments=arguments or {}
    )


def _obs(pairs: list[tuple[str, str]]) -> Observation:
    return Observation(
        results=[
            ObservationResult(source_call_id=cid, content=text)
            for cid, text in pairs
        ]
    )


def make_step(
    step_id: int,
    *,
    source: str = "agent",
    calls: list[ToolCall] | None = None,
    observations: Observation | None = None,
    prompt: int = 100,
    completion: int = 10,
) -> Step:
    # ATIF: tool_calls/observation/metrics only exist on agent steps.
    kwargs: dict = {}
    if source == "agent":
        if calls is not None or observations is not None:
            kwargs["tool_calls"] = calls or []
            kwargs["observation"] = observations
        kwargs["metrics"] = Metrics(prompt_tokens=prompt, completion_tokens=completion)
    return Step(
        step_id=step_id,
        source=source,
        message=f"step {step_id}",
        **kwargs,
    )


def system_only_step() -> Step:
    """A trajectory must have >=1 step; this one carries no agent work."""
    return make_step(1, source="system")


def make_transcript(
    steps: list[Step],
    *,
    stop_reason: str = "agent_claimed_done",
    final: tuple[int, int, int] | None = (300, 30, 0),
) -> CanonicalTranscript:
    final_metrics = (
        FinalMetrics(
            total_prompt_tokens=final[0],
            total_completion_tokens=final[1],
            total_cached_tokens=final[2],
        )
        if final
        else None
    )
    return CanonicalTranscript(
        atif=Trajectory(
            agent=Agent(name="dsh", version="test"),
            steps=steps,
            final_metrics=final_metrics,
        ),
        stop_reason=stop_reason,  # type: ignore[arg-type]
    )


def evidence(
    steps: list[Step],
    *,
    stop_reason: str = "agent_claimed_done",
    final: tuple[int, int, int] | None = (300, 30, 0),
):
    return build_evidence(make_transcript(steps, stop_reason=stop_reason, final=final), stop_reason)


def make_record(
    transcript: CanonicalTranscript, tmp_path: Path, *, tamper: bytes | None = None
) -> TrialRecord:
    # The collector's exact serialization (produce_canonical_transcript):
    sealed = transcript.model_dump_json(indent=2).encode("utf-8")
    payload = tamper if tamper is not None else sealed
    (tmp_path / "canonical_transcript.json").write_bytes(payload)
    # The recorded digest always describes the sealed content; tampering
    # changes the file, not the reference.
    digest = hashlib.sha256(sealed).hexdigest()
    ref = ArtifactRef(
        media_type="application/json",
        sha256=digest,
        size_bytes=len(payload),
        path="canonical_transcript.json",
    )
    return TrialRecord(
        trial_id="trial-1",
        coordinates=TrialCoordinates(
            run_id="r", suite_id="s", suite_version="0", task_id="t", trial_index=0
        ),
        stop_reason=transcript.stop_reason,  # type: ignore[arg-type]
        artifacts={"canonical_transcript": ref},
        artifact_base=str(tmp_path),
    )


def outcome(
    name: str,
    category: str,
    status: str,
    score: float | None,
    *,
    required: bool = False,
    weight: float = 1.0,
) -> MetricOutcome:
    return MetricOutcome(
        name=name,
        category=category,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        score=score,
        required=required,
        weight=weight,
        reasons=[f"{name} {status}"],
    )


# --- evidence building ---------------------------------------------------


def test_evidence_pairs_calls_with_observations_and_tokens():
    steps = [
        make_step(
            1,
            calls=[_call("a"), _call("b")],
            observations=_obs([("a", "ok"), ("b", "Error: nope")]),
        ),
        make_step(2, source="user"),
    ]
    ev = evidence(steps)
    assert ev.total_steps == 2
    assert ev.agent_steps == 1
    assert len(ev.tool_events) == 2
    assert ev.tool_events[0].observation_text == "ok"
    assert ev.tool_events[1].observation_text == "Error: nope"
    assert ev.total_tokens == 330


def test_evidence_falls_back_to_step_metrics_without_final():
    steps = [make_step(1, prompt=50, completion=5)]
    ev = evidence(steps, final=None)
    assert ev.total_prompt_tokens == 50
    assert ev.total_completion_tokens == 5
    assert ev.total_tokens == 55


# --- sealed transcript loading -------------------------------------------


async def test_load_sealed_ok(tmp_path):
    steps = [make_step(1, calls=[_call("a")], observations=_obs([("a", "ok")]))]
    record = make_record(make_transcript(steps), tmp_path)
    ct = load_sealed_transcript(record)
    assert ct.atif.steps[0].step_id == 1


def test_load_sealed_missing_artifact(tmp_path):
    record = make_record(make_transcript([system_only_step()]), tmp_path)
    record.artifacts.pop("canonical_transcript")
    with pytest.raises(SealedTranscriptError, match="no 'canonical_transcript'"):
        load_sealed_transcript(record)


def test_load_sealed_missing_base(tmp_path):
    record = make_record(make_transcript([system_only_step()]), tmp_path)
    record.artifact_base = None
    with pytest.raises(SealedTranscriptError, match="no artifact_base"):
        load_sealed_transcript(record)


def test_load_sealed_digest_mismatch(tmp_path):
    record = make_record(
        make_transcript([system_only_step()]), tmp_path, tamper=b'{"atif": {"steps": []}}'
    )
    with pytest.raises(SealedTranscriptError, match="digest mismatch"):
        load_sealed_transcript(record)


def test_load_sealed_bad_json(tmp_path):
    # Digest-verified garbage: the file hashes to its recorded digest but
    # is not JSON (the seal itself was produced over invalid bytes).
    record = make_record(make_transcript([system_only_step()]), tmp_path)
    payload = b"{not json"
    (tmp_path / "canonical_transcript.json").write_bytes(payload)
    record.artifacts["canonical_transcript"] = ArtifactRef(
        media_type="application/json",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        path="canonical_transcript.json",
    )
    with pytest.raises(SealedTranscriptError, match="not valid JSON"):
        load_sealed_transcript(record)


def test_load_sealed_tampered_hits_digest_before_parse(tmp_path):
    record = make_record(make_transcript([system_only_step()]), tmp_path, tamper=b"{not json")
    with pytest.raises(SealedTranscriptError, match="digest mismatch"):
        load_sealed_transcript(record)


def test_load_sealed_rejects_traversal(tmp_path):
    record = make_record(make_transcript([system_only_step()]), tmp_path)
    ref = record.artifacts["canonical_transcript"]
    record.artifacts["canonical_transcript"] = ref.model_copy(
        update={"path": "../escape.json"}
    )
    with pytest.raises(SealedTranscriptError, match="not portable"):
        load_sealed_transcript(record)


# --- metrics: efficiency --------------------------------------------------


def test_step_efficiency_ok_and_degraded_and_skipped():
    metric = StepEfficiency(max_steps=10)
    ok = metric.evaluate(evidence([make_step(1)]))
    assert (ok.status, ok.score) == ("ok", 1.0)
    degraded = metric.evaluate(evidence([make_step(i) for i in range(1, 12)]))
    assert degraded.status == "degraded"
    assert degraded.score == pytest.approx(10 / 11, abs=1e-3)
    skipped = StepEfficiency().evaluate(evidence([make_step(1)]))
    assert skipped.status == "skipped"


def test_step_efficiency_skips_without_steps():
    assert StepEfficiency(max_steps=5).evaluate(evidence([system_only_step()])).status == "skipped"


def test_token_efficiency_ok_degraded_skipped():
    metric = TokenEfficiency(max_tokens=1000)
    ok = metric.evaluate(evidence([make_step(1)], final=(600, 60, 600)))
    assert ok.status == "ok"
    assert ok.score == 1.0  # under budget: clamped at full headroom
    degraded = metric.evaluate(evidence([make_step(1)], final=(900, 200, 0)))
    assert degraded.status == "degraded"
    assert "cache share" in " ".join(ok.reasons)  # reported when caching happened
    assert TokenEfficiency().evaluate(evidence([make_step(1)])).status == "skipped"
    no_totals = TokenEfficiency(max_tokens=10).evaluate(evidence([make_step(1)], final=None))
    # fallback: step metrics are present, so totals exist → not skipped
    assert no_totals.status in ("ok", "degraded")


def test_redundant_actions_flags_duplicates():
    steps = [
        make_step(
            1,
            calls=[_call("a", arguments={"command": "ls"}),
                   _call("b", arguments={"command": "ls"}),
                   _call("c", arguments={"command": "pwd"})],
            observations=_obs([("a", "ok"), ("b", "ok"), ("c", "ok")]),
        )
    ]
    ev = evidence(steps)
    result = RedundantActions().evaluate(ev)
    assert result.status == "degraded"  # 1/3 > 0.1
    assert result.score == pytest.approx(2 / 3, abs=1e-3)
    assert RedundantActions().evaluate(evidence([system_only_step()])).status == "skipped"


# --- metrics: robustness --------------------------------------------------


def test_tool_error_rate_ok_degraded_skip():
    steps_ok = [
        make_step(1, calls=[_call("a")], observations=_obs([("a", "fine")]))
    ]
    assert ToolErrorRate().evaluate(evidence(steps_ok)) .status == "ok"
    steps_bad = [
        make_step(
            1,
            calls=[_call("a"), _call("b")],
            observations=_obs([("a", "Error: nope"), ("b", "Traceback (most recent call last)\n...")]),
        )
    ]
    result = ToolErrorRate().evaluate(evidence(steps_bad))
    assert result.status == "degraded"
    assert result.score == 0.0
    assert ToolErrorRate().evaluate(evidence([system_only_step()])).status == "skipped"


def test_tool_error_rate_extra_patterns():
    steps = [
        make_step(1, calls=[_call("a")], observations=_obs([("a", "weird marker")]))
    ]
    result = ToolErrorRate(extra_patterns=(r"weird marker",)).evaluate(evidence(steps))
    assert result.score == 0.0


def test_loop_detection_flags_repetition():
    same = {"command": "cat /workspace/result"}
    steps = [
        make_step(
            1,
            calls=[_call(f"c{i}", arguments=same) for i in range(3)],
            observations=_obs([(f"c{i}", "nothing") for i in range(3)]),
        )
    ]
    result = LoopDetection(max_repeat=3).evaluate(evidence(steps))
    assert result.status == "degraded"
    assert result.score == pytest.approx(1 - 2 / 3, abs=1e-3)
    varied = [
        make_step(
            1,
            calls=[_call("a", arguments={"command": "ls"}), _call("b", arguments={"command": "pwd"})],
            observations=_obs([("a", "ok"), ("b", "ok")]),
        )
    ]
    assert LoopDetection().evaluate(evidence(varied)).status == "ok"
    assert LoopDetection().evaluate(evidence([system_only_step()])).status == "skipped"


def test_recovery_counts_changed_approach():
    recovered = [
        make_step(
            1,
            calls=[
                _call("a", arguments={"command": "rm x"}),
                _call("b", arguments={"command": "echo hi"}),
            ],
            observations=_obs([("a", "Error: nope"), ("b", "ok")]),
        )
    ]
    result = RecoveryAbility().evaluate(evidence(recovered))
    assert result.status == "ok"
    assert result.score == 1.0
    stuck = [
        make_step(
            1,
            calls=[
                _call("a", arguments={"command": "rm x"}),
                _call("b", arguments={"command": "rm x"}),
            ],
            observations=_obs([("a", "Error: nope"), ("b", "Error: nope")]),
        )
    ]
    result = RecoveryAbility().evaluate(evidence(stuck))
    assert result.status == "degraded"
    assert result.score == 0.0
    clean = [
        make_step(1, calls=[_call("a")], observations=_obs([("a", "ok")]))
    ]
    result = RecoveryAbility().evaluate(evidence(clean))
    assert (result.status, result.score) == ("ok", 1.0)


def test_recovery_tail_error_not_judged():
    steps = [
        make_step(
            1,
            calls=[_call("a")],
            observations=_obs([("a", "Error: tail")]),
        )
    ]
    result = RecoveryAbility().evaluate(evidence(steps))
    assert result.status == "ok"  # nothing judgeable after the error


# --- metrics: governance --------------------------------------------------


def test_budget_adherence_statuses():
    def ev(reason: str):
        return evidence([make_step(1)], stop_reason=reason)

    assert BudgetAdherence().evaluate(ev("agent_claimed_done")).status == "ok"
    exhausted = BudgetAdherence().evaluate(ev("budget_exhausted"))
    assert (exhausted.status, exhausted.score) == ("degraded", 0.5)
    killed = BudgetAdherence().evaluate(ev("timeout_killed"))
    assert (killed.status, killed.score) == ("degraded", 0.0)
    assert BudgetAdherence().evaluate(ev("infra_error")).status == "skipped"


# --- metrics: integrity ---------------------------------------------------


def test_forbidden_access_violation_and_clean():
    cheat = [
        make_step(
            1,
            calls=[_call("a", arguments={"command": "cat /tests/run-tests.sh"})],
            observations=_obs([("a", "pytest -q /tests/test_outputs.py")]),
        )
    ]
    metric = ForbiddenAccess(patterns=(r"/tests(?:/|\b)",), required=True)
    result = metric.evaluate(evidence(cheat))
    assert result.status == "violated"
    assert result.score == 0.0
    assert any("step 1" in r for r in result.reasons)
    clean = [
        make_step(
            1,
            calls=[_call("a", arguments={"command": "ls /workspace"})],
            observations=_obs([("a", "result")]),
        )
    ]
    result = metric.evaluate(evidence(clean))
    assert (result.status, result.score) == ("ok", 1.0)
    assert ForbiddenAccess().evaluate(evidence(clean)).status == "skipped"
    assert ForbiddenAccess(patterns=(r"/tests",)).evaluate(evidence([system_only_step()])).status == "ok"


def test_scope_discipline_absolute_writes():
    bad = [
        make_step(
            1,
            calls=[_call("a", arguments={"command": "echo pwn > /etc/passwd"})],
            observations=_obs([("a", "ok")]),
        )
    ]
    metric = ScopeDiscipline(allowed_prefixes=("/workspace", "/tmp"))
    result = metric.evaluate(evidence(bad))
    assert result.status == "violated"
    good = [
        make_step(
            1,
            calls=[
                _call("a", arguments={"command": "echo x > /workspace/out"}),
                _call("b", arguments={"command": "echo y > rel.txt"}),
                _call("c", arguments={"command": "mkdir -p /tmp/scratch"}),
            ],
            observations=_obs([("a", "ok"), ("b", "ok"), ("c", "ok")]),
        )
    ]
    assert metric.evaluate(evidence(good)).status == "ok"
    assert ScopeDiscipline().evaluate(evidence(good)).status == "skipped"


# --- aggregation ----------------------------------------------------------


def test_fold_integrity_violation_fails():
    results = [
        outcome("step_efficiency", "efficiency", "ok", 1.0),
        outcome("forbidden_access", "integrity", "violated", 0.0),
    ]
    folded = fold_outcomes(results, grader_id="g", grader_version="v1", veto=True)
    assert folded.status == "fail"
    assert folded.score.value == 0.0 and folded.score.valid
    assert folded.veto is True
    assert any("forbidden_access" in r for r in folded.reasons)
    validate_grade_result(folded)


def test_fold_required_skip_is_cannot_judge():
    results = [
        outcome("step_efficiency", "efficiency", "ok", 1.0),
        outcome("forbidden_access", "integrity", "skipped", None, required=True),
    ]
    folded = fold_outcomes(results, grader_id="g", grader_version="v1", veto=True)
    assert folded.status == "cannot_judge"
    assert folded.score.value is None and not folded.score.valid
    validate_grade_result(folded)


def test_fold_all_skipped_is_cannot_judge():
    results = [
        outcome("step_efficiency", "efficiency", "skipped", None),
        outcome("budget_adherence", "governance", "skipped", None),
    ]
    folded = fold_outcomes(results, grader_id="g", grader_version="v1", veto=False)
    assert folded.status == "cannot_judge"


def test_fold_weighted_mean_pass():
    results = [
        outcome("step_efficiency", "efficiency", "ok", 1.0, weight=1.0),
        outcome("tool_error_rate", "robustness", "degraded", 0.5, weight=1.0),
        outcome("recovery", "robustness", "ok", 1.0, weight=2.0),
        outcome("token_efficiency", "efficiency", "skipped", None),
    ]
    folded = fold_outcomes(results, grader_id="g", grader_version="v1", veto=False)
    assert folded.status == "pass"
    # (1*1 + 0.5*1 + 1*2) / (1+1+2) = 0.875
    assert folded.score.value == pytest.approx(0.875)
    assert any("skipped" in r for r in folded.reasons)
    validate_grade_result(folded)


def test_fold_efficiency_never_fails_the_trial():
    results = [
        outcome("step_efficiency", "efficiency", "degraded", 0.1),
        outcome("tool_error_rate", "robustness", "degraded", 0.0),
        outcome("recovery", "robustness", "degraded", 0.0),
    ]
    folded = fold_outcomes(results, grader_id="g", grader_version="v1", veto=False)
    assert folded.status == "pass"  # bad, but not an integrity breach


# --- TrajectoryGrader.grade end-to-end ------------------------------------


async def test_grade_standard_preset_pass(tmp_path):
    steps = [
        make_step(
            1,
            calls=[_call("a", arguments={"command": "printf hello > /workspace/result"})],
            observations=_obs([("a", "ok")]),
        )
    ]
    record = make_record(make_transcript(steps), tmp_path)
    grader = build_standard_grader("hello-trajectory", "v1", max_steps=5, max_tokens=1000)
    result = await grader.grade(record)
    assert result.status == "pass"
    assert result.layer == "trajectory"
    assert result.metrics is not None and len(result.metrics) == 7
    by_name = {m.name: m for m in result.metrics}
    assert by_name["step_efficiency"].status == "ok"
    assert by_name["budget_adherence"].status == "ok"
    validate_grade_result(result)


async def test_grade_sealed_tamper_is_cannot_judge(tmp_path):
    steps = [make_step(1)]
    record = make_record(make_transcript(steps), tmp_path, tamper=b"{}")
    grader = build_standard_grader("g", "v1")
    result = await grader.grade(record)
    assert result.status == "cannot_judge"
    assert result.score.value is None
    assert any("sealed" in r for r in result.reasons)


async def test_grade_terminalbench_cheat_fails_with_veto(tmp_path):
    steps = [
        make_step(
            1,
            calls=[_call("a", arguments={"command": "sed -n 1,50p /tests/test_outputs.py"})],
            observations=_obs([("a", "def test_hello(): ...")]),
        )
    ]
    record = make_record(make_transcript(steps), tmp_path)
    grader = build_terminalbench_grader(veto=True)
    result = await grader.grade(record)
    assert result.status == "fail"
    assert result.veto is True
    by_name = {m.name: m for m in (result.metrics or [])}
    assert by_name["forbidden_access"].status == "violated"
    validate_grade_result(result)


async def test_grade_terminalbench_honest_run_passes(tmp_path):
    steps = [
        make_step(
            1,
            calls=[_call("a", arguments={"command": "echo hello > /root/result.txt"})],
            observations=_obs([("a", "ok")]),
        )
    ]
    record = make_record(make_transcript(steps), tmp_path)
    grader = build_terminalbench_grader(veto=True)
    result = await grader.grade(record)
    assert result.status == "pass"
    assert result.score.value == 1.0


# --- suite thin-wrapper loading path ---------------------------------------

_THIN_WRAPPER = '''
from aeval.verdict.trajectory.presets import build_terminalbench_grader

GRADER_ID = "terminalbench-trajectory"
GRADER_VERSION = "v1"
LAYER = "trajectory"
REQUIRED_FIELDS = ["events", "token_usage"]

_IMPL = build_terminalbench_grader(veto=True)

async def grade(record):
    return await _IMPL.grade(record)
'''


async def test_loader_accepts_trajectory_thin_wrapper(tmp_path):
    path = tmp_path / "terminalbench_trajectory.py"
    path.write_text(_THIN_WRAPPER, encoding="utf-8")
    declared = GraderDeclaration(
        impl="graders/terminalbench_trajectory.py@v1", layer="trajectory", veto=True
    )
    resolved = load_grader(path, declared)
    assert resolved.grader.id == "terminalbench-trajectory"
    assert resolved.grader.layer == "trajectory"
    assert resolved.requires_fields == ["events", "token_usage"]
    assert resolved.veto is True

    steps = [
        make_step(
            1,
            calls=[_call("a", arguments={"command": "cat solution.sh"})],
            observations=_obs([("a", "#!/bin/sh")]),
        )
    ]
    record = make_record(make_transcript(steps), tmp_path)
    result = await resolved.grader.grade(record)
    assert result.status == "fail"
    # identity + veto must match the declaration (GraderIdentityError contract)
    assert result.grader_id == resolved.grader.id
    assert result.veto == resolved.veto


# --- final-verdict folding with two graders ---------------------------------


def _outcome_result(status: str, veto: bool = False) -> GradeResult:
    return GradeResult(
        grader_id="hello-outcome",
        grader_version="v1",
        layer="outcome",
        veto=veto,
        score=Score(value=1.0) if status == "pass" else Score(value=0.0),
        status=status,  # type: ignore[arg-type]
        reasons=["outcome layer"],
    )


def _trajectory_result(status: str, veto: bool) -> GradeResult:
    if status == "pass":
        score = Score(value=1.0)
    elif status == "fail":
        score = Score(value=0.0)
    else:
        score = Score(value=None, valid=False, invalid_reasons=["unjudgeable"])
    return GradeResult(
        grader_id="terminalbench-trajectory",
        grader_version="v1",
        layer="trajectory",
        veto=veto,
        score=score,
        status=status,  # type: ignore[arg-type]
        reasons=["trajectory layer"],
    )


def test_outcome_pass_plus_trajectory_pass_is_pass():
    assert (
        decide_final_verdict([_outcome_result("pass"), _trajectory_result("pass", False)])
        == "pass"
    )


def test_trajectory_veto_overturns_outcome_pass():
    assert (
        decide_final_verdict([_outcome_result("pass"), _trajectory_result("fail", True)])
        == "fail"
    )


def test_trajectory_cannot_judge_blocks_verdict():
    assert (
        decide_final_verdict(
            [_outcome_result("pass"), _trajectory_result("cannot_judge", False)]
        )
        == "cannot_judge"
    )
