"""Suite models: thin overlay over Harbor-native declarations.

A suite is a directory: ``suite.yaml`` declares ONLY the
four things Harbor does not (baselines, clock, observables, verdict,
plus metrics/driver/provenance). Any fact Harbor already declares in
its own task.yaml/job.yaml must NOT appear here — duplication is a CI
error, not a merge.
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aeval.contracts import BudgetSnapshot

__all__ = [
    "SuiteError",
    "OVERLAY_SCHEMA_VERSION",
    "HARBOR_OWNED_TOP_KEYS",
    "ImagePinAction",
    "BaselineAssertion",
    "ClockSpec",
    "ObservableSpec",
    "RubricDeclaration",
    "GraderDeclaration",
    "VerdictSpec",
    "FamilyEngine",
    "ScopeBinding",
    "FamilySpec",
    "MetricDeclaration",
    "DriverSpec",
    "ProvenanceInfo",
    "HarborInputs",
    "SuiteSource",
    "SuiteOverlay",
    "ResolvedSuite",
    "load_suite_yaml",
    "overlay_digest_of",
]

OVERLAY_SCHEMA_VERSION = 2

# Facts owned by Harbor's native files. If suite.yaml restates any of
# these (outside image.pin/image.rebuild narrowing) → SuiteError.
HARBOR_OWNED_TOP_KEYS = frozenset(
    {
        "environment",
        "egress",
        "writable_roots",
        "mocks",
        # "budget" is deliberately NOT here: Harbor's JobConfig has no budget
        # field at all (verified), so there is nothing to defer to. A spend cap
        # is an evaluation fact and belongs in the suite overlay as
        # ``budget: {max_tokens: ...}``.
        "trials",
        "tasks",
        "parallel",
        "k",
        "attempts",
        "retry",
        "timeout",
    }
)

HARBOR_OWNED_TASK_KEYS = frozenset(
    {"image", "seed_ref", "setup_script", "seed", "env", "setup"}
)


class SuiteError(RuntimeError):
    """Any suite-level validation failure. Always fail-loud, never merge."""


class _SuiteModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImagePinAction(_SuiteModel):
    """The single allowed overlap with Harbor-owned facts: narrowing.

    Either pin a mutable tag to a digest, or force a rebuild published
    by our CI. Both REPLACE the Harbor value at compose time; the
    replaced original is recorded.
    """

    pin: str | None = None
    rebuild: bool = False
    original: str | None = None

    @field_validator("pin")
    @classmethod
    def _pin_is_digest(cls, v: str | None) -> str | None:
        if v is not None and re.fullmatch(r"[^@\s]+@sha256:[0-9a-fA-F]{64}", v) is None:
            raise SuiteError(f"image.pin must be digest-pinned with a full sha256: {v!r}")
        return v

    @model_validator(mode="after")
    def _one_action(self) -> Self:
        if (self.pin is not None) == self.rebuild:
            raise SuiteError("image must set exactly one of pin or rebuild=true")
        return self


class BaselineAssertion(_SuiteModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    probe: str | None = Field(default=None, min_length=1)
    equals: Any = None
    assert_expr: str | None = Field(default=None, alias="assert", min_length=1)

    @model_validator(mode="after")
    def _one_assertion(self) -> Self:
        if (self.probe is not None) == (self.assert_expr is not None):
            raise SuiteError(
                f"baseline {self.id!r} must set exactly one of probe or assert"
            )
        return self


class ClockSpec(_SuiteModel):
    mode: Literal["virtual_offset", "real"]
    epoch: str | None = None


class ObservableSpec(_SuiteModel):
    name: str
    type: Literal["string", "number", "boolean", "json"]
    source: str  # db:/file:/screenshot:/dom:


class RubricDeclaration(_SuiteModel):
    rubric: str
    veto: bool = False


class MetricCatalogEntry(_SuiteModel):
    """指标目录里的一项指标 —— **一个指标的原子定义只有这一处**（Step 6）。

    层、严重度、降级线、归属角色住在这里；族文件与守卫声明只写 ``id``
    引用，不再各自复述定义——于是同一项指标不可能在两个家各写一遍
    ``severity``（它还要跟实现的 ``category`` 对表）。装配期由加载器按
    引用展开成判分计划。

    逐任务输入**不在目录里**：输入是"这个消费者在这条任务上用哪些锚点判
    这项指标"，属于消费方的 rubric 绑定——同一个指标 id 可以被多个族各
    自判定（例如 aeval-intel 的 ``hallucination_check`` 同时在 intelligence
    与 redline 两个族里，判的任务与锚点不同），所以输入留在族声明里。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    #: 指标属于哪一层。必填：层是指标自己的属性（v5 Goal 的
    #: "指标为一等公民"），消费方（族引擎/守卫）的层必须与它相容。
    layer: Literal["outcome", "trajectory", "both"]
    #: ``score``（计分维度）或 ``integrity``（诚信维度，违规即 veto 候选）。
    #: 判分真源；实现自身的 ``MetricOutcome.category`` 只作断言目标。
    severity: Literal["score", "integrity"] = "score"
    #: 本项自身的降级线（缺省由实现决定）。
    degraded_below: float | None = Field(default=None, gt=0, le=1)
    #: 归属角色：``gate`` = 套件级有效性门（只由守卫引用，对全体试次
    #: 生效）；``dimension`` = 计分维度（由族引用，进族行；同一项指标可被
    #: 多个族各自判定——各自的逐任务输入写在族文件里，因为同一项指标在
    #: 不同族上用不同锚点判不同任务）。
    classification: Literal["gate", "dimension"]

    @model_validator(mode="before")
    @classmethod
    def _no_inputs_here(cls, data: Any) -> Any:
        """输入不归目录：同一项指标在不同族上用不同锚点判不同任务。"""
        if isinstance(data, dict) and "inputs" in data:
            raise SuiteError(
                "verdict.metric_catalog entry declares 'inputs' — per-task "
                "inputs are the CONSUMER's rubric binding (the same metric id "
                "can be judged by several families with different anchors); "
                "write inputs in the family's metrics list, not in the catalog"
            )
        return data

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if value != value.strip() or not value.strip():
            raise SuiteError("metric catalog id must be a non-empty slug")
        return value


