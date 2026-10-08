"""Reliability metrics tests: denominator discipline first."""

from __future__ import annotations

import pytest

from aeval.contracts import BudgetSnapshot, ClaimCheck, TrialRecord
from aeval.metrics.reliability import (
    EXCLUSION_RATE_LIMIT,
    cost_per_pass,
    exclusion_summary,
    pass_pow_k,
    valid_trials,
)


def _trial(trial_id="t", *, stop="agent_exit_0", baseline=True, claim=None,
           verdict="pass", tokens=None) -> TrialRecord:
    return TrialRecord(
        trial_id=trial_id,
        coordinates={"run_id": "r", "suite_id": "s", "suite_version": "1",
                     "task_id": "task", "trial_index": 0},
        stop_reason=stop,
        baseline_ok=baseline,
        claim=claim,
        verdict=verdict,
        budget=BudgetSnapshot(used_tokens=tokens) if tokens is not None else None,
    )


def _mismatch_claim() -> ClaimCheck:
    return ClaimCheck(overall="mismatch")


def _downgraded_cost_claim() -> ClaimCheck:
    return ClaimCheck(overall="consistent", cost_source_downgraded=True)


def test_valid_trials_exclude_all_four_classes():
    trials = [
        _trial("ok-pass"),
        _trial("infra", stop="infra_error"),
        _trial("baseline", baseline=False),
        _trial("mismatch", claim=_mismatch_claim()),
        _trial("cannot-judge", verdict="cannot_judge"),
        _trial("infra-verdict", verdict="infra_invalid"),
    ]
    valid = valid_trials(trials)
    assert [t.trial_id for t in valid] == ["ok-pass"]


def test_unfinalized_records_never_enter_the_denominator():
    """Defect: verdict=None (never finally classified) must be excluded.

    A record whose grading never completed — crashed pipeline, missing
    trial, interrupted run — silently counted as a judged sample before.
    """
    trials = [
        _trial("unfinalized", verdict=None),
        _trial("claimed-done-ungraded", verdict=None, stop="agent_claimed_done"),
        _trial("ok-pass"),
    ]
    valid = valid_trials(trials)
    assert [t.trial_id for t in valid] == ["ok-pass"]
    summary = exclusion_summary(trials)
    assert summary.excluded.get("unfinalized") == 2
    assert summary.excluded_trial_ids["unfinalized"] == [
        "unfinalized", "claimed-done-ungraded",
    ]


def test_unfinalized_exclusion_composes_with_other_classes():
    trials = [_trial("infra-and-unfinalized", stop="infra_error", verdict=None)]
    summary = exclusion_summary(trials)
    assert summary.excluded == {"unfinalized": 1, "infra_invalid": 1}
    assert summary.valid == 0


def test_exclusion_classes_deduped_per_trial():
    # one trial qualifying twice for infra_invalid counts once
    trials = [_trial("double", stop="infra_error", verdict="infra_invalid")]
    summary = exclusion_summary(trials)
    assert summary.excluded == {"infra_invalid": 1}
    assert summary.excluded_trial_ids["infra_invalid"] == ["double"]
    assert summary.valid == 0


def test_exclusion_summary_counts_each_class():
    trials = [
        _trial("a", stop="infra_error"),
        _trial("b", baseline=False),
        _trial("c", claim=_mismatch_claim()),
        _trial("d", verdict="cannot_judge"),
        _trial("e"),
    ]
    summary = exclusion_summary(trials)
    assert summary.total == 5
    assert summary.valid == 1
    assert summary.excluded == {
        "infra_invalid": 1, "baseline_failed": 1,
        "claim_mismatch": 1, "cannot_judge": 1,
    }
    assert 0 < summary.exclusion_rate < 1


def test_pass_pow_k_exact_combinatorics():
    # C(2,2)/C(4,2) = 1/6
    assert pass_pow_k(2, 4, 2) == pytest.approx(1 / 6)
    # C(3,2)/C(4,2) = 3/6
    assert pass_pow_k(3, 4, 2) == pytest.approx(0.5)
    assert pass_pow_k(4, 4, 2) == 1.0


def test_pass_pow_k_fewer_passes_than_k_is_zero():
    assert pass_pow_k(1, 5, 2) == 0.0
    assert pass_pow_k(0, 5, 1) == 0.0


@pytest.mark.parametrize("passes,total,k", [(1, 3, 5), (2, 3, 0), (2, 3, -1), (4, 3, 2)])
def test_pass_pow_k_invalid_inputs_raise(passes, total, k):
    with pytest.raises(ValueError):
        pass_pow_k(passes, total, k)


def test_cost_per_pass_mean_over_passing_valid():
    trials = [
        _trial("p1", tokens=100),
        _trial("p2", tokens=300),
        _trial("f", verdict="fail", tokens=1000),  # failing trials don't dilute
    ]
    assert cost_per_pass(trials) == pytest.approx(200.0)


def test_cost_per_pass_none_when_no_usable_cost():
    assert cost_per_pass([]) is None
    assert cost_per_pass([_trial("no-budget")]) is None
    assert cost_per_pass([_trial("zero", tokens=0)]) is None


def test_cost_per_pass_skips_downgraded_cost_source():
    trials = [
        _trial("downgraded", claim=_downgraded_cost_claim(), tokens=100),
        _trial("clean", tokens=200),
    ]
    assert cost_per_pass(trials) == pytest.approx(200.0)


def test_cost_per_pass_excludes_invalid_trials():
    trials = [
        _trial("infra", stop="infra_error", tokens=10),
        _trial("valid", tokens=20),
    ]
    assert cost_per_pass(trials) == pytest.approx(20.0)


def test_exclusion_rate_limit_is_five_percent():
    assert EXCLUSION_RATE_LIMIT == 0.05
