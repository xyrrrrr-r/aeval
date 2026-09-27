"""Run aggregation and reporting (plan §6).

Every number in the report is traceable: evidence refs, versions, the
denominator, comparability, recompute level. Session content never
leaks into reports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence

from aeval.bundle.attestation import ComparabilityReport, compare_manifests
from aeval.contracts import ExclusionSummary, RunManifest, TrialRecord, Verdict
from aeval.metrics.reliability import (
    EXCLUSION_RATE_LIMIT,
    cost_per_pass,
    exclusion_summary,
    pass_pow_k,
    valid_trials,
)

__all__ = [
    "RunSummary",
    "aggregate_run",
    "render_static_report",
    "export_jsonl",
]


@dataclass
class RunSummary:
    run_ids: list[str]
    total_trials: int = 0
    valid_trials: int = 0
    passes: int = 0
    fails: int = 0
    pass_at_k: float | None = None
    pass_pow_k_value: float | None = None
    k: int | None = None
    cost_per_pass: float | None = None
    exclusion: ExclusionSummary | None = None
    exclusion_rate_flagged: bool = False
    comparability: ComparabilityReport | None = None
    verdict_counts: dict[str, int] = field(default_factory=dict)


def aggregate_run(
    run_ids: Sequence[str],
    trials: Iterable[TrialRecord],
    *,
    k: int | None = None,
    manifests: Sequence[RunManifest] | None = None,
) -> RunSummary:
    trials = list(trials)
    valid = valid_trials(trials)
    passes = sum(1 for t in valid if t.verdict == "pass")
    fails = sum(1 for t in valid if t.verdict == "fail")
    summary = RunSummary(
        run_ids=list(run_ids),
        total_trials=len(trials),
        valid_trials=len(valid),
        passes=passes,
        fails=fails,
        k=k,
        cost_per_pass=cost_per_pass(trials),
    )
    counts: dict[str, int] = {}
    for t in trials:
        # None is not cannot_judge: an unclassified trial is its own
        # display class so reports cannot launder missing verdicts.
        verdict = t.verdict if t.verdict is not None else "unfinalized"
        counts[verdict] = counts.get(verdict, 0) + 1
    summary.verdict_counts = counts

    exclusion = exclusion_summary(trials)
    summary.exclusion = exclusion
    summary.exclusion_rate_flagged = (
        exclusion.total > 0 and exclusion.exclusion_rate > EXCLUSION_RATE_LIMIT
    )

    if k is not None and valid:
        summary.pass_pow_k_value = pass_pow_k(passes, len(valid), k)
        # pass@k (at least one of k passes) = 1 - C(n-p, k)/C(n, k).
        import math

        if len(valid) - passes >= k:
            summary.pass_at_k = (
                1.0
                - math.comb(len(valid) - passes, k) / math.comb(len(valid), k)
            )
        else:
            summary.pass_at_k = 1.0

    if manifests and len(manifests) >= 2:
        summary.comparability = compare_manifests(manifests[0], manifests[1])
    return summary


def render_static_report(
    summary: RunSummary,
    comparison: ComparabilityReport | None = None,
) -> str:
    comparison = comparison or summary.comparability
    lines = [
        "# aeval run report",
        "",
        f"- runs: {', '.join(summary.run_ids)}",
        f"- trials: {summary.total_trials} total, {summary.valid_trials} valid "
        f"denominator",
        f"- verdicts: {json.dumps(summary.verdict_counts, sort_keys=True)}",
        f"- passes: {summary.passes}  fails: {summary.fails}",
    ]
    if summary.k is not None:
        if summary.pass_at_k is not None:
            lines.append(f"- pass@{summary.k}: {summary.pass_at_k:.4f}")
        if summary.pass_pow_k_value is not None:
            lines.append(f"- pass^{summary.k}: {summary.pass_pow_k_value:.4f}")
    if summary.cost_per_pass is not None:
        lines.append(f"- cost per pass: {summary.cost_per_pass:.1f} tokens")
    else:
        lines.append("- cost per pass: unavailable (no usable cost evidence)")
    if summary.exclusion is not None:
        lines.append(
            f"- exclusions: {json.dumps(summary.exclusion.excluded, sort_keys=True)}"
        )
        rate = summary.exclusion.exclusion_rate
        flag = " FLAGGED" if summary.exclusion_rate_flagged else ""
        lines.append(f"- exclusion rate: {rate:.1%}{flag}")
        if summary.exclusion_rate_flagged:
            lines.append(
                "  WARNING: exclusion rate above "
                f"{EXCLUSION_RATE_LIMIT:.0%} — this run's scores are not "
                "trustworthy as a standalone signal"
            )
    if comparison is not None:
        if comparison.comparable:
            lines.append("- comparability: comparable")
        else:
            lines.append(
                f"- comparability: NOT comparable — {comparison.first_difference()}"
            )
    lines.append("")
    lines.append(
        "Every score is traceable to sealed evidence, grader versions, the "
        "valid denominator and the runtime lock; see run_manifest.json."
    )
    return "\n".join(lines)


def export_jsonl(trials: Iterable[TrialRecord]) -> Iterator[str]:
    """JSONL export: one record per line, session content excluded.

    The export carries ids, verdicts, stop reasons, requirement
    bitmaps, grader results and artifact refs — everything needed to
    audit a score, nothing that leaks prompt/response bodies.
    """
    for record in trials:
        payload = {
            "trial_id": record.trial_id,
            "coordinates": record.coordinates.model_dump(),
            "stop_reason": record.stop_reason,
            "baseline_ok": record.baseline_ok,
            "requirements": record.requirements.to_dict(),
            "verdict": record.verdict,
            "observed_model": (
                record.observed_model.model_dump(exclude_none=True)
                if record.observed_model
                else None
            ),
            "claim": (
                record.claim.model_dump(exclude_none=True) if record.claim else None
            ),
            "grades": [
                g.model_dump(mode="json", exclude_none=True) for g in record.grades
            ],
            "artifacts": {
                k: v.model_dump() for k, v in record.artifacts.items()
            },
            "versions": (
                record.versions.model_dump(exclude_none=True)
                if record.versions
                else None
            ),
        }
        yield json.dumps(payload, ensure_ascii=False, sort_keys=True)