class GuardMetricSpec(_SuiteModel):
    """守卫的一项轨迹指标：声明里是**纯引用**，绑定后带目录的 severity。

    - 声明文件（``verdict.guards[].metrics``）只写 ``id``；写 ``severity``
      会被加载期的"定义只有一处"检查拒绝（目录才是定义处）；
    - ``severity`` 缺省 ``None`` = 尚未绑定（裸引用）。绑定后由
      ``verdict.metric_catalog`` 补齐 ``score|integrity``——``integrity``
      是诚信门（违规即 fail/veto），``score`` 只降分。
    - 加载期与实现 ``category`` 对表，对不上就拒绝装载。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    #: None = 未绑定（声明文件里的裸引用）。判分前必须由目录补全。
    severity: Literal["score", "integrity"] | None = None


class GraderDeclaration(_SuiteModel):
    impl: str
    layer: Literal["outcome", "trajectory", "both"] = "outcome"
    veto: bool = False
    version: str | None = None
    # v5 A / Step 6：守卫的 trace 判定清单（声明 = 目录 id 引用，顺序 =
    # 判分顺序；severity 由目录绑定）。None/空 = 模块自带清单（未迁移的
    # legacy 守卫，行为逐字节不变）；非空 = 声明驱动装配，模块必须是暴露
    # ``METRICS`` 的指标库。
    metrics: list[GuardMetricSpec] | None = None

    @field_validator("metrics", mode="before")
    @classmethod
    def _metrics_accept_bare_ids(cls, metrics: Any) -> Any:
        """``metrics: [step_efficiency, {id: forbidden_access}]`` 两种写法。"""
        if not isinstance(metrics, (list, tuple)):
            return metrics
        return [
            {"id": entry} if isinstance(entry, str) else entry
            for entry in metrics
        ]

    @field_validator("metrics")
    @classmethod
    def _metrics_unique(
        cls, metrics: list[GuardMetricSpec] | None
    ) -> list[GuardMetricSpec] | None:
        if not metrics:
            return metrics
        seen: set[str] = set()
        for metric in metrics:
            if metric.id in seen:
                raise SuiteError(
                    f"guard declaration lists metric {metric.id!r} twice — "
                    "the declared list is the judging order, so it must be a set"
                )
            seen.add(metric.id)
        return metrics


class FamilyEngine(_SuiteModel):
    """族引擎声明（gradeCategory 的判分绑定）。

    ``impl`` 是 ``<path>.py@<version>``，相对**引用套件**根解析（消费
    套件负责提供同路径实现——族声明因此可移植，D6）。分组式类别
    （D7 特例）例外：引擎 impl 与官方基线（default 槽）相同。
    """

    impl: str
    layer: Literal["outcome", "trajectory", "both"] = "trajectory"
    veto: bool = False


class ScopeBinding(_SuiteModel):
    """族在一个 task 上的**显示元数据与角色**（v5 Step 3）。

    族的适用范围不再是一层定义：一条试次能不能被这个族判，由**哪个
    指标在那条试次上有输入**派生（``MetricSpec.inputs`` 的任务键）。
    这里只放"报告怎么显示、算不算主场"：

    - ``title``：报告里的显示名；
    - ``role``：``home``（本族主场）或 ``enrich``（丰富测量——该 task
      的官方评分照常，本族叠加测量）。

    给某个 task 写 scope 却没有任何指标在那个 task 上有输入，是声明
    错误（这条绑定什么也测不了）。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    title: str | None = None
    role: Literal["home", "enrich"] = "home"


