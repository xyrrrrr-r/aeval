"""Requirement/cannot-judge logic.

A grader declares the transcript fields it needs. If any required
field is partial or unavailable, the grader MUST emit cannot_judge —
never a 0 dressed as a score, never a guess.
"""

from __future__ import annotations

from typing import Collection

from aeval.contracts import (
    CanonicalTranscript,
    CompletenessStatus,
    CoverageSummary,
    GradeResult,
    Score,
)

__all__ = [
    "cannot_judge_for_missing_fields",
    "recompute_coverage",
    "transcript_field_status",
]


def transcript_field_status(
    transcript: CanonicalTranscript, field: str
) -> CompletenessStatus:
    """Look up a field's completeness in the canonical transcript."""
    if transcript.completeness is None:
        return "unavailable"
    return transcript.completeness.status_of(field)


def cannot_judge_for_missing_fields(
    transcript: CanonicalTranscript,
    required_fields: Collection[str],
    grader_id: str,
    grader_version: str,
    layer: str = "outcome",
    veto: bool = False,
) -> GradeResult | None:
    """Return a cannot_judge result when required fields are degraded.

    Returns None when every required field is ``ok`` — the grader may
    proceed. A ``partial`` field is also blocking for strict rubrics:
    a partially-captured token count can flip cost metrics. The result
    carries the grader's declared veto so a generated outcome cannot
    dodge the suite's veto contract.
    """
    missing: list[str] = []
    degraded: list[str] = []
    satisfied: list[str] = []
    for field in required_fields:
        status = transcript_field_status(transcript, field)
        if status == "ok":
            satisfied.append(field)
        elif status == "partial":
            degraded.append(field)
        else:
            missing.append(field)
    if not missing and not degraded:
        return None
    reasons = []
    if missing:
        reasons.append(f"required fields unavailable: {sorted(missing)}")
    if degraded:
        reasons.append(f"required fields partial: {sorted(degraded)}")
    return GradeResult(
        grader_id=grader_id,
        grader_version=grader_version,
        layer=layer,  # type: ignore[arg-type]
        veto=veto,
        score=Score(value=None, valid=False, invalid_reasons=list(reasons)),
        status="cannot_judge",
        reasons=reasons,
        coverage=CoverageSummary(
            required_fields=sorted(required_fields),
            satisfied_fields=satisfied,
            missing_fields=missing,
            degraded_fields=degraded,
        ),
    )


def recompute_coverage(result: GradeResult) -> CoverageSummary:
    """Re-derive a result's coverage from its own claims.

    Guards against graders claiming coverage of fields their reasons
    contradict.
    """
    if result.coverage is not None:
        return result.coverage
    required = sorted(result.score.invalid_reasons)
    return CoverageSummary(
        required_fields=required,
        satisfied_fields=[],
        missing_fields=required,
        degraded_fields=[],
    )
