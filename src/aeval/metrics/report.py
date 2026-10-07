"""Run aggregation and reporting (plan §6).

Every number in the report is traceable: evidence refs, versions, the
denominator, comparability, recompute level. Session content never
leaks into reports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence

from aeval.bundle.attestation import ComparabilityReport, compare_manifests
from aeval.contracts import ExclusionSummary, RunManifest, TrialRecord, Verdict
from aeval.metrics.reliability import (
    EXCLUSION_RATE_LIMIT,
    cost_per_pass,
    exclusion_summary,
    pass_pow_k,
    valid_trials,
)

__all__ = [
    "RunSummary",
    "TaskGroupSummary",
    "aggregate_run",
    "render_static_report",
    "export_jsonl",
]


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
    # 类别聚合声明（清单封存）：类别键 → 中文显示名 + 无点分前缀任务
    # 的默认类别。归组规则确定性：task_id 首个点前的前缀，无点归
    # default。两者皆空 = 套件未声明类别 → 明细表平铺（向后兼容）。
    category_names: dict[str, str] = field(default_factory=dict)
    default_category: str | None = None
    # per-task roll-up (integration P2): the skill/dimension cut. Only
    # meaningful when a run spans several tasks; empty otherwise.
    task_groups: dict[str, TaskGroupSummary] = field(default_factory=dict)


def aggregate_run(
    run_ids: Sequence[str],
    trials: Iterable[TrialRecord],
    *,
    k: int | None = None,
    manifests: Sequence[RunManifest] | None = None,
) -> RunSummary:
    trials = list(trials)
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

    # Per-task roll-up (integration P2): the skill/dimension cut of the
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

    # 中文任务显示名 + 类别聚合声明：各 run 清单里封存的内容（见
    # RunManifest.task_titles / category_names / default_category）。
    # 多 run 聚合时同名任务/类别以先到的 run 为准；这些都是显示层，
    # 不参与任何分母/分数计算。
    titles: dict[str, str] = {}
    names: dict[str, str] = {}
    default: str | None = None
    for manifest in manifests or ():
        for task_id, title in (manifest.task_titles or {}).items():
            titles.setdefault(task_id, title)
        for key, name in (manifest.category_names or {}).items():
            names.setdefault(key, name)
        if default is None:
            default = manifest.default_category
    summary.task_titles = titles
    summary.category_names = names
    summary.default_category = default

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
        summary.comparability = compare_manifests(manifests[0], manifests[1])
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


def _category_of(task_id: str, default: str | None) -> str:
    """确定性归组：task_id 首个点前的前缀；无点归 default（再无则
    未分类）。"""
    if "." in task_id:
        return task_id.split(".", 1)[0]
    return default or "uncategorized"


def _category_display(key: str, names: dict[str, str]) -> str:
    if key in names:
        return names[key]
    return "未分类" if key == "uncategorized" else key


def render_static_report(
    summary: RunSummary,
    comparison: ComparabilityReport | None = None,
) -> str:
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
    if comparison is not None:
        if comparison.comparable:
            lines.append("- 可比性：可比")
        else:
            lines.append(
                f"- 可比性：不可比——{comparison.first_difference()}"
            )
    # 按任务表：技能/维度切口（只在有信息量时渲染——单任务 run 直接
    # 读上面的行）。套件声明了类别（清单里有 category_names /
    # default_category）时按类别聚合：先给「按类别结果」的汇总表
    # （类别是源方案的天然切口），明细表再按类别分组、不再平铺。
    if summary.task_groups and len(summary.task_groups) > 1:
        grouped = bool(summary.category_names or summary.default_category)
        if grouped:
            rollup: dict[str, dict[str, int]] = {}
            for task_id, task_group in summary.task_groups.items():
                cat = _category_of(task_id, summary.default_category)
                bucket = rollup.setdefault(
                    cat,
                    {"tasks": 0, "trials": 0, "valid": 0, "passes": 0,
                     "fails": 0},
                )
                bucket["tasks"] += 1
                bucket["trials"] += task_group.total_trials
                bucket["valid"] += task_group.valid_trials
                bucket["passes"] += task_group.passes
                bucket["fails"] += task_group.fails
            lines.append("")
            lines.append("## 按类别结果")
            lines.append("")
            lines.append(
                "| 类别 | 任务数 | 试次 | 有效 | 通过 | 失败 | pass^k |"
            )
            lines.append("|---|---|---|---|---|---|---|")
            for cat in sorted(rollup):
                bucket = rollup[cat]
                # 类别级 pass^k：按类别内合并的有效试次做同一组合估计
                # （口径与任务级一致，不足 k 个有效试次不编造）。
                cat_pk = "-"
                if summary.k is not None and bucket["valid"] >= summary.k:
                    cat_pk = f"{pass_pow_k(bucket['passes'], bucket['valid'], summary.k):.4f}"
                display = _category_display(cat, summary.category_names)
                lines.append(
                    f"| {display}({cat}) | {bucket['tasks']} "
                    f"| {bucket['trials']} | {bucket['valid']} "
                    f"| {bucket['passes']} | {bucket['fails']} | {cat_pk} |"
                )
            lines.append("")
            lines.append("## 按任务结果（按类别分组）")
        else:
            lines.append("")
            lines.append("## 按任务结果")
        lines.append("")
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
            return (
                f"| {label} | {group.total_trials} | {group.valid_trials} "
                f"| {group.passes} | {group.fails} | {pk} |"
            )

        if grouped:
            for cat in sorted(rollup):
                display = _category_display(cat, summary.category_names)
                lines.append(f"| **{display}({cat})** | | | | | |")
                for task_id in sorted(summary.task_groups):
                    if _category_of(task_id, summary.default_category) == cat:
                        lines.append(_task_row(task_id, indent=True))
        else:
            for task_id in sorted(summary.task_groups):
                lines.append(_task_row(task_id, indent=False))
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