class MetricSpec(_SuiteModel):
    """族的一项指标（v5：指标是判定真源，清单进族声明）。

    指标不再只是运行期 ``GradeResult.metrics`` 的副产物：族声明逐项
    列出它要判的指标（顺序 = 判分顺序），每项自带严重度与（可选的）
    降级线。实现由族引擎模块的注册表按 ``id`` 提供——声明说"判哪
    些"，实现说"怎么判"，两处不再互相复述。

    - ``layer``：这项指标属于哪一层（v5 Step 2：指标自带层，族按层
      装配；缺省跟随族引擎的层，声明了就必须与之一致——outcome 层
      不会装上 trajectory 指标）；
    - ``severity``：``score``（计分维度）或 ``integrity``（诚信维度，
      违规即 veto 候选）。这是**判分真源**里的严重度：实现自身的
      ``MetricOutcome.category`` 只作断言目标（矛盾即加载期报错）；
    - ``degraded_below``：本项自身的降级线（缺省由实现决定）；
    - ``inputs``：本项自己的判分输入，**按任务键**（``{task_id: {...}}``）
      ——输入是这项指标的数据，任务只是它的键；族不再需要"用例"这一
      层定义，适用范围由指标的输入派生（v5 Step 3）。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    layer: Literal["outcome", "trajectory", "both"] | None = None
    severity: Literal["score", "integrity"] = "score"
    degraded_below: float | None = Field(default=None, gt=0, le=1)
    inputs: dict[str, dict[str, Any]] | None = None

    @field_validator("id")
    @classmethod
    def _id_is_slug(cls, value: str) -> str:
        if value != value.strip() or not value.strip():
            raise SuiteError("metric id must be a non-empty slug")
        return value


def layers_compatible(metric_layer: str, consumer_layer: str) -> bool:
    """指标的层与消费者的层相容？（``both`` = 层无关，与任意层相容。）

    一条规则、一处代码：族声明校验器与目录绑定（``suite_loader.catalog``）
    都调它——否则"层合不合"迟早有两个答案。
    """
    return (
        metric_layer == consumer_layer
        or metric_layer == "both"
        or consumer_layer == "both"
    )


class FamilySpec(_SuiteModel):
    """gradeCategory（判分类别 = 指标族）声明——自足可移植（D6）。

    规格字段与今日类别规格逐一对应（name/block/weight/threshold/
    redline 原样带入）；``engine``、``metrics`` 与 ``scope`` 是新增。
    ``metrics`` 是**判分真源**：这个族判哪些指标（顺序 = 判分顺序），
    引擎模块只提供 ``id → 实现`` 的注册表，不在代码里另存一份清单。
    **族不再定义"用例"**：适用范围由指标的输入（``MetricSpec.inputs``
    的任务键）派生，``scope`` 只放显示名与 home/enrich 角色。
    类别声明不反向引用 suite——由套件 ``families:`` 清单单向
    激活。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    block: str = Field(min_length=1)
    weight: float = Field(default=1.0, gt=0)
    threshold: float = Field(gt=0, le=1)
    redline: bool = False
    engine: FamilyEngine
    #: 判分真源：本族判哪些指标（顺序 = 判分顺序）。**空清单只允许
    #: 分组式类别**（D7 特例：引擎 = 官方基线，族只分组、自带零指标）
    #: ——由加载期强制，避免"忘了写指标清单"被当成合法声明。
    metrics: list[MetricSpec] = Field(default_factory=list)
    #: 显示元数据 / 角色覆盖（可缺省：适用性派生自指标输入）。
    scope: dict[str, ScopeBinding] = Field(default_factory=dict)

    @field_validator("metrics", mode="before")
    @classmethod
    def _metrics_accept_bare_ids(cls, metrics: Any) -> Any:
        """允许 ``metrics: [tool_selection, {id: ...}]`` 两种写法。

        裸字符串是常见情形（只列 id、用实现缺省），写成映射时才声明
        严重度/降级线/输入；两种写法归一化到同一模型，重复 id 报错。
        """
        if not isinstance(metrics, (list, tuple)):
            return metrics
        normalized: list[Any] = []
        for entry in metrics:
            if isinstance(entry, str):
                normalized.append({"id": entry})
            else:
                normalized.append(entry)
        seen: set[str] = set()
        for entry in normalized:
            metric_id = entry.get("id") if isinstance(entry, dict) else None
            if isinstance(metric_id, str):
                if metric_id in seen:
                    raise SuiteError(
                        f"duplicate metric id {metric_id!r} in one grade "
                        "category — a metric is judged once per family"
                    )
                seen.add(metric_id)
        return normalized

    @field_validator("scope")
    @classmethod
    def _scope_ids_nonempty(cls, scope: dict[str, ScopeBinding]) -> dict[str, ScopeBinding]:
        for task_id in scope:
            if not task_id.strip():
                raise SuiteError("grade category scope keys must be task ids (non-empty)")
        return scope

    @model_validator(mode="after")
    def _metrics_own_their_inputs(self) -> Self:
        """指标输入的任务键 = 本族的适用范围（v5 Step 3：用例退场）。

        两条 fail-closed：scope 里的 task 必须真的被某个指标输入覆盖
        （否则这条绑定什么也测不了），指标 layer 必须与族引擎同层
        （outcome 层不会装上 trajectory 指标）。
        """
        derived = self.input_task_ids()
        for task_id in self.scope:
            # 结构判定族（没有任何指标吃输入，如安全族）的适用范围只能是
            # 显式 scope（今天的 23 个 task 就是这样来的）；吃了输入的族
            # 则 scope 只是显示元数据，键必须被某个指标覆盖。
            if task_id not in derived and derived:
                raise SuiteError(
                    f"grade category {self.name!r} scope declares task "
                    f"{task_id!r} but no metric carries inputs for it — a "
                    "family's reach is derived from its metrics' inputs "
                    "(scope only carries display metadata)"
                )
        engine_layer = self.engine.layer
        for metric in self.metrics:
            if metric.layer is None or layers_compatible(metric.layer, engine_layer):
                continue
            raise SuiteError(
                f"grade category {self.name!r} metric {metric.id!r} declares "
                f"layer {metric.layer!r} but the family engine runs on "
                f"{engine_layer!r} — a family is assembled per layer"
            )
        return self

    @model_validator(mode="after")
    def _redline_full_threshold(self) -> Self:
        if self.redline and self.threshold != 1.0:
            raise SuiteError(
                f"grade category {self.name!r}: redline: true requires "
                "threshold: 1.0 — a redline has no partial achievement"
            )
        return self

    def metric_ids(self) -> tuple[str, ...]:
        """声明的指标 id（判分顺序）。"""
        return tuple(metric.id for metric in self.metrics)

    def input_task_ids(self) -> tuple[str, ...]:
        """**由指标输入派生**的任务键（稳定序 = 声明序）。"""
        seen: list[str] = []
        for metric in self.metrics:
            for task_id in (metric.inputs or {}):
                if task_id not in seen:
                    seen.append(task_id)
        return tuple(seen)

    def task_ids(self) -> tuple[str, ...]:
        """本族实际的适用范围（D7 族行口径）。

        - 有指标吃输入 ⇒ 那些输入的任务键（派生；scope 只是显示元数据）；
        - 没有指标吃输入（结构判定族，如安全族）⇒ 显式 ``scope`` 的任务
          列表；scope 也空 ⇒ 空元组，表示"对每条试次都成立"。
        """
        derived = self.input_task_ids()
        if derived:
            return derived
        return tuple(self.scope)

    def metric_layer(self, metric_id: str) -> str:
        """该项指标的层（缺省 = 族引擎的层）。"""
        for metric in self.metrics:
            if metric.id == metric_id:
                return metric.layer or self.engine.layer
        raise SuiteError(f"family {self.name!r} declares no metric {metric_id!r}")

    def inputs_for(self, task_id: str) -> dict[str, dict[str, Any]]:
        """该 task 上、按指标 id 归位的输入（判定适用性的唯一依据）。"""
        return {
            metric.id: metric.inputs[task_id]
            for metric in self.metrics
            if metric.inputs and task_id in metric.inputs
        }

    def binds(self, task_id: str) -> bool:
        """该 task 是否落在本族范围内（v5 Step 3：派生适用性）。

        有指标的输入 ⇒ 由输入派生；结构判定族（无输入）⇒ 显式 scope；
        两者都空 ⇒ 对每条试次都成立（如全局安全族）。
        """
        derived = self.input_task_ids()
        if derived:
            return task_id in derived
        if self.scope:
            return task_id in self.scope
        return True

    def spec_digest(self) -> str:
        """规格摘要（族声明的对账键）：name/block/weight/threshold/
        redline/指标清单。跨 run 合并行的前提是 run 可比
        （``compare_manifests`` 全维度，含本摘要与用例集逐字节）；不可
        比 ⇒ 同键族各自成行。指标清单进摘要：改判哪些指标就是换身份。"""
        payload = {
            "name": self.name,
            "block": self.block,
            "weight": self.weight,
            "threshold": self.threshold,
            "redline": self.redline,
            "metrics": [metric.model_dump(mode="json", exclude_none=True)
                        for metric in self.metrics],
        }
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()


