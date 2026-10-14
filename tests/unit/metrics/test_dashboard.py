"""可视化面板渲染测试：与 markdown 报告同一份聚合、自包含、确定性。"""

from __future__ import annotations

from aeval.contracts import TrialRecord
from aeval.metrics.dashboard import render_dashboard_html
from aeval.metrics.report import aggregate_run


def _trial(trial_id, index, *, task="memory.store_recall", verdict="pass"):
    return TrialRecord(
        trial_id=trial_id,
        coordinates={"run_id": "r1", "suite_id": "s", "suite_version": "1",
                     "task_id": task, "trial_index": index},
        stop_reason="agent_exit_0",
        verdict=verdict,
    )


def _summary(trials, **kwargs):
    return aggregate_run(["r1"], trials, k=3, **kwargs)


def _trials():
    return [
        _trial("a0", 0, verdict="pass"),
        _trial("a1", 1, verdict="fail"),
        _trial("a2", 2, verdict="pass"),
        _trial("b0", 0, task="memory.update", verdict="pass"),
        _trial("b1", 1, task="memory.update", verdict="pass"),
        _trial("b2", 2, task="memory.update", verdict="pass"),
        _trial("c0", 0, task="identity-intro", verdict="pass"),
        _trial("c1", 1, task="identity-intro", verdict="pass"),
        _trial("c2", 2, task="identity-intro", verdict="pass"),
    ]


def test_dashboard_shows_the_same_numbers_as_the_report():
    """面板与 markdown 报告同源：同一 RunSummary（含密封清单的显示名），
    报告里的行（数字）在面板里原样出现。"""
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
        task_titles={"memory.store_recall": "取货码召回"},
    )
    summary = _summary(_trials(), manifests=[manifest])
    html = render_dashboard_html(summary)
    # 指标卡：试次/有效分母、pass^3（= C(8,3)/C(9,3) = 56/84）。
    assert "9 <small>有效分母 9</small>" in html
    assert "0.6667" in html  # pass^3
    # 判定 donut + 图例（契约词对照）。
    assert "通过(pass)" in html and "<svg" in html
    # 没有族的套件：不凭 task_id 前缀造类别分区（报告与面板同一份聚合）。
    assert "跨会话记忆" not in html
    assert "按类别结果" not in html
    # 任务行：标题对照原 id（密封清单里的显示名照旧生效）。
    assert "取货码召回(memory.store_recall)" in html
    # 与 markdown 报告同数字：头部指标一致。
    from aeval.metrics.report import render_static_report

    text = render_static_report(summary)
    assert "- 试次：共 9，有效分母 9" in text
    assert "- pass^3：0.6667" in text
    # 头部带套件标识（清单 overlay）。
    assert "套件 s@1" in html


def test_dashboard_is_selfcontained_deterministic_and_scriptfree():
    """自包含单文件：无 <script>、无外链资源；同输入必得同字节。"""
    html = render_dashboard_html(_summary(_trials()))
    assert "<script" not in html
    assert "http://" not in html and "https://" not in html
    assert "src=" not in html  # 无外链资源（样式内联）
    again = render_dashboard_html(_summary(_trials()))
    assert html == again


def test_dashboard_escapes_titles_and_ids():
    """标题/任务 id 经 HTML 转义——面板不能被套件数据注入标记。"""
    summary = _summary(_trials())
    summary.task_titles["memory.store_recall"] = "<script>alert(1)</script>"
    html = render_dashboard_html(summary)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_dashboard_falls_back_to_flat_without_categories():
    """未声明类别的套件：无类别条形区，任务表进「全部任务」折叠块。"""
    html = render_dashboard_html(_summary(_trials()))
    assert "按类别结果" not in html
    assert "全部任务" in html


def test_dashboard_flags_exclusion_rate():
    """排除率超标时顶部出红色警示条。"""
    trials = _trials() + [
        _trial("x0", 3, task="memory.update", verdict="cannot_judge"),
        _trial("x1", 4, task="memory.update", verdict="infra_invalid"),
    ]
    html = render_dashboard_html(_summary(trials))
    assert "排除率超标" in html


def test_the_failed_trial_table_quotes_the_grader_reason():
    """失败清单必须说出「为什么失败」：决定性判分器的理由逐字出现；
    判分器没写理由的试次显示「—」，不推断。"""
    from aeval.contracts import GradeResult, MetricOutcome, Score

    failing = _trial("x0", 0, task="openssl-selfsigned-cert", verdict="fail")
    failing.grades = [
        GradeResult(
            grader_id="official-baseline",
            grader_version="v1",
            layer="outcome",
            score=Score(value=0.0),
            status="fail",
            reasons=["upstream verifier reward is 0 (the task's tests failed)"],
        ),
        GradeResult(
            grader_id="terminal-beach-trajectory",
            grader_version="v1",
            layer="trajectory",
            score=Score(value=0.9),
            status="pass",
            reasons=["trajectory healthy"],
            metrics=[
                MetricOutcome(
                    name="tool_error_rate",
                    category="robustness",
                    status="ok",
                    score=0.9,
                    reasons=["failures at: step 7"],
                )
            ],
        ),
    ]
    # a failed grade that recorded no reason at all: the cell must say so
    bare = _trial("x2", 2, task="openssl-selfsigned-cert", verdict="fail")
    bare.grades = [
        GradeResult(
            grader_id="official-baseline",
            grader_version="v1",
            layer="outcome",
            score=Score(valid=False, invalid_reasons=["judged by the task's own verifier"]),
            status="fail",
            reasons=[],
        )
    ]
    summary = _summary([*_trials(), failing, bare])
    # ``_verdict_source_html`` is embedded by the family section (families exist
    # on a real run); here the helper is the unit and gets asserted directly,
    # plus the embedded form once a family is present.
    from aeval.metrics.dashboard import _verdict_source_html

    html = _verdict_source_html(summary)
    assert "失败理由（逐字）" in html
    assert "upstream verifier reward is 0 (the task&#x27;s tests failed)" in html
    assert "tool_error_rate: failures at: step 7" in html
    assert "<td>—</td>" in html
    # a failure with no recorded reason is shown as "—", never invented
    assert "<td>—</td>" in html
    # the passing grader's own reason is not part of the failure explanation
    assert "trajectory healthy" not in html
