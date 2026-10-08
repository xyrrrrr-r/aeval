"""Verdict semantics tests: matrix, validation, cannot_judge.

Every negative case asserts the structural invariant: cannot_judge is
never a disguised 0, a veto is never outvoted, and a grader whose
evidence is missing never executes.
"""

from __future__ import annotations

import asyncio

import pytest

from aeval.contracts import (
    CanonicalTranscript,
    CompletenessRecord,
    CoverageSummary,
    FieldCompleteness,
    GradeResult,
    Score,
    TrialRecord,
)
from aeval.verdict.base import (
    ResolvedGrader,
    decide_final_verdict,
    validate_grade_result,
)
from aeval.verdict.executor import (
    ExecIsolationUnavailableError,
    GraderIdentityError,
    execute_exec_grader,
    execute_pure_grader,
    grade_trial,
)
from aeval.verdict.requirements import cannot_judge_for_missing_fields, recompute_coverage


def _result(status="pass", value=1.0, veto=False, grader_id="g", grader_version="v1", **kw) -> GradeResult:
    return GradeResult(
        grader_id=grader_id,
        grader_version=grader_version,
        layer="outcome",
        veto=veto,
        score=Score(value=value) if value is not None else
              Score(value=None, valid=False, invalid_reasons=["x"]),
        status=status,
        **kw,
    )


def test_veto_fail_dominates_passes():
    verdict = decide_final_verdict([
        _result(status="pass", value=1.0),
        _result(status="fail", value=0.0, veto=True),
        _result(status="pass", value=0.9),
    ])
    assert verdict == "fail"


def test_plain_fail_still_fails_without_veto():
    assert decide_final_verdict([
        _result(status="pass", value=1.0),
        _result(status="fail", value=0.0),
    ]) == "fail"


def test_cannot_judge_beats_pass_when_no_fail():
    assert decide_final_verdict([
        _result(status="pass", value=1.0),
        _result(status="cannot_judge", value=None),
    ]) == "cannot_judge"


def test_all_pass_is_pass_and_empty_is_cannot_judge():
    assert decide_final_verdict([_result(status="pass", value=1.0)]) == "pass"
    assert decide_final_verdict([]) == "cannot_judge"


def test_validate_rejects_cannot_judge_with_valid_score():
    bad = _result(status="cannot_judge", value=0.0)  # valid score on cannot_judge
    with pytest.raises(ValueError, match="must not carry a valid score"):
        validate_grade_result(bad)


def test_validate_rejects_invalid_score_with_value():
    # The pydantic model blocks this shape at construction; use
    # model_construct to reach the validator's own defense line.
    bad = GradeResult.model_construct(
        grader_id="g", grader_version="v1", layer="outcome", veto=False,
        score=Score.model_construct(value=0.0, valid=False, invalid_reasons=["x"]),
        status="cannot_judge", reasons=[], coverage=None,
    )
    with pytest.raises(ValueError, match="must not carry a value"):
        validate_grade_result(bad)


def test_validate_rejects_invalid_score_without_reasons():
    bad = GradeResult.model_construct(
        grader_id="g", grader_version="v1", layer="outcome", veto=False,
        score=Score.model_construct(value=None, valid=False, invalid_reasons=[]),
        status="cannot_judge", reasons=[], coverage=None,
    )
    with pytest.raises(ValueError, match="without reasons"):
        validate_grade_result(bad)


def test_validate_rejects_empty_reason_string():
    bad = _result(status="fail", value=0.0)
    object.__setattr__(bad, "reasons", [" "])
    with pytest.raises(ValueError, match="empty reason"):
        validate_grade_result(bad)


def test_validate_rejects_coverage_contradiction():
    bad = _result(status="pass", value=1.0, coverage=CoverageSummary(
        required_fields=["token_usage"],
        satisfied_fields=["token_usage"],
        missing_fields=["token_usage"],
    ))
    with pytest.raises(ValueError, match="both satisfied and missing"):
        validate_grade_result(bad)