class VerdictSpec(_SuiteModel):
    requirements: list[str] = Field(min_length=1)
    # 只有 default 槽（官方基线）。其余判分一律走 guards 或族——v5 起
    # 没有第二种声明形状，所以这里不再接受 list 形式与 extra 槽。
    graders: dict[str, GraderDeclaration] = Field(default_factory=dict)
    # 守卫（新格式可选字段）：套件级有效性守门（反作弊 veto / 预算
    # 信息性），对全体 task 生效、折叠进终判、无类别行。与族的分工
    # （D4）：族有引擎有用例集有类别行；守卫只守门。
    guards: list[GraderDeclaration] = Field(default_factory=list)
    # 指标目录（v5 Step 6）：套件里每项判分指标的**唯一定义处**。每项自带
    # 层/严重度/降级线/逐任务输入与**归属**（classification: gate 或族键）；
    # 族文件与守卫声明只写 id 引用。空 = 还没迁移的旧套件（族文件自带
    # 定义，行为逐字节不变）。
    metric_catalog: list[MetricCatalogEntry] = Field(default_factory=list)
    # Opt-in sealed rubric-anchors channel: when set,
    # every trial's evidence bundle must seal the suite's
    # ``rubric/task_anchors.json`` as the ``task_anchors`` fixed output,
    # and trajectory graders read their anchors from the sealed copy —
    # a regrade then depends only on sealed bytes, never on the suite
    # repo's current state. None (the default) keeps every suite
    # sealed before the channel existed byte-identical.
    anchors: Literal["task_anchors"] | None = None

    @field_validator("graders")
    @classmethod
    def _only_the_baseline_slot(cls, graders: dict[str, GraderDeclaration]):
        """判分声明只有一个家：基线占 default，其余是守卫或族。"""
        extra = sorted(set(graders) - {"default"})
        if extra:
            raise SuiteError(
                f"verdict.graders declares slot(s) {extra} — the only slot is "
                "'default' (the official baseline); every other grader is either "
                "a guard (verdict.guards) or a metric family "
                "(families/<key>.yaml)"
            )
        default = graders.get("default")
        if default is not None and default.layer not in ("outcome", "both"):
            raise SuiteError(
                "verdict.graders.default must be the outcome-layer official "
                "baseline"
            )
        if default is not None and default.metrics:
            raise SuiteError(
                "verdict.graders.default declares a trace metric list — the "
                "official baseline judges the task's own outcome; trajectory "
                "metrics belong to a guard (verdict.guards) or a family"
            )
        return graders

    @field_validator("metric_catalog")
    @classmethod
    def _catalog_ids_unique(
        cls, catalog: list[MetricCatalogEntry]
    ) -> list[MetricCatalogEntry]:
        """一个指标一个定义：目录里 id 不允许重复（重复 = 两个家）。"""
        seen: set[str] = set()
        for entry in catalog:
            if entry.id in seen:
                raise SuiteError(
                    f"verdict.metric_catalog defines metric {entry.id!r} twice "
                    "— a metric has exactly one definition"
                )
            seen.add(entry.id)
        return catalog

    def catalog_entry(self, metric_id: str) -> MetricCatalogEntry | None:
        for entry in self.metric_catalog:
            if entry.id == metric_id:
                return entry
        return None

    @field_validator("guards")
    @classmethod
    def _guards_are_trajectory(cls, guards: list[GraderDeclaration]) -> list[GraderDeclaration]:
        """守卫是轨迹层事实（反作弊/预算），不是 outcome——outcome 恰好
        一个，就是官方基线（default 槽）。"""
        for guard in guards:
            if guard.layer not in ("trajectory", "both"):
                raise SuiteError(
                    f"verdict.guards entry {guard.impl!r} must be a trajectory-layer "
                    "grader — the single outcome layer is the baseline (default slot)"
                )
        return guards

    def resolved_graders(self) -> list[GraderDeclaration]:
        if isinstance(self.graders, dict):
            out: list[GraderDeclaration] = []
            keys = ["default"] if "default" in self.graders else []
            keys.extend(key for key in self.graders if key != "default")
            for key in keys:
                grader = self.graders[key]
                out.extend(grader if isinstance(grader, list) else [grader])
            return out
        return list(self.graders)


