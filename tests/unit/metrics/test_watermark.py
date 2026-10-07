"""维度达标评分（能力水位）测试：阈值/权重/大块/红线/短板。"""

from __future__ import annotations

from aeval.agents.dsh.release import build_official_dsh_lock
from aeval.contracts import (
    OverlayIdentity,
    RunManifest,
    TrialRecord,
    VersionsBundle,
)
from aeval.metrics.dashboard import render_dashboard_html
from aeval.metrics.report import aggregate_run, render_static_report
from aeval.metrics.watermark import score_watermark
from aeval.provenance import build_runtime_lock


def _trial(trial_id, index, task, verdict):
    return TrialRecord(
        trial_id=trial_id,
        coordinates={"run_id": "r1", "suite_id": "s", "suite_version": "1",
                     "task_id": task, "trial_index": index},
        stop_reason="agent_exit_0",
        verdict=verdict,
    )


_MODEL = {
    "categories": {
        "alpha": {"name": "甲", "block": "b1", "weight": 1.0,
                  "threshold": 0.9, "redline": False},
        "beta": {"name": "乙", "block": "b2", "weight": 2.0,
                 "threshold": 1.0, "redline": True},
        "gamma": {"name": "丙", "block": "b3", "weight": 1.0,
                  "threshold": 0.9, "redline": False},
    },
    "blocks": {"b1": "块一", "b2": "块二", "b3": "块三", "redline": "红线"},
    "redline_tasks": ["gamma.secret"],
    "default": "alpha",
}


def _manifest(model):
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
        dimension_model=model,
    )


def _trials():
    return [
        _trial("a0", 0, "alpha.a", "pass"),
        _trial("a1", 1, "alpha.a", "pass"),
        _trial("a2", 2, "alpha.a", "pass"),
        _trial("b0", 0, "alpha.b", "pass"),
        _trial("b1", 1, "alpha.b", "fail"),
        _trial("b2", 2, "alpha.b", "pass"),
        _trial("c0", 0, "beta.c", "pass"),
        _trial("c1", 1, "beta.c", "fail"),
        _trial("c2", 2, "beta.c", "pass"),
        _trial("d0", 0, "gamma.secret", "pass"),
        _trial("d1", 1, "gamma.secret", "pass"),
        _trial("d2", 2, "gamma.secret", "pass"),
    ]


def _summary(trials=None, model=_MODEL):
    return aggregate_run(
        ["r1"], trials if trials is not None else _trials(), k=3,
        manifests=[_manifest(model)] if model else [],
    )


def test_watermark_none_without_model():
    """未声明维度模型的套件：无水位层（报告/面板向后兼容）。"""
    assert score_watermark(_summary(model={})) is None


def test_watermark_bands_and_achievement():
    """达标度 = 通过率/阈值；状态带 绿≥1、黄≥0.9、红<0.9。"""
    wm = score_watermark(_summary())
    dims = {d.key: d for d in wm.dimensions}
    # 甲：5/6 = 0.8333，阈值 0.9 → 达标度 0.9259 → 黄。
    assert dims["alpha"].value == 5 / 6
    assert dims["alpha"].achievement == 5 / 6 / 0.9
    assert dims["alpha"].band == "yellow"
    # 乙：2/3 = 0.6667，阈值 1.0 → 达标度 0.6667 → 红。
    assert dims["beta"].band == "red"
    assert dims["beta"].failing_tasks == ("beta.c",)
    # 丙：3/3 → 达标度 1.1111 → 绿。
    assert dims["gamma"].band == "green"
    # 任务级红线伪维度：阈值固定 1.0，全过 → 绿。
    pseudo = dims["redline"]
    assert pseudo.display == "任务红线"
    assert pseudo.redline and pseudo.threshold == 1.0
    assert pseudo.value == 1.0 and pseudo.band == "green"


def test_watermark_blocks_composite_and_redline():
    """大块 = 成员加权均分；综合评分 = 全维度加权均分；红线 = 低于阈值
    的红线维度集合。"""
    wm = score_watermark(_summary())
    blocks = {b.key: b for b in wm.blocks}
    assert blocks["b1"].score == 5 / 6
    # 综合评分： (1×5/6 + 2×2/3 + 1×1 + 2×1) / (1+2+1+2)。
    assert abs(wm.composite - (5 / 6 + 4 / 3 + 1 + 2) / 6) < 1e-12
    assert wm.overall_pass_rate == 10 / 12
    # 乙是红线维度且低于阈值 → 告警；任务红线全过不触发。
    assert wm.redline_tripped
    assert wm.redline_offenders == ("乙",)


def test_watermark_bottom_k():
    """短板摘要：达标度最低的前 K 个维度（升序）。"""
    wm = score_watermark(_summary())
    assert [d.key for d in wm.bottom] == [
        "beta", "alpha", "redline", "gamma",
    ]


def test_watermark_in_markdown_report():
    """markdown 报告的「维度达标」层：头部摘要行 + 评分表。"""
    text = render_static_report(_summary())
    assert f"- 综合评分：{score_watermark(_summary()).composite:.4f}" in text
    assert "- 红线状态：告警（乙）" in text
    assert "## 维度达标" in text
    # 表行：显示名(key) + 大块 + 权重 + 通过率 + 阈值 + 达标度 + 状态。
    assert "| 甲(alpha) | 块一 | 1 | 0.8333 | 0.9 | 0.9259 | 黄 |" in text
    assert "| 乙(beta)（红线） | 块二 | 2 | 0.6667 | 1 | 0.6667 | 红 |" in text
    assert "| 任务红线(redline)（红线） | 红线 | 2 | 1.0000 | 1 | 1.0000 | 绿 |" in text


def test_watermark_in_dashboard():
    """面板的能力水位四层：健康度、雷达、四象限、热力图、短板。"""
    html = render_dashboard_html(_summary())
    assert "综合评分" in html and "红线告警：乙" in html
    assert "能力雷达" in html and "<polygon" in html
    assert "四象限矩阵" in html and "<circle" in html
    assert 'class="cell yellow"' in html and 'class="cell red"' in html
    assert "短板摘要" in html and "差距 33.3%" in html


def test_watermark_all_pass_redline():
    """红线全过：状态「全通过」，无告警。"""
    trials = [
        _trial("c0", 0, "beta.c", "pass"),
        _trial("c1", 1, "beta.c", "pass"),
        _trial("c2", 2, "beta.c", "pass"),
    ] + [t for t in _trials() if not t.coordinates.task_id.startswith("beta")]
    wm = score_watermark(_summary(trials=trials))
    assert not wm.redline_tripped
    assert wm.redline_offenders == ()
    html = render_dashboard_html(_summary(trials=trials))
    assert "红线全通过" in html