def _ct_with(**statuses) -> CanonicalTranscript:
    from harbor.models.trajectories import Agent, Step, Trajectory

    atif = Trajectory(
        agent=Agent(name="dsh", version="unknown"),
        steps=[Step(step_id=1, source="system", message="m")],
    )
    return CanonicalTranscript(
        atif=atif,
        stop_reason="agent_exit_0",
        completeness=CompletenessRecord(fields=[
            FieldCompleteness(field=f, status=s) for f, s in statuses.items()
        ]),
    )


def test_cannot_judge_none_when_all_required_ok():
    ct = _ct_with(events="ok", token_usage="ok")
    assert cannot_judge_for_missing_fields(ct, ["events", "token_usage"], "g", "v1") is None


def test_cannot_judge_for_unavailable_field():
    ct = _ct_with(events="ok", token_usage="unavailable")
    result = cannot_judge_for_missing_fields(ct, ["token_usage"], "g", "v1")
    assert result is not None
    assert result.status == "cannot_judge"
    assert result.score.value is None
    assert result.score.valid is False
    assert "unavailable" in result.reasons[0]


def test_cannot_judge_for_partial_field():
    ct = _ct_with(token_usage="partial")
    result = cannot_judge_for_missing_fields(ct, ["token_usage"], "g", "v1")
    assert result is not None
    assert "partial" in result.reasons[0]


def test_missing_completeness_means_unavailable():
    ct = _ct_with(events="ok")
    result = cannot_judge_for_missing_fields(ct, ["anything_else"], "g", "v1")
    assert result is not None
    assert result.coverage.missing_fields == ["anything_else"]


def test_recompute_coverage_from_reasons():
    r = _result(status="cannot_judge", value=None)
    object.__setattr__(r, "coverage", None)
    coverage = recompute_coverage(r)
    assert coverage.missing_fields == ["x"]


class _SpyGrader:
    """Records invocation; returns a fixed result or raises."""

    id = "spy"
    version = "v1"
    layer = "outcome"

    def __init__(self, result=None, raise_exc=None):
        self.calls = 0
        self._result = result
        self._raise = raise_exc

    async def grade(self, record):
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return self._result


def _record(transcript_extra=None) -> TrialRecord:
    return TrialRecord(
        trial_id="t-1",
        coordinates={
            "run_id": "r", "suite_id": "s", "suite_version": "1",
            "task_id": "task", "trial_index": 0,
        },
        stop_reason="agent_exit_0",
        transcript_extra=transcript_extra,
    )


async def test_grade_trial_skips_grader_with_missing_evidence():
    spy = _SpyGrader(result=_result(status="pass", value=1.0))
    record = _record()  # no transcript extra → fields unavailable
    results = await grade_trial(
        record,
        [ResolvedGrader(grader=spy, requires_fields=["token_usage"])],
    )
    assert spy.calls == 0  # side effect never happened
    assert results[0].status == "cannot_judge"


async def test_grade_trial_executes_when_evidence_present():
    spy = _SpyGrader(result=_result(status="pass", value=1.0, grader_id="spy"))
    record = _record(transcript_extra={
        "aeval": {"completeness": {
            "fields": [{"field": "token_usage", "status": "ok"}],
        }},
    })
    results = await grade_trial(record, [ResolvedGrader(grader=spy)])
    assert spy.calls == 1
    assert results[0].status == "pass"


def test_grade_trial_crashing_grader_never_scores():
    spy = _SpyGrader(raise_exc=RuntimeError("grader bug"))
    record = _record(transcript_extra={
        "aeval": {"completeness": {
            "fields": [{"field": "token_usage", "status": "ok"}],
        }},
    })
    with pytest.raises(RuntimeError, match="grader bug"):
        asyncio.run(grade_trial(record, [ResolvedGrader(grader=spy)]))


# --- score/status consistency and identity contracts -----------------


def test_validate_rejects_pass_on_invalid_score():
    """Defect: pass + score.valid=False must never validate."""
    bad = _result(status="pass", value=None)  # valid=False, no value
    with pytest.raises(ValueError, match="pass verdict on an invalid score"):
        validate_grade_result(bad)