class MetricDeclaration(_SuiteModel):
    id: str
    kind: Literal["pass_pow_k", "cost_normalized", "exclusion_rate"]
    k: int | None = None


class DriverSpec(_SuiteModel):
    require: list[str] = Field(default_factory=list)
    # Which session-record collect output this suite's evidence bundles use.
    # The slot generalised off the DSH-only hardcode: an adapter declares the
    # slot it can produce (contract ``SESSION_RECORD_OUTPUT``) and the suite
    # declares the slot its tasks' collect commands name — a pairing where
    # the two disagree is refused at composition, not discovered mid-collection.
    # Any well-formed slug is a declared slot (its adapter must then carry the
    # fixed path); the DEFAULT is untouched so every suite sealed before the
    # vocabulary opened keeps parsing and behaving byte-identically (I2).
    session_record: str = "dsh_session"

    @field_validator("session_record")
    @classmethod
    def _session_record_slot(cls, value: str) -> str:
        from aeval.agents.contract import session_record_slot_well_formed

        if not session_record_slot_well_formed(value):
            raise ValueError(
                "must be a lowercase slot slug (built-ins: dsh_session, "
                f"agent_session_record), got {value!r}"
            )
        return value
    # Stage the task's `tests/` directory into the sandbox right after the
    # agent ends and BEFORE evidence collection.
    #
    # Needed when the suite's collect command runs the task's own verifier
    # and the suite reads the reward it publishes: Terminal-Bench tasks
    # write /logs/verifier/reward.txt from tests/test.sh, and aeval
    # collects observables during the collect phase — but Harbor uploads
    # `tests/` only at verification time, i.e. AFTER collection. Staging
    # here closes that ordering gap. It does not leak the tests to the
    # agent: the upload happens once the agent has stopped.
    stage_tests_before_collect: bool = False
    # Working directory inside the sandbox for the agent's run — and for
    # the DSH session the control plugin mints. It must equal the cwd the
    # task's own verifier assumes, i.e. the environment Dockerfile's
    # WORKDIR: Terminal-Bench tasks hardcode absolute paths (`/app/...`,
    # and hello-world says "the current directory"), so an agent that runs
    # in the owner's default `/workspace` writes its answer where the
    # tests will never look (measured on the pilot: every trial failed
    # with `FileNotFoundError: /app/hello.txt`).
    #
    # It is one value, not two: DSH refuses to resume a session whose
    # recorded cwd differs from the run's, so the mint and the run must
    # agree.
    workspace_dir: str = "/workspace"
    # Per-flavor options for the in-sandbox control stack, namespaced by the
    # registered flavor name (``control_stack``). The framework never
    # interprets them: the flavor that owns the namespace validates its own
    # keys and values (fail-closed), so a family-specific knob stays a family
    # fact instead of becoming a framework field only one agent ever reads —
    # DSH's permission mode used to be ``driver.sandbox_mode`` here.
    #
    # Example (DSH, whose sealed pilot image ships no bwrap/Landlock runner, so
    # its shell would refuse every command without this):
    #
    #     control_options:
    #       dsh: {permission_mode: danger-full-access}
    control_options: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("control_options")
    @classmethod
    def _option_namespaces(
        cls, value: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        import re as _re

        for name, options in value.items():
            if not _re.match(r"^[a-z][a-z0-9-]*$", str(name)):
                raise ValueError(
                    f"control_options names {name!r}; namespaces are control "
                    "flavor names (lowercase slugs)"
                )
            if not isinstance(options, dict):
                raise ValueError(
                    f"control_options.{name} must be a mapping of option -> value"
                )
        return value


class ProvenanceInfo(_SuiteModel):
    source: str
    source_url: str | None = None
    original_id: str | None = None
    imported_at: str | None = None
    converter_version: str | None = None
    license: Literal["CC0", "MIT", "Apache-2.0", "NONE_DECLARED", "UNKNOWN"]
    data_imported: bool = False
    rewritten_by_us: bool = False

    @field_validator("license", mode="before")
    @classmethod
    def _normalize_license(cls, v: Any) -> Any:
        return "CC0" if v == "CC0-1.0" else v

    @model_validator(mode="after")
    def _license_gate(self) -> Self:
        if self.license in ("NONE_DECLARED", "UNKNOWN") and self.data_imported:
            raise SuiteError(
                "provenance with license NONE_DECLARED/UNKNOWN must set "
                "data_imported=false — format skeleton only, no data"
            )
        return self


class HarborInputs(_SuiteModel):
    """Resolved references to Harbor-native declarations (not copies)."""

    dataset: str
    job: str
    dataset_digest: str | None = None
    job_digest: str | None = None


class SuiteOverlay(_SuiteModel):
    schema_version: int
    id: str
    version: str
    harbor: HarborInputs
    image: dict[str, ImagePinAction] = Field(default_factory=dict)
    baselines: list[BaselineAssertion] = Field(min_length=1)
    clock: ClockSpec
    observables: list[ObservableSpec] = Field(min_length=1)
    verdict: VerdictSpec
    metrics: list[MetricDeclaration] = Field(default_factory=list)
    driver: DriverSpec = Field(default_factory=DriverSpec)
    # 激活的族清单（单向锁定，D6）：相对套件根的路径列表，按列表序
    # 激活；列出的族文件必须存在且可解析（缺 → 报错）。未列入的
    # families/ 文件惰性存在（不报错、不进 digest）。
    families: list[str] = Field(default_factory=list)
    # 大块显示名（雷达轴）。显示层：聚合报告沿用 setdefault（先到者取）。
    blocks: dict[str, str] = Field(default_factory=dict)
    # Optional spend cap for a trial (max_tokens/max_seconds/max_steps). Declaring
    # one makes the enforcement point load-bearing: an adapter that cannot be
    # metered is refused at run start (see aeval.agents.contract.budget_gate_violation).
    budget: BudgetSnapshot | None = None
    provenance: ProvenanceInfo

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, v: int) -> int:
        if v != OVERLAY_SCHEMA_VERSION:
            raise SuiteError(
                f"suite.yaml schema_version: expected {OVERLAY_SCHEMA_VERSION}, got {v}"
            )
        return v

    @property
    def has_families(self) -> bool:
        """套件是否激活了指标族。空 = baseline-only：只有官方基线 + 守卫。"""
        return bool(self.families)

    @model_validator(mode="after")
    def _declaration_shape(self) -> Self:
        """判分声明的唯一形状（v5：所有套件同一形状，没有"新旧格式"两条路）。

        - 官方基线恰好一个（P2）：``verdict.graders`` 只有 default 槽，且
          必须是 outcome 层——其余判分一律走守卫或族；
        - 锚点密封通道按需强制：族里有指标吃输入时才要求
          ``verdict.anchors: task_anchors``（判据在 loader 里，因为那里
          才看得到族文件；结构判定族不需要这条通道）。

        这里不再有 ``if not family_mode: return self`` 的豁免——baseline-only
        套件（outcome-only）与族套件走同一套约束。
        """
        graders = self.verdict.graders
        if set(graders) != {"default"}:
            raise SuiteError(
                "verdict.graders must declare exactly one slot: default (the "
                "official baseline) — every other grader moves to verdict.guards "
                "or a metric family"
            )
        default = graders["default"]
        if default.layer not in ("outcome", "both"):
            raise SuiteError(
                "verdict.graders.default must be the outcome-layer official baseline"
            )
        return self


