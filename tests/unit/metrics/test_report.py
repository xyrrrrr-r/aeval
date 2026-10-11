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
    assert "## 终判结果" in text
    assert "| identity-intro | 2 | 2 | 1 | 1 | 0.0000 |" in text
    assert "| secret-guard | 2 | 2 | 2 | 0 | 1.0000 |" in text
    assert "| noise-resilience | 1 | 0 | 0 | 0 | - |" in text

    single = aggregate_run(["r1"], _mixed_trials(), k=2)  # all one task
    assert "## 终判结果" not in render_static_report(single)


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


def test_report_groups_tasks_by_family_only():
    """归组只有一个来源：族（v5）。没有族的套件平铺——不再按 task_id
    前缀猜类别，也不再渲染「按类别结果」表。

    族表的数字（含族级 pass^k）在 test_families 里逐位核对；
    这里守的是"没有第二条归组路"。
    """
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
    ]
    manifest = _manifest(task_titles={"memory.store_recall": "取货码召回"})
    text = render_static_report(aggregate_run(["r1"], trials, k=3,
                                             manifests=[manifest]))
    # 无族 ⇒ 没有「按类别结果」，任务表平铺（显示名仍来自密封清单）。
    assert "## 按类别结果" not in text
    assert "## 终判结果\n" in text
    assert "| 取货码召回(memory.store_recall) | 3 | 3 | 2 | 1 | 0.0000 |" in text
    assert "| memory.update | 3 | 3 | 3 | 0 | 1.0000 |" in text
    # 前缀没有变成类别：不会出现凭前缀造出来的分组标题。
    assert "| **memory(" not in text


def test_v4_sealed_run_still_renders_trajectory_metrics():
    """v4 封存模型没有族/守卫归属声明，但 store 里轨迹层判分器实际判过的
    指标必须照旧有落点：按判分层列出（轨迹层判分器 = 今天的
    classification: gate），不因声明形态换代而整批从报告里消失。
    """
    from aeval.agents.dsh.release import build_official_dsh_lock
    from aeval.contracts import (
        GradeResult,
        MetricOutcome,
        OverlayIdentity,
        RunManifest,
        Score,
        VersionsBundle,
    )
    from aeval.metrics.dashboard import render_dashboard_html
    from aeval.provenance import build_runtime_lock

    manifest = RunManifest(
        run_id="r1",
        runtime_lock=build_runtime_lock(
            release_locks={"dsh": build_official_dsh_lock()}
        ),
        overlay=OverlayIdentity(
            suite_id="tbench-pilot", suite_version="0.4.0",
            overlay_digest="d" * 64, source_commit="9" * 40,
        ),
        versions=VersionsBundle(aeval_version="0.1.0"),
        # v4 形态：只有 categories/blocks/redline_tasks —— 没有
        # families / guards / baseline / metric_catalog。
        dimension_model={
            "categories": {
                "tbench": {
                    "name": "terminal-bench 任务", "block": "core",
                    "weight": 1.0, "threshold": 0.6, "redline": False,
                }
            },
            "blocks": {"core": "任务执行"},
            "redline_tasks": [],
            "default": "tbench",
        },
    )

    def _graded(trial_id, index, task, metric_status):
        record = _trial(trial_id, index, task=task, verdict="pass")
        record.coordinates.run_id = "r1"
        record.grades.append(GradeResult(
            grader_id="tbench-outcome", grader_version="v1", layer="outcome",
            veto=False, score=Score(value=1.0), status="pass",
            reasons=[], metrics=None,
        ))
        record.grades.append(GradeResult(
            grader_id="tbench-trajectory", grader_version="v1",
            layer="trajectory", veto=True, score=Score(value=1.0),
            status="pass", reasons=[],
            metrics=[
                MetricOutcome(name="step_efficiency", category="efficiency",
                              status=metric_status),
                MetricOutcome(name="forbidden_access", category="integrity",
                              status="ok"),
            ],
        ))
        return record

    trials = [
        _graded("t0", 0, "hello-world", "ok"),
        _graded("t1", 1, "sqlite-db-truncate", "skipped"),
    ]
    summary = aggregate_run(["r1"], trials, k=2, manifests=[manifest])
    text = render_static_report(summary)
    # v4 的类别层照旧在：读时升级把 categories 合成归组族，
    # 「维度达标 / 按类别结果」不因模型换代从报告里消失。
    assert "## 维度达标" in text
    assert (
        "| terminal-bench 任务(tbench) | 任务执行 | 1 | 1.0000 | 0.6 "
        "| 1.6667 | 绿 |" in text
    )
    assert "## 按类别结果" in text
    assert (
        "| terminal-bench 任务(tbench) | 2 | 2 | 2 | 2 | 0 | - "
        "| 本族 0 / 他层 0 | 1.0000 | tbench-outcome@v1 |" in text
    )
    # 轨迹层指标按判分层补出（归属不代声明断言）。
    assert "## 逐项指标（按判分层）" in text
    assert "### 基线与轨迹层判分器（v4 封存模型：声明未记录族/守卫归属）" in text
    assert "classification: gate" in text
    assert "- 官方基线：tbench-outcome@v1" in text
    assert "- 轨迹层判分器 tbench-trajectory@v1（2 试）" in text
    # 指标列带中文展示名：中文(id)——id 仍可 grep，中文给人读。
    assert "| 步数效率(step_efficiency) | 1 | 0 | 0 | 1 |" in text
    assert "| 禁区访问(forbidden_access) | 2 | 0 | 0 | 0 |" in text
    # 同一份明细进 dashboard（两个渲染器同源）。守卫/轨迹层明细是**表格**：
    # 判分器列 rowspan 合并，一行一项指标。
    html = render_dashboard_html(summary)
    assert "<h2>按类别结果（指标族）</h2>" in html
    assert "<h2>逐项指标（按判分层）</h2>" in html
    assert '<table class="guard-metrics">' in html
    assert '<th class="gname">判分器</th><th class="mid">指标</th>' in html
    assert "轨迹层判分器 tbench-trajectory@v1<small>（2 试）</small>" in html
    assert (
        '<td class="mid">步数效率(step_efficiency)</td><td>1</td><td>0</td><td>0</td>'
        in html
    )
    assert (
        '<td class="mid">禁区访问(forbidden_access)</td><td>2</td><td>0</td><td>0</td>'
        "<td>0</td>" in html
    )
    # 这张基线的逐任务表与「终判结果」逐行同值（同试次、同通过/失败）⇒
    # 不再复列：重复一张表只会让人怀疑哪张才准。
    assert "逐任务计数与「终判结果」逐行相同" in text
    assert "| hello-world | 1 | 1 | 0 | 0 |" not in text
    assert "逐任务计数与「终判结果」逐行相同" in html


