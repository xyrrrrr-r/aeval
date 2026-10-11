"""Declared-metric family assembly (v5: the metric plan is the truth).

A family declares **which metrics it judges** (``FamilySpec.metrics``,
in judging order). The engine module no longer keeps its own copy of that
list, its threshold or its veto flag: it supplies an ``id → factory``
registry (``METRICS``) plus a strict anchors/inputs parser, and this module
builds the family grader from the declaration.

Contract (fail-closed, checked at load time by
``aeval.verdict.pipeline.validate_metric_contracts`` and again here
so a mis-wired module can never execute):

- every declared metric id must exist in ``module.METRICS``;
- a declared ``severity: integrity`` metric must be implemented with
  ``category == "integrity"`` and vice versa — a declaration that disagrees
  with its implementation is a load error, not a silent relabelling;
- each metric gets exactly the inputs bound to **its own id** for that
  task (``cases[task].inputs[metric_id]``); a metric with no binding falls
  back to the family-level ``inputs`` in its own declaration, and a metric
  that needs no inputs gets ``{}``. There is **no anchor-key → metric table
  anywhere in code** any more — the binding is the declaration;

Assembly happens per trial (inputs are per task), so the graded result
carries the same identity as the module and the same per-trial binding the
legacy path injected.

**Guards (v5 A)** use the same split: a guard declaration may list the
trace metrics it judges (``verdict.guards[].metrics``). The guard module is
then a metric LIBRARY (``METRICS``) and the declared list — not a coded
one — decides which metrics run over the sealed ATIF trajectory. A guard
declared without a metric list keeps the legacy path (module-built grader),
so unmigrated suites behave byte-identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from aeval.verdict.base import GraderLoadError

__all__ = [
    "DeclaredGuardPlan",
    "DeclaredMetricPlan",
    "GraderLoadError",
    "build_declared_family_grader",
    "build_declared_guard_grader",
    "build_family_metrics",
    "check_declared_metric_contract",
    "metric_library",
    "metric_registry",
    "probe_metric_factory",
]


@dataclass(frozen=True)
class DeclaredMetricPlan:
    """One family's declared metric plan (the judging truth)."""

    grader_id: str
    grader_version: str
    layer: str
    threshold: float
    veto: bool
    metric_ids: tuple[str, ...]
    declared_severities: Mapping[str, str]
    declared_inputs: Mapping[str, Mapping[str, Any] | None]
    family_key: str
    metric_specs: Sequence[Any] = ()
    #: 该族是否消费密封输入（族声明里至少一个用例绑定了逐指标输入）。
    #: False = 结构判定族（如安全族）：不读锚点通道，缺输入不会把判定
    #: 变成 cannot_judge。
    consumes_inputs: bool = False


def metric_registry(module: Any) -> Mapping[str, Callable[..., Any]] | None:
    """The module's ``METRICS`` registry, or ``None`` for legacy modules.

    A registry module is a metric LIBRARY: ``{metric_id: factory(inputs)}``.
    Its shape is verified here so a broken registry fails the load, not the
    first trial.
    """
    registry = getattr(module, "METRICS", None)
    if registry is None:
        return None
    if not isinstance(registry, Mapping) or not registry:
        raise GraderLoadError(
            "a metric-library module must declare METRICS as a non-empty "
            "mapping of metric id → factory"
        )
    for metric_id, factory in registry.items():
        if not isinstance(metric_id, str) or not metric_id.strip():
            raise GraderLoadError("METRICS keys must be non-empty metric ids")
        if not callable(factory):
            raise GraderLoadError(
                f"METRICS[{metric_id!r}] must be callable (a factory taking "
                "that metric's inputs)"
            )
    return registry


def _metric_category(metric: Any) -> str | None:
    category = getattr(metric, "category", None)
    return category if isinstance(category, str) else None


def check_metric_severity(
    metric_ids: Sequence[str],
    expected: Mapping[str, str],
    registry: Mapping[str, Callable[..., Any]],
    declared_inputs: Mapping[str, Mapping[str, Any] | None] | None = None,
) -> None:
    """Declared severity must agree with the implementation's category.

    Read statically from the registry entry when it carries ``category``
    (the id → class case), otherwise probe-build the metric with its
    declared inputs. A factory that cannot be probed is left unchecked
    rather than falsely rejected.
    """
    for metric_id in metric_ids:
        want = expected.get(metric_id)
        if want is None:
            raise GraderLoadError(
                f"metric {metric_id!r} has no bound severity — declared metric "
                "references are bound through verdict.metric_catalog at suite "
                "load; an unbound reference must never judge"
            )
        entry = registry[metric_id]
        metric = entry if _metric_category(entry) is not None else None
        if metric is None:
            probe = (declared_inputs or {}).get(metric_id)
            try:
                metric = entry(probe if probe is not None else {})
            except Exception:  # noqa: BLE001 — probing is best-effort here
                continue
        got = _metric_category(metric)
        if got is None:
            continue
        if want == "integrity" and got != "integrity":
            raise GraderLoadError(
                f"metric {metric_id!r} is declared severity: integrity but "
                f"its implementation carries category {got!r} — declare the "
                "severity the implementation actually has"
            )
        if want == "score" and got == "integrity":
            raise GraderLoadError(
                f"metric {metric_id!r} is implemented as an integrity metric "
                "(category: integrity) but declared severity: score — an "
                "integrity violation must be declared as such"
            )


