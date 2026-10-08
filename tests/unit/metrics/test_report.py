"""Aggregation and report tests: traceable numbers only."""

from __future__ import annotations

import json

from aeval.contracts import TrialRecord
from aeval.metrics.report import aggregate_run, export_jsonl, render_static_report


def _trial(trial_id, index, *, verdict="pass", stop="agent_exit_0", tokens=None,
           task="task"):
    return TrialRecord(
        trial_id=trial_id,
        coordinates={"run_id": "r1", "suite_id": "s", "suite_version": "1",
                     "task_id": task, "trial_index": index},
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
    assert "不可用" in text
    assert "警告" in text  # 排除率超标
    assert "40.0%" in text


def test_report_comparable_and_not_comparable():
    summary = aggregate_run(["r1"], _mixed_trials())
    comparable_text = render_static_report(summary)
    assert "可比性" not in comparable_text  # 未提供对比

    from aeval.bundle.attestation import ComparabilityReport

    not_comparable = ComparabilityReport({"overlay": ["digest a != digest b"]})
    text = render_static_report(summary, comparison=not_comparable)
    assert "不可比" in text
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


# --- per-task roll-up (integration) -------------------------------------


def _multi_task_trials():
    return [
        _trial("a0", 0, task="identity-intro", verdict="pass"),
        _trial("a1", 1, task="identity-intro", verdict="fail"),
        _trial("b0", 0, task="secret-guard", verdict="pass"),
        _trial("b1", 1, task="secret-guard", verdict="pass"),
        _trial("c0", 0, task="noise-resilience",
               stop="infra_error", verdict="infra_invalid"),
    ]


def test_aggregate_run_groups_by_task_with_valid_denominators():
    summary = aggregate_run(["r1"], _multi_task_trials(), k=2)
    assert set(summary.task_groups) == {
        "identity-intro", "secret-guard", "noise-resilience",
    }
    intro = summary.task_groups["identity-intro"]
    assert (intro.total_trials, intro.valid_trials, intro.passes, intro.fails) == (
        2, 2, 1, 1,
    )
    # pass^2 over 1-of-2 valid: C(1,2)/C(2,2) = 0
    assert intro.pass_pow_k_value == 0.0
    guard = summary.task_groups["secret-guard"]
    assert guard.passes == 2 and guard.pass_pow_k_value == 1.0
    # the excluded trial is counted as a task total, never as a valid one
    noise = summary.task_groups["noise-resilience"]
    assert (noise.total_trials, noise.valid_trials, noise.passes) == (1, 0, 0)
    assert noise.pass_pow_k_value is None
    # the run-level numbers are unchanged by the grouping
    assert summary.total_trials == 5 and summary.valid_trials == 4


def test_report_renders_the_per_task_table_only_when_it_informs():
    multi = aggregate_run(["r1"], _multi_task_trials(), k=2)
    text = render_static_report(multi)
    assert "## 按任务结果" in text
    assert "| identity-intro | 2 | 2 | 1 | 1 | 0.0000 |" in text
    assert "| secret-guard | 2 | 2 | 2 | 0 | 1.0000 |" in text
    assert "| noise-resilience | 1 | 0 | 0 | 0 | - |" in text

    single = aggregate_run(["r1"], _mixed_trials(), k=2)  # all one task
    assert "## 按任务结果" not in render_static_report(single)


def test_pass_pow_k_is_omitted_when_exclusions_shrink_the_pool_below_k():
    """Regression (found running the offline dsh × tbench chain): a task
    that lost an attempt to an exclusion has fewer valid trials than k —
    pass^k over k-subsets is not computable, so it is omitted. Never a
    fabricated 0, and never a crash from pass_pow_k's strict guard."""
    trials = [
        _trial("a0", 0, task="hello-world", verdict="pass"),
        _trial("a1", 1, task="hello-world", verdict="pass"),
        _trial("b0", 0, task="openssl", verdict="pass"),
        _trial("b1", 1, task="openssl",
               stop="infra_error", verdict="infra_invalid"),
    ]
    summary = aggregate_run(["r1"], trials, k=2)
    assert summary.task_groups["hello-world"].pass_pow_k_value == 1.0
    # 1 valid < k=2: the group value is omitted, not 0
    assert summary.task_groups["openssl"].pass_pow_k_value is None
    assert summary.valid_trials == 3
    # run level: still computable here (3 valid >= k=2)
    assert summary.pass_pow_k_value is not None

    # and a whole run smaller than k omits the run-level value too
    tiny = aggregate_run(["r1"], trials[:1], k=2)
    assert tiny.valid_trials == 1
    assert tiny.pass_pow_k_value is None


def test_report_renders_task_titles_from_the_manifest():
    """清单里封存的中文任务名进按任务表：「标题(task_id)」对照原值；
    没有标题的任务回退原 id。标题是显示层——不参与任何分母/分数。"""
    from aeval.agents.dsh.release import build_official_dsh_lock
    from aeval.contracts import OverlayIdentity, RunManifest, VersionsBundle
    from aeval.provenance import build_runtime_lock

    manifest = RunManifest(
        run_id="r1",
        runtime_lock=build_runtime_lock(
            release_locks={"dsh": build_official_dsh_lock()}
        ),
        overlay=OverlayIdentity(
            suite_id="s", suite_version="1", overlay_digest="d" * 64,
            source_commit="9" * 40,
        ),
        versions=VersionsBundle(aeval_version="0.1.0"),
        task_titles={"identity-intro": "身份认知"},
    )
    summary = aggregate_run(["r1"], _multi_task_trials(), k=2, manifests=[manifest])
    assert summary.task_titles == {"identity-intro": "身份认知"}
    text = render_static_report(summary)
    assert "| 身份认知(identity-intro) | 2 | 2 | 1 | 1 | 0.0000 |" in text
    assert "| secret-guard | 2 | 2 | 2 | 0 | 1.0000 |" in text  # 无标题 → 原 id


def test_report_groups_tasks_by_category_with_pooled_pass_pow_k():
    """按类别聚合：类别汇总表（合并试次、同一组合估计的类别级
    pass^k）+ 明细表按类别分组不再平铺；无点分前缀的任务归 default。
    无类别声明时保持平铺（向后兼容）。"""
    from aeval.agents.dsh.release import build_official_dsh_lock
    from aeval.contracts import OverlayIdentity, RunManifest, VersionsBundle
    from aeval.provenance import build_runtime_lock

    def _manifest(**extra):
        return RunManifest(
            run_id="r1",
            runtime_lock=build_runtime_lock(
                release_locks={"dsh": build_official_dsh_lock()}
            ),
            overlay=OverlayIdentity(
                suite_id="s", suite_version="1", overlay_digest="d" * 64,
                source_commit="9" * 40,
            ),
            versions=VersionsBundle(aeval_version="0.1.0"),
            **extra,
        )

    trials = [
        _trial("a0", 0, task="memory.store_recall", verdict="pass"),
        _trial("a1", 1, task="memory.store_recall", verdict="fail"),
        _trial("a2", 2, task="memory.store_recall", verdict="pass"),
        _trial("b0", 0, task="memory.update", verdict="pass"),
        _trial("b1", 1, task="memory.update", verdict="pass"),
        _trial("b2", 2, task="memory.update", verdict="pass"),
        _trial("c0", 0, task="identity-intro", verdict="pass"),
        _trial("c1", 1, task="identity-intro", verdict="pass"),
        _trial("c2", 2, task="identity-intro", verdict="pass"),
    ]
    manifest = _manifest(
        task_titles={"memory.store_recall": "取货码召回"},
        category_names={"memory": "跨会话记忆", "intelligence": "会话智能度"},
        default_category="intelligence",
    )
    summary = aggregate_run(["r1"], trials, k=3, manifests=[manifest])
    text = render_static_report(summary)

    # 类别汇总表：memory 2 任务 6 试 5 过 1 败，pass^3 = C(5,3)/C(6,3)。
    assert "## 按类别结果" in text
    assert "| 跨会话记忆(memory) | 2 | 6 | 6 | 5 | 1 | 0.5000 |" in text
    assert "| 会话智能度(intelligence) | 1 | 3 | 3 | 3 | 0 | 1.0000 |" in text
    # 明细表按类别分组：类别分隔行 + 缩进任务行，不再平铺。
    assert "## 按任务结果（按类别分组）" in text
    assert "| **跨会话记忆(memory)** | | | | | |" in text
    assert "| · 取货码召回(memory.store_recall) | 3 | 3 | 2 | 1 | 0.0000 |" in text
    # 无点分前缀的任务归 default（identity-intro → intelligence）。
    assert "| · identity-intro | 3 | 3 | 3 | 0 | 1.0000 |" in text

    # 无类别声明 → 平铺（旧清单/未声明套件的向后兼容）。
    flat = render_static_report(
        aggregate_run(["r1"], trials, k=3, manifests=[_manifest()])
    )
    assert "## 按类别结果" not in flat
    assert "## 按任务结果\n" in flat
