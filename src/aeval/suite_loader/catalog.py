"""指标目录绑定（v5 Step 6）：一处定义、两处引用。

套件在 ``verdict.metric_catalog`` 里定义每项判分指标的**原子事实**
（层/严重度/降级线 + 归属角色 ``classification``）；族文件与守卫声明只写
``id`` 引用。这个模块把引用展开成判分用的完整声明，并在加载期钉死
"一个指标一个定义"：

- 目录项角色只有两种：``gate``（套件级有效性门，只由守卫引用）与
  ``dimension``（计分维度，只由族引用）；
- 引用必须命中目录；门指标恰好被一个守卫引用，维度指标至少被一个族
  引用——定义了却没人判的目录项、指向不存在指标或角色不符的引用都当场
  报错；
- ``severity``/``degraded_below``/``layer`` 只在目录里写一次：族文件与
  守卫声明里再写一遍会被拒（fail-closed）；
- 逐任务输入留在消费者一侧：输入是"这个消费者在这条任务上用哪些锚点判
  这项指标"，同一项指标可被多个族各自判定（各自锚点不同）；
- 指标的层必须与消费者的层相容（``both`` 与任意层相容）。

没有目录的旧套件（族文件自带定义）走 legacy 路径，行为逐字节不变；但
"守卫声明指标清单"必须有目录——那正是为了不出现第二份清单。
"""

from __future__ import annotations

from typing import Any

from aeval.suite_models import (
    FamilySpec,
    GuardMetricSpec,
    MetricCatalogEntry,
    SuiteError,
    SuiteOverlay,
    VerdictSpec,
    layers_compatible,
)

__all__ = ["bind_metric_catalog"]

#: 引用里允许出现的字段：族可以带自己的 rubric 输入，守卫只有 id。
_FAMILY_REF_FIELDS = frozenset({"id", "inputs"})
_GUARD_REF_FIELDS = frozenset({"id"})

_GATE = "gate"
_DIMENSION = "dimension"


def _reference_only(entries: Any, *, where: str, allowed: frozenset[str]) -> None:
    """声明里的指标条目只能是引用（+ 族自己的逐任务输入）。"""
    for entry in entries or ():
        written = set(getattr(entry, "model_fields_set", ()))
        extra = sorted(written - allowed)
        if extra:
            raise SuiteError(
                f"{where} writes {extra} on metric "
                f"{getattr(entry, 'id', '?')!r} — a metric is DEFINED once in "
                "verdict.metric_catalog; declarations only reference it by id "
                "(severity/degraded_below/layer belong to the catalog; a family "
                "may carry its own per-task inputs)"
            )


