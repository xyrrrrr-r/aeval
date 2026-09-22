"""Aggregation and report tests (plan §7 row 13): traceable numbers only."""

from __future__ import annotations

import json

from aeval.contracts import TrialRecord
from aeval.metrics.report import aggregate_run, export_jsonl, render_static_report


def _trial(trial_id, index, *, verdict="pass", stop="agent_exit_0", tokens=None):
    return TrialRecord(
        trial_id=trial_id,
        coordinates={"run_id": "r1", "suite_id": "s", "suite_version": "1",
                     "task_id": "task", "trial_index": index},
        stop_reason=stop,
        verdict=verdict,
    )


def _mixed_trials():
    return [
        _trial("t0", 0, verdict="pass"),
        _trial("t1", 1, verdict="pass"),
        _trial("t2", 2, verdict="fail"),
        _trial("t3", 3, verdict="cannot_judge"),
        _trial("t4", 4, stop="infra_error", verdict="infra_invalid"),
    ]


def test_aggregate_run_counts_and_reliability():
    summary = aggregate_run(["r1"], _mixed_trials(), k=2)
    assert summary.total_trials == 5
    assert summary.valid_trials == 3  # cannot_judge + infra excluded
    assert summary.passes == 2
    assert summary.fails == 1
    assert summary.verdict_counts == {
        "pass": 2, "fail": 1, "cannot_judge": 1, "infra_invalid": 1,
    }
    # pass@2 = 1 - C(1,2)/C(3,2) — C(1,2)=0 → always at least one pass
    assert summary.pass_at_k == 1.0
    # pass^2 = C(2,2)/C(3,2) = 1/3
    assert summary.pass_pow_k_value == 1 / 3
    assert summary.exclusion_rate_flagged is True  # 2/5 = 40% > 5%


def test_aggregate_run_unflagged_below_limit():
    trials = [_trial(f"t{i}", i) for i in range(20)] + [_trial("bad", 20, verdict="cannot_judge")]
    summary = aggregate_run(["r1"], trials, k=5)
    assert summary.exclusion_rate_flagged is False  # 1/21 < 5%


def test_aggregate_run_cost_none_without_budgets():
    summary = aggregate_run(["r1"], _mixed_trials(), k=2)
    assert summary.cost_per_pass is None


def test_report_shows_both_pass_metrics_and_unavailable_cost():
    summary = aggregate_run(["r1"], _mixed_trials(), k=2)
    text = render_static_report(summary)
    assert "pass@2" in text
    assert "pass^2" in text
    assert "unavailable" in text
    assert "WARNING" in text  # exclusion rate flagged
    assert "40.0%" in text


def test_report_comparable_and_not_comparable():
    summary = aggregate_run(["r1"], _mixed_trials())
    comparable_text = render_static_report(summary)
    assert "comparability" not in comparable_text  # no comparison given

    from aeval.bundle.attestation import ComparabilityReport

    not_comparable = ComparabilityReport({"overlay": ["digest a != digest b"]})
    text = render_static_report(summary, comparison=not_comparable)
    assert "NOT comparable" in text
    assert "overlay" in text


def test_export_jsonl_is_line_delimited_and_traceable():
    lines = list(export_jsonl(_mixed_trials()))
    assert len(lines) == 5
    for line in lines:
        payload = json.loads(line)
        assert {"trial_id", "coordinates", "stop_reason", "verdict"} <= set(payload)


def test_export_jsonl_never_leaks_session_content():
    trials = _mixed_trials()
    # simulate stored transcript content on the record
    trials[0].transcript_extra = {
        "aeval": {"completeness": {"fields": []}},
        "dsh": {"header": {"secret-session-body": "prompt text"}},
    }
    blob = "\n".join(export_jsonl(trials))
    assert "transcript" not in blob
    assert "secret-session-body" not in blob
    assert "prompt text" not in blob
