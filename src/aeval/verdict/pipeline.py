"""Grading pipeline: evidence in, classified record out.

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
    AdapterSpec,
    BudgetSnapshot,
    ArtifactRef,
    EvidenceBundle,
    GradeResult,
    RequirementBitmap,
    TrialCoordinates,
    TrialRecord,
    VersionsBundle,
)
from aeval.hooks.evidence import evaluate_requirements
from aeval.suite_models import ResolvedSuite
from aeval.store.sqlite import StoreConflictError, TrialStore
from aeval.verdict.base import ResolvedGrader, decide_final_verdict
from aeval.verdict.executor import grade_trial
from aeval.verdict.loader import GraderLoadError, load_grader, split_impl
from aeval.verdict.progress import RequirementProgress

__all__ = [
    "GradingPipelineError",
    "logical_artifacts",
    "load_suite_graders",
    "build_trial_record",
    "grade_and_record",
]


class GradingPipelineError(RuntimeError):
    """The grading pipeline could not produce a valid record."""


def logical_artifacts(evidence: EvidenceBundle) -> dict[str, ArtifactRef]:
    """Re-key sealed artifacts by their logical collect names.

    The evidence bundle keys by on-disk path (fixed-path discipline);
    the grader-facing contract keys by LOGICAL name — ``observable:result``,
    ``canonical_transcript``, … — so versioned suite graders stay
    decoupled from the trial-dir layout. The collection manifest is the
    only authoritative path→name mapping: an artifact it does not name,
    or a path it maps twice, fails closed instead of being graded under
    a guessed key.

    A bundle without a manifest (direct API use; production bundles
    always carry one — the evidence gate enforces it) passes through
    with its caller-authored keys unchanged.
    """
    manifest = evidence.collection_manifest
    if manifest is None:
        return dict(evidence.artifacts)
    by_path: dict[str, str] = {}
    for outcome in manifest.outcomes:
        if outcome.output_path in by_path:
            raise GradingPipelineError(
                f"collection manifest maps {outcome.output_path!r} twice — "
                "logical artifact names are ambiguous"
            )
        by_path[outcome.output_path] = outcome.name
    logical: dict[str, ArtifactRef] = {}
    for path, ref in evidence.artifacts.items():
        name = by_path.get(path)
        if name is None:
            raise GradingPipelineError(
                f"sealed artifact {path!r} has no logical name in the "
                "collection manifest — refusing to grade under a guessed key"
            )
        logical[name] = ref
    return logical


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
    artifact_base: str | None = None,
    adapter: AdapterSpec | None = None,
) -> TrialRecord:
    """Assemble the sealed TrialRecord from verified stage facts.

    The requirements bitmap comes from the staged progress tracker —
    never a default-true copy. Grading results and the verdict are added
    by :func:`grade_and_record` after grading actually ran.
    ``artifact_base`` is the runtime-only directory the sealed artifact
    paths resolve against (trajectory graders read the sealed canonical
    transcript from it); the store never persists it.
    ``adapter`` is the observed agent identity for this trial (which adapter
    actually produced it); absent means the record cannot say.
    """
    record = TrialRecord(
        trial_id=trial_id,
        coordinates=coordinates,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        baseline_ok=baseline_ok,
        requirements=progress.snapshot(),
        artifact_base=artifact_base,
        artifacts=dict(artifacts),
        transcript_extra=transcript_extra,
        versions=VersionsBundle(
            aeval_version=_dist_version("aeval-harbor"),
            grader_versions=dict(grader_versions),
        ),
        adapter=adapter,
    )
    # The enforcement point is a fact about this trial: where spend was actually
    # enforced. Usage numbers stay None unless measured — an invented cost is
    # worse than an unavailable one (reliability metrics skip such records).
    if adapter is not None:
        record.budget = BudgetSnapshot(enforcement_point=adapter.budget_enforcement)
    return record


def _note_requirement_shortfall(
    extra: dict[str, Any] | None, unmet: list[str]
) -> dict[str, Any]:
    """Record which required facts were unmet, under the record's audit extra.

    Merges rather than replaces: whatever the trial already recorded stays
    readable, and the shortfall is added beside it so a reader of the sealed
    record can see *why* the verdict is ``cannot_judge``.
    """
    merged: dict[str, Any] = dict(extra or {})
    audit = merged.get("aeval")
    audit = dict(audit) if isinstance(audit, dict) else {}
    audit["requirement_shortfall"] = list(unmet)
    merged["aeval"] = audit
    return merged


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
    artifact_base: str | None = None,
    adapter: AdapterSpec | None = None,
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
            artifacts=logical_artifacts(evidence),
            transcript_extra=transcript_extra,
            grader_versions={},
            artifact_base=artifact_base,
            adapter=adapter,
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
        artifacts=logical_artifacts(evidence),
        transcript_extra=transcript_extra,
        grader_versions={g.grader.id: g.grader.version for g in graders},
        artifact_base=artifact_base,
        adapter=adapter,
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
    # The sealed bitmap: the stage bits accumulated during the trial, finalized
    # here (judge_finished can only be set by this pipeline, and an
    # ``infra_error`` stop clears it again).
    record.requirements = evaluate_requirements(
        evidence, staged=progress.snapshot(), stop_reason=stop_reason
    )
    record.grades = results
    record.verdict = decide_final_verdict(results)
    # Requirement gate. A suite declares in ``verdict.requirements`` which facts
    # must hold for its trials to be judgeable at all. An unmet one means the
    # outcome cannot be trusted, so the trial is recorded ``cannot_judge`` — an
    # explicit, reasoned exclusion — rather than silently counting as a pass or
    # a failure. Grades are kept: the qualifying evidence is not thrown away,
    # only the verdict it would have produced. ``infra_invalid`` is left alone
    # (the run itself is soured, which the seal reports separately).
    unmet = record.requirements.missing(tuple(suite.overlay.verdict.requirements))
    if unmet and record.verdict != "infra_invalid":
        record.verdict = "cannot_judge"
        record.transcript_extra = _note_requirement_shortfall(
            record.transcript_extra, unmet
        )
    try:
        store.persist_trial_with_grades(record, results)
    except StoreConflictError:
        raise
    return record
