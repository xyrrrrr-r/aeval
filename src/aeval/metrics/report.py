"""Run aggregation and reporting.

Every number in the report is traceable: evidence refs, versions, the
denominator, comparability, recompute level. Session content never
leaks into reports.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

from aeval.bundle.attestation import (
    ComparabilityReport,
    compare_all_manifests,
    compare_manifests,
)
from aeval.contracts import ExclusionSummary, RunManifest, TrialRecord, Verdict
from aeval.metrics.reliability import (
    EXCLUSION_RATE_LIMIT,
    cost_per_pass,
    exclusion_summary,
    pass_pow_k,
    valid_trials,
)
from aeval.metrics.reliability import _excluded_reasons as _trial_excluded_reasons
from aeval.verdict.trajectory.labels import metric_zh

__all__ = [
    "RunSummary",
    "TaskGroupSummary",
    "CategoryTrajectory",
    "TrajectoryAggregate",
    "FamilyRollup",
    "MetricStatusCounts",
    "TaskMetricItem",
    "TaskMetricRow",
    "FamilyMetricBlock",
    "GuardMetricRow",
    "BaselineTaskRow",
    "BaselineGuardDetail",
    "FamilyDetail",
    "aggregate_run",
    "rollup_families",
    "family_pass_pow_k",
    "render_static_report",
    "export_jsonl",
]


@dataclass(frozen=True)
class MetricStatusCounts:
    """一项指标在若干试次上的状态计数（skipped 必带原因）。"""

    ok: int = 0
    degraded: int = 0
    violated: int = 0
    skipped: int = 0
    skipped_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskMetricItem:
    """族引擎在一个任务上对一项指标的逐试次状态聚合。"""

    metric_id: str
    counts: MetricStatusCounts


@dataclass(frozen=True)
class TaskMetricRow:
    """族 → 任务行：task_id + 显示名 + home/enrich + 逐指标状态。

    v5 Step 3：这里不再是"用例"行——族的适用范围由指标输入派生，
    任务是那些输入的键，行只回答"这个族在这个任务上判了什么"。
    """

    task_id: str
    title: str | None
    role: str
    trials: int                    # 出过引擎结果的（有效）试次数
    metrics: tuple[TaskMetricItem, ...]


@dataclass(frozen=True)
class FamilyMetricBlock:
    """一个族的逐项指标块。

    ``declared`` 是该族声明的指标清单（判分顺序，来自密封的族声明）
    ——报告的行序与"测了哪几项"以它为准，不再靠运行结果反推；声明了
    却没有判定落点的项显示为 0 计数并注明，不静默消失。
    """

    key: str
    display: str
    engine_labels: tuple[str, ...]  # 各 run 的 grader_id@version（去重）
    tasks: tuple[TaskMetricRow, ...]
    declared: tuple[str, ...] = ()


@dataclass(frozen=True)
class GuardMetricRow:
    """一个守卫的逐项指标聚合（守卫无类别行，明细单列）。

    ``declared`` = 套件声明里这个守卫的 trace 指标清单（判分顺序，v5 A）。
    空 = 未迁移的 legacy 守卫（模块自带清单）——两行的区别在报告里要看得
    出来，否则"声明是判分真源"就只是口号。
    """

    grader_label: str
    metrics: tuple[TaskMetricItem, ...]
    trials: int
    declared: tuple[str, ...] = ()
    legacy: bool = False  # v4 封存 run：按判分层列出，归属不代声明断言


@dataclass(frozen=True)
class BaselineTaskRow:
    """官方基线在一个任务上的判定计数（reward 口径）。"""

    task_id: str
    title: str | None
    trials: int
    passes: int
    fails: int
    cannot_judge: int


@dataclass(frozen=True)
class BaselineGuardDetail:
    """「基线与守卫」明细：官方基线逐任务判定 + 守卫逐指标状态。"""

    baseline_labels: tuple[str, ...]
    tasks: tuple[BaselineTaskRow, ...]
    guards: tuple[GuardMetricRow, ...]
    legacy: bool = False  # v4 封存模型：无 metric_catalog，按判分层列出
    #: 基线逐任务计数与「终判结果」**不同值**的那些行——守卫 veto / 族阈值
    #: 改判的证据。空 = 基线表只是终判表的翻版（同任务集、同试次、同通过/
    #: 失败），一个字的新事实都没有，不该复列。
    divergent_tasks: tuple[BaselineTaskRow, ...] = ()

    @property
    def repeats_final(self) -> bool:
        """基线逐任务表是否只是「终判结果」那张表的翻版。"""
        return bool(self.tasks) and not self.divergent_tasks


@dataclass(frozen=True)
class FamilyDetail:
    """逐项指标层：族块 + 基线与守卫。"""

    families: tuple[FamilyMetricBlock, ...]
    baseline_guards: BaselineGuardDetail | None


def metric_detail_absence_note(
    detail: FamilyDetail | None, *, final_table_rendered: bool = True
) -> tuple[str, ...]:
    """「逐项指标」没有内容可列时，讲清楚**为什么空**。

    这一节静默消失过一次，读者的第一反应是"指标去哪了"（sbench 型：12 个族
    都是归组式、``metrics: []``，89 个试次各只有一条 outcome 层基线判定，没有
    任何指标级落点）。与其让人猜，不如把"本套件没声明指标"这件事本身写出来，
    并指向真正有数字的两节。
    """
    if detail is None or metric_detail_has_content(
        detail, final_table_rendered=final_table_rendered
    ):
        return ()
    baseline = detail.baseline_guards
    labels = "、".join(baseline.baseline_labels) if baseline else "—"
    lines: list[str] = []
    if detail.families:
        lines.append(
            f"本套件没有声明指标：{len(detail.families)} 个族都是归组式"
            f"（engine 就是官方基线 {labels}，metrics: []），判定由基线给出。"
        )
    if baseline is None or not baseline.guards:
        lines.append("本 run 也没有守卫 / 轨迹层判分器产出的指标判定。")
    lines.append(
        "逐任务通过 / 失败见「终判结果」；按族的通过率与判定分工见「按类别结果」。"
    )
    return tuple(lines)


def task_metric_split(
    case: TaskMetricRow, declared: Sequence[str] = ()
) -> tuple[tuple[TaskMetricItem, ...], int]:
    """任务行该讲什么：本任务**判过**的指标项（按声明顺序）+ 跳过的项数。

    跳过项的名字不再逐条复列——它们就是「声明清单 − 本任务判的项」，同一节
    上面的指标表已经把每一项连同跳过原因列全了。12 项指标 × 11 个任务的
    跳过明细 = 把同一批名字抄 11 遍（用户实测点名的问题）。
    """
    order = {metric_id: i for i, metric_id in enumerate(declared)}
    judged = [
        item
        for item in case.metrics
        if item.counts.ok or item.counts.degraded or item.counts.violated
    ]
    judged.sort(key=lambda item: (order.get(item.metric_id, len(order)), item.metric_id))
    skipped_items = sum(1 for item in case.metrics if item.counts.skipped)
    return tuple(judged), skipped_items


def metric_counts_text(item: TaskMetricItem) -> str:
    """一项指标在一个任务上的计数文案：``ok 3 · 降级 1``（零计数不写）。"""
    pieces = []
    if item.counts.ok:
        pieces.append(f"ok {item.counts.ok}")
    if item.counts.degraded:
        pieces.append(f"降级 {item.counts.degraded}")
    if item.counts.violated:
        pieces.append(f"违规 {item.counts.violated}")
    return " · ".join(pieces)


def guard_role(guard: GuardMetricRow, legacy: bool) -> str:
    """守卫的角色名：v4 封存 run 只按判分层可讲，不冒充「守卫」。"""
    return "轨迹层判分器" if (guard.legacy or legacy) else "守卫"


def metric_display(metric_id: str) -> str:
    """指标列文案：``中文(id)``；没登记过中文名就只给 id（不硬编）。"""
    zh = metric_zh(metric_id)
    return f"{zh}({metric_id})" if zh else metric_id


def baseline_divergent_rows(
    summary: RunSummary, rows: Sequence[BaselineTaskRow]
) -> tuple[BaselineTaskRow, ...]:
    """基线里哪几行与「终判结果」不同值（= 守卫 veto / 族阈值改判的证据）。

    同任务集 + 同试次 + 同通过/失败的行没有任何新事实（``无法判定`` 也等于
    「有效 − 通过 − 失败」，终判那张表读得出来）——那不是明细，是把上面
    那张表抄一遍。返回空元组 ⇒ 这一层与终判逐行同值；返回全部行 ⇒ 任务集
    本身不同（终判没提到的任务也得列）。
    """
    if not rows or not summary.task_groups:
        return ()
    if {row.task_id for row in rows} != set(summary.task_groups):
        return tuple(rows)
    divergent: list[BaselineTaskRow] = []
    for row in rows:
        group = summary.task_groups[row.task_id]
        if (row.trials, row.passes, row.fails) != (
            group.total_trials,
            group.passes,
            group.fails,
        ):
            divergent.append(row)
    return tuple(divergent)


def baseline_rows_to_render(
    baseline_guards: BaselineGuardDetail, *, final_table_rendered: bool
) -> tuple[BaselineTaskRow, ...]:
    """逐项指标里该列哪几行基线：只列与终判**不同值**的行。

    「终判结果」本身不渲染时（单任务 run 的 markdown 直接读上面的行）不能省：
    那时基线表是唯一的逐任务落点。
    """
    if not final_table_rendered:
        return baseline_guards.tasks
    return baseline_guards.divergent_tasks


def metric_detail_has_content(
    detail: FamilyDetail | None, *, final_table_rendered: bool = True
) -> bool:
    """「逐项指标」这一节有没有值得写的东西？

    有：声明了指标的族 / 输出了指标的守卫（含 v4 的轨迹层判分器）/ 基线与
    终判**不同值**的那些行（那才是"谁判的"的证据）。
    没有：只剩一张与终判结果逐行同值的基线表——那是重复，不是明细：同一份
    事实写两遍，读者只会怀疑哪张才准（基线型套件的 dashboard 正是如此）。
    """
    if detail is None:
        return False
    if any(block.declared for block in detail.families):
        return True
    baseline_guards = detail.baseline_guards
    if baseline_guards is None:
        return False
    if any(guard.metrics for guard in baseline_guards.guards):
        return True
    return bool(
        baseline_rows_to_render(
            baseline_guards, final_table_rendered=final_table_rendered
        )
    )


def guard_metric_rows(
    guard: GuardMetricRow,
) -> tuple[tuple[str, int, int, int, int, str], ...]:
    """守卫逐指标表行：(指标, 判定 ok, 降级, 违规, 跳过, 原因/备注)。

    markdown 报告与 dashboard 共用这一张表——同一份判定事实在两处各写一份
    渲染逻辑，就是下一次漂移的来源。
    """
    declared = set(guard.declared)
    rows: list[tuple[str, int, int, int, int, str]] = []
    for item in guard.metrics:
        counts = item.counts
        reasons: list[str] = []
        for reason in counts.skipped_reasons:
            if reason not in reasons:
                reasons.append(reason)
        note = "；".join(reasons[:3]) if reasons else "—"
        if len(reasons) > 3:
            note += f"（另 {len(reasons) - 3} 项）"
        if declared and item.metric_id not in declared:
            note = "未在守卫声明里（运行期多出，核对声明）"
        elif (
            declared
            and counts.ok == 0
            and counts.degraded == 0
            and counts.violated == 0
            and counts.skipped == 0
        ):
            note = "本 run 未产出判定（声明项）"
        rows.append(
            (
                metric_display(item.metric_id),
                counts.ok,
                counts.degraded,
                counts.violated,
                counts.skipped,
                note,
            )
        )
    return tuple(rows)


@dataclass(frozen=True)
class FamilyRowScope:
    """族行的聚合范围：一组两两可比的 run。

    同键族只在**可比**的 run 之间合并；不可比 = 不同实验条件（不同
    套件/锁/agent/执行参数），它们的判定不叠加进同一个池——每个组
    一行，``label`` 写进行键与聚合说明。
    """

    run_ids: tuple[str, ...]
    label: str                  # suite_id@suite_version（组内可比 ⇒ 同一份声明）
    shared: bool                # 该族键被多个不可比组声明 → 行键带 label 后缀


@dataclass(frozen=True)
class AggregationReport:
    """多 run 聚合身份：参与 run、可比性分组与被拒绝合并的原因。

    报告头如实交代"哪些 run 进了同一份聚合、为什么有的没合并"——
    聚合口径不能只体现在行键上（读报告的人不看代码）。一份报告只在
    **同一套件谱系**内聚合（跨套件在 ``aggregate_run`` 就被拒绝），
    所以这里的"没合并"永远是**同套件不同条件**：换引擎/agent/参数/锁
    的 run 各自成行，``blocked`` 列出差异维度。
    """

    run_ids: tuple[str, ...]
    groups: tuple[tuple[str, ...], ...]     # 每组 run_id（组内两两可比）
    labels: tuple[str, ...]                 # 每组 suite_id@suite_version
    suite_id: str                           # 本报告的套件谱系（唯一）
    comparable: bool                        # 全部 run 同属一组
    blocked: tuple[str, ...]                # 同套件谱系内的不可比（差异维度）


@dataclass(frozen=True)
class FamilyRollup:
    """一个指标族（gradeCategory）的类别行（D7 口径）。

    分母（valid）= 族引擎实际出 pass/fail 判定的**有效**试次
    （requirement 排除的试次照旧出分母）；cannot_judge 出分母、
    计入 ``cannot_judge`` 并带原因。任务数 = 族**派生适用范围**的任务
    数（v5 Step 3：由指标输入的任务键导出，不是另写一份用例清单）——
    未承接的任务如实可见。

    多 run 聚合：一行 = **一个可比组**的判定叠加。跨不可比 run
    （不同套件/锁/agent/执行参数）的同键族各自成行，行键带套件
    后缀（``security@tbench-intel@0.2.0``）——不同实验条件的分数
    不池化（``compare_manifests`` 的契约）。
    """

    key: str
    display: str
    tasks: int                       # 派生适用任务数（同一可比组内，组间各自成行）
    task_ids: tuple[str, ...]        # 派生适用任务（已排序）
    trials: int                      # 承接该族判定的试次总数
    valid: int
    passes: int
    fails: int
    cannot_judge: int
    cannot_judge_reasons: tuple[str, ...]
    failing_tasks: tuple[str, ...]   # 引擎判过 fail 的任务 id（已排序）
    engines: tuple[str, ...]         # 本行各 run 的引擎 grader_id@version
    spec: dict                       # name/block/weight/threshold/redline
    family_key: str = ""             # 纯族键（行键跨组分行时带套件后缀）
    runs: tuple[str, ...] = ()       # 本行统计的 run_id（可比组内，有序）
    deciding_fails: int = 0          # 本族判 fail 的试次（⇒ 终判 fail，本层决定）
    foreign_fails: int = 0           # 本族判 pass 但终判 fail（失败由其他层决定）


@dataclass
class TaskGroupSummary:
    """Per-task roll-up — the skill/dimension cut of a run.

    Tasks are the finest unit a trial's coordinates always carry, so
    per-skill reporting groups by ``task_id``; a suite that models its
    intelligence dimensions as tasks gets its per-dimension table for
    free. Same denominators as the run: only valid trials count.
    """

    task_id: str
    total_trials: int = 0
    valid_trials: int = 0
    passes: int = 0
    fails: int = 0
    pass_pow_k_value: float | None = None


@dataclass(frozen=True)
class VerdictSource:
    """一个失败试次的终判归属：哪一层把它判失败的。

    报告要能回答"这个 fail 是谁判的"——否则读者只会看到族行与基线
    行数字相近，却看不出族阈值与守卫 veto 各决定了哪一个试次。
    """

    trial_id: str
    task_id: str
    trial_index: int
    verdict: str                     # fail（cannot_judge 归「排除明细」）
    deciders: tuple[str, ...]        # 决定性判分器（veto 优先，其次全部 fail 层）
    roles: tuple[str, ...]           # 对应角色：基线 / 族 …(key) / 守卫
    mechanism: str                   # "veto" | "阈值折叠" | "判定"
    note: str                        # 对照说明（如"基线判 pass，被本层否决"）


@dataclass
class RunSummary:
    run_ids: list[str]
    total_trials: int = 0
    valid_trials: int = 0
    passes: int = 0
    fails: int = 0
    pass_at_k: float | None = None
    pass_pow_k_value: float | None = None
    k: int | None = None
    cost_per_pass: float | None = None
    exclusion: ExclusionSummary | None = None
    exclusion_rate_flagged: bool = False
    comparability: ComparabilityReport | None = None
    verdict_counts: dict[str, int] = field(default_factory=dict)
    # 中文任务显示名：从各 run 的清单合并（task_id → 标题）。清单缺失
    # 或未声明标题时为空，报告回退到原 task_id——显示层增强，不改变
    # 任何坐标/判分语义。
    task_titles: dict[str, str] = field(default_factory=dict)
    # 各 run 的套件标识（id@version，来自清单）——报告头/面板头展示
    # 用。同样只是显示层。
    suite_labels: list[str] = field(default_factory=list)
    # 维度模型（清单封存的评分参数：阈值/权重/大块/红线任务）。空 =
    # 套件未声明 → 报告/面板不加「维度达标」层（向后兼容）。
    dimension_model: dict[str, object] = field(default_factory=dict)
    # 族声明密封映射（gradeCategory v4）：族键 →
    # {name/block/weight/threshold/redline, engine{impl, grader_id,
    # grader_version, layer, veto}, cases{task_id: {title, role}}}。
    # 空 = 无族 run（legacy）→ 族行/逐项指标层不渲染。多 run 聚合
    # 时同键族只在**可比**的 run 之间合并（P14 修订）：不可比 ⇒ 各自
    # 成行，行键带套件后缀（见 FamilyRowScope）。
    families: dict[str, dict] = field(default_factory=dict)
    # 行键 → 聚合范围（可比 run 组）；行内只统计本组 run 的试次。
    family_row_scopes: dict[str, FamilyRowScope] = field(default_factory=dict)
    # 聚合身份：参与 run + 可比性分组（报告头如实交代）。
    aggregation: AggregationReport | None = None
    # 族键 → 各 run 的引擎标签（grader_id@version，去重）——行来源
    # 标注用；与 families 一起在 aggregate_run 里装配。
    family_engines: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # task_id → 声明它的族键（按激活序）——「终判结果」的分组列与
    # 轨迹采集的类别列换源为密封声明映射（多值，不再前缀启发）。
    task_families: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # 族行（D7：族自身 GradeResult 判定聚合）。None = 无族 run。
    family_rollups: tuple[FamilyRollup, ...] | None = None
    # 失败试次的终判归属（哪一层判的 fail）：基线 / 族阈值 / 守卫 veto。
    verdict_sources: tuple[VerdictSource, ...] = ()
    # 逐项指标（族 → 任务 → 指标；基线与守卫明细）。
    metric_detail: FamilyDetail | None = None
    # 轨迹采集聚合（可选输入）：CLI 从密封轨迹逐试次采集后传入。
    # None = 未采集 → 报告不加「轨迹采集」节（向后兼容）。
    trajectory: TrajectoryAggregate | None = None
    # per-task roll-up: the skill/dimension cut. Only
    # meaningful when a run spans several tasks; empty otherwise.
    task_groups: dict[str, TaskGroupSummary] = field(default_factory=dict)


def _assert_single_suite(manifests: Sequence[RunManifest] | None) -> None:
    """一份报告只聚合同一套件谱系的 run；跨套件聚合直接拒绝。

    不同套件是**不同实验**（不同契约、不同任务集、不同 agent 形态），
    它们的分数不可加、行不可比——把 tbench 与 aeval 塞进一份报告只会
    制造一个没人该读的综合评分。要看多个套件，就读它们各自的报告；
    本函数拒绝的是"把它们合成一份"。同一套件谱系（``overlay.suite_id``）
    的多个 run（重复跑 / 多条件）仍然允许聚合。
    """
    seen: list[str] = []
    labels: dict[str, str] = {}
    for manifest in manifests or ():
        suite_id = _group_suite_id(manifest)
        if suite_id not in seen:
            seen.append(suite_id)
            labels[suite_id] = _group_label(manifest)
    if len(seen) > 1:
        listed = "、".join(labels[suite_id] for suite_id in seen)
        raise ValueError(
            "cross-suite aggregation is not supported: runs span "
            f"{len(seen)} suites ({listed}) — a report is scoped to one "
            "experiment (one suite_id). Aggregate re-runs of the same suite, "
            "or read each suite's report separately"
        )


def aggregate_run(
    run_ids: Sequence[str],
    trials: Iterable[TrialRecord],
    *,
    k: int | None = None,
    manifests: Sequence[RunManifest] | None = None,
    trajectory: Sequence[Any] = (),
    trajectory_unavailable: Sequence[tuple[str, str]] = (),
) -> RunSummary:
    _assert_single_suite(manifests)
    trials = list(trials)
    # v4 封存模型（categories/blocks/redline_tasks）读时升级为 v5 族模型：
    # 不改封存字节、不改判定，但旧 run 的报告不会因为模型换代少掉
    # 「维度达标 / 按类别结果」。升级后的 manifests 在此后全程使用。
    if manifests:
        titles_for_upgrade: dict[str, str] = {}
        for manifest in manifests:
            for task_id, title in (manifest.task_titles or {}).items():
                titles_for_upgrade.setdefault(task_id, title)
        task_ids_by_run: dict[str, list[str]] = {}
        for trial in trials:
            run_id = trial.coordinates.run_id
            task_id = trial.coordinates.task_id
            bucket = task_ids_by_run.setdefault(run_id, [])
            if task_id not in bucket:
                bucket.append(task_id)
        manifests = [
            _upgrade_legacy_model(
                manifest,
                task_ids_by_run.get(manifest.run_id, ()),
                titles_for_upgrade,
                _legacy_baseline_engine(trials, manifest.run_id),
            )
            for manifest in manifests
        ]
    valid = valid_trials(trials)
    passes = sum(1 for t in valid if t.verdict == "pass")
    fails = sum(1 for t in valid if t.verdict == "fail")
    summary = RunSummary(
        run_ids=list(run_ids),
        total_trials=len(trials),
        valid_trials=len(valid),
        passes=passes,
        fails=fails,
        k=k,
        cost_per_pass=cost_per_pass(trials),
    )
    counts: dict[str, int] = {}
    for t in trials:
        # None is not cannot_judge: an unclassified trial is its own
        # display class so reports cannot launder missing verdicts.
        verdict = t.verdict if t.verdict is not None else "unfinalized"
        counts[verdict] = counts.get(verdict, 0) + 1
    summary.verdict_counts = counts

    # Per-task roll-up: the skill/dimension cut of the
    # same numbers — same valid-trial denominator, same pass counting,
    # per-task pass^k when a k was declared.
    groups: dict[str, TaskGroupSummary] = {}
    for t in trials:
        group = groups.setdefault(
            t.coordinates.task_id, TaskGroupSummary(task_id=t.coordinates.task_id)
        )
        group.total_trials += 1
        if t in valid:
            group.valid_trials += 1
            if t.verdict == "pass":
                group.passes += 1
            elif t.verdict == "fail":
                group.fails += 1
    if k is not None:
        for group in groups.values():
            # pass^k is a combinatorial estimate over k-subsets; a task that
            # lost attempts to exclusions has fewer valid trials than k and
            # the number is NOT computable — omitted, never fabricated as 0.
            if group.valid_trials >= k:
                group.pass_pow_k_value = pass_pow_k(
                    group.passes, group.valid_trials, k
                )
    summary.task_groups = groups

    exclusion = exclusion_summary(trials)
    summary.exclusion = exclusion
    summary.exclusion_rate_flagged = (
        exclusion.total > 0 and exclusion.exclusion_rate > EXCLUSION_RATE_LIMIT
    )

    # 中文任务显示名 + 族声明：各 run 清单里封存的内容（见
    # RunManifest.task_titles / dimension_model）。多 run 聚合时同名任务
    # 以先到的 run 为准；这些都是显示层，不参与任何分母/分数计算。
    titles: dict[str, str] = {}
    suite_labels: list[str] = []
    model: dict[str, object] = {}
    for manifest in manifests or ():
        for task_id, title in (manifest.task_titles or {}).items():
            titles.setdefault(task_id, title)
        label = f"{manifest.overlay.suite_id}@{manifest.overlay.suite_version}"
        if label not in suite_labels:
            suite_labels.append(label)
        # 维度模型（每族一份的密封声明 + 大块表）：多 run 合并按
        # setdefault（先到的 run 为准）——评分参数不因聚合口径漂移。
        manifest_model = manifest.dimension_model or {}
        for key, spec in (manifest_model.get("families") or {}).items():
            model.setdefault("families", {}).setdefault(key, spec)
        for key, name in (manifest_model.get("blocks") or {}).items():
            model.setdefault("blocks", {}).setdefault(key, name)
    summary.task_titles = titles
    summary.suite_labels = suite_labels
    summary.dimension_model = model

    # 族声明（gradeCategory v4）：各 run 清单密封映射的合并——同键族
    # 只在**可比**的 run 组内合并，跨组各自成行（P14 修订）。族行 =
    # 族自身 GradeResult 判定聚合（D7）——与试次终判口径各归各节。
    run_groups = _family_groups(manifests)
    merged_families, family_engines, row_scopes, row_of = (
        _merge_family_declarations(manifests, run_groups)
    )
    summary.aggregation = _aggregation_report(manifests, run_groups)
    # 终判归属（谁把失败判出来的）——与族行同一份密封声明推出，独立于
    # 是否有族：即使是 legacy 套件，"基线判的失败"也照实归属。
    summary.verdict_sources = _verdict_sources(manifests, trials)
    # 逐项明细的入口条件从"有族"放宽到"有密封声明可讲"：v4 封存 run 没有
    # families，但 store 里仍有基线与轨迹层判分器的判定——那批指标不能因为
    # 声明形态换代就从报告里消失（rollup_families 内部对空族集合直接返回）。
    if manifests:
        engine_labels: dict[str, list[str]] = {}
        for key, engine_list in family_engines.items():
            for engine in engine_list:
                if not engine:
                    continue
                label = (
                    f"{engine.get('grader_id', '?')}@{engine.get('grader_version', '?')}"
                )
                if label not in engine_labels.setdefault(key, []):
                    engine_labels[key].append(label)
        summary.families = merged_families
        summary.family_row_scopes = row_scopes
        summary.family_engines = {
            key: tuple(labels) for key, labels in engine_labels.items()
        }
        rollups, detail_layer, task_families = rollup_families(
            summary, trials, manifests, row_of
        )
        summary.family_rollups = rollups
        summary.metric_detail = detail_layer
        summary.task_families = task_families

    # 轨迹采集聚合：仅当调用方采集了数据（或有不可用原因要如实呈现）
    # 时才生成该层；类别映射与「按类别结果」同一规则（族模式换源为
    # 密封声明映射，多值）。
    if trajectory or trajectory_unavailable:
        if summary.families:

            def _family_of(task_id: str) -> tuple[str, str]:
                """``(显示名, 族键)``——表格渲染 ``显示名(族键)``。

                v5 的任务可以同时属多个族（一次执行、多族丰富），所以
                两半都是 ``+`` 连接的多值：``会话智能度+安全
                (intelligence+security)``。只给显示名会让括号里重复一遍
                名字（``A2A 协议(A2A 协议)``），族键是人读得到的那把钥匙。
                """
                keys = summary.task_families.get(task_id) or ()
                if not keys:
                    return ("uncategorized", "uncategorized")
                display = "+".join(
                    summary.families.get(key, {}).get("name", key)
                    for key in keys
                )
                return (display, "+".join(keys))

            category_fn = _family_of
        else:
            # 没有族的套件（baseline-only）：不按 task_id 前缀猜族，
            # 全部落一个"未分族"桶——面板只呈现事实。
            category_fn = lambda task_id: ("未分族", "未分族")  # noqa: E731
        summary.trajectory = _aggregate_trajectory(
            list(trajectory),
            list(trajectory_unavailable),
            len(trials),
            category_fn,
        )

    if k is not None and len(valid) >= k:
        summary.pass_pow_k_value = pass_pow_k(passes, len(valid), k)
        # pass@k (at least one of k passes) = 1 - C(n-p, k)/C(n, k).
        import math

        if len(valid) - passes >= k:
            summary.pass_at_k = (
                1.0
                - math.comb(len(valid) - passes, k) / math.comb(len(valid), k)
            )
        else:
            summary.pass_at_k = 1.0

    if manifests and len(manifests) >= 2:
        # 逐个 run 两两判定：只看前两个会让第三个不可比 run 蒙混过关
        # （分数不该跨条件平均）。
        summary.comparability = compare_all_manifests(manifests)
    return summary


# --- 中文渲染的显示名（契约枚举保留原名对照，可追溯性不丢） ---------------
# verdict / 排除类是跨系统契约词汇（存储、判分、断言都用原值），报告是
# 人读的：中文显示名 + 括号内原值。未知键原样显示，不静默吞掉。

_VERDICT_ZH = {
    "pass": "通过",
    "fail": "失败",
    "cannot_judge": "无法判定",
    "infra_invalid": "基础设施无效",
    "unfinalized": "未终裁",
}

_EXCLUSION_ZH = {
    "cannot_judge": "无法判定",
    "infra_invalid": "基础设施无效",
}


def _zh_counts(counts: dict[str, int], mapping: dict[str, str]) -> str:
    if not counts:
        return "无"
    return "、".join(
        f"{mapping.get(key, key)}({key}) {number}"
        for key, number in sorted(counts.items())
    )


@dataclass(frozen=True)
class CategoryTrajectory:
    """一个类别的轨迹采集聚合（「轨迹采集」表的行）。"""

    key: str
    display: str
    trials: int
    total_tokens: int | None
    mean_wall_seconds: float | None
    tool_calls: int
    missing_obs: int


@dataclass(frozen=True)
class TrajectoryAggregate:
    """run 级轨迹采集聚合——只汇总 TrajectoryStats 里的事实。

    ``unavailable`` 是不可用试次的原因（带计数）；覆盖不满时报告如
    实展示，不假装完整。
    """

    trials_total: int
    trials_with_evidence: int
    unavailable: tuple[str, ...]
    total_tokens: int | None
    total_prompt_tokens: int | None
    total_completion_tokens: int | None
    total_cached_tokens: int | None
    mean_wall_seconds: float | None
    longest_wall: tuple[str, float] | None      # (trial_id, 秒)
    tool_calls: int
    tool_missing_obs: int
    tool_retries: int
    peak_input_tokens: int | None
    peak_trial_id: str | None
    peak_step_id: int | None
    longest_call: tuple[str, float, str] | None  # (工具, 秒, trial_id)
    categories: tuple[CategoryTrajectory, ...]


def _aggregate_trajectory(
    stats: Sequence[Any],
    unavailable: Sequence[tuple[str, str]],
    trials_total: int,
    category_of,
) -> TrajectoryAggregate:
    reasons: dict[str, int] = {}
    for _trial_id, reason in unavailable:
        reasons[reason] = reasons.get(reason, 0) + 1
    unavailable_text = tuple(
        f"{reason} ×{count}" for reason, count in sorted(reasons.items())
    )

    def _sum(attr: str) -> int | None:
        values = [getattr(s, attr) for s in stats]
        values = [v for v in values if v is not None]
        return sum(values) if values else None

    walls = [
        (s.trial_id, s.wall_clock_seconds)
        for s in stats
        if s.wall_clock_seconds is not None
    ]
    mean_wall = (
        sum(w for _, w in walls) / len(walls) if walls else None
    )
    longest_wall = max(walls, key=lambda t: t[1]) if walls else None

    peaks = [
        (s.peak_input_tokens, s.trial_id, s.peak_step_id)
        for s in stats
        if s.peak_input_tokens is not None
    ]
    peak = max(peaks, key=lambda t: t[0]) if peaks else None

    calls = [
        (tool, seconds, s.trial_id)
        for s in stats
        if s.longest_call is not None
        for tool, seconds in [s.longest_call]
    ]
    longest_call = max(calls, key=lambda t: t[1]) if calls else None

    by_cat: dict[tuple[str, str], list[Any]] = {}
    for s in stats:
        by_cat.setdefault(category_of(s.task_id), []).append(s)
    categories = tuple(
        CategoryTrajectory(
            key=key,
            display=display,
            trials=len(items),
            total_tokens=(
                sum(t for t in (s.total_tokens for s in items) if t is not None)
                if any(s.total_tokens is not None for s in items)
                else None
            ),
            mean_wall_seconds=(
                sum(s.wall_clock_seconds for s in items
                    if s.wall_clock_seconds is not None)
                / len([s for s in items if s.wall_clock_seconds is not None])
                if any(s.wall_clock_seconds is not None for s in items)
                else None
            ),
            tool_calls=sum(s.tool_calls_total for s in items),
            missing_obs=sum(s.tool_missing_obs_total for s in items),
        )
        for (display, key), items in sorted(by_cat.items())
    )

    prompt = _sum("total_prompt_tokens")
    completion = _sum("total_completion_tokens")
    return TrajectoryAggregate(
        trials_total=trials_total,
        trials_with_evidence=len(stats),
        unavailable=unavailable_text,
        total_tokens=(
            prompt + completion
            if prompt is not None and completion is not None
            else None
        ),
        total_prompt_tokens=prompt,
        total_completion_tokens=completion,
        total_cached_tokens=_sum("total_cached_tokens"),
        mean_wall_seconds=mean_wall,
        longest_wall=longest_wall,
        tool_calls=sum(s.tool_calls_total for s in stats),
        tool_missing_obs=sum(s.tool_missing_obs_total for s in stats),
        tool_retries=sum(s.tool_retries_total for s in stats),
        peak_input_tokens=peak[0] if peak else None,
        peak_trial_id=peak[1] if peak else None,
        peak_step_id=peak[2] if peak else None,
        longest_call=longest_call,
        categories=categories,
    )


def family_pass_pow_k(rollup: FamilyRollup, k: int | None) -> float | None:
    """族级 pass^k：同一公式换输入（族引擎判定的通过/有效）。"""
    if k is None or rollup.valid < k:
        return None
    return pass_pow_k(rollup.passes, rollup.valid, k)


_FAMILY_SPEC_KEYS = ("name", "block", "weight", "threshold", "redline")


def sealed_family_tasks(spec: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """一个密封族声明的适用范围（``{task_id: {title?, role?}}``）。

    ``scope`` 只放显示元数据，适用范围由 ``metrics[].inputs`` 的任务键
    派生（结构判定族没有输入 ⇒ 退回 ``scope`` 的显式列表）。
    """
    scope = spec.get("scope") or {}
    derived: list[str] = []
    for metric in spec.get("metrics") or ():
        if not isinstance(metric, Mapping):
            continue
        for task_id in (metric.get("inputs") or {}):
            if task_id not in derived:
                derived.append(task_id)
    if not derived:
        derived = list(scope)
    return {
        task_id: dict(scope.get(task_id) or {}) for task_id in derived
    }


def _group_label(manifest: RunManifest) -> str:
    """一个 run 的聚合标签：套件 id@version（缺失时退回 run_id）。"""
    overlay = getattr(manifest, "overlay", None)
    suite_id = getattr(overlay, "suite_id", None)
    if suite_id:
        return f"{suite_id}@{overlay.suite_version}"
    return manifest.run_id


def _group_suite_id(manifest: RunManifest) -> str:
    """一个 run 的套件谱系（overlay.suite_id；缺失时退回 run_id）。"""
    overlay = getattr(manifest, "overlay", None)
    return getattr(overlay, "suite_id", None) or manifest.run_id


def _family_groups(
    manifests: Sequence[RunManifest] | None,
) -> list[list[RunManifest]]:
    """按可比性给 run 分组：组内两两可比，跨组不可比。

    可比性 = ``compare_manifests`` 的全部锁定维度（runtime_lock /
    agents / images / python_env / harbor / dsh / overlay /
    families / adapters / versions / budget / plugin / argv /
    config）。不同套件、不同 agent、不同锁、不同执行参数的 run 属于
    不同实验条件——"scores from incomparable runs must never be
    averaged or trended together"（``compare_manifests`` 契约），所以
    它们的族判定不池化：同键族各自成行。

    贪心成组：与组的首个成员可比即入组（可比性含声明逐字节相等，
    因此组内成员的族声明天然一致）。
    """
    groups: list[list[RunManifest]] = []
    for manifest in manifests or ():
        for group in groups:
            if compare_manifests(group[0], manifest).comparable:
                group.append(manifest)
                break
        else:
            groups.append([manifest])
    return groups


def _merge_family_declarations(
    manifests: Sequence[RunManifest] | None,
    groups: list[list[RunManifest]] | None = None,
) -> tuple[
    dict[str, dict],
    dict[str, list[dict]],
    dict[str, FamilyRowScope],
    dict[tuple[str, str], str],
]:
    """合并各 run 清单封存的族声明（多 run 聚合）。

    - 只在**同一可比组**内合并同键族（``_family_groups``）；不可比 ⇒
      各自成行，行键带套件后缀（``security@tbench-intel@0.2.0``），
      两个条件的数字互不污染；
    - 组内可比 ⇒ ``families`` 整体逐字节相等，规格与适用范围
      必然一致；仍逐项兜底校验，不一致 = 可比性定义被绕过，直接
      报错（内部矛盾，不静默取先到）；
    - 引擎差异同样落在可比性里：``families`` 含 engine 身份，
      引擎不同的 run 不可比 ⇒ 分行标注（各行的 engines 各自单一）。

    返回 ``(合并声明, 行键 → 各 run 引擎, 行键 → 聚合范围,
    (run_id, 族键) → 行键)``。
    """
    groups = groups if groups is not None else _family_groups(manifests)

    # 族键被哪些组声明 → 决定行键是否需要分组后缀（唯一化）。组标签
    # 可能重复（同一套件的两个不可比条件：换引擎/换 agent/换参数），
    # 重复时补组序号——行键必须唯一，否则两行会互相覆盖。
    declared_in: dict[str, list[int]] = {}
    for gidx, group in enumerate(groups):
        for manifest in group:
            declared = (manifest.dimension_model or {}).get("families") or {}
            for key in declared:
                buckets = declared_in.setdefault(key, [])
                if gidx not in buckets:
                    buckets.append(gidx)
    group_labels = [_group_label(group[0]) for group in groups]
    label_counts = Counter(group_labels)
    group_suffix = [
        label if label_counts[label] == 1 else f"{label}#{gidx + 1}"
        for gidx, label in enumerate(group_labels)
    ]

    merged: dict[str, dict] = {}
    pristine: dict[str, dict] = {}
    engines: dict[str, list[dict]] = {}
    scopes: dict[str, FamilyRowScope] = {}
    row_of: dict[tuple[str, str], str] = {}

    for gidx, group in enumerate(groups):
        label = group_labels[gidx]
        suffix = group_suffix[gidx]
        group_runs = tuple(manifest.run_id for manifest in group)
        for manifest in group:
            declared = (manifest.dimension_model or {}).get("families") or {}
            for key, spec in declared.items():
                shared = len(declared_in.get(key, ())) > 1
                row_key = f"{key}@{suffix}" if shared else key
                owner = scopes.get(row_key)
                if owner is not None and owner.run_ids != group_runs:
                    # 字面撞车（族键里正好带套件标签）：退到行键 + 组序号，
                    # 绝不静默覆盖另一组的行。
                    row_key = f"{row_key}#{gidx + 1}"
                row_of[(manifest.run_id, key)] = row_key
                engines.setdefault(row_key, []).append(spec.get("engine") or {})
                scopes.setdefault(
                    row_key,
                    FamilyRowScope(run_ids=group_runs, label=label, shared=shared),
                )
                if row_key not in merged:
                    entry = dict(spec)
                    entry["family_key"] = key
                    # 行聚合标签（多 run 分组的展示名）——与族声明里的
                    # ``scope``（适用范围显示元数据）不是一回事，键名
                    # 不许撞车：声明原样留在 entry 里。
                    entry["aggregation_label"] = label if shared else ""
                    merged[row_key] = entry
                    pristine[row_key] = spec
                    continue
                diff = [
                    field
                    for field in _FAMILY_SPEC_KEYS
                    if pristine[row_key].get(field) != spec.get(field)
                ]
                if not diff and pristine[row_key].get("metrics") != spec.get(
                    "metrics"
                ):
                    diff = ["metrics"]
                elif not diff and sealed_family_tasks(
                    pristine[row_key]
                ) != sealed_family_tasks(spec):
                    diff = ["scope"]
                if diff:
                    raise ValueError(
                        f"grade category {key!r} cannot be aggregated inside a "
                        f"comparable run group: spec differs in {diff} "
                        f"({manifest.run_id} vs {group[0].run_id}) — comparable "
                        "runs must agree byte for byte"
                    )
    return merged, engines, scopes, row_of


def _aggregation_report(
    manifests: Sequence[RunManifest] | None,
    groups: list[list[RunManifest]],
) -> AggregationReport | None:
    """聚合身份：参与 run、分组、同套件内未合并的原因（报告头用）。

    跨套件的组合到不了这里（``_assert_single_suite`` 已拒绝），所以
    ``blocked`` 只可能来自同套件谱系内的条件差异——那才是读者可能误当
    同批次、需要诊断的场景。
    """
    if not manifests:
        return None
    labels = tuple(_group_label(group[0]) for group in groups)
    blocked: list[str] = []
    for gidx, group in enumerate(groups[1:], start=1):
        diff = compare_manifests(groups[0][0], group[0])
        dims = "、".join(sorted(key for key, msgs in diff.items() if msgs)) or "?"
        blocked.append(
            f"{labels[gidx]}：{'、'.join(m.run_id for m in group)}"
            f" ≠ {'、'.join(m.run_id for m in groups[0])}：{dims}"
        )
    return AggregationReport(
        run_ids=tuple(manifest.run_id for manifest in manifests),
        groups=tuple(tuple(m.run_id for m in group) for group in groups),
        labels=labels,
        suite_id=_group_suite_id(groups[0][0]),
        comparable=len(groups) <= 1,
        blocked=tuple(blocked),
    )


def _run_family_engine_ids(
    manifests: Sequence[RunManifest] | None,
    row_of: dict[tuple[str, str], str] | None = None,
) -> dict[str, dict[str, str]]:
    """行键 → {run_id → 引擎 grader_id}（报告侧 join，P4）。

    join 键来自清单密封映射——报告永不 import 判分器模块。清单缺
    grader_id（老清单）时该 run 对该行无 join，行里如实少计。行键是
    族键在可比组内的唯一化结果（``row_of``）；不给时行键 = 族键。
    """
    joins: dict[str, dict[str, str]] = {}
    for manifest in manifests or ():
        declared = (manifest.dimension_model or {}).get("families") or {}
        for key, spec in declared.items():
            grader_id = (spec.get("engine") or {}).get("grader_id")
            if not grader_id:
                continue
            row_key = (row_of or {}).get((manifest.run_id, key), key)
            joins.setdefault(row_key, {})[manifest.run_id] = grader_id
    return joins


def _category_of(task_id: str, default: str | None) -> str:
    """v4 归组规则（读时升级用）：task_id 首个点前的前缀；无点归 default。"""
    if "." in task_id:
        return task_id.split(".", 1)[0]
    return default or "uncategorized"


def _upgrade_legacy_model(
    manifest: RunManifest,
    task_ids: Sequence[str],
    titles: Mapping[str, str],
    engine: Mapping[str, Any] | None = None,
) -> RunManifest:
    """v4 封存模型 → v5 族模型的**读时升级**（不改封存字节、不改判定）。

    v4 run（如 tbench-pilot@0.4.0）的 ``dimension_model`` 只有
    ``categories/blocks/redline_tasks``，没有族声明——v5 渲染器会让
    「维度达标 / 按类别结果」整层消失。这里把每个类别合成一个**归组族**
    （``metrics: []`` ⇒ 行口径 = 试次终判，与 v4「按类别结果」逐位相同），
    ``scope`` = ``_category_of`` 归到该类别的任务（含封存标题）。

    升级后的模型带 ``legacy_upgraded`` 标记：明细层据此知道族/守卫归属
    不是当年封存的声明——轨迹层判分器按判分层回退补出（见
    ``_run_baseline_guard_ids``），措辞也不代当时的声明断言。
    """
    dm = manifest.dimension_model or {}
    categories = dm.get("categories") or {}
    if (dm.get("families") or {}) or not categories:
        return manifest
    scope_by_category: dict[str, dict[str, dict]] = {key: {} for key in categories}
    for task_id in task_ids:
        key = _category_of(task_id, dm.get("default"))
        entry: dict[str, object] = {}
        if task_id in titles:
            entry["title"] = titles[task_id]
        scope_by_category.setdefault(key, {})[task_id] = entry
    families: dict[str, dict] = {}
    for key, scope in scope_by_category.items():
        raw = categories.get(key) or {}
        spec = {"name": raw} if isinstance(raw, str) else dict(raw)
        families[key] = {
            "name": spec.get("name") or ("未分类" if key == "uncategorized" else key),
            "block": spec.get("block", "other"),
            "weight": spec.get("weight", 1.0),
            "threshold": spec.get("threshold", 0.9),
            "redline": bool(spec.get("redline", False)),
            "metrics": [],
            "scope": scope,
            # D7 分组式类别：引擎 = 官方基线（v4 的行口径就是试次终判，
            # 归组族要按基线 grade 出数才能逐位对上）。
            **({"engine": dict(engine)} if engine else {}),
        }
    upgraded = dict(dm)
    upgraded["families"] = families
    upgraded["legacy_upgraded"] = True
    return manifest.model_copy(update={"dimension_model": upgraded})


def _legacy_baseline_engine(
    trials: Sequence[TrialRecord], run_id: str
) -> dict[str, str] | None:
    """v4 回退：从 grades 里认出官方基线（唯一的 outcome 层判分器）。

    认不出（0 个或多个）就不猜——归组族不挂引擎，行里试次如实计数。
    """
    by_layer = _legacy_layer_graders(trials, run_id)
    outcome = sorted(by_layer.get("outcome", ()))
    if len(outcome) != 1:
        return None
    grader_id = outcome[0]
    version = ""
    for trial in trials:
        if trial.coordinates.run_id != run_id:
            continue
        for grade in getattr(trial, "grades", ()) or ():
            if grade.grader_id == grader_id:
                version = grade.grader_version
                break
        if version:
            break
    return {"grader_id": grader_id, "grader_version": version}


def _legacy_layer_graders(
    trials: Sequence[TrialRecord], run_id: str
) -> dict[str, set[str]]:
    """一个 run 的判分器按**判分层**分组（v4 回退用；层来自密封 grade）。"""
    by_layer: dict[str, set[str]] = {}
    for trial in trials:
        if trial.coordinates.run_id != run_id:
            continue
        for grade in getattr(trial, "grades", ()) or ():
            layer = getattr(grade, "layer", None) or "?"
            by_layer.setdefault(layer, set()).add(grade.grader_id)
    return by_layer


def _run_baseline_guard_ids(
    manifests: Sequence[RunManifest] | None,
    trials: Sequence[TrialRecord] = (),
) -> tuple[dict[str, str], dict[str, list[dict]], set[str]]:
    """run_id → 基线 grader_id、守卫声明列表与 v4 封存 run 集合。

    v5 模型直接在密封声明里读（``baseline.grader_id`` / ``guards[]``）。
    v4 封存模型（``dimension_model`` 只有 categories/blocks/redline_tasks）
    **没有**可读的族/守卫归属：那时轨迹层判分器判了什么在报告里一概消失。
    这类 run 回退到 store 里实际产出的 grades，按**判分层**列出判分器
    （outcome → 官方基线；trajectory → 轨迹层判分器），并把 run_id 记进
    第三个返回值——渲染层据此改用"不代当时的声明断言归属"的措辞。
    """
    baselines: dict[str, str] = {}
    guards: dict[str, list[dict]] = {}
    legacy_runs: set[str] = set()
    for manifest in manifests or ():
        dm = manifest.dimension_model or {}
        baseline = dm.get("baseline") or {}
        declared = list(dm.get("guards") or [])
        if baseline.get("grader_id"):
            baselines[manifest.run_id] = baseline["grader_id"]
        guards[manifest.run_id] = declared
        if declared or baseline.get("grader_id") or not dm.get("legacy_upgraded"):
            continue
        by_layer = _legacy_layer_graders(trials, manifest.run_id)
        outcome = sorted(by_layer.get("outcome", ()))
        if len(outcome) == 1:
            # 一个 outcome 层判分器 = 官方基线（v4 也只有一个基线槽）。
            baselines[manifest.run_id] = outcome[0]
        trajectory = sorted(by_layer.get("trajectory", ()))
        guards[manifest.run_id] = [
            {"grader_id": grader_id, "legacy": True} for grader_id in trajectory
        ]
        if trajectory or baselines.get(manifest.run_id):
            legacy_runs.add(manifest.run_id)
    return baselines, guards, legacy_runs


def _grader_roles(
    manifests: Sequence[RunManifest] | None,
    trials: Sequence[TrialRecord] = (),
) -> tuple[dict[str, str], dict[str, list[tuple[str, tuple[str, ...]]]]]:
    """判分器身份：grader_id → 非族角色 / grader_id → 声明它的族列表。

    一个族引擎可以被多个族共用（aeval-intel 的智能度/红线/记忆三族共用
    ``aeval-intel@v4``）。角色不能按 grader_id 一对一定死——归属要按
    "该族的适用范围是否覆盖这个试次的 task" 再判一次，否则红线族判的失败
    会被写成别的族。
    """
    plain: dict[str, str] = {}
    families: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for manifest in manifests or ():
        dm = manifest.dimension_model or {}
        baseline = (dm.get("baseline") or {}).get("grader_id")
        if baseline:
            plain[baseline] = "官方基线"
        for guard in dm.get("guards") or []:
            grader_id = guard.get("grader_id")
            if grader_id:
                plain[grader_id] = "守卫"
        if dm.get("legacy_upgraded"):
            # v4 封存 run：归属声明不存在，按判分层给角色标签（不冒充守卫）。
            by_layer = _legacy_layer_graders(trials, manifest.run_id)
            outcome = sorted(by_layer.get("outcome", ()))
            if len(outcome) == 1:
                plain.setdefault(outcome[0], "官方基线")
            for grader_id in sorted(by_layer.get("trajectory", ())):
                plain.setdefault(grader_id, "轨迹层判分器")
        for key, spec in (dm.get("families") or {}).items():
            grader_id = (spec.get("engine") or {}).get("grader_id")
            if not grader_id:
                continue
            entry = (
                key,
                spec.get("name") or key,
                tuple(sealed_family_tasks(spec)),
            )
            bucket = families.setdefault(grader_id, [])
            if entry not in bucket:
                bucket.append(entry)
    return plain, families


def _role_of(
    grader_id: str,
    task_id: str,
    plain: dict[str, str],
    families: dict[str, list[tuple[str, tuple[str, ...]]]],
) -> str:
    """一个判分器对这个试次的角色标签（族按派生适用范围命中，可多族）。"""
    labels = [
        f"族 {name}({key})"
        for key, name, cases in families.get(grader_id, ())
        if task_id in cases
    ]
    if labels:
        return "、".join(labels)
    if grader_id in families:
        # 派生范围没命中（老清单/未声明该 task）：只报族键，不张冠李戴。
        return "族 " + "、".join(
            f"{name}({key})" for key, name, _cases in families[grader_id]
        )
    return plain.get(grader_id, "判分器")


def _verdict_sources(
    manifests: Sequence[RunManifest] | None,
    trials: Sequence[TrialRecord],
) -> tuple[VerdictSource, ...]:
    """失败试次的终判归属：谁把它判失败的（veto 优先，其次折叠的 fail）。

    ``decide_final_verdict`` 的折叠序是：任一 veto fail → fail；否则任一
    fail → fail。报告把这个归属印出来——否则读者看到族行数字与基线相近，
    会以为族只是基线的复述（实际上族阈值与守卫 veto 各自决定了别的试次）。
    """
    plain_roles, family_roles = _grader_roles(manifests, trials)
    sources: list[VerdictSource] = []
    for trial in trials:
        if trial.verdict != "fail":
            continue
        grades = list(getattr(trial, "grades", ()) or ())
        failed = [g for g in grades if g.status == "fail"]
        if not failed:
            continue
        vetoes = [g for g in failed if getattr(g, "veto", False)]
        deciders = vetoes or failed
        task_id = trial.coordinates.task_id
        roles_of = tuple(
            _role_of(g.grader_id, task_id, plain_roles, family_roles)
            for g in deciders
        )
        mechanism = "veto" if vetoes else (
            "阈值折叠" if any(r.startswith("族 ") for r in roles_of) else "判定"
        )
        labels = tuple(
            f"{g.grader_id}@{g.grader_version}" for g in deciders
        )
        passing = [
            f"{g.grader_id}@{g.grader_version}"
            for g in grades
            if g.status == "pass"
        ]
        if mechanism == "veto":
            note = (
                f"否决了 {'、'.join(passing)} 的 pass" if passing else "veto"
            )
        elif passing:
            note = f"{'、'.join(passing)} 判 pass，本层折叠为 fail"
        else:
            note = ""
        sources.append(
            VerdictSource(
                trial_id=trial.trial_id,
                task_id=trial.coordinates.task_id,
                trial_index=trial.coordinates.trial_index,
                verdict=trial.verdict,
                deciders=labels,
                roles=roles_of,
                mechanism=mechanism,
                note=note,
            )
        )
    # 按 (task_id, trial_index) 排序：失败清单读起来和「终判结果」同序，
    # 而不是随 store 落库顺序跳动。
    return tuple(
        sorted(sources, key=lambda s: (s.task_id, s.trial_index, s.trial_id))
    )


def _metric_counts_update(
    counts: dict[str, dict], metric: Any, reasons: dict[str, int]
) -> None:
    status = getattr(metric, "status", None)
    if status not in ("ok", "degraded", "violated", "skipped"):
        return
    bucket = counts.setdefault(status, {"count": 0, "reasons": []})
    bucket["count"] += 1
    if status == "skipped":
        raw = getattr(metric, "reasons", None) or []
        reason = "；".join(r.strip() for r in raw if r and r.strip())
        if not reason:
            reason = "(no reason stated)"
        reasons[reason] = reasons.get(reason, 0) + 1


def _counts_of(statuses: dict[str, dict], reasons: dict[str, int]) -> MetricStatusCounts:
    skipped_reasons = tuple(
        f"{reason} ×{count}"
        for reason, count in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return MetricStatusCounts(
        ok=statuses.get("ok", {}).get("count", 0),
        degraded=statuses.get("degraded", {}).get("count", 0),
        violated=statuses.get("violated", {}).get("count", 0),
        skipped=statuses.get("skipped", {}).get("count", 0),
        skipped_reasons=skipped_reasons,
    )



def _declared_metric_ids(spec: dict) -> tuple[str, ...]:
    """族声明里的指标清单（判分顺序）。兼容裸 id 与映射两种密封形状。"""
    ids: list[str] = []
    for entry in spec.get("metrics") or ():
        if isinstance(entry, str):
            ids.append(entry)
        elif isinstance(entry, dict) and entry.get("id"):
            ids.append(str(entry["id"]))
    return tuple(ids)

def rollup_families(
    summary: RunSummary,
    trials: Sequence[TrialRecord],
    manifests: Sequence[RunManifest] | None,
    row_of: dict[tuple[str, str], str] | None = None,
) -> tuple[tuple[FamilyRollup, ...], FamilyDetail | None, dict[str, tuple[str, ...]]]:
    """族行（D7）+ 逐项指标 + task→族 映射。

    分母口径：族引擎实际出 pass/fail 判定的**有效**试次；引擎
    cannot_judge 出分母、计入排除并聚合原因；requirement 排除的
    试次照旧出分母（与试次终判节一致，D7）。同一 task 可被多个族
    测量（丰富模型）——每个族独立出数，试次不翻倍。

    多 run：每行只统计**本行聚合范围**（``family_row_scopes``，一个
    可比组）内的 run 试次——跨不可比 run 的同键族各自成行，试次不
    跨条件泄漏。
    """
    baseline_ids, guard_decls, legacy_runs = _run_baseline_guard_ids(manifests, trials)
    if (
        not summary.families
        and not any(baseline_ids.values())
        and not any(guard_decls.values())
    ):
        # 既没有族声明，也没有基线/轨迹层判分器：没有逐项明细可讲。
        return None, None, {}
    valid = valid_trials(trials)
    engine_joins = _run_family_engine_ids(manifests, row_of)

    # task → 族键（激活序，多 run 先到先占位）
    task_families: dict[str, list[str]] = {}
    for key, spec in summary.families.items():
        for task_id in sealed_family_tasks(spec):
            if key not in task_families.setdefault(task_id, []):
                task_families[task_id].append(key)

    rollups: list[FamilyRollup] = []
    family_blocks: list[FamilyMetricBlock] = []
    engine_labels_by_key = summary.family_engines or {}

    for key, spec in summary.families.items():
        cases = sealed_family_tasks(spec)
        # v4 D7 特例（v5 照旧）：归组族（declared metrics 为空）不是判定
        # 单元，它只把任务归到一起——行数字与**试次终判**口径逐位相同，
        # 不拿"本族引擎自己的判"当行口径（那会让守卫/他层翻掉的 fail
        # 在行里变成 pass，进而抬高综合评分）。
        grouping = not _declared_metric_ids(spec)
        engine_ids = engine_joins  # 行键 → {run_id → grader_id}
        scope = summary.family_row_scopes.get(key)
        allowed_runs = set(scope.run_ids) if scope else None
        trials_total = 0
        valid_n = passes = fails = cj = 0
        deciding_fails = foreign_fails = 0
        cj_reasons: dict[str, int] = {}
        failing: set[str] = set()
        # detail: task → metric_id → statuses
        detail: dict[str, dict[str, dict[str, dict]]] = {}
        detail_reasons: dict[str, dict[str, dict[str, int]]] = {}
        case_trials: dict[str, int] = {}
        for trial in trials:
            task_id = trial.coordinates.task_id
            if task_id not in cases:
                continue
            run_id = trial.coordinates.run_id
            if allowed_runs is not None and run_id not in allowed_runs:
                # 该 run 属于另一个不可比组：本行是另一份实验条件，
                # 它的试次不进这个池（数字不跨条件平均）。
                continue
            grader_id = engine_ids.get(key, {}).get(run_id)
            if grader_id is None:
                # 该 run 未封存此族的引擎身份（老清单/未声明）——行里
                # 试次如实计数，判定不猜。
                trials_total += 1
                continue
            result = next(
                (g for g in trial.grades if g.grader_id == grader_id), None
            )
            trials_total += 1
            if result is None:
                continue
            if grouping:
                case_trials[task_id] = case_trials.get(task_id, 0) + 1
                if trial not in valid:
                    # 出分母的试次照旧进本行（行口径 = 试次口径）。
                    for reason in _trial_excluded_reasons(trial):
                        # 与运行级「排除明细」同一套标签，读起来是一件事。
                        label = _EXCLUSION_ZH.get(reason, reason)
                        cj_reasons[label] = cj_reasons.get(label, 0) + 1
                    cj += 1
                    continue
                if trial.verdict == "pass":
                    valid_n += 1
                    passes += 1
                    continue
                valid_n += 1
                fails += 1
                failing.add(task_id)
                if result.status == "fail":
                    deciding_fails += 1
                else:
                    foreign_fails += 1
                continue
            if trial not in valid:
                continue
            case_trials[task_id] = case_trials.get(task_id, 0) + 1
            if result.status == "pass":
                valid_n += 1
                passes += 1
                if trial.verdict == "fail":
                    # 本族判 pass，终判却是 fail：失败是别的层判的。这一
                    # 计数让族行不再被读成"终判的复述"。
                    foreign_fails += 1
            elif result.status == "fail":
                valid_n += 1
                fails += 1
                deciding_fails += 1
                failing.add(task_id)
            else:
                reason = "；".join(result.reasons[:2]) or "(no reason)"
                cj_reasons[reason] = cj_reasons.get(reason, 0) + 1
                cj += 1
            for metric in result.metrics or []:
                mid = getattr(metric, "name", None) or "?"
                statuses = (
                    detail.setdefault(task_id, {})
                    .setdefault(mid, {})
                )
                reasons_map = (
                    detail_reasons.setdefault(task_id, {}).setdefault(mid, {})
                )
                _metric_counts_update(statuses, metric, reasons_map)

        rollups.append(
            FamilyRollup(
                key=key,
                display=spec.get("name") or key,
                tasks=len(cases),
                task_ids=tuple(sorted(cases)),
                trials=trials_total,
                valid=valid_n,
                passes=passes,
                fails=fails,
                cannot_judge=cj,
                cannot_judge_reasons=tuple(
                    f"{reason} ×{count}"
                    for reason, count in sorted(
                        cj_reasons.items(), key=lambda kv: (-kv[1], kv[0])
                    )
                ),
                failing_tasks=tuple(sorted(failing)),
                engines=tuple(engine_labels_by_key.get(key, ())),
                spec={field: spec.get(field) for field in _FAMILY_SPEC_KEYS},
                family_key=spec.get("family_key") or key,
                runs=scope.run_ids if scope else (),
                deciding_fails=deciding_fails,
                foreign_fails=foreign_fails,
            )
        )

        case_rows: list[TaskMetricRow] = []
        for task_id in sorted(cases):
            case = cases.get(task_id) or {}
            metric_items = []
            task_detail = detail.get(task_id, {})
            for mid in sorted(task_detail):
                metric_items.append(
                    TaskMetricItem(
                        metric_id=mid,
                        counts=_counts_of(task_detail[mid], detail_reasons.get(task_id, {}).get(mid, {})),
                    )
                )
            case_rows.append(
                TaskMetricRow(
                    task_id=task_id,
                    title=case.get("title") or summary.task_titles.get(task_id),
                    role=case.get("role") or "home",
                    trials=case_trials.get(task_id, 0),
                    metrics=tuple(metric_items),
                )
            )
        family_blocks.append(
            FamilyMetricBlock(
                key=key,
                display=spec.get("name") or key,
                engine_labels=tuple(engine_labels_by_key.get(key, ())),
                tasks=tuple(case_rows),
                declared=_declared_metric_ids(spec),
            )
        )

    # 基线与守卫明细（无类别行——有效性门，不是族）。
    baseline_detail: BaselineGuardDetail | None = None
    baseline_labels: list[str] = []
    baseline_tasks: dict[str, dict[str, int]] = {}
    guard_rows: dict[str, dict[str, dict[str, dict]]] = {}
    guard_reasons: dict[str, dict[str, dict[str, int]]] = {}
    guard_trials: dict[str, int] = {}
    guard_label_of: dict[str, str] = {}
    # v5 A：守卫声明的 trace 指标清单（judging order）——行按它出，
    # 声明了却没产出判定的项也照实出行。
    guard_declared: dict[str, tuple[str, ...]] = {}
    legacy_guard_ids: set[str] = set()
    for _run_id, decls in guard_decls.items():
        for decl in decls:
            gid = decl.get("grader_id")
            if gid and decl.get("legacy"):
                legacy_guard_ids.add(gid)
            ids = tuple(
                entry.get("id")
                for entry in (decl.get("metrics") or ())
                if isinstance(entry, dict) and entry.get("id")
            )
            if gid and ids and gid not in guard_declared:
                guard_declared[gid] = ids
    if any(baseline_ids.values()) or any(guard_decls.values()):
        for trial in trials:
            if trial not in valid:
                continue
            run_id = trial.coordinates.run_id
            baseline_id = baseline_ids.get(run_id)
            if baseline_id:
                result = next(
                    (g for g in trial.grades if g.grader_id == baseline_id), None
                )
                if result is not None:
                    bucket = baseline_tasks.setdefault(
                        trial.coordinates.task_id,
                        {"pass": 0, "fail": 0, "cannot_judge": 0, "trials": 0},
                    )
                    bucket["trials"] += 1
                    bucket[result.status] = bucket.get(result.status, 0) + 1
                    label = f"{result.grader_id}@{result.grader_version}"
                    if label not in baseline_labels:
                        baseline_labels.append(label)
            for guard in guard_decls.get(run_id, ()):
                guard_id = guard.get("grader_id")
                if not guard_id:
                    continue
                result = next(
                    (g for g in trial.grades if g.grader_id == guard_id), None
                )
                if result is None:
                    continue
                label = f"{result.grader_id}@{result.grader_version}"
                guard_label_of.setdefault(guard_id, label)
                guard_trials[guard_id] = guard_trials.get(guard_id, 0) + 1
                for metric in result.metrics or []:
                    mid = getattr(metric, "name", None) or "?"
                    statuses = guard_rows.setdefault(guard_id, {}).setdefault(mid, {})
                    reasons_map = guard_reasons.setdefault(guard_id, {}).setdefault(mid, {})
                    _metric_counts_update(statuses, metric, reasons_map)
        def _guard_metric_ids(guard_id: str) -> list[str]:
            declared = list(guard_declared.get(guard_id, ()))
            produced = guard_rows.get(guard_id, {})
            extra = [mid for mid in sorted(produced) if mid not in set(declared)]
            return declared + extra

        guards = tuple(
            GuardMetricRow(
                grader_label=guard_label_of[guard_id],
                trials=guard_trials.get(guard_id, 0),
                declared=guard_declared.get(guard_id, ()),
                legacy=guard_id in legacy_guard_ids,
                metrics=tuple(
                    TaskMetricItem(
                        metric_id=mid,
                        counts=_counts_of(
                            guard_rows.get(guard_id, {}).get(mid, {}),
                            guard_reasons.get(guard_id, {}).get(mid, {}),
                        ),
                    )
                    for mid in _guard_metric_ids(guard_id)
                ),
            )
            for guard_id in sorted(guard_rows)
        )
        task_rows = tuple(
            BaselineTaskRow(
                task_id=task_id,
                title=summary.task_titles.get(task_id),
                trials=bucket["trials"],
                passes=bucket["pass"],
                fails=bucket["fail"],
                cannot_judge=bucket["cannot_judge"],
            )
            for task_id, bucket in sorted(baseline_tasks.items())
        )
        baseline_detail = BaselineGuardDetail(
            baseline_labels=tuple(baseline_labels),
            tasks=task_rows,
            guards=guards,
            legacy=bool(guards) and all(guard.legacy for guard in guards),
            divergent_tasks=baseline_divergent_rows(summary, task_rows),
        )

    detail_layer = FamilyDetail(
        families=tuple(family_blocks),
        baseline_guards=baseline_detail,
    )
    return (
        tuple(rollups),
        detail_layer,
        {task: tuple(keys) for task, keys in task_families.items()},
    )


def _num(value: float | None) -> str:
    """报告数字：None → —（无数据不编造），否则四位小数。"""
    return "—" if value is None else f"{value:.4f}"


def render_static_report(
    summary: RunSummary,
    comparison: ComparabilityReport | None = None,
) -> str:
    # watermark 反向依赖本模块（rollup/RunSummary），函数内导入避免环。
    from aeval.metrics.watermark import score_watermark

    comparison = comparison or summary.comparability
    lines = [
        "# aeval 运行报告",
        "",
        f"- 运行：{', '.join(summary.run_ids)}",
        f"- 试次：共 {summary.total_trials}，有效分母 {summary.valid_trials}",
        f"- 判定分布：{_zh_counts(summary.verdict_counts, _VERDICT_ZH)}",
        f"- 通过 {summary.passes} · 失败 {summary.fails}",
    ]
    if summary.k is not None:
        if summary.pass_at_k is not None:
            lines.append(f"- pass@{summary.k}：{summary.pass_at_k:.4f}")
        if summary.pass_pow_k_value is not None:
            lines.append(f"- pass^{summary.k}：{summary.pass_pow_k_value:.4f}")
    if summary.cost_per_pass is not None:
        lines.append(f"- 单次通过成本：{summary.cost_per_pass:.1f} tokens")
    else:
        lines.append("- 单次通过成本：不可用（无可用的成本证据）")
    if summary.exclusion is not None:
        lines.append(
            f"- 排除明细：{_zh_counts(summary.exclusion.excluded, _EXCLUSION_ZH)}"
        )
        rate = summary.exclusion.exclusion_rate
        flag = "（超标）" if summary.exclusion_rate_flagged else ""
        lines.append(f"- 排除率：{rate:.1%}{flag}")
        if summary.exclusion_rate_flagged:
            lines.append(
                "  警告：排除率超过 "
                f"{EXCLUSION_RATE_LIMIT:.0%}——本次运行的分数不可单独"
                "作为可信信号"
            )
    if summary.aggregation is not None and len(summary.aggregation.run_ids) > 1:
        agg = summary.aggregation
        if agg.comparable:
            lines.append(
                f"- 聚合：{len(agg.run_ids)} run 同套件 "
                f"（{agg.labels[0]}）且全部可比——"
                "同键族合并，数字跨 run 叠加"
            )
        else:
            detail = "；".join(
                f"{label} ← {'、'.join(runs)}"
                for label, runs in zip(agg.labels, agg.groups)
            )
            lines.append(
                f"- 聚合：{len(agg.run_ids)} run 同套件、{len(agg.groups)} 组条件"
                f"（组内可比才合并同键族，跨组各自成行）：{detail}"
            )
            for item in agg.blocked:
                lines.append(f"  - 未合并：{item}")
    if comparison is not None:
        if comparison.comparable:
            lines.append("- 可比性：可比")
        else:
            lines.append(
                f"- 可比性：不可比——{comparison.first_difference()}"
            )
    # 维度达标评分机制：阈值/权重/大块/红线
    # 来自清单封存的维度模型；值为类别聚合的通过率，这里不重新数任何
    # 试次。未声明维度模型的套件不渲染本层（向后兼容）。
    watermark = score_watermark(summary)
    if watermark is not None:
        lines.append(f"- 综合评分：{_num(watermark.composite)}")
        if watermark.redline_tripped:
            lines.append(
                "- 红线状态：告警（"
                + "、".join(watermark.redline_offenders) + "）"
            )
        else:
            lines.append("- 红线状态：全通过")
    # 轨迹采集（密封轨迹的事实汇总）：只有 CLI 采集了数据才渲染；
    # 数字全部来自 TrajectoryStats，这里只做格式化，不重新计算。
    traj = summary.trajectory
    if traj is not None:
        lines.append("")
        lines.append("## 轨迹采集")
        lines.append("")
        coverage = (
            f"- 证据覆盖：{traj.trials_with_evidence}/{traj.trials_total}"
            " 个试次"
        )
        if traj.unavailable:
            coverage += "（不可用：" + "、".join(traj.unavailable) + "）"
        lines.append(coverage)
        if traj.total_tokens is not None:
            cached = (
                f" · 缓存 {traj.total_cached_tokens:,}"
                if traj.total_cached_tokens is not None
                else ""
            )
            lines.append(
                f"- 总 Token：{traj.total_tokens:,}"
                f"（输入 {traj.total_prompt_tokens:,}"
                f" · 输出 {traj.total_completion_tokens:,}{cached}）"
            )
        else:
            lines.append("- 总 Token：不可用（证据无 token 计量）")
        if traj.mean_wall_seconds is not None:
            wall_line = f"- 时长：平均 {traj.mean_wall_seconds:.1f}s"
            if traj.longest_wall is not None:
                wall_line += (
                    f" · 最长 {traj.longest_wall[1]:.1f}s"
                    f"（{traj.longest_wall[0]}）"
                )
            lines.append(wall_line)
        lines.append(
            f"- 工具调用：{traj.tool_calls} 次"
            f" · 无观测 {traj.tool_missing_obs}"
            + (
                f"（{traj.tool_missing_obs / traj.tool_calls:.1%}）"
                if traj.tool_calls
                else ""
            )
            + f" · 重试 {traj.tool_retries}"
        )
        if traj.peak_input_tokens is not None:
            lines.append(
                f"- 上下文峰值：单步最大输入 "
                f"{traj.peak_input_tokens:,} tokens"
                f"（{traj.peak_trial_id} · step {traj.peak_step_id}）"
            )
        if traj.longest_call is not None:
            tool, seconds, trial_id = traj.longest_call
            lines.append(
                f"- 最长调用：{tool} {seconds:.1f}s（{trial_id}）"
            )
        if traj.categories:
            lines.append("")
            lines.append("| 类别 | 试次 | Token | 平均时长(s) | 工具调用 | 无观测 |")
            lines.append("|---|---|---|---|---|---|")
            for cat in traj.categories:
                tokens = (
                    f"{cat.total_tokens:,}"
                    if cat.total_tokens is not None
                    else "—"
                )
                mean_wall = (
                    f"{cat.mean_wall_seconds:.1f}"
                    if cat.mean_wall_seconds is not None
                    else "—"
                )
                lines.append(
                    f"| {cat.display}({cat.key}) | {cat.trials} "
                    f"| {tokens} | {mean_wall} | {cat.tool_calls} "
                    f"| {cat.missing_obs} |"
                )
    # 按任务表：技能/维度切口（只在有信息量时渲染——单任务 run 直接
    # 读上面的行）。族是唯一的归组来源（v5）；没有族的套件平铺，
    # 不再按 task_id 前缀猜类别。
    if summary.task_groups and len(summary.task_groups) > 1:
        families = summary.family_rollups
        if families:
            if watermark is not None:
                band_zh = {"green": "绿", "yellow": "黄", "red": "红",
                           "gray": "灰"}
                lines.append("")
                lines.append("## 维度达标")
                lines.append("")
                lines.append(
                    "| 维度 | 大块 | 权重 | 通过率 | 阈值 | 达标度 | 状态 |"
                )
                lines.append("|---|---|---|---|---|---|---|")
                for dim in watermark.dimensions:
                    mark = "（红线）" if dim.redline else ""
                    lines.append(
                        f"| {dim.display}({dim.key}){mark} "
                        f"| {dim.block_display} | {dim.weight:g} "
                        f"| {_num(dim.value)} | {dim.threshold:g} "
                        f"| {_num(dim.achievement)} | {band_zh[dim.band]} |"
                    )
            lines.append("")
            lines.append("## 按类别结果")
            lines.append("")
            if families:
                # 族行（D7）：族自身 GradeResult 判定聚合；分母 = 族实
                # 际出判定的有效试次；cannot_judge 出分母、入排除列。
                # 「判定分工」= 本族判 fail 的试次 / 本族判 pass 但终判
                # fail（失败由别的层决定）——让族行不再被读成终判的复述。
                lines.append(
                    "| 类别（指标族） | 任务数 | 试次 | 有效 | 通过 | 失败 "
                    "| 无法判定(排除) | 判定分工 | pass^k | 引擎 |"
                )
                lines.append("|---|---|---|---|---|---|---|---|---|---|")
                for family in families:
                    pk = family_pass_pow_k(family, summary.k)
                    pk_text = f"{pk:.4f}" if pk is not None else "-"
                    cj_text = str(family.cannot_judge) if family.cannot_judge else "-"
                    lines.append(
                        f"| {family.display}({family.key}) | {family.tasks} "
                        f"| {family.trials} | {family.valid} | {family.passes} "
                        f"| {family.fails} | {cj_text} "
                        f"| 本族 {family.deciding_fails} / 他层 {family.foreign_fails} "
                        f"| {pk_text} "
                        f"| {'、'.join(family.engines)} |"
                    )
                if any(
                    f.deciding_fails or f.foreign_fails for f in families
                ):
                    lines.append(
                        "- 判定分工：**本族** = 本族引擎判 fail（终判即 fail，"
                        "由本层决定）；**他层** = 本族判 pass 但终判是 fail"
                        "（失败由基线/其他族/守卫决定）。"
                    )
                for family in families:
                    if family.cannot_judge_reasons:
                        lines.append(
                            f"- {family.display}({family.key}) 排除原因："
                            + "；".join(family.cannot_judge_reasons)
                        )
            sources = summary.verdict_sources
            if families and sources:
                # 终判归属：谁把失败判出来的。没有这一节，族行与基线行
                # 数字相近时读者只能得出"族是基线复述"的错误结论。
                lines.append("")
                lines.append("## 终判来源分解")
                lines.append("")
                lines.append(
                    "折叠序：任一 veto fail → fail；否则任一 fail → fail；"
                    "否则任一 cannot_judge → cannot_judge；否则 pass。"
                )
                lines.append("")
                lines.append(
                    "| 失败试次 | 任务 | 决定性判分器 | 角色 | 机制 | 对照 |"
                )
                lines.append("|---|---|---|---|---|---|")
                for source in sources:
                    task = f"{source.task_id} #{source.trial_index}"
                    if summary.task_titles.get(source.task_id):
                        task = (
                            f"{summary.task_titles[source.task_id]}"
                            f"({source.task_id}) #{source.trial_index}"
                        )
                    lines.append(
                        f"| {source.trial_id} | {task} "
                        f"| {'、'.join(source.deciders)} "
                        f"| {'、'.join(source.roles)} | {source.mechanism} "
                        f"| {source.note or '—'} |"
                    )
                baseline_n = sum(
                    1 for s in sources if "官方基线" in s.roles
                )
                veto_n = sum(1 for s in sources if s.mechanism == "veto")
                family_n = sum(
                    1
                    for s in sources
                    if s.mechanism != "veto" and any(r.startswith("族 ") for r in s.roles)
                )
                guard_n = sum(
                    1
                    for s in sources
                    if s.mechanism != "veto" and "守卫" in s.roles
                )
                parts = [f"官方基线 {baseline_n}"]
                if family_n:
                    parts.append(f"族 {family_n}")
                if guard_n:
                    parts.append(f"守卫 {guard_n}")
                if veto_n:
                    veto_parts = []
                    family_veto = sum(
                        1
                        for s in sources
                        if s.mechanism == "veto"
                        and any(r.startswith("族 ") for r in s.roles)
                    )
                    guard_veto = sum(
                        1
                        for s in sources
                        if s.mechanism == "veto" and "守卫" in s.roles
                    )
                    if family_veto:
                        veto_parts.append(f"族 {family_veto}")
                    if guard_veto:
                        veto_parts.append(f"守卫 {guard_veto}")
                    if not veto_parts:
                        # v4 封存 run：veto 来自轨迹层判分器（归属声明不存在）。
                        trajectory_veto = sum(
                            1
                            for s in sources
                            if s.mechanism == "veto"
                            and "轨迹层判分器" in s.roles
                        )
                        if trajectory_veto:
                            veto_parts.append(f"轨迹层 {trajectory_veto}")
                    parts.append(
                        f"veto {veto_n}（{'·'.join(veto_parts) or '—'}）"
                    )
                lines.append("")
                lines.append(
                    f"- 失败 {len(sources)} = " + " · ".join(parts)
                    + "（层计数：一个试次可被多层判 fail，veto 层优先）"
                )
            lines.append("")
            lines.append("## 终判结果（按类别分组）")
        else:
            lines.append("")
            lines.append("## 终判结果")
        lines.append("")
        if families:
            lines.append("| 任务 | 所属族 | 试次 | 有效 | 通过 | 失败 | pass^k |")
            lines.append("|---|---|---|---|---|---|---|")
        else:
            lines.append("| 任务 | 试次 | 有效 | 通过 | 失败 | pass^k |")
            lines.append("|---|---|---|---|---|---|")

        def _task_row(task_id: str, indent: bool) -> str:
            group = summary.task_groups[task_id]
            pk = (
                f"{group.pass_pow_k_value:.4f}"
                if group.pass_pow_k_value is not None
                else "-"
            )
            # 任务名与判定枚举同法：中文显示名 + 括号内原 task_id（坐标
            # 是可追溯的钥匙，不能只剩翻译）。
            label = (
                f"{summary.task_titles[task_id]}({task_id})"
                if task_id in summary.task_titles
                else task_id
            )
            if indent:
                label = "· " + label
            numbers = (
                f"| {group.total_trials} | {group.valid_trials} "
                f"| {group.passes} | {group.fails} | {pk} |"
            )
            if families:
                keys = summary.task_families.get(task_id) or ()
                family_names = (
                    "、".join(
                        f"{summary.families[k]['name']}({k})"
                        for k in keys
                    )
                    if keys
                    else "—（仅基线与守卫）"
                )
                return f"| {label} | {family_names} {numbers}"
            return f"| {label} {numbers}"

        if families:
            grouped: dict[tuple[str, ...], list[str]] = {}
            for task_id in sorted(summary.task_groups):
                grouped.setdefault(
                    summary.task_families.get(task_id) or (), []
                ).append(task_id)
            for keys in sorted(grouped, key=lambda ks: (len(ks), ks)):
                if keys:
                    header = "、".join(
                        f"{summary.families[k]['name']}({k})" for k in keys
                    )
                    lines.append(f"| **{header}** | | | | | | | |")
                else:
                    lines.append("| **未入族（基线与守卫）** | | | | | | | |")
                for task_id in grouped[keys]:
                    lines.append(_task_row(task_id, indent=True))
        else:
            for task_id in sorted(summary.task_groups):
                lines.append(_task_row(task_id, indent=False))
    # 逐项指标：族 → 任务 → 指标；基线与守卫明细单列。跑不
    # 了的指标必须带原因可见——这正是「25 项指标在报告里没有落点」
    # 的修复本体。
    detail = summary.metric_detail
    # 「终判结果」只在多任务 run 渲染（单任务直接读上面的行）——基线表该不该
    # 省，取决于终判那张表到底在不在。
    final_table_rendered = bool(summary.task_groups) and len(summary.task_groups) > 1
    if metric_detail_has_content(detail, final_table_rendered=final_table_rendered):
        legacy_detail = (
            detail.baseline_guards is not None and detail.baseline_guards.legacy
        )
        lines.append("")
        lines.append("## 逐项指标（按判分层）" if legacy_detail
                     else "## 逐项指标（按族 / 任务）")
        lines.append("")
        for block in detail.families:
            if not block.declared:
                # 归组族（metrics: []）没有自己的指标——规则由基线/守卫给。
                # 空表不提（行数字仍有落点，见「按类别结果」）。
                continue
            engines = "、".join(block.engine_labels) or "—"
            lines.append(
                f"### {block.display}({block.key}) · 引擎 {engines} · "
                f"适用任务 {len(block.tasks)} · 声明指标 {len(block.declared)} 项"
            )
            lines.append("")
            family_totals: dict[str, list[int]] = {}
            for case in block.tasks:
                for item in case.metrics:
                    bucket = family_totals.setdefault(item.metric_id, [0, 0, 0, 0])
                    bucket[0] += item.counts.ok
                    bucket[1] += item.counts.degraded
                    bucket[2] += item.counts.violated
                    bucket[3] += item.counts.skipped
            # 声明清单不再单独列一遍：下面的指标表就按声明顺序列同样的 id
            # （外加运行期多出的项和 0 计数的声明项），抄第二遍只是重复。
            lines.append("| 指标（按声明顺序） | 判定 ok | 降级 | 违规 | 跳过 | 跳过原因 |")
            lines.append("|---|---|---|---|---|---|")
            declared_order = list(block.declared)
            extra = [m for m in sorted(family_totals) if m not in set(declared_order)]
            for metric_id in declared_order + extra:
                ok, degraded, violated, skipped = family_totals.get(
                    metric_id, [0, 0, 0, 0]
                )
                reasons: list[str] = []
                for case in block.tasks:
                    for item in case.metrics:
                        if item.metric_id == metric_id and item.counts.skipped_reasons:
                            reasons.extend(item.counts.skipped_reasons)
                deduped: list[str] = []
                for reason in reasons:
                    if reason not in deduped:
                        deduped.append(reason)
                reason_text = "；".join(deduped[:3]) if deduped else "—"
                if len(deduped) > 3:
                    reason_text += f"（另 {len(deduped) - 3} 项）"
                if metric_id in declared_order and metric_id not in family_totals:
                    reason_text = "本 run 未产出判定（声明项）"
                if metric_id in extra:
                    reason_text = "未在族声明里（运行期多出，核对声明）"
                lines.append(
                    f"| {metric_display(metric_id)} | {ok} | {degraded} "
                    f"| {violated} | {skipped} | {reason_text} |"
                )
            lines.append("")
            for case in block.tasks:
                title = f"「{case.title}」" if case.title else ""
                judged, skipped_items = task_metric_split(case, block.declared)
                parts = [
                    f"{metric_display(item.metric_id)}"
                    f"（{metric_counts_text(item)}）"
                    for item in judged
                ]
                summary_line = " · ".join(parts) if parts else "无有效判定（全部跳过）"
                tail = f"；跳过 {skipped_items} 项" if skipped_items else ""
                lines.append(
                    f"- {case.task_id} [{case.role}] {title}"
                    f"（{case.trials} 试）：{summary_line}{tail}"
                )
            lines.append("")
        baseline_guards = detail.baseline_guards
        if baseline_guards is not None:
            if baseline_guards.legacy:
                lines.append("### 基线与轨迹层判分器（v4 封存模型：声明未记录族/守卫归属）")
                lines.append("")
                lines.append(
                    "- 本 run 封存于 v5 指标模型之前（dimension_model 只有 "
                    "categories/blocks/redline_tasks，没有 metric_catalog）。"
                    "下面按判分层列出 store 里**实际产出判定**的判分器：轨迹层"
                    "判分器在今天的模型里对应 classification: gate，但归属以当时"
                    "的声明为准，本报告不代它断言。"
                )
            else:
                lines.append("### 基线与守卫（官方评分 + 有效性门，无类别行）")
            lines.append("")
            labels = "、".join(baseline_guards.baseline_labels) or "—"
            lines.append(f"- 官方基线：{labels}")
            divergent = baseline_rows_to_render(
                baseline_guards, final_table_rendered=final_table_rendered
            )
            if divergent:
                same = len(baseline_guards.tasks) - len(divergent)
                scope = (
                    f"只列**基线层与「终判结果」不同值**的 {len(divergent)} 行"
                    + (f"（其余 {same} 行同值，不复列）" if same else "（全部）")
                )
                lines.append(
                    f"- 下表{scope}：这些行就是被守卫 veto / 族阈值改判的行"
                    "（归因见「终判来源分解」）；无法判定 = 有效 − 通过 − 失败。"
                )
                lines.append("")
                lines.append("| 任务 | 试次 | 通过 | 失败 | 无法判定 |")
                lines.append("|---|---|---|---|---|")
                for row in divergent:
                    label = (
                        f"{row.title}({row.task_id})" if row.title else row.task_id
                    )
                    lines.append(
                        f"| {label} | {row.trials} | {row.passes} "
                        f"| {row.fails} | {row.cannot_judge} |"
                    )
            elif baseline_guards.tasks:
                # 与「终判结果」逐行同值：不复列（重复的表只会让人怀疑哪张才准）。
                lines.append(
                    "- 逐任务计数与「终判结果」逐行相同（同任务、同试次、"
                    "同通过/失败），故不复列；无法判定 = 有效 − 通过 − 失败。"
                )
            for guard in baseline_guards.guards:
                lines.append("")
                declared_note = (
                    f" · 声明清单 {len(guard.declared)} 项 · 读密封 ATIF 轨迹"
                    if guard.declared
                    else ""
                )
                role = guard_role(guard, baseline_guards.legacy)
                lines.append(
                    f"- {role} {guard.grader_label}（{guard.trials} 试"
                    f"{declared_note}）"
                )
                if guard.metrics:
                    lines.append("")
                    lines.append(
                        "| 指标 | 判定 ok | 降级 | 违规 | 跳过 | 跳过原因 |"
                    )
                    lines.append("|---|---|---|---|---|---|")
                    for (
                        metric_id,
                        ok,
                        degraded,
                        violated,
                        skipped,
                        reason_text,
                    ) in guard_metric_rows(guard):
                        lines.append(
                            f"| {metric_id} | {ok} "
                            f"| {degraded} | {violated} "
                            f"| {skipped} | {reason_text} |"
                        )
    else:
        # 没有指标级判定也要把话说清楚：这一节曾经静默消失，读者的第一反应
        # 就是"指标去哪了"（sbench 型 outcome-only 契约）。
        absence = metric_detail_absence_note(
            detail, final_table_rendered=final_table_rendered
        )
        if absence:
            lines.append("")
            lines.append("## 逐项指标（本 run 无指标级判定）")
            lines.append("")
            for line in absence:
                lines.append(f"- {line}")
    lines.append("")
    lines.append(
        "每个分数都可追溯到密封证据、判分器版本、有效分母与运行时锁；"
        "详见 run_manifest.json。"
    )
    return "\n".join(lines)


def export_jsonl(trials: Iterable[TrialRecord]) -> Iterator[str]:
    """JSONL export: one record per line, session content excluded.

    The export carries ids, verdicts, stop reasons, requirement
    bitmaps, grader results and artifact refs — everything needed to
    audit a score, nothing that leaks prompt/response bodies.
    """
    for record in trials:
        payload = {
            "trial_id": record.trial_id,
            "coordinates": record.coordinates.model_dump(),
            "stop_reason": record.stop_reason,
            "baseline_ok": record.baseline_ok,
            "requirements": record.requirements.to_dict(),
            "verdict": record.verdict,
            "observed_model": (
                record.observed_model.model_dump(exclude_none=True)
                if record.observed_model
                else None
            ),
            "claim": (
                record.claim.model_dump(exclude_none=True) if record.claim else None
            ),
            "grades": [
                g.model_dump(mode="json", exclude_none=True) for g in record.grades
            ],
            "artifacts": {
                k: v.model_dump() for k, v in record.artifacts.items()
            },
            "versions": (
                record.versions.model_dump(exclude_none=True)
                if record.versions
                else None
            ),
        }
        yield json.dumps(payload, ensure_ascii=False, sort_keys=True)
