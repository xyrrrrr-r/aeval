"""指标目录（v5 Step 6）单元测试：一处定义、两处引用。

判分指标的**原子定义**（层/严重度/降级线 + 归属角色）只住在
``verdict.metric_catalog``；族文件与守卫声明只写 ``id`` 引用（族可以带自
己的逐任务输入）。这一组测试盯住加载期的 fail-closed 约束，以及
"目录化不改变判分契约"。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aeval.suite_loader.loader import load_suite
from aeval.suite_models import SuiteError

AEVAL_ROOT = Path(__file__).parents[3]
SUITES_ROOT = AEVAL_ROOT / "suites"

BASE_GRADER = '''
GRADER_ID = "demo-outcome"
GRADER_VERSION = "v1"
async def grade(record):
    from aeval.contracts import GradeResult, Score
    return GradeResult(
        grader_id=GRADER_ID, grader_version=GRADER_VERSION,
        layer="outcome", veto=False, score=Score(value=1.0),
        status="pass", reasons=[], metrics=None,
    )
'''

METRIC_LIBRARY = '''
from aeval.contracts import MetricOutcome
from aeval.verdict.trajectory.metrics import TrajectoryMetric

GRADER_ID = "demo-library"
GRADER_VERSION = "v1"
REQUIRED_FIELDS = []


class _Metric(TrajectoryMetric):
    category = "score"

    def __init__(self, name, inputs=None):
        self.name = name
        self.inputs = inputs

    def evaluate(self, evidence):
        return MetricOutcome(
            name=self.name, category=self.category, status="ok",
            score=1.0, weight=self.weight, reasons=["demo"],
        )


class _Integrity(_Metric):
    category = "integrity"


def _needs_known_keys(inputs):
    unknown = sorted(set(inputs or ()) - {"identity_probes"})
    if unknown:
        raise ValueError(f"unknown anchor keys {unknown}")
    return _Metric("identity_probe", inputs)


METRICS = {
    "identity_probe": _needs_known_keys,
    "format_check": lambda inputs: _Metric("format_check", inputs),
    "integrity_guard": lambda inputs: _Integrity("integrity_guard", inputs),
}
'''


def _catalog_entry(metric_id, *, layer="trajectory", severity="score",
                   classification="dimension", **extra):
    entry = {"id": metric_id, "layer": layer, "severity": severity,
             "classification": classification}
    entry.update(extra)
    return entry


def _write_suite(
    tmp_path: Path,
    *,
    catalog: list[dict] | None = None,
    family: dict | None = None,
    guard: dict | None = None,
) -> Path:
    """最小离线套件：基线（legacy outcome）+ 可选族 + 可选守卫 + 目录。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "graders").mkdir(exist_ok=True)
    (tmp_path / "graders" / "engine.py").write_text(BASE_GRADER, encoding="utf-8")
    (tmp_path / "graders" / "library.py").write_text(METRIC_LIBRARY, encoding="utf-8")
    (tmp_path / "graders" / "guardlib.py").write_text(METRIC_LIBRARY, encoding="utf-8")

    listed: list[str] = []
    if family is not None:
        (tmp_path / "families").mkdir(exist_ok=True)
        body = {"name": "甲族", "block": "core", "weight": 1.0, "threshold": 0.8,
                "redline": False,
                "engine": {"impl": "graders/library.py@v1", "layer": "trajectory"},
                "metrics": [{"id": "identity_probe"}],
                "scope": {"task-a": {"title": "任务甲"}}}
        body.update(family)
        (tmp_path / "families" / "alpha.yaml").write_text(
            yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        listed.append("families/alpha.yaml")
    if family is not None and "beta" in str(family.get("name", "")):
        listed.append("families/beta.yaml")

    verdict: dict = {
        "graders": {"default": {"impl": "graders/engine.py@v1", "layer": "outcome"}},
    }
    if family is not None and any(
        entry.get("inputs")
        for entry in (family.get("metrics") or [])
        if isinstance(entry, dict)
    ):
        # 族指标吃逐任务输入 ⇒ 判分只读密封副本（既有规则）。
        verdict["anchors"] = "task_anchors"
    if catalog is not None:
        verdict["metric_catalog"] = catalog
    if guard is not None:
        guards = verdict.setdefault("guards", [])
        guards.append(guard)
    overlay = {
        "schema_version": 2,
        "id": "demo-catalog",
        "version": "0.1.0",
        "extends": "_base/harbor.base.yaml",
        "blocks": {"core": "核心"},
        "provenance": {"source": "authored-internally", "license": "MIT"},
        "harbor": {"job": "jobs/demo.yaml"},
        "verdict": verdict,
    }
    if listed:
        overlay["families"] = listed
    (tmp_path / "suite.yaml").write_text(
        yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    jobs = tmp_path / "jobs"
    jobs.mkdir(exist_ok=True)
    (jobs / "demo.yaml").write_text(
        "job_name: demo-catalog\nn_attempts: 3\nagents:\n  - name: nop\n",
        encoding="utf-8",
    )
    return tmp_path


def _load(root: Path):
    return load_suite(root, suites_root=SUITES_ROOT)


def _load_error(root: Path) -> str:
    with pytest.raises(SuiteError) as excinfo:
        _load(root)
    return str(excinfo.value)


FAMILY = {"metrics": [{"id": "identity_probe",
                       "inputs": {"task-a": {"identity_probes": ["你是谁"]}}}]}
GUARD = {"impl": "graders/guardlib.py@v1", "layer": "trajectory", "veto": True,
         "metrics": ["integrity_guard"]}


# --- 绑定：目录定义 + 引用 ------------------------------------------------


def test_catalog_binds_family_and_guard_from_one_definition(tmp_path):
    root = _write_suite(
        tmp_path,
        catalog=[
            _catalog_entry("identity_probe", classification="dimension"),
            _catalog_entry("integrity_guard", severity="integrity",
                           classification="gate"),
        ],
        family=FAMILY,
        guard=GUARD,
    )
    suite = _load(root)
    spec = suite.families["alpha"]
    assert [(m.id, m.layer, m.severity) for m in spec.metrics] == [
        ("identity_probe", "trajectory", "score")
    ]
    # 输入留在族一侧（消费者自己的 rubric 绑定）。
    assert spec.inputs_for("task-a") == {
        "identity_probe": {"identity_probes": ["你是谁"]}
    }
    guard_decl = suite.overlay.verdict.guards[0]
    assert [(m.id, m.severity) for m in guard_decl.metrics or []] == [
        ("integrity_guard", "integrity")
    ]
    sealed = suite.dimension_model
    assert sealed["guards"][0]["metrics"] == [
        {"id": "integrity_guard", "severity": "integrity"}
    ]
    assert sealed["families"]["alpha"]["metrics"][0]["id"] == "identity_probe"


def test_catalog_layer_and_severity_are_the_only_home(tmp_path):
    """族文件里再写一遍 severity 会被拒；目录才是定义处。"""
    root = _write_suite(
        tmp_path,
        catalog=[_catalog_entry("identity_probe")],
        family={"metrics": [{"id": "identity_probe", "severity": "integrity"}]},
    )
    message = _load_error(root)
    assert "DEFINED once" in message and "severity" in message


def test_family_may_keep_its_own_inputs(tmp_path):
    """逐任务输入是消费者的绑定，不搬进目录。"""
    root = _write_suite(
        tmp_path,
        catalog=[_catalog_entry("identity_probe")],
        family=FAMILY,
    )
    assert _load(root).families["alpha"].inputs_for("task-a")["identity_probe"] == {
        "identity_probes": ["你是谁"]
    }


def test_catalog_is_optional_for_legacy_inline_families(tmp_path):
    """没有目录的旧套件（族文件自带定义）照常加载（legacy 路径）。"""
    root = _write_suite(
        tmp_path,
        family={"metrics": [{"id": "identity_probe", "severity": "integrity",
                             "layer": "trajectory"}]},
    )
    assert _load(root).families["alpha"].metrics[0].severity == "integrity"


# --- fail-closed 约束 ----------------------------------------------------


def test_duplicate_catalog_id_refused(tmp_path):
    root = _write_suite(
        tmp_path,
        catalog=[_catalog_entry("identity_probe"),
                 _catalog_entry("identity_probe")],
        family=FAMILY,
    )
    assert "defines metric 'identity_probe' twice" in _load_error(root)


def test_reference_to_undefined_metric_refused(tmp_path):
    root = _write_suite(
        tmp_path,
        catalog=[_catalog_entry("format_check")],
        family=FAMILY,
    )
    message = _load_error(root)
    assert "does not define" in message and "identity_probe" in message


def test_orphan_catalog_entry_refused(tmp_path):
    root = _write_suite(
        tmp_path,
        catalog=[_catalog_entry("identity_probe"),
                 _catalog_entry("format_check")],
        family=FAMILY,
    )
    message = _load_error(root)
    assert "no family or guard references" in message and "format_check" in message


def test_dimension_metric_cannot_be_referenced_by_a_guard(tmp_path):
    root = _write_suite(
        tmp_path,
        catalog=[_catalog_entry("integrity_guard", severity="integrity",
                                classification="dimension")],
        guard=GUARD,
    )
    message = _load_error(root)
    assert "classified as 'dimension'" in message and "guard" in message


def test_gate_metric_cannot_be_referenced_by_a_family(tmp_path):
    root = _write_suite(
        tmp_path,
        catalog=[_catalog_entry("identity_probe", classification="gate")],
        family=FAMILY,
    )
    message = _load_error(root)
    assert "classified as 'gate'" in message and "family" in message


def test_gate_metric_claimed_by_two_guards_refused(tmp_path):
    root = _write_suite(
        tmp_path,
        catalog=[_catalog_entry("format_check", classification="gate")],
        guard={"impl": "graders/guardlib.py@v1", "layer": "trajectory",
               "metrics": ["format_check"]},
    )
    # 第二个守卫用同一个指标 → 门被判两次。
    data = yaml.safe_load((root / "suite.yaml").read_text("utf-8"))
    data["verdict"]["guards"].append(
        {"impl": "graders/guardlib.py@v1", "layer": "trajectory",
         "metrics": ["format_check"]}
    )
    (root / "suite.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    assert "more than one guard" in _load_error(root)


def test_guard_metric_list_needs_a_catalog(tmp_path):
    root = _write_suite(tmp_path, guard=GUARD)
    message = _load_error(root)
    assert "verdict.metric_catalog is empty" in message


def test_catalog_cannot_carry_inputs(tmp_path):
    """输入是消费方的绑定：写进目录会被拒（同一指标在不同族锚点不同）。"""
    root = _write_suite(
        tmp_path,
        catalog=[_catalog_entry("identity_probe",
                                inputs={"task-a": {"identity_probes": ["你是谁"]}})],
        family=FAMILY,
    )
    message = _load_error(root)
    assert "per-task inputs are the CONSUMER's rubric binding" in message


def test_unknown_classification_refused(tmp_path):
    root = _write_suite(
        tmp_path,
        catalog=[_catalog_entry("identity_probe", classification="security")],
        family=FAMILY,
    )
    assert "invalid suite overlay" in _load_error(root)


def test_metric_layer_must_fit_its_consumer(tmp_path):
    root = _write_suite(
        tmp_path,
        catalog=[_catalog_entry("identity_probe", layer="outcome")],
        family=FAMILY,
    )
    assert "must fit" in _load_error(root) or "assembled per layer" in _load_error(root)


def test_both_layer_metric_fits_any_consumer(tmp_path):
    root = _write_suite(
        tmp_path,
        catalog=[_catalog_entry("identity_probe", layer="both")],
        family=FAMILY,
    )
    assert _load(root).families["alpha"].metrics[0].layer == "both"


def test_unbuildable_inputs_still_fail_the_contract_check(tmp_path):
    """目录化没有削弱输入对表：构造不出来的输入仍是声明错误。"""
    from aeval.verdict.pipeline import (
        GradingPipelineError,
        validate_metric_contracts,
    )

    root = _write_suite(
        tmp_path,
        catalog=[_catalog_entry("identity_probe")],
        family={"metrics": [{"id": "identity_probe",
                             "inputs": {"task-a": {"wrong_key": 1}}}]},
    )
    suite = _load(root)
    with pytest.raises(GradingPipelineError) as excinfo:
        validate_metric_contracts(suite)
    assert "cannot be built" in str(excinfo.value)


# --- 跨族共用一项指标（真实声明里的形态） --------------------------------


def test_one_metric_can_be_judged_by_two_families_with_own_inputs(tmp_path):
    """aeval-intel 的真实形态：同一项指标被两个族各自判定，锚点不同。"""
    root = _write_suite(
        tmp_path,
        catalog=[_catalog_entry("identity_probe")],
        family=FAMILY,
    )
    (root / "families" / "beta.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "乙族", "block": "core", "weight": 1.0, "threshold": 1.0,
                "redline": True,
                "engine": {"impl": "graders/library.py@v1", "layer": "trajectory"},
                "metrics": [{"id": "identity_probe",
                             "inputs": {"task-b": {"identity_probes": ["你在吗"]}}}],
                "scope": {"task-b": {"title": "任务乙"}},
            },
            allow_unicode=True, sort_keys=False,
        ),
        encoding="utf-8",
    )
    data = yaml.safe_load((root / "suite.yaml").read_text("utf-8"))
    data["families"] = ["families/alpha.yaml", "families/beta.yaml"]
    (root / "suite.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    suite = _load(root)
    assert sorted(suite.families) == ["alpha", "beta"]
    assert suite.families["alpha"].inputs_for("task-a") == {
        "identity_probe": {"identity_probes": ["你是谁"]}
    }
    assert suite.families["beta"].inputs_for("task-b") == {
        "identity_probe": {"identity_probes": ["你在吗"]}
    }


# --- 目录化不改变判分契约（迁移等价） ------------------------------------


def _contract(dimension_model: dict) -> dict:
    """判分契约：指标 id / 顺序 / 严重度 / 输入（层是目录化后新显式的）。"""

    def metric(entry: dict) -> dict:
        out = {"id": entry["id"], "severity": entry.get("severity") or "score"}
        if entry.get("inputs") is not None:
            out["inputs"] = entry["inputs"]
        if entry.get("degraded_below") is not None:
            out["degraded_below"] = entry["degraded_below"]
        return out

    return {
        "families": {
            key: [metric(m) for m in spec["metrics"]]
            for key, spec in dimension_model["families"].items()
        },
        "guards": [
            [metric(m) for m in guard.get("metrics") or []]
            for guard in dimension_model["guards"]
        ],
    }


def test_catalog_migration_preserves_the_judging_contract(tmp_path):
    """同一份判分声明：内联（legacy）与目录化装载出同一个契约。

    守卫声明清单必须有目录（否则就是第二份清单），所以 legacy 一侧只比
    族；守卫的契约由真实套件的固定期望值盯住（见下一个测试）。
    """
    inline = _write_suite(tmp_path / "inline", family={
        "metrics": [{"id": "identity_probe", "layer": "trajectory",
                     "severity": "score",
                     "inputs": {"task-a": {"identity_probes": ["你是谁"]}}}],
    })
    catalog = _write_suite(
        tmp_path / "catalog",
        catalog=[_catalog_entry("identity_probe")],
        family=FAMILY,
    )
    assert _contract(_load(inline).dimension_model) == _contract(
        _load(catalog).dimension_model
    )


def test_real_guard_catalog_keeps_the_declared_contract():
    """tbench 守卫：9 项、其中 2 项 integrity——目录化后逐项不变。"""
    suite = _load(AEVAL_ROOT / "suites" / "tbench-pilot")
    guard = suite.overlay.verdict.guards[0]
    contract = [(m.id, m.severity) for m in guard.metrics or []]
    assert contract == [
        ("step_efficiency", "score"),
        ("token_efficiency", "score"),
        ("tool_error_rate", "score"),
        ("loop_detection", "score"),
        ("recovery", "score"),
        ("redundant_actions", "score"),
        ("budget_adherence", "score"),
        ("forbidden_access", "integrity"),
        ("scope_discipline", "integrity"),
    ]


# --- 真实套件 -------------------------------------------------------------


@pytest.mark.parametrize(
    "suite_dir,expected_version",
    [
        ("suites/tbench-pilot", "0.7.0"),
        ("suites/aeval-intel", "0.6.0"),
        ("suites/sbench-pilot", "0.7.0"),
        ("examples/offline-chain/suite/tbench-intel", "0.4.0"),
    ],
)
def test_real_suites_load_with_catalog(suite_dir, expected_version):
    suite = _load(AEVAL_ROOT / suite_dir)
    assert suite.version == expected_version
    catalog = suite.overlay.verdict.metric_catalog
    # 目录项的 id 就是判分用的 id：每个 id 只定义一次。
    assert len(catalog) == len({entry.id for entry in catalog})
    judged = {m.id for spec in suite.families.values() for m in spec.metrics}
    judged |= {m.id for guard in suite.guards()
               for m in (getattr(guard, "metrics", None) or [])}
    assert judged == {entry.id for entry in catalog}


def test_real_intel_suites_use_the_catalog_for_shared_metrics():
    """intelligence 与 redline 共用 hallucination_check——一个定义，两处引用。"""
    suite = _load(AEVAL_ROOT / "suites" / "aeval-intel")
    hits = {
        entry.id: entry
        for entry in suite.overlay.verdict.metric_catalog
        if entry.id == "hallucination_check"
    }
    assert len(hits) == 1
    assert hits["hallucination_check"].classification == "dimension"
    users = [
        key for key, spec in suite.families.items()
        if "hallucination_check" in spec.metric_ids()
    ]
    assert sorted(users) == ["intelligence", "redline"]
    assert suite.families["intelligence"].inputs_for("capability-boundary") != (
        suite.families["redline"].inputs_for("memory.unverified_claim")
    )
