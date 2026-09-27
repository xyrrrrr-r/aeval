"""Grading pipeline (P0-7): evidence in, classified record out.

This is the production path the plugin drives once a trial's evidence
gate has passed: load the suite's versioned graders, grade the sealed
record, validate returned identities and veto declarations, decide the
final verdict, and persist record + grades in one atomic transaction.

Fail-closed rules:

- a grader that cannot be loaded, crashes, or returns a mismatched
  identity classifies the trial ``infra_invalid`` — never a lucky pass;
- ``judge_finished`` is only marked after every grader returned a
  validated result;
- missing required transcript fields short-circuit to ``cannot_judge``
  without executing the grader (handled by ``grade_trial``).
"""

from __future__ import annotations

from importlib.metadata import version as _dist_version
from pathlib import Path
from typing import Any

from aeval.contracts import (
    ArtifactRef,
    EvidenceBundle,
    GradeResult,
    RequirementBitmap,
    TrialCoordinates,
    TrialRecord,
    VersionsBundle,
)
from aeval.suite_models import ResolvedSuite
from aeval.store.sqlite import StoreConflictError, TrialStore
from aeval.verdict.base import ResolvedGrader, decide_final_verdict
from aeval.verdict.executor import grade_trial
from aeval.verdict.loader import GraderLoadError, load_grader, split_impl
from aeval.verdict.progress import RequirementProgress

__all__ = [
    "GradingPipelineError",
    "load_suite_graders",
    "build_trial_record",
    "grade_and_record",
]


class GradingPipelineError(RuntimeError):
    """The grading pipeline could not produce a valid record."""


def load_suite_graders(suite: ResolvedSuite) -> list[ResolvedGrader]:
    """Load every declared grader against the suite root, fail-closed."""
    root = Path(suite.suite_dir)
    graders: list[ResolvedGrader] = []
    for declared in suite.overlay.verdict.resolved_graders():
        reference, _declared_version = split_impl(declared.impl)
        graders.append(load_grader(root / reference, declared))
    return graders


def build_trial_record(
    *,
    trial_id: str,
    coordinates: TrialCoordinates,
    stop_reason: str,
    baseline_ok: bool,
    progress: RequirementProgress,
    artifacts: dict[str, ArtifactRef],
    transcript_extra: dict[str, Any] | None,
    grader_versions: dict[str, str],
) -> TrialRecord:
    """Assemble the sealed TrialRecord from verified stage facts.

    The requirements bitmap comes from the staged progress tracker —
    never a default-true copy. Grading results and the verdict are added
    by :func:`grade_and_record` after grading actually ran.
    """
    return TrialRecord(
        trial_id=trial_id,
        coordinates=coordinates,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        baseline_ok=baseline_ok,
        requirements=progress.snapshot(),
        artifacts=dict(artifacts),
        transcript_extra=transcript_extra,
        versions=VersionsBundle(
            aeval_version=_dist_version("aeval"),
            grader_versions=dict(grader_versions),
        ),
    )


async def grade_and_record(
    *,
    suite: ResolvedSuite,
    trial_id: str,
    coordinates: TrialCoordinates,
    stop_reason: str,
    baseline_ok: bool,
    progress: RequirementProgress,
    evidence: EvidenceBundle,
    transcript_extra: dict[str, Any] | None,
    store: TrialStore,
) -> TrialRecord:
    """Grade one sealed trial and atomically persist record + grades.

    On a grader-side failure the trial is classified ``infra_invalid``
    with ``judge_finished`` unset and still persisted (an unrecorded
    failure is worse than a recorded invalid one). A store conflict
    propagates to the caller — the store stays clean, never half full.
    """
    try:
        graders = load_suite_graders(suite)
    except GraderLoadError as exc:
        record = build_trial_record(
            trial_id=trial_id,
            coordinates=coordinates,
            stop_reason="infra_error",
            baseline_ok=baseline_ok,
            progress=progress,
            artifacts=evidence.artifacts,
            transcript_extra=transcript_extra,
            grader_versions={},
        )
        record.verdict = "infra_invalid"
        record.grades = []
        store.persist_trial_with_grades(record, [])
        raise GradingPipelineError(f"grader loading failed: {exc}") from exc

    record = build_trial_record(
        trial_id=trial_id,
        coordinates=coordinates,
        stop_reason=stop_reason,
        baseline_ok=baseline_ok,
        progress=progress,
        artifacts=evidence.artifacts,
        transcript_extra=transcript_extra,
        grader_versions={g.grader.id: g.grader.version for g in graders},
    )

    try:
        results = await grade_trial(record, graders)
    except Exception as exc:
        # A crashing or lying grader is our infrastructure fault.
        record.stop_reason = "infra_error"
        record.verdict = "infra_invalid"
        record.grades = []
        store.persist_trial_with_grades(record, [])
        raise GradingPipelineError(f"grader execution failed: {exc}") from exc

    progress.mark_judging_finished()
    record.requirements = progress.snapshot()
    record.grades = results
    record.verdict = decide_final_verdict(results)
    try:
        store.persist_trial_with_grades(record, results)
    except StoreConflictError:
        raise
    return record
