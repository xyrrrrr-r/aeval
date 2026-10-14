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
from aeval.verdict.trajectory.failures import FailureReport, FailingTest
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


def test_trajectory_aggregate_without_families_is_one_bucket():
    """没有族的套件：轨迹只落一个"未分族"桶（v5 不按 task_id 前缀猜族，
    也不编造类别显示名）。"""
    traj = _summary().trajectory
    rows = {c.key: c for c in traj.categories}
    assert set(rows) == {"未分族"}
    assert rows["未分族"].trials == 2
    assert rows["未分族"].total_tokens == 2 * 3730
    assert rows["未分族"].tool_calls == 4
    assert rows["未分族"].missing_obs == 2
    assert rows["未分族"].display == "未分族"


def test_trajectory_rows_label_display_with_family_key():
    """族行的括号里是族键，不是显示名的复读机。

    v5 的任务可以同时属多个族，所以两半都是多值：单族
    ``terminal-bench 任务(tbench)``，多族
    ``会话智能度+安全(intelligence+security)``。
    """
    summary = _summary(manifests=[_family_manifest()])
    rows = {c.key: c for c in summary.trajectory.categories}
    assert set(rows) == {"intel+sec", "sec"}
    assert rows["intel+sec"].display == "会话智能度+安全"
    assert rows["intel+sec"].trials == 1
    assert rows["sec"].trials == 1
    md = render_static_report(summary)
    assert "| 会话智能度+安全(intel+sec) | 1 |" in md
    assert "| 安全(sec) | 1 |" in md


def _family_manifest():
    """两个归组族（metrics: [] + 引擎 = 基线）→ alpha.one 属两族。"""
    from aeval.agents.dsh.release import build_official_dsh_lock
    from aeval.contracts import OverlayIdentity, RunManifest, VersionsBundle
    from aeval.provenance import build_runtime_lock

    baseline = {
        "impl": "graders/outcome.py@v1",
        "grader_id": "outcome",
        "grader_version": "v1",
    }

    def family(name, block, tasks):
        return {
            "name": name,
            "block": block,
            "weight": 1.0,
            "threshold": 0.9,
            "redline": False,
            "engine": dict(baseline, veto=False),
            "metrics": [],
            "scope": {task: {"role": "home"} for task in tasks},
        }

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
        dimension_model={
            "families": {
                "intel": family("会话智能度", "b1", ["alpha.one"]),
                "sec": family("安全", "b2", ["alpha.one", "beta.two"]),
            },
            "blocks": {"b1": "会话", "b2": "安全"},
            "baseline": baseline,
            "guards": [],
        },
    )


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
    assert "| 未分族(未分族) | 2 | 7,460 | 40.0 | 4 | 2 |" in md
    # 缓存占输入口径：缓存读 / 计费输入（样本 200/7,300）
    assert "- 缓存占输入：2.7%（缓存读 200 / 计费输入 7,300）" in md
    # 首 token 时延：样本没有这个字段 → 如实写不可用，不写 0
    assert "- 首 token 时延：不可用（账本未记录首个输出增量）" in md


def test_report_states_first_token_latency_when_the_evidence_has_it():
    import dataclasses

    from aeval.metrics.report import aggregate_run, render_static_report

    records = [_record("t1", 0, "alpha.one"), _record("t2", 1, "beta.two", verdict="fail")]
    base = sample_stats("t1", "alpha.one")
    timed = dataclasses.replace(
        base,
        steps=tuple(
            dataclasses.replace(step, first_token_ms=800 if step.step_id == 2 else None)
            for step in base.steps
        ),
    )
    md = render_static_report(aggregate_run(["r1"], records, k=1, trajectory=[timed]))
    assert "- 首 token 时延：1 次调用有记录 · 最快 800ms · 中位 800ms · 最慢 800ms" in md


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
    assert "六轨联动执行视图" in html
    for track in ("执行图谱", "工具调用", "Token 脉冲", "输入 Token", "上下文压力", "首 Token 时延"):
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


def _card(html: str, title: str):
    """抓出标题对应的关注项卡（kind, 汇总行, 卡内逐试次行）。"""
    import re

    match = re.search(
        r'<div class="insight ([a-z]+)"><b>' + re.escape(title) + r'</b><span>([^<]*)</span>'
        r'<div class="insight-rows"[^>]*>(.*?)</div></div>',
        html,
        re.S,
    )
    assert match, f"没有找到关注项卡：{title}"
    kind, detail, rows = match.groups()
    parsed = re.findall(
        r'<button type="button" class="insight-row"[^>]*><b>([^<]*)</b><span>([^<]*)</span>',
        rows,
    )
    return kind, detail, parsed


def _two_trial_panel(**over):
    """两个试次，峰值与缺失原因都不同——用来验证"按类别收敛、卡内逐试次"。"""
    first = replace(
        sample_stats("t1", "alpha.one", verdict="fail"),
        peak_input_tokens=1400,
        missing=("无可用时间戳：时长/耗时版块不可用",),
    )
    second = replace(
        sample_stats("t2", "alpha.one", verdict="fail"),
        peak_input_tokens=900,
        missing=("步骤无 token 计量：Token/上下文版块不可用",),
    )
    return render_trajectory_html(
        title="任务(alpha.one) 轨迹分析",
        meta_lines=["run r1 · s@1 · 2/2 个试次有可用密封轨迹"],
        trials=[
            TrialPanel(
                heading="试次 1/3 · t1",
                meta="r1 · s@1 · 任务(alpha.one)",
                stats=first,
                failure=FailureReport(
                    verdict="fail",
                    reasons=("official-baseline@v1: upstream verifier reward is 0",),
                    failing_tests=(FailingTest("test_x", "AssertionError: nope"),),
                    implicated_steps=(4,),
                    path_steps=(4,),
                    step_basis="path",
                ),
            ),
            TrialPanel(
                heading="试次 2/3 · t2",
                meta="r1 · s@1 · 任务(alpha.one)",
                stats=second,
                failure=FailureReport(
                    verdict="fail",
                    reasons=("official-baseline@v1: upstream verifier reward is 0",),
                    implicated_steps=(),
                    unavailable=("相关步骤未定位：…",),
                ),
            ),
        ],
        context_window=over.get("context_window"),
    )