def _baseline_only_manifest(*, guards=()):
    """基线型套件（sbench 型）：一个分组式族 + 官方基线，没有自己的指标。"""
    from aeval.agents.dsh.release import build_official_dsh_lock
    from aeval.contracts import OverlayIdentity, RunManifest, VersionsBundle
    from aeval.provenance import build_runtime_lock

    return RunManifest(
        run_id="r1",
        runtime_lock=build_runtime_lock(
            release_locks={"dsh": build_official_dsh_lock()}
        ),
        overlay=OverlayIdentity(
            suite_id="sbench-offline", suite_version="0.1.0",
            overlay_digest="d" * 64, source_commit="9" * 40,
        ),
        versions=VersionsBundle(aeval_version="0.1.0"),
        dimension_model={
            "families": {
                "health": {
                    "name": "基础连通", "block": "core", "weight": 1.0,
                    "threshold": 0.6, "redline": False,
                    "engine": {"impl": "graders/outcome.py@v1",
                               "grader_id": "sbench-outcome",
                               "grader_version": "v1"},
                    "metrics": [],          # 分组式类别：没有自己的指标
                    "scope": {"health.ping": {"role": "home"}},
                },
            },
            "blocks": {"core": "核心"},
            "baseline": {"impl": "graders/outcome.py@v1",
                         "grader_id": "sbench-outcome",
                         "grader_version": "v1"},
            "guards": list(guards),
        },
    )


def _outcome_graded(trial_id, index, status, task="health.ping"):
    from aeval.contracts import GradeResult, Score

    record = _trial(trial_id, index, task=task, verdict=status)
    record.grades.append(GradeResult(
        grader_id="sbench-outcome", grader_version="v1", layer="outcome",
        veto=False, score=Score(value=1.0 if status == "pass" else 0.0),
        status=status, reasons=[], metrics=None,
    ))
    return record


