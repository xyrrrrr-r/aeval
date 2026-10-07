"""轨迹采集进报告/面板 + Trajectory 面板渲染测试。"""

from __future__ import annotations

from aeval.contracts import TrialRecord
from aeval.metrics.dashboard import render_dashboard_html
from aeval.metrics.report import aggregate_run, render_static_report
from aeval.metrics.trajectory_panel import (
    TrialPanel,
    render_trajectory_html,
)
from tests.unit.verdict.test_stats import sample_record, sample_stats


def _record(trial_id: str, index: int, task: str, verdict: str = "pass"):
    return TrialRecord(
        trial_id=trial_id,
        coordinates={"run_id": "r1", "suite_id": "s", "suite_version": "1",
                     "task_id": task, "trial_index": index},
        stop_reason="agent_claimed_done",
        verdict=verdict,
    )


def _summary(**kwargs):
    records = [
        _record("t1", 0, "alpha.one"),
        _record("t2", 1, "beta.two", verdict="fail"),
        _record("t9", 2, "beta.two"),
    ]
    stats = [
        sample_stats("t1", "alpha.one"),
        sample_stats("t2", "beta.two", verdict="fail"),
    ]
    return aggregate_run(
        ["r1"], records, k=1, trajectory=stats,
        trajectory_unavailable=[("t9", "试次目录缺失")],
        **kwargs,
    )


# --- run 级聚合 -----------------------------------------------------------


def test_trajectory_aggregate_numbers():
    traj = _summary().trajectory
    assert traj is not None
    assert (traj.trials_with_evidence, traj.trials_total) == (2, 3)
    assert traj.unavailable == ("试次目录缺失 ×1",)
    assert traj.total_tokens == 2 * 3730
    assert traj.total_prompt_tokens == 2 * 3650
    assert traj.total_cached_tokens == 200
    assert traj.mean_wall_seconds == 40.0
    assert traj.longest_wall == ("t1", 40.0) or traj.longest_wall == ("t2", 40.0)
    assert (traj.tool_calls, traj.tool_missing_obs, traj.tool_retries) == (4, 2, 2)
    assert traj.peak_input_tokens == 1400
    assert traj.longest_call[0] == "bash" and traj.longest_call[1] == 10.0


def test_trajectory_aggregate_by_category():
    traj = _summary().trajectory
    rows = {c.key: c for c in traj.categories}
    assert set(rows) == {"alpha", "beta"}
    assert rows["alpha"].trials == 1 and rows["beta"].trials == 1
    assert rows["alpha"].total_tokens == 3730
    assert rows["beta"].tool_calls == 2 and rows["beta"].missing_obs == 1
    # 无类别显示名声明时回退键名（不编造）。
    assert rows["alpha"].display == "alpha"


def test_backward_compatible_without_trajectory():
    """未采集轨迹 → summary.trajectory 为 None，报告不加该节。"""
    records = [_record("t1", 0, "alpha.one")]
    summary = aggregate_run(["r1"], records, k=1)
    assert summary.trajectory is None
    assert "轨迹采集" not in render_static_report(summary)
    assert "轨迹采集" not in render_dashboard_html(summary)


# --- markdown 报告 --------------------------------------------------------


def test_report_trajectory_section():
    md = render_static_report(_summary())
    assert "## 轨迹采集" in md
    assert "- 证据覆盖：2/3 个试次（不可用：试次目录缺失 ×1）" in md
    assert "- 总 Token：7,460（输入 7,300 · 输出 160 · 缓存 200）" in md
    assert "- 时长：平均 40.0s · 最长 40.0s（" in md
    assert "- 工具调用：4 次 · 无观测 2（50.0%） · 重试 2" in md
    assert "- 上下文峰值：单步最大输入 1,400 tokens（" in md
    assert "- 最长调用：bash 10.0s（" in md
    assert "| 类别 | 试次 | Token | 平均时长(s) | 工具调用 | 无观测 |" in md
    assert "| alpha(alpha) | 1 | 3,730 | 40.0 | 2 | 1 |" in md


# --- 运行面板（dashboard）轨迹带 ------------------------------------------