class SuiteSource(_SuiteModel):
    """One manifest in a suite's inheritance chain (base first, child last).

    ``path`` is portable and relative to the suites root; ``digest`` is the
    sha256 of that file's raw bytes. The chain is what makes an inherited
    fact visible to run identity: editing a base changes every dependent
    suite's ``overlay_chain_digest``.

    ``family`` sources are the ACTIVATED family declarations
    (``families/<key>.yaml``): their bytes join the chain digest
    (v4 design) so editing a family changes run identity; ``path`` is relative
    to the suite directory (family files are self-contained there).
    Unactivated library files never appear — listing is activation.
    """

    path: str
    digest: str
    role: Literal["base", "child", "family"]


class ResolvedSuite(_SuiteModel):
    model_config = {"arbitrary_types_allowed": True}

    overlay: SuiteOverlay
    suite_dir: Any  # Path
    # Raw bytes of THIS suite's suite.yaml. Kept unchanged (and still computed)
    # so evidence sealed before inheritance existed can be recomputed.
    suite_yaml_digest: str
    # Digest of the resolved overlay PLUS every source file in the chain.
    # This is what identity and comparability use.
    overlay_chain_digest: str = ""
    sources: list[SuiteSource] = Field(default_factory=list)
    # 中文任务显示名（task_id → 标题）：套件目录 task_titles.yaml 的内
    # 容，随清单封存。显示层专用——坐标/判分/存储永远用原 task_id。
    task_titles: dict[str, str] = Field(default_factory=dict)
    # 类别聚合（报告用）：类别键 → 中文显示名 + 无点分前缀任务的默认
    # 类别。归组规则在报告侧确定性推导（task_id 首个点前的前缀）。
    # 维度模型：categories（name/block/weight/threshold/redline 详式
    # 或 str 简式）+ blocks + redline_tasks + default——评分参数（阈
    # 值/权重/大块/红线），不改变判定/分母语义。新格式（族模式）下
    # 换为 families/baseline/guards 的密封映射（v4）。
    dimension_model: dict[str, object] = Field(default_factory=dict)
    # 族声明（新格式）：激活的 gradeCategory，键 = 文件名主干，按套
    # 件 families 列表序（解析顺序确定性）。旧套件为空。
    families: dict[str, FamilySpec] = Field(default_factory=dict)
    # 族键 → 声明文件相对套件根的路径（解释页/诊断用）。
    family_paths: dict[str, str] = Field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.overlay.id

    @property
    def version(self) -> str:
        return self.overlay.version

    @property
    def has_families(self) -> bool:
        """套件是否激活了指标族（空 = baseline-only）。"""
        return bool(self.families)

    @property
    def extends(self) -> list[str]:
        """Base files this suite inherits from, in application order."""
        return [source.path for source in self.sources if source.role == "base"]

    def identity(self) -> tuple[str, str, str]:
        return (self.overlay.id, self.overlay.version, self.identity_digest)

    @property
    def identity_digest(self) -> str:
        """Chain digest, falling back to the raw file digest for legacy values."""
        return self.overlay_chain_digest or self.suite_yaml_digest

    # --- 族装配（逐试次装配，P13） --------------------------------

    def baseline(self) -> GraderDeclaration | None:
        """官方基线（default 槽；声明形状保证它存在）。"""
        graders = self.overlay.verdict.graders
        if isinstance(graders.get("default"), GraderDeclaration):
            return graders["default"]
        return None

    def guards(self) -> list[GraderDeclaration]:
        """守卫声明序（套件级有效性门，对全体 task 生效）。"""
        return list(self.overlay.verdict.guards)

    def family_task_union(self) -> set[str]:
        """族适用范围之并（离线判分对账的口径）——由指标输入派生。"""
        union: set[str] = set()
        for spec in self.families.values():
            union.update(spec.task_ids())
        return union

    def resolved_graders(self, task_id: str | None = None) -> list[GraderDeclaration]:
        """一条试次的有效判分集合（D1 装配）。

        新格式（task_id 给出）::

            [官方基线] + [守卫（声明序）] + [族引擎（激活列表序，取
            声明了该 task 的族）]   —— 按 impl 去重（防双跑兜底）

        task_id 为 None = 全体声明展开（诊断/身份对账用）；baseline-only
        套件（无族）= [官方基线] + [守卫]。
        """
        baseline = self.baseline()
        out: list[GraderDeclaration] = [baseline] if baseline is not None else []
        out.extend(self.guards())
        for key, spec in self.families.items():
            if task_id is not None and not spec.binds(task_id):
                continue
            out.append(spec.engine)
        # impl 去重（兜底断言）：同一 impl 至多跑一次——分组式类别
        # （D7 特例）引擎与官方基线同 impl 不属于双跑。
        seen: set[str] = set()
        deduped: list[GraderDeclaration] = []
        for declared in out:
            if declared.impl in seen:
                continue
            seen.add(declared.impl)
            deduped.append(declared)
        return deduped

    def sealed_anchor_projection(self) -> bytes:
        """族锚点的确定性投影 → ``rubric/task_anchors.json`` 字节。

        结构：``{task_id: {<族键>: {<指标 id>: inputs}}}``（只含声明了
        逐指标输入的族×用例；无输入的族不占键）。sort_keys 保证同一份
        声明两次投影字节一致——输入变更 ⇒ 族声明变 ⇒ 新 run 身份，投影
        本身不单独进身份摘要。
        """
        projection: dict[str, dict[str, Any]] = {}
        for key, spec in self.families.items():
            for task_id in spec.task_ids():
                projection.setdefault(task_id, {})[key] = spec.inputs_for(task_id)
        return json.dumps(
            projection, sort_keys=True, indent=2, ensure_ascii=False
        ).encode("utf-8") + b"\n"

def load_suite_yaml(path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SuiteError(f"Cannot read suite manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SuiteError(f"suite manifest must be a mapping: {path}")
    return data


def overlay_digest_of(path) -> str:
    """Stable digest of the suite.yaml file — part of run identity."""
    raw = path.read_bytes()
    return sha256(raw).hexdigest()
