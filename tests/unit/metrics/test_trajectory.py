"""轨迹采集进报告/面板 + Trajectory 面板渲染测试。"""

from __future__ import annotations

from dataclasses import replace

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


def test_panel_renders_five_tracks_insights_and_inspector():
    html = _panel()
    assert "五轨联动执行视图" in html
    for track in ("执行图谱", "工具调用", "Token 脉冲", "输入 Token", "上下文压力"):
        assert track in html
    assert 'class="trajectory-timeline"' in html
    assert 'id="trajectory-inspector"' in html
    assert 'id="trajectory-data"' in html
    # step 4 同时有工具、Token 与输入上下文，多条轨道共用同一交互事件 ID。
    assert html.count('data-event="trial-0-step-4"') >= 4
    assert "工具无观测信号" in html
    assert "最长含工具步骤" in html
    assert "峰值上下文" in html
    assert "失败(fail)" in html
    assert "记忆基底" in html
    assert '<section class="trial"' not in html


def test_panel_uses_global_step_axis_for_invalid_time_data():
    stats = sample_stats("t1", "alpha.one", verdict="fail")
    broken = replace(stats.steps[0], seconds_from_start=None)
    html = render_trajectory_html(
        title="t", meta_lines=[],
        trials=[TrialPanel("试次 1/1 · t1", "m", replace(stats, steps=(broken, *stats.steps[1:])))],
    )
    assert '"mode":"step"' in html
    assert "已统一按步骤序对齐" in html


def test_panel_context_pressure_declares_window_boundary():
    declared = _panel(window=2000)
    assert "相对上下文窗口" in declared
    assert "70%" in declared and "90%" in declared
    plain = _panel()
    # No window: the pressure track states the gap honestly and draws no
    # threshold lines; the input-token track still labels its absolute values.
    assert "无窗口声明" in plain
    assert "相对上下文窗口" not in plain
    assert "绝对值" in plain


def test_panel_falls_back_to_the_evidence_carried_window():
    # No caller-declared flag, but the sealed transcript carries a window: the
    # panel uses it and labels the source as evidence-carried, not declared.
    html = render_trajectory_html(
        title="t", meta_lines=[],
        trials=[
            TrialPanel(
                heading="试次 1/1 · t1", meta="m",
                stats=sample_stats("t1", "alpha.one", verdict="fail"),
                context_window=2000,
            )
        ],
    )
    assert '"context_window":2000' in html
    assert '"context_window_source":"evidence"' in html
    assert "相对上下文窗口（证据携带）" in html
    assert "证据携带窗口" in html


def test_caller_declared_window_wins_over_evidence():
    html = render_trajectory_html(
        title="t", meta_lines=[],
        trials=[
            TrialPanel(
                heading="试次 1/1 · t1", meta="m",
                stats=sample_stats("t1", "alpha.one", verdict="fail"),
                context_window=2000,
            )
        ],
        context_window=4096,
    )
    assert '"context_window":4096' in html
    assert '"context_window_source":"caller"' in html
    assert "相对上下文窗口（调用方声明）" in html


def test_disagreeing_evidence_windows_are_not_collapsed():
    # Two trials carrying different windows: no honest shared axis exists, so
    # the panel states no window rather than picking one.
    stats = sample_stats("t1", "alpha.one", verdict="fail")
    html = render_trajectory_html(
        title="t", meta_lines=[],
        trials=[
            TrialPanel("试次 1/2 · t1", "m", stats, context_window=2000),
            TrialPanel("试次 2/2 · t2", "m", stats, context_window=4096),
        ],
    )
    assert '"context_window":null' in html
    assert '"context_window_source":"none"' in html
    assert "无窗口声明" in html


def test_panel_selfcontained_deterministic_and_escaped():
    html = _panel()
    assert '<script src=' not in html and "http://" not in html
    assert "fetch(" not in html
    assert html == _panel()
    escaped = _panel(heading="<img src=x onerror=alert(1)>")
    assert "<img src=x" not in escaped
    assert "&lt;img src=x onerror=alert(1)&gt;" in escaped
    assert "\\u003cimg" in escaped


def test_panel_fits_width_without_horizontal_panning():
    """面板按容器宽度铺满，不靠固定 min-width 逼出横向滚动。"""
    html = _panel()
    # The old 900px floor is what forced the timeline to pan; it must stay gone.
    assert "min-width:900px" not in html
    # The SVG fills its column and the scroll pane suppresses the x axis.
    assert ".trajectory-timeline{width:100%" in html
    assert "overflow-x:hidden" in html
    # A responsive viewport + a wide centered page container, not a narrow column.
    assert 'name="viewport"' in html and "width=device-width" in html
    assert 'class="panel-page"' in html


def test_panel_buckets_large_trajectories_without_losing_events():
    stats = sample_stats("t1", "alpha.one", verdict="fail")
    template = stats.steps[0]
    steps = tuple(
        replace(template, step_id=index + 1, seconds_from_start=float(index))
        for index in range(2001)
    )
    html = render_trajectory_html(
        title="t", meta_lines=[],
        trials=[TrialPanel("试次 1/1 · t1", "m", replace(stats, steps=steps, total_steps=len(steps)))],
    )
    assert '"dense":true' in html
    assert "data-bucket=" in html
    assert '"context_peak":' in html
    assert '"tool_calls":' in html
    assert '"event_ids":[' in html
    assert '"step_id":2001' in html


def test_panel_toolless_trial_keeps_honest_summary_data():
    """无工具调用的试次保留在时间轴与详情数据中，不伪造工具统计。"""
    from harbor.models.trajectories import Agent, FinalMetrics, Trajectory

    from aeval.contracts import CanonicalTranscript
    from aeval.verdict.trajectory.base import build_evidence
    from aeval.verdict.trajectory.stats import collect_stats
    from tests.unit.verdict.test_stats import sample_steps

    transcript = Trajectory(
        agent=Agent(name="dsh", version="test"),
        steps=[sample_steps()[2].model_copy(update={"step_id": 1})],
        final_metrics=FinalMetrics(
            total_prompt_tokens=10, total_completion_tokens=2,
            total_cached_tokens=0,
        ),
    )
    ct = CanonicalTranscript(atif=transcript, stop_reason="agent_claimed_done")
    stats = collect_stats(sample_record(), build_evidence(ct, "agent_claimed_done"))
    html = render_trajectory_html(
        title="t", meta_lines=[], trials=[TrialPanel("试次 1/1 · t1", "m", stats)],
    )
    assert '"tools":[]' in html
    assert '"tool_calls_total":0' in html
    assert "该试次无工具调用" in html