def test_baseline_only_run_does_not_repeat_the_final_table():
    """基线型套件：基线逐任务表 = 「终判结果」逐行同值 ⇒ 不复列。

    sbench 型 dashboard 的实测问题：同一批任务/试次/通过/失败在「终判结果」
    和「逐项指标 → 基线与守卫」各写一遍，读者只会怀疑哪张才准。复列去掉之后
    这一节也不能静默消失——它要说清"为什么没有指标可列"（否则就是下一个
    "指标去哪了"）。
    """
    from aeval.metrics.dashboard import render_dashboard_html

    summary = aggregate_run(
        ["r1"],
        [
            _outcome_graded("t0", 0, "pass", task="health.ping"),
            _outcome_graded("t1", 1, "pass", task="health.pong"),
        ],
        k=1,
        manifests=[_baseline_only_manifest()],
    )
    detail = summary.metric_detail
    assert detail is not None and detail.baseline_guards is not None
    assert detail.baseline_guards.repeats_final is True
    text = render_static_report(summary)
    assert "## 终判结果" in text
    # 没有重复的基线表，但有"为什么空"的交代与落点指引。
    assert "## 逐项指标（本 run 无指标级判定）" in text
    assert "本套件没有声明指标：1 个族都是归组式" in text
    assert "本 run 也没有守卫 / 轨迹层判分器产出的指标判定" in text
    assert "见「按类别结果」" in text
    assert "| 任务 | 试次 | 通过 | 失败 | 无法判定 |" not in text
    # 任务名在整份报告里只出现一次（就是「终判结果」那张表）。
    assert text.count("health.pong") == 1
    html = render_dashboard_html(summary)
    assert "<h2>终判结果</h2>" in html
    assert "<h2>逐项指标（本 run 无指标级判定）</h2>" in html
    assert html.count("health.pong") == 1


def test_baseline_table_kept_when_it_differs_from_final_verdicts():
    """基线层自己的计数与终判不同 ⇒ 必须列（那是"这例失败是谁判的"的证据）。"""
    from aeval.contracts import GradeResult, MetricOutcome, Score
    from aeval.metrics.dashboard import render_dashboard_html

    def _vetoed(trial_id, index):
        record = _outcome_graded(trial_id, index, "pass", task="health.pong")
        record.verdict = "fail"
        record.grades.append(GradeResult(
            grader_id="sbench-trajectory", grader_version="v1",
            layer="trajectory", veto=True, score=Score(value=1.0),
            status="pass", reasons=[],
            metrics=[MetricOutcome(name="scope_discipline",
                                   category="integrity", status="violated")],
        ))
        return record

    summary = aggregate_run(
        ["r1"],
        [
            _outcome_graded("t0", 0, "pass", task="health.ping"),
            _vetoed("t1", 1),
        ],
        k=1,
        manifests=[
            _baseline_only_manifest(guards=[{
                "impl": "graders/tbench_trajectory.py@v1",
                "grader_id": "sbench-trajectory", "grader_version": "v1",
                "layer": "trajectory", "veto": True,
                "metrics": ["scope_discipline"],
            }]),
        ],
    )
    detail = summary.metric_detail
    assert detail is not None and detail.baseline_guards is not None
    assert detail.baseline_guards.repeats_final is False
    text = render_static_report(summary)
    assert "## 逐项指标" in text
    # 只列与终判不同值的那一行：health.pong（基线 1 通过，终判 0 通过 1 失败）；
    # 同值的 health.ping 不再复列。
    section = text.split("### 基线与守卫")[1]
    assert (
        "只列**基线层与「终判结果」不同值**的 1 行（其余 1 行同值，不复列）"
        in section
    )
    assert "| health.pong | 1 | 1 | 0 | 0 |" in section
    assert "health.ping" not in section
    assert "| 工作区范围纪律(scope_discipline) | 0 | 0 | 1 | 0 |" in section
    html = render_dashboard_html(summary)
    assert "只列<b>基线与「终判结果」不同值</b>的 1 行（其余 1 行同值，不复列）" in html
    baseline_block = html.split("官方基线：")[1].split('class="guard-metrics"')[0]
    assert "health.pong" in baseline_block and "health.ping" not in baseline_block


def test_single_task_md_keeps_baseline_rows_without_final_table():
    """单任务 run 的 markdown 不渲染「终判结果」⇒ 基线表是唯一的逐任务落点，
    一行都不能省（面板那条路有自己的终判表，照旧不重复）。"""
    from aeval.metrics.dashboard import render_dashboard_html

    summary = aggregate_run(
        ["r1"], [_outcome_graded("t0", 0, "pass")], k=1,
        manifests=[_baseline_only_manifest()],
    )
    text = render_static_report(summary)
    assert "## 终判结果" not in text
    assert "## 逐项指标" in text
    assert "| health.ping | 1 | 1 | 0 | 0 |" in text
    assert "（全部）" in text
    # 面板有「终判结果」⇒ 同值即不重复；这一节改为交代"没有指标可列"。
    html = render_dashboard_html(summary)
    assert "<h2>终判结果</h2>" in html
    assert "<h2>逐项指标（本 run 无指标级判定）</h2>" in html
    assert "本套件没有声明指标：1 个族都是归组式" in html
    assert "| 1 | 1 | 0 | 0 |" not in html.split("无指标级判定")[1]