def probe_metric_factory(
    metric_id: str, factory: Callable[..., Any], inputs: Any
) -> None:
    """Can this metric really be built from this declaration's inputs?

    Called at load time on every declared input binding. v5 removed the
    code-side anchor vocabulary, so this probe *is* the key check: a
    factory that rejects its inputs (unknown key, wrong shape) fails the
    suite load instead of silently degrading the metric to a skip.
    """
    try:
        factory(inputs)
    except GraderLoadError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GraderLoadError(
            f"metric {metric_id!r} cannot be built from the declared inputs "
            f"{sorted(inputs) if isinstance(inputs, Mapping) else inputs!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def metric_library(
    module: Any,
    *,
    grader_id: str,
    grader_version: str,
    owner: str,
    layer: str | None = None,
    require_trajectory: bool = False,
) -> Mapping[str, Callable[..., Any]]:
    """模块的指标库（``METRICS``）——声明了清单就必须是库，没有第二份清单。

    族装配与守卫装配共用这一段：``owner`` 是错误信息里的声明位置
    （``family 'security'`` / ``guard 'graders/x.py@v1'``）。
    ``require_trajectory`` 时校验层——轨迹指标只能由轨迹层消费者判。
    """
    prefix = f"{grader_id}@{grader_version}: {owner}"
    if require_trajectory and layer not in ("trajectory", "both"):
        raise GraderLoadError(
            f"{prefix} declares layer {layer!r} together with a trace metric "
            "list — trace metrics are judged on the sealed trajectory "
            "(layer: trajectory)"
        )
    registry = metric_registry(module)
    if registry is None:
        raise GraderLoadError(
            f"{prefix} declares a metric list, so the module must be a metric "
            "library (a METRICS id → factory registry): this module has no "
            "METRICS registry — there is no coded second list any more"
        )
    return registry


def check_declared_metric_contract(
    registry: Mapping[str, Callable[..., Any]],
    *,
    owner: str,
    metric_ids: Sequence[str],
    declared_severities: Mapping[str, str],
    declared_inputs: Mapping[str, Mapping[str, Any] | None] | None = None,
) -> None:
    """声明 × 实现对表：逐 id 有实现，severity 与实现 ``category`` 一致。

    v5 Step 6 起族与守卫共用（同一份声明驱动两种消费者，对账也必须同一
    段代码——否则两条路径迟早漂）。
    """
    missing = [mid for mid in metric_ids if mid not in registry]
    if missing:
        raise GraderLoadError(
            f"{owner} declares metrics {missing} its module does not implement "
            "— a declared metric that cannot be judged is a declaration error, "
            "not a skipped row"
        )
    check_metric_severity(
        metric_ids, declared_severities, registry, declared_inputs
    )


def build_family_metrics(
    registry: Mapping[str, Callable[..., Any]],
    metric_specs: Sequence[Any],
    inputs_by_metric: Mapping[str, Any] | None,
) -> list[Any]:
    """Build one family's metric objects in declared order.

    One construction path for grading and for the turn-attribution panel:
    the declaration's metric list and order are authoritative, so the
    panel never judges on a second rubric.

    Inputs are resolved **per metric id** from the task's derived binding
    (``spec.inputs_for(task_id)``): every metric gets exactly its own
    inputs, and a metric with no inputs for this task gets ``{}`` (it may
    honestly skip). Nothing in code maps an anchor key to a metric.
    """
    bound = inputs_by_metric or {}
    metrics: list[Any] = []
    for spec in metric_specs:
        metrics.append(registry[spec.id](bound.get(spec.id) or {}))
    return metrics