def test_validate_rejects_pass_on_invalid_score_via_model_construct():
    """The validator's own defense line, past the pydantic shape checks."""
    bad = GradeResult.model_construct(
        grader_id="g", grader_version="v1", layer="outcome", veto=False,
        score=Score.model_construct(value=None, valid=False, invalid_reasons=["x"]),
        status="pass", reasons=[], coverage=None,
    )
    with pytest.raises(ValueError, match="pass verdict on an invalid score"):
        validate_grade_result(bad)


def test_decide_final_verdict_rejects_pass_with_invalid_score():
    """The folding path validates every result — a lying pass cannot reach a verdict."""
    bad = _result(status="pass", value=None)
    with pytest.raises(ValueError, match="pass verdict on an invalid score"):
        decide_final_verdict([bad])


def test_validate_allows_fail_with_invalid_score():
    """A fail may carry an invalid score: failure does not rest on the score."""
    result = _result(status="fail", value=None)
    assert validate_grade_result(result) is result


def test_exec_graders_refused_without_isolation():
    """Exec graders never run in-process; the refusal is explicit."""
    spy = _SpyGrader(result=_result(status="fail", value=0.0, grader_id="spy"))
    with pytest.raises(ExecIsolationUnavailableError, match="no isolated execution"):
        asyncio.run(execute_exec_grader(
            ResolvedGrader(grader=spy, execution="exec"), _record()))
    assert spy.calls == 0


def test_exec_refusal_also_covers_pure_declaration_misuse():
    spy = _SpyGrader(result=_result(status="pass", value=1.0, grader_id="spy"))
    with pytest.raises(ExecIsolationUnavailableError):
        asyncio.run(execute_exec_grader(
            ResolvedGrader(grader=spy, execution="pure"), _record()))


async def test_grade_trial_rejects_wrong_result_identity():
    """A result attributed to another grader identity is infra, not a verdict."""
    spy = _SpyGrader(result=_result(status="pass", value=1.0, grader_id="someone-else"))
    record = _record(transcript_extra={
        "aeval": {"completeness": {"fields": [{"field": "token_usage", "status": "ok"}]}},
    })
    with pytest.raises(GraderIdentityError, match="attributed to someone-else"):
        await grade_trial(record, [ResolvedGrader(grader=spy)])


async def test_grade_trial_rejects_wrong_result_version():
    spy = _SpyGrader(result=_result(status="pass", value=1.0, grader_id="spy", grader_version="v9"))
    record = _record(transcript_extra={
        "aeval": {"completeness": {"fields": [{"field": "token_usage", "status": "ok"}]}},
    })
    with pytest.raises(GraderIdentityError, match="attributed to spy@v9"):
        await grade_trial(record, [ResolvedGrader(grader=spy)])


async def test_grade_trial_rejects_veto_flip():
    """The result's veto must match the suite's declared veto."""
    spy = _SpyGrader(result=_result(status="fail", value=0.0, grader_id="spy", veto=False))
    record = _record(transcript_extra={
        "aeval": {"completeness": {"fields": [{"field": "token_usage", "status": "ok"}]}},
    })
    with pytest.raises(GraderIdentityError, match="declared veto=True"):
        await grade_trial(record, [ResolvedGrader(grader=spy, veto=True)])


async def test_grade_trial_generated_cannot_judge_carries_declared_veto():
    """A short-circuited cannot_judge inherits the declared veto flag."""
    spy = _SpyGrader(result=_result(status="pass", value=1.0, grader_id="spy"))
    record = _record()  # fields unavailable
    results = await grade_trial(
        record, [ResolvedGrader(grader=spy, veto=True, requires_fields=["token_usage"])],
    )
    assert spy.calls == 0
    assert results[0].status == "cannot_judge"
    assert results[0].veto is True


def test_pure_grader_validates_output():
    bad = GradeResult.model_construct(
        grader_id="spy", grader_version="v1", layer="outcome", veto=False,
        score=Score.model_construct(value=0.0, valid=False, invalid_reasons=["x"]),
        status="cannot_judge", reasons=[], coverage=None,
    )
    spy = _SpyGrader(result=bad)
    with pytest.raises(ValueError):
        asyncio.run(execute_pure_grader(spy, _record()))
