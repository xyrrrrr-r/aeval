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


class GraderIdentityError(RuntimeError):
    """A grader returned a result attributed to another identity.

    The result's grader id/version or its veto flag does not match the
    resolved, loaded declaration. A lying grader is an infrastructure
    fault: the trial becomes infra_invalid, never a borrowed verdict.
    """


class ExecIsolationUnavailableError(ExecEnvironmentError):
    """Exec graders need an isolated execution environment; none exists.

    P0-7 explicitly refuses to run exec graders in-process: the name
    and comments do not create a filesystem/network sandbox, and the
    orchestration process holds host credentials. Until a real isolated
    executor is implemented and wired, exec graders fail closed.
    """


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

    There is no isolated execution environment in this codebase yet:
    calling this raises :class:`ExecIsolationUnavailableError` instead
    of silently grading in-process. Suites must declare pure graders
    for the first closed loop (HARBOR_DSH_E2E_LINUX.md §11.3).
    """
    raise ExecIsolationUnavailableError(
        f"exec grader {grader.grader.id}@{grader.grader.version} refused: no "
        "isolated execution environment is implemented; declare the grader "
        "as pure"
    )


def _check_returned_identity(resolved: ResolvedGrader, result: GradeResult) -> None:
    """The result must be attributed to the grader that produced it."""
    if (
        result.grader_id != resolved.grader.id
        or result.grader_version != resolved.grader.version
    ):
        raise GraderIdentityError(
            f"grader {resolved.grader.id}@{resolved.grader.version} returned a "
            f"result attributed to {result.grader_id}@{result.grader_version}"
        )
    if result.veto != resolved.veto:
        raise GraderIdentityError(
            f"grader {resolved.grader.id}@{resolved.grader.version} returned "
            f"veto={result.veto} but the suite declared veto={resolved.veto}"
        )


async def grade_trial(
    record: TrialRecord,
    graders: Sequence[ResolvedGrader],
) -> list[GradeResult]:
    """Run every grader against the sealed record.

    Required-field checks run first: a grader whose evidence is
    missing gets a cannot_judge result without ever executing — a
    crashed grader and an unjudgeable grader must never be confused.
    Every returned result is re-attributed to its grader's verified
    identity; mismatches raise :class:`GraderIdentityError`.
    """
    results: list[GradeResult] = []
    for resolved in graders:
        missing = cannot_judge_for_missing_fields(
            _ct_of(record),
            resolved.requires_fields,
            resolved.grader.id,
            getattr(resolved.grader, "version", "unknown"),
            resolved.grader.layer,
            veto=resolved.veto,
        )
        if missing is not None:
            results.append(missing)
            continue
        if resolved.execution == "pure":
            result = await execute_pure_grader(resolved.grader, record)
        else:
            result = await execute_exec_grader(resolved, record)
        _check_returned_identity(resolved, result)
        results.append(result)
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
    # A minimal ATIF shell: field checks only need the extra metadata. The agent
    # identity comes from the record — it used to be hardcoded to "dsh", so every
    # archived grading of every adapter was labelled dsh.
    from harbor.models.trajectories import Agent, Step, Trajectory

    adapter = getattr(record, "adapter", None)
    atif = Trajectory(
        agent=Agent(
            name=getattr(adapter, "id", None) or "unknown",
            version=getattr(adapter, "version", None) or "unknown",
        ),
        steps=[Step(step_id=1, source="system", message="(stored record)")],
        extra=extra,
    )
    ct = CanonicalTranscript(
        atif=atif,
        stop_reason=record.stop_reason,
        completeness=completeness,
    )
    return ct
