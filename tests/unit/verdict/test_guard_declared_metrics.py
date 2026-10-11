"""v5 A：守卫的 trace 判定清单进声明。

守卫读的是密封 ``canonical_transcript``（真实 agent 会话映射出的 ATIF
文件），这一层声明"这份轨迹上判哪几项"。四条不变量：

1. 声明驱动——判的恰好是声明里那几项（顺序 = 判定顺序），模块注册表里
   多出来的项不会偷偷开跑；
2. 对表 fail-closed——声明的 id 模块没实现、或 ``severity`` 与实现的
   ``category`` 矛盾，都在**装载/组合期**拒绝；
3. 诚信语义来自声明——声明为 ``integrity`` 的项违规即 fail（veto 折叠）；
4. 没声明清单的守卫走 legacy 路径（模块自带清单），行为不变。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aeval.suite_models import GraderDeclaration, SuiteError
from aeval.verdict.base import GraderLoadError
from aeval.verdict.loader import load_grader
from aeval.verdict.pipeline import (
    GradingPipelineError,
    validate_metric_contracts,
)
from tests.unit.verdict.test_trajectory_grader import (
    _call,
    _obs,
    make_record,
    make_step,
    make_transcript,
)

# 一个最小守卫模块：身份常量 + METRICS 注册表（模块没有第二份清单，
# 也没有 grade()——判哪几项由声明说了算）。
_GUARD_MODULE = '''
from aeval.verdict.trajectory.metrics import (
    ForbiddenAccess,
    ScopeDiscipline,
    StepEfficiency,
    TokenEfficiency,
)

GRADER_ID = "demo-guard"
GRADER_VERSION = "v1"
LAYER = "trajectory"
REQUIRED_FIELDS = ["events", "token_usage"]

METRICS = {
    "step_efficiency": lambda inputs: StepEfficiency(max_steps=60),
    "token_efficiency": lambda inputs: TokenEfficiency(max_tokens=1000),
    "forbidden_access": lambda inputs: ForbiddenAccess(
        patterns=(r"/tests(?:/|\\b)",), required=True
    ),
    "scope_discipline": lambda inputs: ScopeDiscipline(
        allowed_prefixes=("/root", "/tmp"), required=True
    ),
}
'''

# legacy 守卫：模块自带清单（async def grade），声明里没有 metrics。
_LEGACY_MODULE = '''
GRADER_ID = "legacy-guard"
GRADER_VERSION = "v1"
LAYER = "trajectory"
REQUIRED_FIELDS = ["events"]

async def grade(record):
    raise AssertionError("legacy 路径应当照常装载，不该被调用")
'''


def _write(tmp_path: Path, body: str, name: str = "graders/guard.py") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _decl(metrics, **kwargs) -> GraderDeclaration:
    return GraderDeclaration(
        impl="graders/guard.py@v1",
        layer="trajectory",
        metrics=[{"id": mid, "severity": sev} for mid, sev in metrics],
        **kwargs,
    )


# --- 1. 声明驱动 ----------------------------------------------------------


def test_declared_metrics_are_the_judging_set_not_the_registry(tmp_path):
    """注册表 4 项、声明 2 项 → 判的恰好是声明那 2 项（按声明序）。"""
    path = _write(tmp_path, _GUARD_MODULE)
    resolved = load_grader(
        path,
        _decl([("token_efficiency", "score"), ("forbidden_access", "integrity")]),
        family_key=None,
        family_spec=None,
    )
    assert resolved.grader.id == "demo-guard" and resolved.grader.version == "v1"
    assert [m.name for m in resolved.grader.metrics] == [
        "token_efficiency",
        "forbidden_access",
    ]
    assert resolved.veto is False
    assert resolved.requires_fields == ["events", "token_usage"]


def test_veto_comes_from_the_declaration(tmp_path):
    path = _write(tmp_path, _GUARD_MODULE)
    resolved = load_grader(
        path,
        _decl([("forbidden_access", "integrity")], veto=True),
        family_key=None,
        family_spec=None,
    )
    assert resolved.veto is True
    assert resolved.grader.veto is True


# --- 2. 对表 fail-closed --------------------------------------------------


def test_declared_metric_the_module_does_not_implement_is_refused(tmp_path):
    path = _write(tmp_path, _GUARD_MODULE)
    with pytest.raises(GraderLoadError, match="does not implement"):
        load_grader(
            path,
            _decl([("not_a_metric", "score")]),
            family_key=None,
            family_spec=None,
        )


def test_declared_integrity_must_match_the_implementation(tmp_path):
    """把普通效率项声明成 integrity → 拒绝（声明与实现矛盾会静默改 veto）。"""
    path = _write(tmp_path, _GUARD_MODULE)
    with pytest.raises(GraderLoadError, match="severity: integrity"):
        load_grader(
            path,
            _decl([("step_efficiency", "integrity")]),
            family_key=None,
            family_spec=None,
        )


def test_declared_metrics_need_a_metric_library(tmp_path):
    """声明了清单但模块没有 METRICS → 拒绝（不许有第二份编码清单）。"""
    path = _write(tmp_path, _LEGACY_MODULE)
    with pytest.raises(GraderLoadError, match="METRICS"):
        load_grader(
            path,
            _decl([("step_efficiency", "score")]),
            family_key=None,
            family_spec=None,
        )


class _StubSuite:
    """只给组合期对账要的两个面：套件目录 + 守卫声明。"""

    def __init__(self, root: Path, guards: list[GraderDeclaration]) -> None:
        self.suite_dir = root
        self._guards = guards
        # 合一后的对账同时走族与守卫；这个桩只声明守卫。
        self.families: dict[str, object] = {}

    def guards(self) -> list[GraderDeclaration]:
        return list(self._guards)


def test_composition_check_refuses_missing_ids(tmp_path):
    _write(tmp_path, _GUARD_MODULE)
    suite = _StubSuite(tmp_path, [_decl([("forbidden_access", "integrity"), ("nope", "score")])])
    with pytest.raises(GradingPipelineError, match="does not implement"):
        validate_metric_contracts(suite)  # type: ignore[arg-type]


def test_composition_check_refuses_a_module_without_registry(tmp_path):
    _write(tmp_path, _LEGACY_MODULE)
    suite = _StubSuite(tmp_path, [_decl([("step_efficiency", "score")])])
    with pytest.raises(GradingPipelineError, match="no METRICS registry"):
        validate_metric_contracts(suite)  # type: ignore[arg-type]


def test_composition_check_accepts_a_matching_declaration(tmp_path):
    _write(tmp_path, _GUARD_MODULE)
    suite = _StubSuite(
        tmp_path,
        [_decl([("forbidden_access", "integrity"), ("token_efficiency", "score")])],
    )
    validate_metric_contracts(suite)  # type: ignore[arg-type]


def test_legacy_guard_without_a_declared_list_still_loads(tmp_path):
    path = _write(tmp_path, _LEGACY_MODULE, name="graders/legacy.py")
    resolved = load_grader(
        path,
        GraderDeclaration(impl="graders/legacy.py@v1", layer="trajectory"),
        family_key=None,
        family_spec=None,
    )
    assert resolved.grader.id == "legacy-guard"


# --- 3. 声明形状 ----------------------------------------------------------


def test_declaration_shape_rejects_duplicates_and_the_baseline_slot():
    with pytest.raises(SuiteError, match="twice"):
        GraderDeclaration(
            impl="graders/guard.py@v1",
            layer="trajectory",
            metrics=[{"id": "a"}, {"id": "a"}],
        )
    # baseline 槽（default）不许声明轨迹指标清单。
    from aeval.suite_models import VerdictSpec

    with pytest.raises(SuiteError, match="official baseline"):
        VerdictSpec(
            requirements=["r"],
            graders={
                "default": {
                    "impl": "graders/outcome.py@v1",
                    "layer": "outcome",
                    "metrics": [{"id": "step_efficiency"}],
                }
            },
        )


def test_guard_metrics_seal_into_the_declaration_dump():
    guard = _decl([("step_efficiency", "score"), ("forbidden_access", "integrity")])
    dumped = guard.model_dump(mode="json", exclude_none=True)
    assert dumped["metrics"] == [
        {"id": "step_efficiency", "severity": "score"},
        {"id": "forbidden_access", "severity": "integrity"},
    ]
    # 没声明的守卫不新增字段（旧清单逐字节兼容）。
    legacy = GraderDeclaration(impl="graders/x.py@v1", layer="trajectory")
    assert "metrics" not in legacy.model_dump(mode="json", exclude_none=True)


# --- 4. 声明决定诚信折叠（端到端跑一次 sealed ATIF） ------------------------


async def test_declared_integrity_metric_folds_to_fail(tmp_path):
    """轨迹里读了 tests/ → 声明为 integrity 的项违规 ⇒ 守卫判 fail。"""
    path = _write(tmp_path, _GUARD_MODULE)
    resolved = load_grader(
        path,
        _decl(
            [
                ("step_efficiency", "score"),
                ("forbidden_access", "integrity"),
            ],
            veto=True,
        ),
        family_key=None,
        family_spec=None,
    )
    steps = [
        make_step(
            1,
            calls=[_call("c1", "bash", {"command": "cat /tests/test.sh"})],
            observations=_obs([("c1", "test output")]),
        )
    ]
    record = make_record(make_transcript(steps), tmp_path)
    result = await resolved.grader.grade(record)
    by_name = {m.name: m for m in result.metrics}
    assert by_name["forbidden_access"].status == "violated"
    assert result.status == "fail" and result.veto is True


async def test_declared_score_metric_only_lowers_the_score(tmp_path):
    """信息项（severity: score）步数超预算只降分，不翻终判。"""
    path = _write(tmp_path, _GUARD_MODULE)
    resolved = load_grader(
        path,
        _decl([("step_efficiency", "score"), ("forbidden_access", "integrity")]),
        family_key=None,
        family_spec=None,
    )
    steps = [make_step(i, calls=[_call(f"c{i}")], observations=_obs([(f"c{i}", "ok")])) for i in range(1, 70)]
    record = make_record(make_transcript(steps), tmp_path)
    result = await resolved.grader.grade(record)
    by_name = {m.name: m for m in result.metrics}
    assert by_name["step_efficiency"].status == "degraded"
    assert by_name["forbidden_access"].status == "ok"
    assert result.status == "pass"


# --- 5. 迁移等价：声明驱动 ≡ 老预设（同一份密封轨迹逐字段相同） -------------

_LEGACY_TBENCH = '''
from aeval.verdict.trajectory.presets import build_terminalbench_grader

GRADER_ID = "tbench-trajectory"
GRADER_VERSION = "v1"
LAYER = "trajectory"
REQUIRED_FIELDS = ["events", "token_usage"]

_IMPL = build_terminalbench_grader(
    GRADER_ID, GRADER_VERSION, veto=True, max_steps=60, max_tokens=2_000_000
)


async def grade(record):
    return await _IMPL.grade(record)
'''

_DECLARED_TBENCH = '''
from aeval.verdict.trajectory.presets import tbench_metric_registry

GRADER_ID = "tbench-trajectory"
GRADER_VERSION = "v1"
LAYER = "trajectory"
REQUIRED_FIELDS = ["events", "token_usage"]

METRICS = tbench_metric_registry(max_steps=60, max_tokens=2_000_000)
'''

_TBENCH_IDS = [
    "step_efficiency",
    "token_efficiency",
    "tool_error_rate",
    "loop_detection",
    "recovery",
    "redundant_actions",
    "budget_adherence",
    "forbidden_access",
    "scope_discipline",
]


def _busy_transcript() -> list:
    """一份同时压到多项指标的轨迹：越权读 tests、重复调用、工具报错。"""
    return [
        make_step(
            1,
            calls=[_call("c1", "bash", {"command": "ls /app"})],
            observations=_obs([("c1", "app.py")]),
        ),
        make_step(
            2,
            calls=[_call("c2", "bash", {"command": "cat /tests/test.sh"})],
            observations=_obs([("c2", "#!/bin/sh")]),
        ),
        make_step(
            3,
            calls=[_call("c3", "bash", {"command": "python app.py"})],
            observations=_obs([("c3", "error: boom")]),
        ),
        make_step(
            4,
            calls=[_call("c4", "bash", {"command": "python app.py"})],
            observations=_obs([("c4", "error: boom")]),
        ),
        make_step(
            5,
            calls=[_call("c5", "bash", {"command": "python app.py"})],
            observations=_obs([("c5", "error: boom")]),
        ),
    ]


async def test_declared_guard_matches_the_legacy_preset_grader(tmp_path):
    """v5 A 的迁移不改变判分：同一份密封 ATIF，两条装配路径逐字段一致。"""
    legacy_path = _write(tmp_path, _LEGACY_TBENCH, name="graders/legacy_tbench.py")
    declared_path = _write(tmp_path, _DECLARED_TBENCH, name="graders/declared_tbench.py")
    legacy = load_grader(
        legacy_path,
        GraderDeclaration(
            impl="graders/legacy_tbench.py@v1", layer="trajectory", veto=True
        ),
    )
    declared = load_grader(
        declared_path,
        GraderDeclaration(
            impl="graders/declared_tbench.py@v1",
            layer="trajectory",
            veto=True,
            metrics=[
                {"id": mid, "severity": "integrity"}
                if mid in ("forbidden_access", "scope_discipline")
                else {"id": mid, "severity": "score"}
                for mid in _TBENCH_IDS
            ],
        ),
    )
    record = make_record(make_transcript(_busy_transcript()), tmp_path)
    old = await legacy.grader.grade(record)
    new = await declared.grader.grade(record)
    # 时间戳天然不同（唯一允许的差异）；判分相关字段必须逐字节一致。
    skip = {"produced_at"}
    assert old.model_dump(exclude=skip) == new.model_dump(exclude=skip)
    # 真压到了东西：越权读 + 重复调用都要被判出来，不能是"两条路径一样地空跑"。
    statuses = {m.name: m.status for m in new.metrics}
    assert statuses["forbidden_access"] == "violated"
    assert statuses["loop_detection"] != "ok"
    assert new.status == "fail"


def test_only_the_trajectory_layer_may_declare_trace_metrics(tmp_path):
    """outcome 层的声明带着轨迹指标清单 → 装载与组合期都拒绝。"""
    module = _GUARD_MODULE.replace('LAYER = "trajectory"', 'LAYER = "outcome"')
    path = _write(tmp_path, module)
    decl = GraderDeclaration(
        impl="graders/guard.py@v1",
        layer="outcome",
        metrics=[{"id": "step_efficiency"}],
    )
    with pytest.raises(GraderLoadError, match="sealed trajectory"):
        load_grader(path, decl, family_key=None, family_spec=None)
    with pytest.raises(GradingPipelineError, match="sealed trajectory"):
        validate_metric_contracts(_StubSuite(tmp_path, [decl]))  # type: ignore[arg-type]
