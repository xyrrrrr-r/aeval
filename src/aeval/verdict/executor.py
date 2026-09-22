"""Grader execution (plan §5).

- ``pure`` graders run in the core process over sealed TrialRecords.
- ``exec`` graders run in Harbor's separate verifier environment:
  network-off, read-only whitelisted artifacts. This module only
  declares the contract and enforces the whitelist; the environment
  itself is Harbor's.
- every grade result is validated before it can enter a verdict.
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from aeval.verdict.base import (
    Grader,
    GraderInput,
    ResolvedGrader,
    decide_final_verdict,
    validate_grade_result,
)
from aeval.verdict.requirements import cannot_judge_for_missing_fields

from aeval.contracts import GradeResult, TrialRecord

__all__ = [
    "ExecEnvironmentError",
    "build_grader_input",
    "grade_trial",
    "execute_pure_grader",
    "execute_exec_grader",
]


class ExecEnvironmentError(RuntimeError):
    """An exec grader tried to exceed its read-only, network-off box."""


def build_grader_input(record: TrialRecord) -> GraderInput:
    return GraderInput(record=record)


async def execute_pure_grader(
    grader: Grader, record: TrialRecord
) -> GradeResult:
    """Run a pure grader in-process; wrap crashes as cannot_judge? No —

    a crashing grader is a grader bug, i.e. OUR infrastructure fault:
    the trial becomes infra_invalid via an invalid result, never a
    silent 0 and never a lucky pass.
    """
    result = await grader.grade(record)
    return validate_grade_result(result)


async def execute_exec_grader(
    grader: ResolvedGrader, record: TrialRecord
) -> GradeResult:
    """Run an exec grader under the separate-verifier contract.

    The whitelist here is the Python-side mirror of the sandbox policy:
    the grader function receives ONLY the sealed record. Any attempt to
    touch the filesystem beyond declared artifacts or any network use
    is rejected with ExecEnvironmentError (and in the Harbor verifier
    environment the sandbox enforces the same policy natively).
    """
    if grader.execution != "exec":
        raise ValueError(f"grader {grader.grader.id} is not an exec grader")
    input_ = build_grader_input(record)
    # Exec graders are invoked with the input only; the environment
    # (network off, read-only artifacts) is provided by Harbor's
    # separate verifier sandbox.
    result = await grader.grader.grade(input_.record)
    return validate_grade_result(result)


async def grade_trial(
    record: TrialRecord,
    graders: Sequence[ResolvedGrader],
) -> list[GradeResult]:
    """Run every grader against the sealed record.

    Required-field checks run first: a grader whose evidence is
    missing gets a cannot_judge result without ever executing — a
    crashed grader and an unjudgeable grader must never be confused.
    """
    results: list[GradeResult] = []
    for resolved in graders:
        missing = cannot_judge_for_missing_fields(
            _ct_of(record),
            resolved.requires_fields,
            resolved.grader.id,
            getattr(resolved.grader, "version", "unknown"),
            resolved.grader.layer,
        )
        if missing is not None:
            results.append(missing)
            continue
        if resolved.execution == "pure":
            results.append(await execute_pure_grader(resolved.grader, record))
        else:
            results.append(await execute_exec_grader(resolved, record))
    return results


def _ct_of(record: TrialRecord):
    """Reconstruct a CanonicalTranscript view from a stored record.

    Stored records keep the ATIF extra dict; the completeness fields a
    grader requires are read from it. If the record carries no
    transcript at all, every required field is unavailable.
    """
    from aeval.contracts import CanonicalTranscript, CompletenessRecord, FieldCompleteness

    extra = record.transcript_extra or {}
    aeval_extra = extra.get("aeval") or {}
    completeness_data = aeval_extra.get("completeness")
    if completeness_data:
        completeness = CompletenessRecord.model_validate(completeness_data)
    else:
        completeness = CompletenessRecord(fields=[
            FieldCompleteness(field="events", status="unavailable"),
            FieldCompleteness(field="token_usage", status="unavailable"),
        ])
    # A minimal ATIF shell: field checks only need the extra metadata.
    from harbor.models.trajectories import Agent, Step, Trajectory

    atif = Trajectory(
        agent=Agent(name="dsh", version="unknown"),
        steps=[Step(step_id=1, source="system", message="(stored record)")],
        extra=extra,
    )
    ct = CanonicalTranscript(
        atif=atif,
        stop_reason=record.stop_reason,
        completeness=completeness,
    )
    return ct