def bind_metric_catalog(
    overlay: SuiteOverlay,
    families: dict[str, FamilySpec],
    *,
    suite_label: str,
) -> tuple[SuiteOverlay, dict[str, FamilySpec]]:
    """展开目录引用；返回（展开后的 overlay，展开后的族声明）。

    调用点：加载期（``suite_loader.loader.load_suite``），族文件已经解析、
    族键已知之后，任何组件（判分器/锚点投影/密封模型）之前。
    """
    verdict = overlay.verdict
    catalog: list[MetricCatalogEntry] = list(verdict.metric_catalog)

    if not catalog:
        # legacy：族文件自带定义；守卫不许声明指标清单（那是第二份清单）。
        for guard in verdict.guards:
            if guard.metrics:
                raise SuiteError(
                    f"{suite_label}: guard {guard.impl!r} lists metrics but "
                    "verdict.metric_catalog is empty — declare the metric "
                    "definitions in the suite catalog once and reference them "
                    "by id"
                )
        return overlay, families

    by_id: dict[str, MetricCatalogEntry] = {entry.id: entry for entry in catalog}
    #: 指标 id → 引用它的消费者（孤儿检查 + "门只有一个家"）。
    consumers: dict[str, list[str]] = {}
    #: 门指标 → 引用它的守卫数（门每试次折叠一次，判两次 = 折叠两次）。
    gate_guards: dict[str, int] = {}

    def _entry(metric_id: str, *, consumer: str, want: str) -> MetricCatalogEntry:
        entry = by_id.get(metric_id)
        if entry is None:
            raise SuiteError(
                f"{suite_label}: {consumer} references metric {metric_id!r} "
                "which verdict.metric_catalog does not define"
            )
        if entry.classification != want:
            raise SuiteError(
                f"{suite_label}: metric {metric_id!r} is classified as "
                f"{entry.classification!r} but {consumer} references it — a "
                f"{want!r} metric belongs to "
                f"{'a guard' if want == _GATE else 'a family'} only"
            )
        consumers.setdefault(metric_id, []).append(consumer)
        return entry

    # --- 守卫（classification: gate） -------------------------------------
    guards = []
    for guard in verdict.guards:
        _reference_only(
            guard.metrics, where=f"guard {guard.impl!r}", allowed=_GUARD_REF_FIELDS
        )
        expanded: list[GuardMetricSpec] = []
        for ref in guard.metrics or ():
            entry = _entry(ref.id, consumer=f"guard {guard.impl!r}", want=_GATE)
            gate_guards[entry.id] = gate_guards.get(entry.id, 0) + 1
            if not layers_compatible(entry.layer, guard.layer):
                raise SuiteError(
                    f"{suite_label}: guard {guard.impl!r} runs on layer "
                    f"{guard.layer!r} but catalog metric {entry.id!r} is "
                    f"{entry.layer!r} — an assembled metric must fit its layer"
                )
            expanded.append(GuardMetricSpec(id=entry.id, severity=entry.severity))
        guards.append(guard.model_copy(update={"metrics": expanded or None}))

    twice = sorted(mid for mid, n in gate_guards.items() if n > 1)
    if twice:
        raise SuiteError(
            f"{suite_label}: gate metric(s) {twice} are referenced by more than "
            "one guard — a gate is folded once per trial, so each gate metric "
            "has exactly one guard"
        )

    # --- 族（classification: dimension） ----------------------------------
    bound_families: dict[str, FamilySpec] = {}
    for key, spec in families.items():
        _reference_only(
            spec.metrics, where=f"family {key!r}", allowed=_FAMILY_REF_FIELDS
        )
        payload = spec.model_dump(mode="json", exclude_none=True)
        metrics_payload: list[dict[str, Any]] = []
        for ref in spec.metrics:
            entry = _entry(ref.id, consumer=f"family {key!r}", want=_DIMENSION)
            if not layers_compatible(entry.layer, spec.engine.layer):
                raise SuiteError(
                    f"{suite_label}: family {key!r} engine runs on layer "
                    f"{spec.engine.layer!r} but catalog metric {entry.id!r} is "
                    f"{entry.layer!r} — a family is assembled per layer"
                )
            defined: dict[str, Any] = {
                "id": entry.id,
                "layer": entry.layer,
                "severity": entry.severity,
            }
            if entry.degraded_below is not None:
                defined["degraded_below"] = entry.degraded_below
            # 输入是消费者的 rubric 绑定，留在族一侧。
            if ref.inputs is not None:
                defined["inputs"] = ref.inputs
            metrics_payload.append(defined)
        payload["metrics"] = metrics_payload
        # 重新过一遍 FamilySpec 校验器（scope 覆盖、层一致等），因为定义是
        # 绑定后才到位的。
        bound_families[key] = FamilySpec.model_validate(payload)

    # --- 孤儿：定义了却没人判 ---------------------------------------------
    orphans = sorted(mid for mid in by_id if mid not in consumers)
    if orphans:
        raise SuiteError(
            f"{suite_label}: verdict.metric_catalog defines metric(s) {orphans} "
            "that no family or guard references — every defined metric is "
            "judged by at least one consumer (delete it, or reference it)"
        )

    bound_verdict: VerdictSpec = verdict.model_copy(update={"guards": guards})
    return overlay.model_copy(update={"verdict": bound_verdict}), bound_families
