"""维度达标评分（能力水位）测试：阈值/权重/大块/红线/短板。

v5：维度 = 族（族行通过率是唯一来源）。族声明由清单密封，族判定来自
试次里该族引擎的 GradeResult——不再有「按 task_id 前缀归类别 + 任务级
红线伪维度」的第二条路。
"""

from __future__ import annotations

from aeval.agents.dsh.release import build_official_dsh_lock
from aeval.contracts import (
    GradeResult,
    OverlayIdentity,
    RunManifest,
    Score,
    TrialRecord,
    VersionsBundle,
)
from aeval.metrics.dashboard import render_dashboard_html
from aeval.metrics.report import aggregate_run, render_static_report
from aeval.metrics.watermark import score_watermark
from aeval.provenance import build_runtime_lock


def _graded(trial_id, index, task, grades):
    """一个试次带若干族引擎结果。"""
    record = TrialRecord(
        trial_id=trial_id,
        coordinates={"run_id": "r1", "suite_id": "s", "suite_version": "1",
                     "task_id": task, "trial_index": index},
        stop_reason="agent_exit_0",
    )
    for engine_id, status, veto in grades:
        record.grades.append(GradeResult(
            grader_id=engine_id, grader_version="v1", layer="trajectory",
            veto=veto,
            score=(
                Score(value=1.0) if status != "cannot_judge"
                else Score(valid=False, invalid_reasons=["no sealed anchors"])
            ),
            status=status, reasons=[], metrics=None,
        ))
    return _with_baseline(record)


def _with_baseline(trial, status="pass"):
    """给试次补一条官方基线结果，终判用生产的折叠序。"""
    from aeval.verdict.base import decide_final_verdict

    trial.grades.insert(0, GradeResult(
        grader_id="official-outcome", grader_version="v1", layer="outcome",
        veto=False, score=Score(value=1.0), status=status, reasons=[],
        metrics=None,
    ))
    trial.verdict = decide_final_verdict(trial.grades)
    return trial


# 三个族：甲（阈值 0.9，普通）、乙（阈值 1.0，红线）、丙（阈值 0.9）。
# 引擎身份各不相同：族行按自己的引擎结果出数，互不冒充。
_MODEL = {
    "families": {
        "alpha": {
            "name": "甲", "block": "b1", "weight": 1.0,
            "threshold": 0.9, "redline": False,
            "engine": {"impl": "graders/alpha.py@v1",
                       "grader_id": "engine-alpha",
                       "grader_version": "v1"},
            "scope": {"alpha.a": {}, "alpha.b": {}},
        },
        "beta": {
            "name": "乙", "block": "b2", "weight": 2.0,
            "threshold": 1.0, "redline": True,
            "engine": {"impl": "graders/beta.py@v1",
                       "grader_id": "engine-beta",
                       "grader_version": "v1"},
            "scope": {"beta.c": {}},
        },
        "gamma": {
            "name": "丙", "block": "b3", "weight": 1.0,
            "threshold": 0.9, "redline": False,
            "engine": {"impl": "graders/gamma.py@v1",
                       "grader_id": "engine-gamma",
                       "grader_version": "v1"},
            "scope": {"gamma.d": {}},
        },
    },
    "blocks": {"b1": "块一", "b2": "块二", "b3": "块三"},
    "baseline": {"impl": "graders/outcome.py@v1",
                 "grader_id": "official-outcome", "grader_version": "v1"},
    "guards": [],
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


# 每个试次只带自己那一族引擎的结果：族只吃自己声明范围内的试次。
_GRADED_TRIALS = (
    [("a0", 0, "alpha.a", "engine-alpha", "pass"),
     ("a1", 1, "alpha.a", "engine-alpha", "pass"),
     ("a2", 2, "alpha.a", "engine-alpha", "pass"),
     ("b0", 0, "alpha.b", "engine-alpha", "pass"),
     ("b1", 1, "alpha.b", "engine-alpha", "fail"),
     ("b2", 2, "alpha.b", "engine-alpha", "pass")]
    + [("c0", 0, "beta.c", "engine-beta", "pass"),
       ("c1", 1, "beta.c", "engine-beta", "fail"),
       ("c2", 2, "beta.c", "engine-beta", "pass")]
    + [("d0", 0, "gamma.d", "engine-gamma", "pass"),
       ("d1", 1, "gamma.d", "engine-gamma", "pass"),
       ("d2", 2, "gamma.d", "engine-gamma", "pass")]
)


def _trials(rows=_GRADED_TRIALS):
    return [
        _graded(trial_id, index, task, [(engine, status, False)])
        for trial_id, index, task, engine, status in rows
    ]


def _summary(trials=None, model=_MODEL):
    return aggregate_run(
        ["r1"], trials if trials is not None else _trials(), k=3,
        manifests=[_manifest(model)] if model else [],
    )


def test_watermark_none_without_families():
    """没有族的套件（baseline-only）：无水位层（报告/面板向后兼容）。"""
    assert score_watermark(_summary(model={})) is None
    assert score_watermark(
        _summary(model={"families": {}, "blocks": {}})
    ) is None


def test_watermark_bands_and_achievement():
    """达标度 = 族行通过率/阈值；状态带 绿≥1、黄≥0.9、红<0.9。"""
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
    # 红线是真实族行，不再有"任务级红线伪维度"。
    assert "redline" not in dims
    assert dims["beta"].redline and dims["beta"].threshold == 1.0


def test_watermark_blocks_composite_and_redline():
    """大块 = 成员加权均分；综合评分 = 全维度加权均分；红线 = 低于阈值
    的红线维度集合。"""
    wm = score_watermark(_summary())
    blocks = {b.key: b for b in wm.blocks}
    assert blocks["b1"].score == 5 / 6
    # 综合评分：(1×5/6 + 2×2/3 + 1×1) / (1+2+1)。
    assert abs(wm.composite - (5 / 6 + 4 / 3 + 1) / 4) < 1e-12
    assert wm.overall_pass_rate == 10 / 12
    assert wm.redline_tripped
    assert wm.redline_offenders == ("乙",)


def test_watermark_bottom_k():
    """短板摘要：达标度最低的前 K 个维度（升序）。"""
    wm = score_watermark(_summary())
    assert [d.key for d in wm.bottom] == ["beta", "alpha", "gamma"]


def test_watermark_in_markdown_report():
    """markdown 报告的「维度达标」层：头部摘要行 + 评分表。"""
    text = render_static_report(_summary())
    assert f"- 综合评分：{score_watermark(_summary()).composite:.4f}" in text
    assert "- 红线状态：告警（乙）" in text
    assert "## 维度达标" in text
    # 表行：显示名(key) + 大块 + 权重 + 通过率 + 阈值 + 达标度 + 状态。
    assert "| 甲(alpha) | 块一 | 1 | 0.8333 | 0.9 | 0.9259 | 黄 |" in text
    assert "| 乙(beta)（红线） | 块二 | 2 | 0.6667 | 1 | 0.6667 | 红 |" in text


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
    rows = [
        ("c0", 0, "beta.c", "engine-beta", "pass"),
        ("c1", 1, "beta.c", "engine-beta", "pass"),
        ("c2", 2, "beta.c", "engine-beta", "pass"),
    ]
    summary = _summary(trials=_trials(rows))
    wm = score_watermark(summary)
    assert not wm.redline_tripped
    assert wm.redline_offenders == ()
    assert "红线全通过" in render_dashboard_html(summary)