def test_first_token_latency_track_shows_recorded_values_and_unavailable_state():
    """首 token 时延轨：有记录就画柱并写毫秒；没有记录写"不可用"，不写 0。"""
    steps = sample_stats("t1", "alpha.one").steps
    with_data = replace(
        sample_stats("t1", "alpha.one"),
        steps=tuple(
            replace(step, first_token_ms=900 if step.step_id == 2 else None)
            for step in steps
        ),
    )
    html = render_trajectory_html(
        title="t", meta_lines=["m"],
        trials=[TrialPanel(heading="试次 1/1 · t1", meta="r1", stats=with_data)],
    )
    assert "首 Token 时延" in html
    assert 'class="latency-bar"' in html
    assert "步骤 2 · 首 token 900ms" in html
    assert "首 token 时延" in html and "未记录" in html  # 检查器口径
    assert "1 次调用有记录" in html

    # 没有记录：轨道仍然在，但明说不可用
    bare = render_trajectory_html(
        title="t", meta_lines=["m"],
        trials=[TrialPanel(heading="试次 1/1 · t1", meta="r1",
                           stats=sample_stats("t1", "alpha.one"))],
    )
    assert "首 Token 时延" in bare
    assert "不可用" in bare and "first_token_ms" in bare
    assert 'class="latency-bar"' not in bare


def test_a_step_view_embeds_the_failure_card_for_an_implicated_step():
    """失败相关步骤的详情里直接带失败原因卡，不必先跳回试次。"""
    html = _two_trial_panel()
    assert "renderFailure(inspector, trial)" in html     # 步骤详情里的同一张卡
    assert "查看该试次失败原因" in html                    # 非相关步骤仍给出跳转按钮


def test_token_track_shows_cached_share_of_input():
    """Token 脉冲轨标出缓存占输入比例；没有缓存计量写"未拆分"，不写 0%。"""
    html = _panel()
    assert 'class="token-ratio"' in html
    assert "缓存占输入 7%（缓存 100 / 输入 1,400）" in html   # 步骤 5：100/1400
    assert "缓存占输入 —（未拆分）" in html                       # 步骤 2/4/6 没有缓存计量
    assert "缓存/输入" in html                                    # 检查器里的同一口径
    assert '<text class="token-ratio"' in html


def test_insights_converge_one_card_per_category_with_per_trial_rows():
    html = _two_trial_panel(context_window=10000)

    # 每个类别恰好一张卡——不再"每试次一张卡"
    for title in ("试次失败", "峰值上下文", "最长含工具步骤", "数据不可用", "工具无观测信号"):
        assert html.count(f"<b>{title}</b>") == 1, title

    kind, detail, rows = _card(html, "峰值上下文")
    assert "各试次峰值 900–1,400 tokens" in detail and "2/2 试次" in detail
    assert "最高 1,400（试次 1）" in detail and "最高占声明窗口 14%" in detail
    assert [label for label, _ in rows] == ["试次 1", "试次 2"]
    assert "1,400 tokens" in rows[0][1] and "900 tokens" in rows[1][1]
    assert "占声明窗口 14%" in rows[0][1] and "占声明窗口 9%" in rows[1][1]

    _kind, _, longest = _card(html, "最长含工具步骤")
    assert [label for label, _ in longest] == ["试次 1", "试次 2"]

    _kind, detail, unavailable = _card(html, "数据不可用")
    assert "2/2 试次" in detail and len(unavailable) == 2


def test_insights_failure_card_lists_each_failed_trial_once():
    html = _two_trial_panel()
    kind, detail, rows = _card(html, "试次失败")
    assert kind == "danger"
    assert "2/2 试次失败" in detail and "1/2 试次已定位相关步骤（共 1 步）" in detail
    assert [label for label, _ in rows] == ["试次 1", "试次 2"]
    assert "test_x: AssertionError: nope" in rows[0][1] and "相关步骤 4" in rows[0][1]
    # 未定位的那次如实写出来，不硬指一步
    assert "test_x" not in rows[1][1]


def test_insight_rows_use_page_local_numbering_and_failure_rows_open_the_trial():
    """行标签是页内序号（1..N），失败行点开的是试次（失败原因卡），不是某个步骤。"""
    html = _two_trial_panel()
    _kind, detail, rows = _card(html, "试次失败")
    assert [label for label, _ in rows] == ["试次 1", "试次 2"]
    assert detail.startswith("2/2 试次失败")
    import re

    block = re.search(r'<div class="insight danger"><b>试次失败</b>.*?</div></div>', html, re.S).group(0)
    assert 'data-trial="trial-0"' in block and 'data-trial="trial-1"' in block
    assert 'data-event="' not in block


def test_insight_rows_are_clickable_targets():
    html = _two_trial_panel()
    import re

    _kind, _detail, _rows = _card(html, "峰值上下文")
    block = re.search(r'<div class="insight info"><b>峰值上下文</b>.*?</div></div>', html, re.S).group(0)
    assert block.count('data-event="') >= 2
    # 数据不可用只有试次级目标（没有步骤可用）
    block = re.search(r'<div class="insight muted"><b>数据不可用</b>.*?</div></div>', html, re.S).group(0)
    assert 'data-trial="trial-0"' in block or 'data-trial="trial-1"' in block


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