def test_dashboard_trajectory_strip():
    html = render_dashboard_html(_summary())
    assert "<h2>轨迹采集</h2>" in html
    assert "7,460" in html and "试次目录缺失 ×1" in html
    assert "峰值上下文" in html and "1,400" in html
    assert "<script" not in html


# --- 轨迹面板（Trajectory 视图） -------------------------------------------


def _panel(window: int | None = None, heading: str = "试次 1/1 · t1"):
    return render_trajectory_html(
        title="任务(alpha.one) 轨迹分析",
        meta_lines=["run r1 · s@1 · 1/1 个试次有可用密封轨迹"],
        trials=[
            TrialPanel(
                heading=heading,
                meta="r1 · s@1 · 任务(alpha.one)",
                stats=sample_stats("t1", "alpha.one", verdict="fail"),
            )
        ],
        context_window=window,
    )


def test_panel_sections_and_facts():
    html = _panel()
    for section in (
        "执行图谱", "工具调用", "Token 脉冲", "上下文压力",
        "工具失败与重试 · 时间消耗", "工具结果矩阵", "耗时分布（按工具）",
    ):
        assert f"<h3>{section}</h3>" in html
    # 会话信息栏事实。
    assert "失败(fail)" in html
    assert "总时长 <b>40.0s</b>" in html
    assert "总 Token <b>3,730</b>" in html
    assert "峰值上下文 <b>1,400</b>" in html
    assert "<b>1</b> live + <b>1</b> 记忆基底" in html
    # 执行图谱：记忆基底带 + 失败红点（step5 无观测）。
    assert "记忆基底" in html
    assert 'class="node failed"' in html
    assert 'class="node copied"' in html
    # Token 脉冲三段图例 + 缓存段。
    assert "缓存输入" in html and "#93c5fd" in html
    # 工具矩阵行：bash 2 次调用、1 无观测、成功率 50%。
    assert "<td>bash</td><td>2</td>" in html
    assert "<td>50%</td>" in html
    # 摘要行。
    assert "1 次无观测（50%）" in html
    assert "最长调用 bash 10.0s" in html
    assert "工具步骤耗时占比 40%" in html


def test_panel_context_window_honesty():
    """窗口只能来自调用方声明；未声明时如实说明纵轴口径。"""
    declared = _panel(window=2000)
    assert "70% 阈值" in declared and "90% 阈值" in declared
    assert "声明窗口 2,000（占用率 70%）" in declared
    plain = _panel()
    assert "70% 阈值" not in plain
    assert "证据未声明窗口——纵轴为绝对输入 Token" in plain


def test_panel_selfcontained_deterministic_escaped():
    html = _panel()
    assert "<script" not in html and "http://" not in html
    assert html == _panel()
    escaped = _panel(heading="<img src=x onerror=alert(1)>")
    assert "<img src=x" not in escaped
    assert "&lt;img src=x onerror=alert(1)&gt;" in escaped


def test_panel_toolless_trial_notes_unavailable():
    """无工具调用的试次：相关版块如实标注不可用，不编造。"""
    from harbor.models.trajectories import Agent, FinalMetrics, Trajectory

    from aeval.contracts import CanonicalTranscript
    from aeval.verdict.trajectory.base import build_evidence
    from aeval.verdict.trajectory.stats import collect_stats
    from tests.unit.verdict.test_stats import sample_steps

    transcript = Trajectory(
        agent=Agent(name="dsh", version="test"),
        # 仅一条 live 用户消息、无工具；step_id 须从 1 连续。
        steps=[sample_steps()[2].model_copy(update={"step_id": 1})],
        final_metrics=FinalMetrics(
            total_prompt_tokens=10, total_completion_tokens=2,
            total_cached_tokens=0,
        ),
    )
    ct = CanonicalTranscript(
        atif=transcript, stop_reason="agent_claimed_done"
    )
    stats = collect_stats(
        sample_record(), build_evidence(ct, "agent_claimed_done")
    )
    html = render_trajectory_html(
        title="t", meta_lines=[],
        trials=[TrialPanel("试次 1/1 · t1", "m", stats)],
    )
    assert "不可用：该试次无工具调用" in html
    assert "工具结果矩阵" in html
