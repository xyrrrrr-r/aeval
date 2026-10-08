"""Grader protocol and verdict semantics.

Denominator discipline:

- any failing ``veto`` grader ⇒ final verdict fail;
- a grader whose required evidence fields are partial/unavailable ⇒
  that grader returns cannot_judge (never a disguised 0);
- final verdict: infra_invalid dominates (never graded), then any
  veto-fail, then cannot_judge if no grader could judge, else the
  aggregate of pass/fail.
"""

from __future__ import annotations

from typing import Literal, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, Field

from aeval.contracts import (
    CanonicalTranscript,
    CompletenessStatus,
    CoverageSummary,
    GradeResult,
    Score,
    TrialRecord,
    Verdict,
)

__all__ = [
    "Grader",
    "ResolvedGrader",
    "GraderInput",
    "validate_grade_result",
    "decide_final_verdict",
]


@runtime_checkable
class Grader(Protocol):
    """A grader judges sealed evidence only — no env handle, no network."""

    id: str
    version: str
    layer: Literal["outcome", "trajectory", "both"]

    async def grade(self, record: TrialRecord) -> GradeResult: ...


class ResolvedGrader(BaseModel):
    """A grader plus its declared execution class and veto flag."""

    model_config = {"arbitrary_types_allowed": True}

    grader: Grader
    execution: Literal["pure", "exec"] = "pure"
    veto: bool = False
    requires_fields: list[str] = Field(default_factory=list)


class GraderInput(BaseModel):
    """The sealed input a grader receives — never live state."""

    record: TrialRecord


def validate_grade_result(result: GradeResult) -> GradeResult:
    """Reject score shapes that would corrupt the denominator.

    - a ``pass`` must rest on a valid score: ``status='pass'`` with
      ``score.valid=False`` claims success from evidence the grader
      itself declared unjudgeable — rejected outright;
    - invalid score (valid=False) must have at least one reason and
      must not carry a value (a number next to "invalid" invites
      accidental aggregation);
    - status cannot_judge must not carry a valid value;
    - reasons must be non-empty strings.

    A ``fail`` may carry an invalid score: the failure verdict does not
    rest on the score, and it can never inflate the denominator.
    """
    if result.status == "pass" and result.score.valid is False:
        raise ValueError(
            f"grader {result.grader_id}: pass verdict on an invalid score — "
            "unjudgeable evidence cannot yield pass"
        )
    if result.score.valid is False:
        if not result.score.invalid_reasons:
            raise ValueError(
                f"grader {result.grader_id}: invalid score without reasons"
            )
        if result.score.value is not None:
            raise ValueError(
                f"grader {result.grader_id}: invalid score must not carry a value"
            )
    if result.status == "cannot_judge" and result.score.valid and result.score.value is not None:
        raise ValueError(
            f"grader {result.grader_id}: cannot_judge must not carry a valid score"
        )
    for reason in result.reasons:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"grader {result.grader_id}: empty reason")
    if result.coverage is not None:
        overlap = set(result.coverage.satisfied_fields) & set(result.coverage.missing_fields)
        if overlap:
            raise ValueError(
                f"grader {result.grader_id}: fields both satisfied and missing: {overlap}"
            )
    return result


def decide_final_verdict(results: Sequence[GradeResult]) -> Verdict:
    """Fold grader results into the trial's final verdict."""
    if not results:
        return "cannot_judge"
    for result in results:
        validate_grade_result(result)
    if any(r.status == "fail" and r.veto for r in results):
        return "fail"
    statuses = {r.status for r in results}
    if "fail" in statuses:
        return "fail"
    if "cannot_judge" in statuses:
        return "cannot_judge"
    return "pass"
