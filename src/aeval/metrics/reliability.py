"""Reliability and cost metrics (plan §6): denominator discipline first.

valid_trials excludes infra_invalid, baseline failures, claim
mismatches and cannot_judge — an exclusion rate > 5% flags the whole
run as untrustworthy (the report says so, it does not hide it).
"""

from __future__ import annotations

import math
from itertools import combinations
from typing import Iterable

from aeval.contracts import ExclusionSummary, TrialRecord

__all__ = [
    "valid_trials",
    "exclusion_summary",
    "pass_pow_k",
    "cost_per_pass",
    "EXCLUSION_RATE_LIMIT",
]

EXCLUSION_RATE_LIMIT = 0.05


def _excluded_reasons(record: TrialRecord) -> list[str]:
    """Unique exclusion reason classes for one trial.

    A trial can qualify for several classes (e.g. infra stop reason AND
    infra verdict); each class counts the trial once, so the summary
    reports how many trials hit each class, not how many flags fired.
    """
    reasons: list[str] = []
    for reason in (
        "infra_invalid" if record.stop_reason == "infra_error" else None,
        "baseline_failed" if not record.baseline_ok else None,
        "claim_mismatch"
        if record.claim is not None and record.claim.overall == "mismatch"
        else None,
        record.verdict if record.verdict in ("infra_invalid", "cannot_judge") else None,
    ):
        if reason is not None and reason not in reasons:
            reasons.append(reason)
    return reasons


def valid_trials(trials: Iterable[TrialRecord]) -> list[TrialRecord]:
    """The valid denominator — strict, per-verdict, never lenient."""
    return [t for t in trials if not _excluded_reasons(t)]


def exclusion_summary(trials: Iterable[TrialRecord]) -> ExclusionSummary:
    trials = list(trials)
    summary = ExclusionSummary(total=len(trials))
    valid: list[TrialRecord] = []
    for record in trials:
        reasons = _excluded_reasons(record)
        if not reasons:
            valid.append(record)
            continue
        for reason in reasons:
            summary.excluded[reason] = summary.excluded.get(reason, 0) + 1
            summary.excluded_trial_ids.setdefault(reason, []).append(record.trial_id)
    summary.valid = len(valid)
    return summary


def pass_pow_k(passes: int, total: int, k: int) -> float:
    """pass^k: probability that ALL k samples pass (plan §8.5).

    Unbiased estimator over the hypergeometric combination count:
    C(passes, k) / C(total, k). k > passes ⇒ 0; k > total is a caller
    error (the suite declared more attempts than exist).
    """
    if total < 0 or passes < 0 or passes > total:
        raise ValueError(f"invalid pass_pow_k inputs: passes={passes} total={total}")
    if k > total:
        raise ValueError(f"k={k} exceeds total={total}")
    if k <= 0:
        raise ValueError("k must be >= 1")
    if passes < k:
        return 0.0
    numerator = math.comb(passes, k)
    denominator = math.comb(total, k)
    return numerator / denominator


def cost_per_pass(trials: Iterable[TrialRecord]) -> float | None:
    """Mean cost per PASSING trial over the valid denominator.

    Returns None (never 0.0) when no passing trial has usable cost —
    an uncomputable cost must be displayed as unavailable.
    """
    valid = valid_trials(trials)
    costs: list[float] = []
    for record in valid:
        if record.verdict != "pass":
            continue
        budget = record.budget
        if budget is None or budget.used_tokens is None or budget.used_tokens <= 0:
            continue
        if record.claim is not None and record.claim.cost_source_downgraded:
            continue
        costs.append(float(budget.used_tokens))
    if not costs:
        return None
    return sum(costs) / len(costs)