def build_declared_family_grader(module: Any, plan: DeclaredMetricPlan) -> Any:
    """Build the family grader from the declared plan + module registry."""
    from aeval.verdict.base import ResolvedGrader
    from aeval.verdict.trajectory.base import (
        SealedArtifactError,
        load_sealed_family_anchors,
    )
    from aeval.verdict.trajectory.presets import ThresholdTrajectoryGrader

    owner = f"family {plan.family_key!r}"
    registry = metric_library(
        module,
        grader_id=plan.grader_id,
        grader_version=plan.grader_version,
        owner=owner,
    )
    check_declared_metric_contract(
        registry,
        owner=owner,
        metric_ids=plan.metric_ids,
        declared_severities=plan.declared_severities,
        declared_inputs=plan.declared_inputs,
    )

    uses_inputs = bool(plan.consumes_inputs)

    async def _grade(record: Any) -> Any:
        from aeval.contracts import GradeResult, Score

        task_id = (
            getattr(getattr(record, "coordinates", None), "task_id", None)
            or "unknown"
        )
        bound_inputs: Any = {}
        if uses_inputs:
            # Input-consuming family: the sealed rubric copy is the only
            # source, and a task the family declares no inputs for is
            # ``cannot_judge`` (honest, never a guessed rubric).
            try:
                anchors, reason = load_sealed_family_anchors(
                    record, task_id, plan.family_key
                )
            except SealedArtifactError as exc:
                anchors, reason = None, str(exc)
            if anchors is None:
                return GradeResult(
                    grader_id=plan.grader_id,
                    grader_version=plan.grader_version,
                    layer=plan.layer,  # type: ignore[arg-type]
                    veto=plan.veto,
                    score=Score(
                        value=None, valid=False, invalid_reasons=[reason]
                    ),
                    status="cannot_judge",
                    reasons=[reason],
                    metrics=None,
                )
            # 密封形状：{指标 id: 该指标的输入}；没有绑定的指标拿自己的
            # 族级 inputs 或空（需要输入的指标会如实 skipped）。
            bound_inputs = anchors if isinstance(anchors, dict) else {}
        metrics = build_family_metrics(
            registry, plan.metric_specs, bound_inputs
        )
        impl = ThresholdTrajectoryGrader(
            plan.grader_id,
            plan.grader_version,
            metrics,
            veto=plan.veto,
            threshold=plan.threshold,
            # v5 Step 2：诚信语义来自声明（severity: integrity），不再由
            # 实现的 MetricOutcome.category 决定（它只作加载期断言目标）。
            integrity_metrics=frozenset(
                metric_id
                for metric_id, severity in plan.declared_severities.items()
                if severity == "integrity"
            ),
        )
        return await impl.grade(record)

    class _DeclaredFamilyGrader:
        # Protocol view over the declared plan + verified module identity.
        # v5: the module no longer declares an anchor vocabulary or a parser
        # — inputs are bound per metric id by the declaration, so the grader
        # exposes no anchor surface at all.
        id = plan.grader_id
        version = plan.grader_version
        layer = plan.layer
        veto = plan.veto
        anchor_keys = None
        anchors_from_mapping = None
        family_key = plan.family_key

        async def grade(self, record: Any) -> Any:
            return await _grade(record)

    required = getattr(module, "REQUIRED_FIELDS", None) or []
    return ResolvedGrader(
        grader=_DeclaredFamilyGrader(),  # type: ignore[arg-type]
        execution="pure",
        veto=plan.veto,
        requires_fields=list(required),
    )

@dataclass(frozen=True)
class DeclaredGuardPlan:
    """A guard's declared trace-metric plan (the judging truth).

    守卫没有类别行、没有阈值、不吃逐任务输入：它在密封 ATIF 轨迹上判
    声明的那几项，``severity: integrity`` 的项违规即 fail/veto。
    """

    grader_id: str
    grader_version: str
    layer: str
    veto: bool
    metric_ids: tuple[str, ...]
    declared_severities: Mapping[str, str]
    #: 声明它的位置（``graders/<name>.py@<version>``）——错误信息用。
    guard_key: str


def build_declared_guard_grader(module: Any, plan: DeclaredGuardPlan) -> Any:
    """Build a guard grader from the declared metric plan + module registry.

    与族装配同一套对表：声明的 id 必须在模块的 ``METRICS`` 里，声明的
    ``severity`` 必须与实现的 ``category`` 一致；对不上就拒绝装载（声明
    错误，不是被跳过的行）。守卫不读锚点通道、不设阈值——阈值属于维度
    （族）策略。
    """
    from aeval.verdict.base import ResolvedGrader
    from aeval.verdict.trajectory.base import TrajectoryGrader

    owner = f"guard {plan.guard_key!r}"
    registry = metric_library(
        module,
        grader_id=plan.grader_id,
        grader_version=plan.grader_version,
        owner=owner,
        layer=plan.layer,
        require_trajectory=True,
    )
    check_declared_metric_contract(
        registry,
        owner=owner,
        metric_ids=plan.metric_ids,
        declared_severities=plan.declared_severities,
    )
    metrics = [registry[mid]({}) for mid in plan.metric_ids]
    built = tuple(metrics)
    impl = TrajectoryGrader(
        plan.grader_id,
        plan.grader_version,
        metrics,
        veto=plan.veto,
        # 诚信语义来自声明（severity: integrity）；实现的 category 只作
        # 加载期断言目标。
        integrity_metrics=frozenset(
            metric_id
            for metric_id, severity in plan.declared_severities.items()
            if severity == "integrity"
        ),
    )

    class _DeclaredGuardGrader:
        # Protocol view over the declared plan + verified module identity.
        id = plan.grader_id
        version = plan.grader_version
        layer = plan.layer
        veto = plan.veto
        family_key = None
        anchor_keys = None
        anchors_from_mapping = None
        #: 判定顺序的指标对象（只读视图）——与 impl 内部同一批，不重建。
        metrics = built

        async def grade(self, record: Any) -> Any:
            return await impl.grade(record)

    required = getattr(module, "REQUIRED_FIELDS", None) or []
    return ResolvedGrader(
        grader=_DeclaredGuardGrader(),  # type: ignore[arg-type]
        execution="pure",
        veto=plan.veto,
        requires_fields=list(required),
    )

