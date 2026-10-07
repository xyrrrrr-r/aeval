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
    """面板与 markdown 报告同源：同一 RunSummary、同一类别聚合——
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
        category_names={"memory": "跨会话记忆", "intelligence": "会话智能度"},
        default_category="intelligence",
    )
    summary = _summary(_trials(), manifests=[manifest])
    html = render_dashboard_html(summary)
    # 指标卡：试次/有效分母、pass^3（= C(8,3)/C(9,3) = 56/84）。
    assert "9 <small>有效分母 9</small>" in html
    assert "0.6667" in html  # pass^3
    # 判定 donut + 图例（契约词对照）。
    assert "通过(pass)" in html and "<svg" in html
    # 类别聚合与报告同数字：memory 2 任务 6 试 5 过 1 败，pass^3=0.5。
    assert "跨会话记忆" in html
    assert "pass^k 0.5000 · 5/6" in html
    assert "会话智能度" in html
    # 任务行：标题对照原 id。
    assert "取货码召回(memory.store_recall)" in html
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
