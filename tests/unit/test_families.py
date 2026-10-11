"""gradeCategory（判分类别 = 指标族）单元测试。

覆盖加载期约束（fail-closed 表）、逐试次装配保全、族行口径（D7：
族引擎 GradeResult 聚合 + cannot_judge 出分母入排除列）、多族丰富、
跨 run 聚合（可比才合并 / 不可比各自成行）、离线对账、
单向锁定（chain digest）与确定性。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml

from aeval.contracts import GradeResult, RunManifest, Score, TrialRecord
from aeval.metrics.report import aggregate_run, render_static_report
from aeval.metrics.watermark import score_watermark
from aeval.suite_loader.loader import load_suite
from aeval.suite_models import SuiteError
from aeval.verdict.pipeline import (
    GradingPipelineError,
    load_suite_graders,
    validate_metric_contracts,
    resolve_grader_identities,
    sealed_dimension_model,
)

AEVAL_ROOT = Path(__file__).parents[2]
SUITES_ROOT = AEVAL_ROOT / "suites"
INTEL_SUITE = SUITES_ROOT / "aeval-intel"
TBENCH_SUITE = (
    AEVAL_ROOT / "examples" / "offline-chain" / "suite" / "tbench-intel"
)


def _load(root):
    """Load a demo suite (extends the shared base under suites/_base)."""
    return load_suite(root, suites_root=SUITES_ROOT)


BASE_GRADER = '''
GRADER_ID = "demo-engine"
GRADER_VERSION = "v1"
REQUIRED_FIELDS = []
ANCHOR_KEYS = frozenset({"identity_probes", "format_specs"})
def anchors_from_mapping(data):
    return data
def impl_for(anchors):
    return anchors
async def grade(record):
    from aeval.contracts import GradeResult, Score
    return GradeResult(
        grader_id=GRADER_ID, grader_version=GRADER_VERSION,
        layer="trajectory",
        veto=False, score=Score(value=1.0), status="pass", reasons=[],
        metrics=None,
    )
'''

#: 指标库模块（v5 声明驱动装配）：``id → factory(inputs)``。
#: 每个伪指标自证 name/category，供 severity 对账与报告归属使用。
BASE_METRIC_LIBRARY = '''
from aeval.contracts import MetricOutcome
from aeval.verdict.trajectory.metrics import TrajectoryMetric

GRADER_ID = "demo-engine"
GRADER_VERSION = "v1"
REQUIRED_FIELDS = []


class _DemoMetric(TrajectoryMetric):
    """A minimal score-only dimension: always ok (score 1.0)."""

    def __init__(self, name, anchors=None):
        self.name = name
        self.anchors = anchors

    def evaluate(self, evidence):
        return MetricOutcome(
            name=self.name, category=self.category, status="ok",
            score=1.0, weight=self.weight,
            reasons=["demo metric always ok"],
        )


class _DemoGuard(_DemoMetric):
    """An integrity-gated dimension (declared severity must match)."""

    category = "integrity"


def _identity_probe(inputs):
    """自己拒绝未知键：v5 没有代码侧词表，probe 就是键的检查。"""
    unknown = sorted(set(inputs or ()) - {"identity_probes"})
    if unknown:
        raise ValueError(f"unknown anchor keys {unknown}")
    return _DemoMetric("identity_probe", inputs)


METRICS = {
    "identity_probe": _identity_probe,
    "format_check": lambda inputs: _DemoMetric("format_check", inputs),
    "integrity_guard": lambda inputs: _DemoGuard("integrity_guard", inputs),
}
'''


def _legacy_cases_to_v5(body: dict) -> dict:
    """测试书写便利：把 ``cases:{task:{inputs:{指标:...}}}`` 翻成
    v5 Step 3 的形状（指标自带任务键输入 + scope 显示元数据）。

    被测的语义是新形状；这里只是让用例读起来仍然集中在一处。
    """
    if "cases" not in body:
        return body
    body = dict(body)
    cases = body.pop("cases") or {}
    metrics = []
    for entry in body.get("metrics", ["identity_probe"]):
        metrics.append({"id": entry} if isinstance(entry, str) else dict(entry))
    scope: dict[str, dict] = {}
    for task_id, case in cases.items():
        for metric_id, ins in (case.get("inputs") or {}).items():
            target = next((m for m in metrics if m["id"] == metric_id), None)
            if target is None:
                target = {"id": metric_id}
                metrics.append(target)
            target.setdefault("inputs", {})[task_id] = ins
        meta = {k: v for k, v in case.items() if k != "inputs"}
        if meta:
            scope[task_id] = meta
    body["metrics"] = metrics
    if scope:
        body["scope"] = scope
    return body


def _dump_family(path: Path, spec: dict) -> None:
    """测试手写族声明（可用 cases 简写）→ 落盘 v5 形状。"""
    path.write_text(
        yaml.safe_dump(
            _legacy_cases_to_v5(spec), allow_unicode=True, sort_keys=False
        ),
        encoding="utf-8",
    )


def _write_suite(root: Path, *, families=("alpha",), anchors_channel=True,
                 extra_overlay="", family_body=None):
    """A minimal offline family suite (no dataset → offline judging)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "graders").mkdir(exist_ok=True)
    (root / "graders" / "engine.py").write_text(BASE_GRADER, encoding="utf-8")
    (root / "graders" / "library.py").write_text(
        BASE_METRIC_LIBRARY, encoding="utf-8"
    )
    listed = []
    for key in families:
        body = _legacy_cases_to_v5(
            family_body
            or {
                "metrics": ["identity_probe"],
                "cases": {
                    "task-a": {
                        "title": "任务甲",
                        "role": "home",
                        "inputs": {
                            "identity_probe": {"identity_probes": ["你是谁"]}
                        },
                    }
                },
            }
        )
        spec = {
            "name": body.get("name", key.title()),
            "block": body.get("block", "core"),
            "weight": body.get("weight", 1.0),
            "threshold": body.get("threshold", 0.8),
            "redline": body.get("redline", False),
            "engine": {
                "impl": body.get("impl", "graders/library.py@v1"),
                "layer": body.get("layer", "trajectory"),
            },
            "metrics": body.get("metrics", ["identity_probe"]),
        }
        if body.get("scope"):
            spec["scope"] = body["scope"]
        d = root / "families"
        d.mkdir(exist_ok=True)
        (d / f"{key}.yaml").write_text(
            yaml.safe_dump(spec, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        listed.append(f"families/{key}.yaml")
    overlay = {
        "schema_version": 2,
        "id": "demo-family",
        "version": "0.1.0",
        "extends": "_base/harbor.base.yaml",
        "families": listed,
        "blocks": {"core": "核心"},
        "provenance": {"source": "authored-internally", "license": "MIT"},
        "harbor": {"job": "jobs/demo-family.yaml"},
        "verdict": {"graders": {
            "default": {"impl": "graders/engine.py@v1", "layer": "outcome"},
        }},
    }
    if anchors_channel:
        overlay["verdict"]["anchors"] = "task_anchors"
    if extra_overlay:
        overlay.update(extra_overlay)
    (root / "suite.yaml").write_text(
        yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    jobs = root / "jobs"
    jobs.mkdir(exist_ok=True)
    (jobs / "demo-family.yaml").write_text(
        "job_name: demo-family\nn_attempts: 3\nagents:\n  - name: nop\n",
        encoding="utf-8",
    )
    return root


# --- loading: positive -----------------------------------------------------


def test_family_suite_loads_with_dimension_model(tmp_path):
    suite = _load(_write_suite(tmp_path))
    assert suite.has_families is True
    assert list(suite.families) == ["alpha"]
    spec = suite.families["alpha"]
    # Step 3：适用范围由指标输入派生，任务只是输入的键
    assert spec.task_ids() == ("task-a",)
    assert spec.inputs_for("task-a") == {
        "identity_probe": {"identity_probes": ["你是谁"]}
    }
    assert spec.binds("task-a") is True
    assert spec.binds("task-z") is False
    # task_titles 自族用例 title 投影；类别名自族 name 投影
    assert suite.task_titles == {"task-a": "任务甲"}
    assert {k: spec.name for k, spec in suite.families.items()} == {"alpha": "Alpha"}
    model = suite.dimension_model
    assert set(model["families"]) == {"alpha"}
    assert model["baseline"]["impl"] == "graders/engine.py@v1"
    assert model["guards"] == []
    # 输入投影：确定性字节（task_id → 族键 → {指标 id: 该指标的输入}）
    import json
    proj = json.loads(suite.sealed_anchor_projection())
    assert proj == {
        "task-a": {
            "alpha": {"identity_probe": {"identity_probes": ["你是谁"]}}
        }
    }
    assert suite.sealed_anchor_projection() == suite.sealed_anchor_projection()


def test_assembly_is_baseline_then_families_per_task(tmp_path):
    _write_suite(tmp_path, families=("alpha", "beta"),
                 family_body={"cases": {}})
    # give each family a disjoint case set sharing one engine impl
    for key, task in (("alpha", "task-a"), ("beta", "task-b")):
        spec = yaml.safe_load(
            (tmp_path / "families" / f"{key}.yaml").read_text("utf-8")
        )
        spec["cases"] = {
            task: {
                "role": "home",
                "inputs": {"identity_probe": {"identity_probes": ["x"]}},
            }
        }
        _dump_family(tmp_path / "families" / f"{key}.yaml", spec)
    suite = _load(tmp_path)
    # v5 装配：官方基线 + 该 task 所属族的指标库（两族共用一个库 impl，
    # 按 impl 去重后只加载一次）
    for task in ("task-a", "task-b"):
        assert [g.impl for g in suite.resolved_graders(task)] == [
            "graders/engine.py@v1",
            "graders/library.py@v1",
        ]
    # 全体展开：baseline + 两族引擎（同 impl 去重后一个）
    assert [g.impl for g in suite.resolved_graders(None)] == [
        "graders/engine.py@v1",
        "graders/library.py@v1",
    ]
    assert suite.family_task_union() == {"task-a", "task-b"}


def test_real_intel_suite_preserves_assembly_sets():
    """0.5.0 迁移保全：每 task 装配集合与 0.4.0 三件套逐一相同。"""
    suite = load_suite(INTEL_SUITE)
    assert suite.has_families is True
    for task_id in suite.family_task_union():
        impls = [g.impl for g in suite.resolved_graders(task_id)]
        assert impls == [
            "graders/aeval_outcome.py@v1",
            "graders/aeval_intel.py@v6",
            "graders/aeval_security.py@v2",
        ], task_id
    # (用例, 引擎) 唯一：aeval_intel 三族用例集互不相交且并为全体
    intel_cases = [
        set(s.task_ids()) for k, s in suite.families.items()
        if s.engine.impl.endswith("aeval_intel.py@v6")
    ]
    union = set().union(*intel_cases)
    assert all(not (a & b) for i, a in enumerate(intel_cases)
               for b in intel_cases[i + 1:])
    assert len(union) == 23
    # 安全族 = 全体 23 例（保全套件级全跑）
    # 安全族是结构判定族（指标不吃输入）：适用范围 = 显式 scope
    security = suite.families["security"]
    assert len(security.task_ids()) == 23
    assert security.input_task_ids() == ()
    assert security.binds("memory.update") is True
    # 安全族规格与 tbench-intel 侧逐项一致（跨 run 聚合键）
    tbench = load_suite(TBENCH_SUITE, suites_root=SUITES_ROOT)
    assert (suite.families["security"].spec_digest()
            == tbench.families["security"].spec_digest())


# --- loading: negative (fail-closed) --------------------------------------


def _load_error(root):
    with pytest.raises(SuiteError):
        _load(root)


def test_missing_family_file_fails(tmp_path):
    _write_suite(tmp_path)
    (tmp_path / "families" / "alpha.yaml").unlink()
    _load_error(tmp_path)


def test_bad_path_shape_fails(tmp_path):
    """激活清单只收 ``families/<key>.yaml`` 这一种便携相对路径。"""
    root = _write_suite(tmp_path)
    text = (root / "suite.yaml").read_text("utf-8")
    for bad in ("alpha.yaml", "../families/alpha.yaml", "families/alpha.txt"):
        (root / "suite.yaml").write_text(
            text.replace("families/alpha.yaml", bad), encoding="utf-8"
        )
        _load_error(root)
    # 形状对了但文件缺失 → 单向锁定：列了就必须在
    (root / "suite.yaml").write_text(
        text.replace("families/alpha.yaml", "families/missing.yaml"),
        encoding="utf-8",
    )
    _load_error(root)


def test_outcome_engine_must_equal_baseline(tmp_path):
    _write_suite(tmp_path, family_body={"impl": "graders/other.py@v1"})
    (tmp_path / "graders" / "other.py").write_text(BASE_GRADER, "utf-8")
    root = tmp_path
    spec = yaml.safe_load(
        (root / "families" / "alpha.yaml").read_text("utf-8")
    )
    spec["engine"]["layer"] = "outcome"
    (root / "families" / "alpha.yaml").write_text(
        yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8"
    )
    _load_error(root)


def test_duplicate_case_engine_across_families(tmp_path):
    _write_suite(tmp_path, families=("alpha", "beta"))
    root = tmp_path
    for key in ("alpha", "beta"):
        spec = yaml.safe_load(
            (root / "families" / f"{key}.yaml").read_text("utf-8")
        )
        spec["cases"] = {
            "task-a": {"role": "home", "inputs": {"identity_probe": {}}}
        }
        _dump_family(root / "families" / f"{key}.yaml", spec)
    _load_error(root)


def test_legacy_category_files_coexisting_fail(tmp_path):
    root = _write_suite(tmp_path)
    (root / "task_categories.yaml").write_text("categories: {}\n", "utf-8")
    _load_error(root)


def test_guard_impl_overlap_with_family_fails(tmp_path):
    root = _write_suite(tmp_path)
    text = (root / "suite.yaml").read_text("utf-8")
    (root / "suite.yaml").write_text(text, encoding="utf-8")
    # guards live in verdict.guards：与族的**指标库**同 impl 即重叠
    overlay = yaml.safe_load(text)
    overlay["verdict"]["guards"] = [
        {"impl": "graders/library.py@v1", "layer": "trajectory"}
    ]
    (root / "suite.yaml").write_text(
        yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    _load_error(root)


def test_outcome_veto_guard_fails(tmp_path):
    root = _write_suite(tmp_path)
    overlay = yaml.safe_load((root / "suite.yaml").read_text("utf-8"))
    overlay["verdict"]["guards"] = [
        {"impl": "graders/engine.py@v1", "layer": "outcome", "veto": True}
    ]
    (root / "suite.yaml").write_text(
        yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    _load_error(root)


def test_unknown_scope_field_fails(tmp_path):
    root = _write_suite(tmp_path)
    spec = yaml.safe_load(
        (root / "families" / "alpha.yaml").read_text("utf-8")
    )
    spec["scope"]["task-a"]["surprise"] = 1
    (root / "families" / "alpha.yaml").write_text(
        yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8",
    )
    _load_error(root)


def test_redline_requires_full_threshold(tmp_path):
    root = _write_suite(tmp_path)
    spec = yaml.safe_load(
        (root / "families" / "alpha.yaml").read_text("utf-8")
    )
    spec["redline"] = True
    spec["threshold"] = 0.9
    (root / "families" / "alpha.yaml").write_text(
        yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8",
    )
    _load_error(root)


def test_family_mode_requires_anchors_channel(tmp_path):
    root = _write_suite(tmp_path, anchors_channel=False)
    _load_error(root)


# --- offline reconciliation ------------------------------------------------


def test_offline_judging_requires_family_case(tmp_path):
    suite = _load(_write_suite(tmp_path))
    # 无数据集（离线判分契约）：sealed trial 的 task 必须在族用例并集
    with pytest.raises(GradingPipelineError, match="not declared by any"):
        load_suite_graders(suite, "unknown-task")
    graders = load_suite_graders(suite, "task-a")
    # 基线（engine.py）与族指标库（library.py）各加载一次
    assert [g.grader.id for g in graders] == ["demo-engine", "demo-engine"]


# --- one-way locking: chain digest ---------------------------------------


def test_family_edit_changes_identity_unactivated_does_not(tmp_path):
    root = _write_suite(tmp_path)
    before = _load(root).identity_digest
    # 编辑已激活族文件 → run 身份变化
    spec = yaml.safe_load(
        (root / "families" / "alpha.yaml").read_text("utf-8")
    )
    spec["metrics"][0]["inputs"]["task-a"] = {
        "identity_probes": ["改了"]
    }
    (root / "families" / "alpha.yaml").write_text(
        yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8",
    )
    assert _load(root).identity_digest != before
    # 未激活的族文件（不在清单里）→ 身份不变
    idle = before
    suite = _load(root)
    idle = suite.identity_digest
    (root / "families" / "inactive.yaml").write_text(
        (root / "families" / "alpha.yaml").read_text("utf-8"),
        encoding="utf-8",
    )
    assert _load(root).identity_digest == idle


# --- grader identities + sealed dimension model ---------------------------


def test_sealed_dimension_model_carries_engine_identities(tmp_path):
    suite = _load(_write_suite(tmp_path))
    model = sealed_dimension_model(suite)
    engine = model["families"]["alpha"]["engine"]
    assert engine["grader_id"] == "demo-engine"
    assert engine["grader_version"] == "v1"
    assert model["baseline"]["grader_id"] == "demo-engine"
    # 每个声明的判分器都注入身份：基线、守卫、分组族引擎（D7）
    pilot = load_suite(AEVAL_ROOT / "suites" / "tbench-pilot")
    model = sealed_dimension_model(pilot)
    assert set(model["families"]) == {"tbench"}
    assert model["families"]["tbench"]["engine"]["grader_id"] == "tbench-outcome"
    assert model["baseline"]["grader_id"] == "tbench-outcome"
    assert model["guards"][0]["grader_id"] == "tbench-trajectory"


def test_real_intel_identities_resolve():
    suite = load_suite(INTEL_SUITE)
    ids = resolve_grader_identities(suite)
    assert ids["graders/aeval_intel.py@v6"] == {
        "grader_id": "aeval-intel", "grader_version": "v6",
    }


# --- rollup: hand-computed numbers (D7) ------------------------------------


def _manifest_for(root: Path, run_id="r1", families=("alpha",),
                  engine_id="demo-engine"):
    suite = _load(root)
    from aeval.agents.dsh.release import build_official_dsh_lock
    from aeval.provenance import build_runtime_lock
    from aeval.contracts import OverlayIdentity, VersionsBundle
    model = sealed_dimension_model(suite)
    return RunManifest(
        run_id=run_id,
        runtime_lock=build_runtime_lock(
            release_locks={"dsh": build_official_dsh_lock()}
        ),
        overlay=OverlayIdentity(
            suite_id="demo-family", suite_version="0.1.0",
            overlay_digest="d" * 64, source_commit="9" * 40,
        ),
        dimension_model=model,
        versions=VersionsBundle(aeval_version="0.1.0"),
    )


def _graded(trial_id, index, task, engine_id, status, *, verdict=None,
            run_id="r1", reasons=(), veto=False):
    verdict = verdict or ("pass" if status == "pass" else
                          "fail" if status == "fail" else "cannot_judge")
    record = TrialRecord(
        trial_id=trial_id,
        coordinates={"run_id": run_id, "suite_id": "demo-family",
                     "suite_version": "0.1.0", "task_id": task,
                     "trial_index": index},
        stop_reason="agent_exit_0",
        verdict=verdict,
    )
    record.grades.append(GradeResult(
        grader_id=engine_id, grader_version="v1", layer="trajectory",
        veto=veto, score=(
            Score(value=1.0) if status != "cannot_judge"
            else Score(valid=False, invalid_reasons=["no sealed anchors"])
        ),
        status=status, reasons=list(reasons), metrics=None,
    ))
    return record


def test_family_rollup_hand_computed(tmp_path):
    root = _write_suite(tmp_path)
    manifest = _manifest_for(root)
    trials = [
        _graded("a0", 0, "task-a", "demo-engine", "pass"),
        _graded("a1", 1, "task-a", "demo-engine", "fail"),
        # 引擎 cannot_judge（有效试次）→ 出分母、入排除列（带原因）
        _graded("a2", 2, "task-a", "demo-engine", "cannot_judge",
                verdict="pass", reasons=["no sealed anchors for task"]),
        # 需求排除的试次 → 不进族行任何分母（照旧出分母 = 与终判一致）
        _graded("a3", 3, "task-a", "demo-engine", "pass",
                verdict="infra_invalid"),
        # 未声明该 task 的 run 试次（异 run 任务）→ 不进本族行
        _graded("a4", 4, "task-z", "demo-engine", "pass"),
    ]
    summary = aggregate_run(["r1"], trials, k=3, manifests=[manifest])
    assert summary.family_rollups is not None
    (family,) = summary.family_rollups
    assert (family.key, family.tasks, family.trials) == ("alpha", 1, 4)
    assert (family.valid, family.passes, family.fails) == (2, 1, 1)
    assert family.cannot_judge == 1
    assert family.cannot_judge_reasons == (
        "no sealed anchors for task ×1",
    )
    # 逐项指标层与任务映射
    assert summary.task_families == {"task-a": ("alpha",)}
    detail = summary.metric_detail
    assert detail is not None and detail.baseline_guards is not None
    # 引擎标签来自密封身份
    assert family.engines == ("demo-engine@v1",)


def test_family_cannot_judge_reasons_aggregate_sorted(tmp_path):
    root = _write_suite(tmp_path)
    manifest = _manifest_for(root)
    trials = [
        _graded("a0", 0, "task-a", "demo-engine", "cannot_judge",
                verdict="pass", reasons=["reason-b"]),
        _graded("a1", 1, "task-a", "demo-engine", "cannot_judge",
                verdict="pass", reasons=["reason-b"]),
        _graded("a2", 2, "task-a", "demo-engine", "cannot_judge",
                verdict="pass", reasons=["reason-a"]),
    ]
    summary = aggregate_run(["r1"], trials, k=3, manifests=[manifest])
    (family,) = summary.family_rollups
    assert family.cannot_judge == 3
    # reason ×count，按 count 降序、同数按字典序
    assert family.cannot_judge_reasons == (
        "reason-b ×2", "reason-a ×1",
    )


def test_grouping_family_matches_trial_verdict_rollup(tmp_path):
    """D7 特例：分组式类别（引擎 = 官方基线）行数字与试次终判口径一致。"""
    root = _write_suite(tmp_path)
    spec = yaml.safe_load(
        (root / "families" / "alpha.yaml").read_text("utf-8")
    )
    spec["engine"] = {"impl": "graders/engine.py@v1", "layer": "outcome"}
    # 分组式类别：引擎 = 官方基线 ⇒ 允许空指标清单；适用范围 = 显式 scope
    spec["metrics"] = []
    spec["scope"] = {
        "task-a": {"role": "home"},
        "task-b": {"role": "home"},
    }
    (root / "families" / "alpha.yaml").write_text(
        yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8",
    )
    suite = _load(root)
    manifest = _manifest_for(root)
    trials = [
        _graded("a0", 0, "task-a", "demo-engine", "pass"),
        _graded("a1", 1, "task-a", "demo-engine", "pass"),
        _graded("a2", 2, "task-a", "demo-engine", "fail"),
        _graded("b0", 0, "task-b", "demo-engine", "pass"),
        # 基线判不出、别层给了 pass：Q8 说试次留分母，终判 pass ⇒ 行里算
        # 通过。归组族不是第二个判定来源——行口径就是终判口径。
        _graded("b1", 1, "task-b", "demo-engine", "cannot_judge",
                verdict="pass", reasons=["r"]),
        # 真正出分母的试次：终判 cannot_judge ⇒ 本行计入排除（带原因）。
        _graded("b2", 2, "task-b", "demo-engine", "cannot_judge",
                verdict="cannot_judge", reasons=["r"]),
    ]
    summary = aggregate_run(["r1"], trials, k=3, manifests=[manifest])
    (family,) = summary.family_rollups
    # 基线引擎 = 族引擎 ⇒ 族行 = 终判口径：5 有效 4 过 1 败 + 1 排除
    assert (family.valid, family.passes, family.fails) == (5, 4, 1)
    assert family.cannot_judge == 1
    assert family.cannot_judge_reasons == ("无法判定 ×1",)


def test_multi_family_enrichment_counts_both_rows(tmp_path):
    """丰富模型：一个 task 被两个族测量——每族独立出数，试次不翻倍。"""
    root = _write_suite(tmp_path)
    # 第二族 beta：引擎同 impl 会被去重——用不同引擎文件
    (root / "graders" / "engine2.py").write_text(
        BASE_GRADER.replace('"demo-engine"', '"demo-engine-2"'), "utf-8",
    )
    beta = {
        "name": "Beta", "block": "core", "weight": 1.0, "threshold": 1.0,
        "redline": True,
        "engine": {"impl": "graders/engine2.py@v1", "layer": "trajectory",
                   "veto": True},
        "metrics": ["identity_probe"],
        "cases": {"task-a": {"role": "enrich"}},
    }
    _dump_family(root / "families" / "beta.yaml", beta)
    text = (root / "suite.yaml").read_text("utf-8")
    overlay = yaml.safe_load(text)
    overlay["families"].append("families/beta.yaml")
    (root / "suite.yaml").write_text(
        yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    manifest = _manifest_for(root)
    trials = [
        _graded("a0", 0, "task-a", "demo-engine", "pass"),
        _graded("a1", 1, "task-a", "demo-engine", "fail"),
        _graded("a2", 2, "task-a", "demo-engine-2", "pass"),
    ]
    summary = aggregate_run(["r1"], trials, k=3, manifests=[manifest])
    by_key = {f.key: f for f in summary.family_rollups}
    # alpha 行只数自己的引擎判定；beta 行只数 beta 引擎
    assert by_key["alpha"].trials == 3
    assert (by_key["alpha"].valid, by_key["alpha"].passes) == (2, 1)
    assert by_key["beta"].trials == 3
    assert (by_key["beta"].valid, by_key["beta"].passes) == (1, 1)
    assert summary.task_families == {"task-a": ("alpha", "beta")}


# --- multi-run aggregation (P14) -------------------------------------------


def _family_manifest(run_id, key, *, name="Alpha", block="core",
                     weight=1.0, threshold=0.8, redline=False,
                     engine_id="demo-engine", cases=("task-a",),
                     metrics=("identity_probe",),
                     baseline="graders/engine.py@v1",
                     baseline_id=None, guards=(), suite_id="demo-family"):
    from aeval.agents.dsh.release import build_official_dsh_lock
    from aeval.provenance import build_runtime_lock
    from aeval.contracts import OverlayIdentity, VersionsBundle
    case_map = {
        task: {"role": "home"} for task in cases
    }
    return RunManifest(
        run_id=run_id,
        runtime_lock=build_runtime_lock(
            release_locks={"dsh": build_official_dsh_lock()}
        ),
        overlay=OverlayIdentity(
            suite_id=suite_id, suite_version="0.1.0",
            overlay_digest="d" * 64, source_commit="9" * 40,
        ),
        dimension_model={
            "families": {
                key: {
                    "name": name, "block": block, "weight": weight,
                    "threshold": threshold, "redline": redline,
                    "engine": {"impl": "graders/engine.py@v1",
                               "grader_id": engine_id,
                               "grader_version": "v1"},
                    "metrics": [{"id": mid} for mid in metrics],
                    # v5 密封形状：适用范围由 metrics[].inputs 派生，没有
                    # 输入的结构判定族退回 scope 的显式列表。
                    "scope": case_map,
                },
            },
            "blocks": {block: "核心"},
            "baseline": {"impl": baseline,
                         "grader_id": baseline_id or engine_id,
                         "grader_version": "v1"},
            "guards": list(guards),
        },
        versions=VersionsBundle(aeval_version="0.1.0"),
    )


def test_same_suite_reruns_merge_into_one_row():
    """同一套件、同一条件的重复 run：可比 ⇒ 同键族合并（唯一合法合并）。"""
    m1 = _family_manifest("r1", "alpha")
    m2 = _family_manifest("r2", "alpha")
    trials = [
        _graded("a0", 0, "task-a", "demo-engine", "pass", run_id="r1"),
        _graded("a1", 1, "task-a", "demo-engine", "fail", run_id="r2"),
    ]
    summary = aggregate_run(["r1", "r2"], trials, k=3, manifests=[m1, m2])
    (family,) = summary.family_rollups
    assert family.key == "alpha"
    assert family.runs == ("r1", "r2")
    assert family.tasks == 1
    assert set(family.engines) == {"demo-engine@v1"}
    assert (family.valid, family.passes, family.fails) == (2, 1, 1)
    assert summary.aggregation is not None and summary.aggregation.comparable


def test_incomparable_spec_conflict_splits_rows():
    """规格不一致 ⇒ 不可比 ⇒ 各自成行（不再拒绝聚合，也不再取先到）。"""
    m1 = _family_manifest("r1", "alpha")
    m2 = _family_manifest("r2", "alpha", threshold=0.9)
    summary = aggregate_run(["r1", "r2"], [], k=3, manifests=[m1, m2])
    rows = {family.key: family for family in summary.family_rollups}
    assert len(rows) == 2
    assert {family.spec["threshold"] for family in rows.values()} == {0.8, 0.9}
    for family in rows.values():
        assert family.runs in (("r1",), ("r2",))
    assert summary.aggregation is not None
    assert not summary.aggregation.comparable
    assert summary.aggregation.groups == (("r1",), ("r2",))


def test_engine_difference_splits_rows_by_run():
    """引擎不同 ⇒ 不可比 ⇒ 各有各的行；试次不跨行泄漏。"""
    m1 = _family_manifest("r1", "alpha", engine_id="engine-a")
    m2 = _family_manifest("r2", "alpha", engine_id="engine-b",
                          cases=("task-a", "task-b"))
    trials = [
        _graded("a0", 0, "task-a", "engine-a", "pass", run_id="r1"),
        _graded("a1", 0, "task-a", "engine-b", "fail", run_id="r2"),
        _graded("b0", 1, "task-b", "engine-b", "pass", run_id="r2"),
    ]
    summary = aggregate_run(["r1", "r2"], trials, k=3, manifests=[m1, m2])
    assert len(summary.family_rollups) == 2
    by_run = {family.runs: family for family in summary.family_rollups}
    first, second = (
        by_run[("r1",)],
        by_run[("r2",)],
    )
    # 各行的用例数只算自己那份声明：r1 只有 task-a，r2 有 task-a/task-b。
    assert (first.tasks, first.trials, first.passes) == (1, 1, 1)
    assert (second.tasks, second.trials, second.passes) == (2, 2, 1)
    assert first.engines == ("engine-a@v1",)
    assert second.engines == ("engine-b@v1",)
    # 行键唯一（同一套件的两个不可比条件 → 补组序号）。
    assert len({family.key for family in summary.family_rollups}) == 2


def test_family_row_counts_only_runs_declaring_it():
    """未声明该族的 run 不往这一行的「试次」里计数（旧口径会灌水）。"""
    m1 = _family_manifest("r1", "alpha", cases=("task-a",))
    m2 = _family_manifest("r2", "beta", cases=("task-a",))
    trials = [
        _graded("a0", 0, "task-a", "demo-engine", "pass", run_id="r1"),
        _graded("b0", 0, "task-a", "demo-engine", "pass", run_id="r2"),
    ]
    summary = aggregate_run(["r1", "r2"], trials, k=3, manifests=[m1, m2])
    (alpha,) = (
        family for family in summary.family_rollups if family.family_key == "alpha"
    )
    assert alpha.runs == ("r1",)
    assert (alpha.trials, alpha.valid, alpha.passes) == (1, 1, 1)


def test_cross_suite_aggregation_is_refused():
    """不同套件 = 不同实验：一份报告只聚合同一套件，跨套件直接拒绝。"""
    m1 = _family_manifest("r1", "alpha")
    m2 = _family_manifest("r2", "alpha", suite_id="other-suite")
    with pytest.raises(ValueError) as excinfo:
        aggregate_run(["r1", "r2"], [], k=3, manifests=[m1, m2])
    message = str(excinfo.value)
    assert "cross-suite aggregation is not supported" in message
    assert "demo-family@0.1.0" in message and "other-suite@0.1.0" in message


def test_cross_suite_manifest_mismatch_reports_expected_count():
    m1 = _family_manifest("r1", "alpha")
    m2 = _family_manifest("r2", "alpha", suite_id="other-suite")
    m3 = _family_manifest("r3", "alpha", suite_id="third-suite")
    with pytest.raises(ValueError) as excinfo:
        aggregate_run(["r1", "r2", "r3"], [], k=3, manifests=[m1, m2, m3])
    assert "3 suites" in str(excinfo.value)


def test_same_suite_conditions_report_blocking_dimensions():
    """同套件的两个条件（换执行参数）= 真不可比：分行 + 列出差异维度。"""
    m1 = _family_manifest("r1", "alpha")
    m2 = _family_manifest("r2", "alpha")
    m2 = m2.model_copy(update={"config_hash": "c" * 64})
    summary = aggregate_run(["r1", "r2"], [], k=3, manifests=[m1, m2])
    agg = summary.aggregation
    assert agg is not None
    assert agg.suite_id == "demo-family"
    assert any("config" in item for item in agg.blocked)
    text = render_static_report(summary)
    assert "同套件、2 组条件" in text
    assert "未合并：demo-family@0.1.0" in text
    assert len(summary.family_rollups) == 2


def test_case_set_difference_splits_rows():
    """同一族键、不同用例集 ⇒ 不可比 ⇒ 两行各自记自己的用例。"""
    m1 = _family_manifest("r1", "alpha", cases=("task-a",))
    m2 = _family_manifest("r2", "alpha", cases=("task-b",))
    summary = aggregate_run(["r1", "r2"], [], k=3, manifests=[m1, m2])
    assert sorted(family.task_ids for family in summary.family_rollups) == [
        ("task-a",),
        ("task-b",),
    ]


# --- watermark: family dimensions -----------------------------------------


def test_watermark_family_dimensions_and_redline(tmp_path):
    root = _write_suite(
        tmp_path, families=("alpha",),
        family_body={"threshold": 0.5, "cases": {
            "task-a": {
                "role": "home",
                "inputs": {"identity_probe": {"identity_probes": ["x"]}},
            },
        }},
    )
    manifest = _manifest_for(root)
    trials = [
        _graded("a0", 0, "task-a", "demo-engine", "pass"),
        _graded("a1", 1, "task-a", "demo-engine", "fail"),
    ]
    summary = aggregate_run(["r1"], trials, k=3, manifests=[manifest])
    wm = score_watermark(summary)
    assert wm is not None
    (dim,) = wm.dimensions
    assert dim.key == "alpha"
    assert dim.value == 0.5
    assert dim.achievement == 1.0  # 0.5 / 0.5
    assert wm.redline_tripped is False
    # 红线族：阈值 1.0，一败即告警
    root2 = _write_suite(
        tmp_path / "red", families=("alpha",),
        family_body={"threshold": 1.0, "redline": True, "cases": {
            "task-a": {
                "role": "home",
                "inputs": {"identity_probe": {"identity_probes": ["x"]}},
            },
        }},
    )
    manifest2 = _manifest_for(root2)
    summary2 = aggregate_run(["r1"], trials, k=3, manifests=[manifest2])
    wm2 = score_watermark(summary2)
    assert wm2.redline_tripped is True
    assert wm2.redline_offenders == ("Alpha",)


def test_report_renders_family_sections(tmp_path):
    root = _write_suite(tmp_path)
    spec = yaml.safe_load(
        (root / "families" / "alpha.yaml").read_text("utf-8")
    )
    spec["metrics"][0]["inputs"]["task-b"] = {"identity_probes": ["x"]}
    spec["scope"]["task-b"] = {"role": "home"}
    (root / "families" / "alpha.yaml").write_text(
        yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8",
    )
    manifest = _manifest_for(root)
    trials = [
        _graded("a0", 0, "task-a", "demo-engine", "pass"),
        _graded("a1", 1, "task-a", "demo-engine", "cannot_judge",
                verdict="pass", reasons=["no sealed anchors"]),
        _graded("b0", 0, "task-b", "demo-engine", "pass"),
    ]
    summary = aggregate_run(["r1"], trials, k=3, manifests=[manifest])
    text = render_static_report(summary)
    assert "## 按类别结果" in text
    assert "| 类别（指标族） | 任务数 | 试次 | 有效 | 通过 | 失败 " in text
    assert "| Alpha(alpha) | 2 | 3 | 2 | 2 | 0 | 1 |" in text
    assert "Alpha(alpha) 排除原因：no sealed anchors ×1" in text
    assert "## 终判结果（按类别分组）" in text
    assert "## 逐项指标（按族 / 任务）" in text
    assert "### 基线与守卫（官方评分 + 有效性门，无类别行）" in text


def test_task_line_lists_what_it_judged_not_the_skip_complement():
    """任务行只回答"这项任务判了什么" + 跳过项数。

    把声明清单的补集（哪些指标被跳过）逐条抄进每个任务行，就是让同一批指标名
    在 11 个任务行里各重复一遍——上面那张指标表已经逐项连同跳过原因列全了。
    """
    from aeval.contracts import MetricOutcome
    from aeval.metrics.dashboard import render_dashboard_html

    manifest = _family_manifest(
        "r1", "alpha", metrics=("identity_probe", "format_check"),
        cases=("task-a", "task-b"),
    )

    def _graded_with(trial_id, index, task, outcomes):
        record = _graded(trial_id, index, task, "demo-engine", "pass")
        record.grades[-1].metrics = list(outcomes)
        return record

    judge_a = MetricOutcome(
        name="identity_probe", category="integrity", status="ok"
    )
    skip_b = MetricOutcome(
        name="format_check", category="efficiency", status="skipped",
        reasons=["no format specs declared"],
    )
    judge_b = MetricOutcome(
        name="format_check", category="efficiency", status="ok"
    )
    skip_a = MetricOutcome(
        name="identity_probe", category="integrity", status="skipped",
        reasons=["no identity probes declared"],
    )
    summary = aggregate_run(
        ["r1"],
        [
            _graded_with("a0", 0, "task-a", [judge_a, skip_b]),
            _graded_with("b0", 0, "task-b", [skip_a, judge_b]),
        ],
        k=3, manifests=[manifest],
    )
    text = render_static_report(summary)
    assert "### Alpha(alpha) · 引擎 demo-engine@v1 · 适用任务 2 · 声明指标 2 项" in text
    assert "| 指标（按声明顺序） | 判定 ok | 降级 | 违规 | 跳过 | 跳过原因 |" in text
    # 声明清单不再单独抄一遍（表就是按声明顺序列的）。
    assert "声明指标 2 项（判分顺序）" not in text
    # 已判的写出来；跳过的只报项数。
    assert "- task-a [home] （1 试）：identity_probe（ok 1）；跳过 1 项" in text
    assert "- task-b [home] （1 试）：format_check（ok 1）；跳过 1 项" in text
    task_lines = "\n".join(
        line for line in text.splitlines() if line.startswith("- task-")
    )
    assert "format_check" not in task_lines.split("\n")[0]
    html = render_dashboard_html(summary)
    assert "<th>任务</th><th>试次</th><th>本任务判的指标</th><th>跳过（项）</th>" in html
    assert 'identity_probe（<span style="color:var(--pass)">ok 1</span>）' in html
    task_table = html.split("本任务判的指标")[1]
    # 跳过项不再逐条复列（旧写法在每个任务行里把 11 个指标名抄一遍）。
    assert "跳过：" not in task_table
    rows = re.findall(r"<tr>(.*?)</tr>", task_table)
    assert ("identity_probe" in rows[0]) and ("format_check" not in rows[0])
    assert ("format_check" in rows[1]) and ("identity_probe" not in rows[1])


# --- composition: dangling case reconciliation ----------------------------


def test_dangling_family_case_fails_composition(tmp_path):
    shutil.copytree(INTEL_SUITE, tmp_path / "aeval-intel")
    root = tmp_path / "aeval-intel"
    spec = yaml.safe_load(
        (root / "families" / "memory.yaml").read_text("utf-8")
    )
    spec["metrics"][0]["inputs"]["memory.ghost-case"] = {}
    (root / "families" / "memory.yaml").write_text(
        yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8",
    )
    from aeval.suite_loader.composition import compose_harbor_job
    with pytest.raises(SuiteError, match="does not scan"):
        compose_harbor_job(_load(root))


def test_undeclared_dataset_task_is_legal(tmp_path):
    """扫描到但未被任何族声明的 task 合法（只吃基线 + 守卫）。"""
    from aeval.suite_loader.composition import compose_harbor_job
    compose_harbor_job(load_suite(INTEL_SUITE))  # 全体声明 → 不悬空
    shutil.copytree(INTEL_SUITE, tmp_path / "aeval-intel")
    root = tmp_path / "aeval-intel"
    spec = yaml.safe_load(
        (root / "families" / "memory.yaml").read_text("utf-8")
    )
    for metric in spec["metrics"]:
        (metric.get("inputs") or {}).pop("memory.update", None)
    spec.get("scope", {}).pop("memory.update", None)
    (root / "families" / "memory.yaml").write_text(
        yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8",
    )
    compose_harbor_job(_load(root))  # 未声明 memory.update → 合法

def _multi_graded(trial_id, index, task, *grades, run_id="r1"):
    """一个试次带多个判分器结果（终判归属测试用）。"""
    record = _graded(trial_id, index, task, grades[0][0], grades[0][1],
                     run_id=run_id, veto=grades[0][2])
    for engine_id, status, veto in grades[1:]:
        record.grades.append(GradeResult(
            grader_id=engine_id, grader_version="v1", layer="trajectory",
            veto=veto,
            score=(
                Score(value=1.0) if status != "cannot_judge"
                else Score(valid=False, invalid_reasons=["no sealed anchors"])
            ),
            status=status, reasons=[], metrics=None,
        ))
    # 终判用生产折叠序（veto fail → fail；否则任一 fail → fail）：
    # 测试不自己编一套折叠规则。
    from aeval.verdict.base import decide_final_verdict

    record.verdict = decide_final_verdict(record.grades)
    return record


def test_verdict_sources_attribute_each_failure_to_its_layer(tmp_path):
    """「终判来源分解」：每个失败试次由哪一层判的（基线/族阈值/守卫 veto）。

    这条是报告可读性的底线：族行数字与基线行相近时，读者必须能看出
    "失败分别是谁判的"，否则只能得出"族是基线复述"的错误结论。
    """
    root = _write_suite(tmp_path)
    manifest = _family_manifest(
        "r1", "alpha", cases=("task-a", "task-b"),
        baseline="graders/outcome.py@v1", baseline_id="demo-outcome",
        guards=({"impl": "graders/guard.py@v1", "grader_id": "demo-guard",
                 "grader_version": "v1", "veto": True},),
    )
    trials = [
        # 全通过 → 不进分解
        _multi_graded("t0", 0, "task-a",
                      ("demo-outcome", "pass", False),
                      ("demo-engine", "pass", False)),
        # 基线判失败（族判 pass）→ 归属基线，族行记"他层"
        _multi_graded("t1", 1, "task-a",
                      ("demo-outcome", "fail", False),
                      ("demo-engine", "pass", False)),
        # 族阈值折叠判失败（基线判 pass）→ 归属族，族行记"本族"
        _multi_graded("t2", 2, "task-a",
                      ("demo-outcome", "pass", False),
                      ("demo-engine", "fail", False)),
        # 守卫 veto 推翻 pass（基线与族都 pass）→ 归属守卫
        _multi_graded("t3", 3, "task-b",
                      ("demo-outcome", "pass", False),
                      ("demo-engine", "pass", False),
                      ("demo-guard", "fail", True)),
    ]
    summary = aggregate_run(["r1"], trials, k=3, manifests=[manifest])
    sources = summary.verdict_sources
    assert [s.trial_id for s in sources] == ["t1", "t2", "t3"]
    assert [s.mechanism for s in sources] == ["判定", "阈值折叠", "veto"]
    assert sources[0].roles == ("官方基线",)
    assert sources[1].roles == ("族 Alpha(alpha)",)
    assert sources[2].roles == ("守卫",)
    assert sources[2].deciders == ("demo-guard@v1",)
    assert "否决了 demo-outcome@v1、demo-engine@v1 的 pass" in sources[2].note

    (family,) = summary.family_rollups
    assert (family.valid, family.passes, family.fails) == (4, 3, 1)
    # 判定分工：本族判 fail 1（t2）· 本族判 pass 而终判 fail 2（t1、t3）
    assert (family.deciding_fails, family.foreign_fails) == (1, 2)

    text = render_static_report(summary)
    assert "## 终判来源分解" in text
    assert "本族 1 / 他层 2" in text
    assert "失败 3 = 官方基线 1 · 族 1 · veto 1（守卫 1）" in text
    assert "| t2 |" in text


def test_family_row_without_foreign_fails_prints_zero_division(tmp_path):
    """族判定与终判完全一致时，分工列如实写 0（不是省略）。"""
    root = _write_suite(tmp_path)
    manifest = _family_manifest(
        "r1", "alpha", cases=("task-a", "task-b"),
        baseline="graders/outcome.py@v1", baseline_id="demo-outcome",
    )
    trials = [
        _multi_graded("t0", 0, "task-a",
                      ("demo-outcome", "pass", False),
                      ("demo-engine", "pass", False)),
        _multi_graded("t1", 1, "task-b",
                      ("demo-outcome", "fail", False),
                      ("demo-engine", "fail", False)),
    ]
    summary = aggregate_run(["r1"], trials, k=3, manifests=[manifest])
    (family,) = summary.family_rollups
    assert (family.deciding_fails, family.foreign_fails) == (1, 0)
    text = render_static_report(summary)
    assert "本族 1 / 他层 0" in text
    assert "失败 1 = 官方基线 1 · 族 1" in text


def test_verdict_source_names_every_family_sharing_an_engine(tmp_path):
    """一个引擎被多族共用时，归属要按用例集命中列出所有相关族。

    aeval-intel 的智能度/红线/记忆三族共用 ``aeval-intel@v4``；按
    grader_id 一对一命名会把红线族判的失败写成别的族。
    """
    root = _write_suite(tmp_path, families=("alpha", "beta"))
    from aeval.agents.dsh.release import build_official_dsh_lock
    from aeval.contracts import OverlayIdentity, VersionsBundle
    from aeval.provenance import build_runtime_lock

    shared = {
        "families": {
            "alpha": {
                "name": "Alpha", "block": "core", "weight": 1.0,
                "threshold": 0.8, "redline": False,
                "engine": {"impl": "graders/engine.py@v1",
                           "grader_id": "demo-engine", "grader_version": "v1"},
                "scope": {"task-a": {"role": "home"}, "task-b": {"role": "home"}},
            },
            "beta": {
                "name": "Beta", "block": "core", "weight": 1.0,
                "threshold": 1.0, "redline": True,
                "engine": {"impl": "graders/engine.py@v1",
                           "grader_id": "demo-engine", "grader_version": "v1"},
                "scope": {"task-b": {"role": "home"}},
            },
        },
        "blocks": {"core": "核心"},
        "baseline": {"impl": "graders/outcome.py@v1",
                     "grader_id": "demo-outcome", "grader_version": "v1"},
        "guards": [],
    }
    manifest = RunManifest(
        run_id="r1",
        runtime_lock=build_runtime_lock(
            release_locks={"dsh": build_official_dsh_lock()}
        ),
        overlay=OverlayIdentity(
            suite_id="demo-family", suite_version="0.1.0",
            overlay_digest="d" * 64, source_commit="9" * 40,
        ),
        dimension_model=shared,
        versions=VersionsBundle(aeval_version="0.1.0"),
    )
    trials = [
        # task-a 只被 alpha 声明 → 角色只列 Alpha
        _multi_graded("t0", 0, "task-a",
                      ("demo-outcome", "pass", False),
                      ("demo-engine", "fail", False)),
        # task-b 被 alpha + beta 声明 → 两个族都是决定层
        _multi_graded("t1", 1, "task-b",
                      ("demo-outcome", "pass", False),
                      ("demo-engine", "fail", False)),
    ]
    summary = aggregate_run(["r1"], trials, k=3, manifests=[manifest])
    by_trial = {s.trial_id: s for s in summary.verdict_sources}
    assert by_trial["t0"].roles == ("族 Alpha(alpha)",)
    assert by_trial["t1"].roles == ("族 Alpha(alpha)、族 Beta(beta)",)
    assert by_trial["t1"].mechanism == "阈值折叠"
    rows = {f.key: f for f in summary.family_rollups}
    assert (rows["alpha"].deciding_fails, rows["beta"].deciding_fails) == (2, 1)

# --- v5：指标清单是判分真源 ------------------------------------------------


def test_family_must_declare_its_metrics(tmp_path):
    """漏写指标清单 = 声明错误（只有分组式类别允许空清单）。"""
    _write_suite(tmp_path, family_body={"metrics": []})
    _load_error(tmp_path)


def test_declared_metric_must_exist_in_library(tmp_path):
    """声明了却判不了的指标 → 装配前报错，不当成"被跳过的行"。"""
    suite = _load(_write_suite(tmp_path, family_body={
        "metrics": ["identity_probe", "nope_not_implemented"],
    }))
    with pytest.raises(GradingPipelineError, match="does not implement"):
        validate_metric_contracts(suite)


def test_declared_severity_must_match_implementation(tmp_path):
    """声明 severity 与实现 category 互相矛盾 → 报错（不静默改 veto 语义）。"""
    binding = {
        "task-a": {
            "title": "任务甲",
            "role": "home",
            "inputs": {"integrity_guard": {"identity_probes": ["你是谁"]}},
        }
    }
    suite = _load(_write_suite(tmp_path, family_body={
        "metrics": [{"id": "integrity_guard", "severity": "score"}],
        "cases": binding,
    }))
    with pytest.raises(GradingPipelineError, match="severity: score"):
        validate_metric_contracts(suite)
    binding_intel = {
        "task-a": {
            "title": "任务甲",
            "role": "home",
            "inputs": {"identity_probe": {"identity_probes": ["你是谁"]}},
        }
    }
    suite = _load(_write_suite(tmp_path / "b", family_body={
        "metrics": [{"id": "identity_probe", "severity": "integrity"}],
        "cases": binding_intel,
    }))
    with pytest.raises(GradingPipelineError, match="severity: integrity"):
        validate_metric_contracts(suite)


def test_family_engine_must_be_a_metric_library(tmp_path):
    """族引擎必须是指标库（METRICS 注册表）——不再有第二份代码内清单。"""
    from aeval.verdict.loader import GraderLoadError

    suite = _load(_write_suite(tmp_path, family_body={
        "impl": "graders/engine.py@v1",
        "metrics": ["identity_probe"],
    }))
    with pytest.raises(GraderLoadError, match="METRICS"):
        load_suite_graders(suite, "task-a")


def test_declared_metric_without_outcome_is_visible(tmp_path):
    """声明了但没有判定落点的指标：0 计数 + 注明，不静默消失。"""
    manifest = _family_manifest(
        "r1", "alpha", metrics=("identity_probe", "format_check"),
    )
    trials = [
        _graded("a0", 0, "task-a", "demo-engine", "pass", run_id="r1"),
    ]
    summary = aggregate_run(["r1"], trials, k=3, manifests=[manifest])
    text = render_static_report(summary)
    # 声明清单不再单独列一遍（指标表就按声明顺序列同样的 id）——但"声明了几项"
    # 与"哪一项没有落点"都要看得见。
    assert "声明指标 2 项" in text
    assert "| identity_probe | 0 | 0 | 0 | 0 | 本 run 未产出判定（声明项） |" in text
    assert "| format_check | 0 | 0 | 0 | 0 | 本 run 未产出判定（声明项） |" in text

def test_scope_must_be_covered_by_metric_inputs(tmp_path):
    """scope 里写着某个 task、却没有任何指标在它上面有输入 = 声明错误。

    Step 3 之后族的适用范围由指标输入派生，scope 只放显示元数据——
    一条谁都测不了的 scope 条目必须 fail-closed，而不是静默成一个空行。
    """
    suite_dir = _write_suite(tmp_path, family_body={
        "metrics": [
            {"id": "format_check",
             "inputs": {"task-b": {"format_specs": ["x"]}}},
        ],
    })
    spec_path = suite_dir / "families" / "alpha.yaml"
    spec = yaml.safe_load(spec_path.read_text("utf-8"))
    spec["scope"] = {
        "task-a": {"title": "任务甲", "role": "home"},
        "task-b": {"title": "任务乙", "role": "home"},
    }
    _dump_family(spec_path, spec)
    _load_error(suite_dir)


def test_unknown_input_key_fails_the_load(tmp_path):
    """代码侧不再有锚点键词表：probe 构造指标时拒绝未知键 ⇒ 加载期报错。

    这条正是"第二真源消失"的证据——键的合法性只由指标实现自己说，
    声明侧写错一个键就会在装配前失败，而不是被静默忽略成一个 skip。
    """
    suite = _load(_write_suite(tmp_path, family_body={
        "metrics": ["identity_probe"],
        "cases": {
            "task-a": {
                "title": "任务甲",
                "role": "home",
                "inputs": {"identity_probe": {"typo_key": ["x"]}},
            }
        },
    }))
    with pytest.raises(GradingPipelineError, match="cannot be built"):
        validate_metric_contracts(suite)


def test_family_without_inputs_never_reads_the_sealed_channel(tmp_path):
    """不消费输入的族（结构判定）不受缺锚点影响：照常出判定。"""
    suite = _load(_write_suite(tmp_path, family_body={
        "metrics": ["identity_probe"],
        "cases": {},
    }))
    spec = suite.families["alpha"]
    # 没有输入 ⇒ 适用范围为空元组 ⇒ binds 对每条试次都成立（结构判定族）
    assert spec.inputs_for("task-a") == {}
    assert spec.task_ids() == ()
    assert spec.binds("task-a") is True
    graders = load_suite_graders(suite, "task-a")
    assert [g.grader.id for g in graders] == ["demo-engine", "demo-engine"]


# --- v5 Step 2：指标自带 layer，族按层装配 ---------------------------------


def test_metric_layer_must_match_the_family_engine(tmp_path):
    """outcome 层不会装上 trajectory 指标：声明不一致即拒绝装载。"""
    _write_suite(tmp_path, family_body={
        "metrics": [
            {"id": "identity_probe", "layer": "outcome",
             "inputs": {"task-a": {"identity_probes": ["x"]}}},
        ],
    })
    _load_error(tmp_path)


def test_metric_layer_defaults_to_the_engine_layer(tmp_path):
    suite = _load(_write_suite(tmp_path))
    spec = suite.families["alpha"]
    assert spec.metric_layer("identity_probe") == "trajectory"
    # 密封声明里逐项可见（可审计）
    model = suite.dimension_model["families"]["alpha"]
    assert model["metrics"][0]["id"] == "identity_probe"


# --- v5 Step 3：用例退场，适用范围由指标输入派生 ---------------------------


def test_family_reach_is_derived_from_metric_inputs(tmp_path):
    """族的适用范围 = 指标输入的任务键；scope 只放显示元数据。"""
    suite = _load(_write_suite(tmp_path, family_body={
        "metrics": [
            {"id": "identity_probe",
             "inputs": {"task-a": {"identity_probes": ["x"]}}},
            {"id": "format_check",
             "inputs": {"task-b": {"format_specs": ["y"]}}},
        ],
        "scope": {"task-a": {"title": "任务甲", "role": "home"}},
    }))
    spec = suite.families["alpha"]
    # task-b 没有任何 scope 条目也照样在范围内：它的输入在指标名下
    assert spec.task_ids() == ("task-a", "task-b")
    assert spec.scope["task-a"].title == "任务甲"
    assert "task-b" not in spec.scope
    assert spec.inputs_for("task-b") == {"format_check": {"format_specs": ["y"]}}
    assert suite.family_task_union() == {"task-a", "task-b"}


def test_coverage_index_parser_counts_metric_rows_only():
    """覆盖索引的「逐项指标」列 = 报告里真正的指标行数。

    两个曾出错的地方：① 表头 ``| 指标（按声明顺序） |`` 不是指标（旧口径按
    "非空首列"计数，改表头文字后每族虚高 +1）；② 守卫的声明清单一行一项，也该
    计进去（旧口径只数族块，导致"指标全在守卫行"的 tbench-offline 显示成 0）。
    """
    import importlib.util
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "offline_chain_run",
        root / "examples" / "offline-chain" / "run_offline_chain.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    text = "\n".join([
        "## 按类别结果（指标族）",
        "",
        "| 类别（指标族） | 任务 | 试次 | 通过 | 失败 | 无法判定 |",
        "|---|---|---|---|---|---|",
        "| 基础连通(health) | 3 | 3 | 3 | 0 | 0 |",
        "",
        "## 逐项指标（按族 / 任务）",
        "",
        "### 会话智能度(intelligence) · 引擎 aeval-intel@v6 · 适用任务 2 · 声明指标 2 项",
        "",
        "| 指标（按声明顺序） | 判定 ok | 降级 | 违规 | 跳过 | 跳过原因 |",
        "|---|---|---|---|---|---|",
        "| 回复简洁度(response_brevity) | 3 | 0 | 0 | 0 | — |",
        "| 歧义澄清(clarification) | 3 | 0 | 0 | 0 | — |",
        "",
        "- 守卫 tbench-trajectory@v1（8 试 · 声明清单 9 项 · 读密封 ATIF 轨迹）",
        "",
        "| 指标 | 判定 ok | 降级 | 违规 | 跳过 | 跳过原因 |",
        "|---|---|---|---|---|---|",
        "| 步数效率(step_efficiency) | 8 | 0 | 0 | 0 | — |",
    ])
    families = module._report_families(text)
    assert families["intelligence"] == ["response_brevity", "clarification"]
    # 归组族的落点是「按类别结果」的族行，指标清单为空（不是"漏了"）。
    assert families["health"] == []
    # 表头 / 守卫行不会被记成族，也不会被记成指标。
    assert "指标（按声明顺序）" not in families
    assert "step_efficiency" not in families
    guard = "- 守卫 tbench-trajectory@v1（8 试 · 声明清单 9 项 · 读密封 ATIF 轨迹）"
    assert module._GUARD_DECLARED.match(guard).group(1) == "9"
