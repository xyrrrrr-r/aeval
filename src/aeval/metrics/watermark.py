"""维度达标评分（能力水位）——维度模型评分机制。

输入只有两样：``RunSummary``（判分链已验证的数字）和清单封存的维度
模型（阈值/权重/大块/红线）。这里不重新数任何试次——维度值就是类别
聚合的通过率，达标度 = 值 / 阈值，大块与综合评分是权重加权均分，
红线是低于阈值的红线维度/任务集合。判定语义（verdict、分母、排除）
完全不经过本模块。

状态带（达标度分层）：达标度 ≥ 1 绿；≥ 0.9 黄；< 0.9 红；
无有效试次灰（未评测不得着色）。
"""

from __future__ import annotations

from dataclasses import dataclass

from aeval.metrics.report import (
    FamilyRollup,
    RunSummary,
)

__all__ = [
    "DimensionScore",
    "BlockScore",
    "Watermark",
    "score_watermark",
    "BOTTOM_K",
]

BOTTOM_K = 5  # 短板摘要取达标度最低的前 K 个维度

_BANDS = (("green", 1.0), ("yellow", 0.9))  # (band, 下限达标度)


@dataclass(frozen=True)
class DimensionScore:
    """一个维度（类别或任务级红线伪维度）的达标评分。"""

    key: str
    display: str
    block: str
    block_display: str
    weight: float
    threshold: float
    redline: bool
    value: float | None          # 通过率（有效试次）；None = 无数据
    achievement: float | None    # 达标度 = value / threshold
    band: str                    # green | yellow | red | gray
    tasks: int
    valid: int
    passes: int
    failing_tasks: tuple[str, ...]  # 有失败试次的任务 id（已排序）


@dataclass(frozen=True)
class BlockScore:
    """一个大块的加权均分（雷达图的一根轴）。"""

    key: str
    display: str
    score: float | None          # 成员维度的加权均分；无数据为 None
    members: tuple[str, ...]     # 成员维度 key（已排序）


@dataclass(frozen=True)
class Watermark:
    composite: float | None      # 综合评分：全部维度的加权均分
    overall_pass_rate: float | None
    redline_tripped: bool
    redline_offenders: tuple[str, ...]  # 触发红线的维度 display（已排序）
    dimensions: tuple[DimensionScore, ...]
    blocks: tuple[BlockScore, ...]
    bottom: tuple[DimensionScore, ...]  # 达标度最低的前 K（无数据不计）


def _band(achievement: float | None) -> str:
    if achievement is None:
        return "gray"
    for band, floor in _BANDS:
        if achievement >= floor:
            return band
    return "red"


def _block_display(summary: RunSummary, block: str) -> str:
    blocks = summary.dimension_model.get("blocks", {})
    if block in blocks:
        return blocks[block]
    return {"redline": "红线", "other": "其他"}.get(block, block)


def _weighted_mean(
    dimensions: list[DimensionScore],
) -> float | None:
    scored = [d for d in dimensions if d.value is not None]
    if not scored:
        return None
    total_weight = sum(d.weight for d in scored)
    return sum(d.weight * d.value for d in scored) / total_weight


def _dimension_from_family(
    summary: RunSummary, family: FamilyRollup
) -> DimensionScore:
    """族维度的达标评分（v4：族是维度，规格来自族声明）。

    值 = 族行通过率（族引擎判定的 passes/valid）；红线族是真实行
    （不再是 legacy 的任务级伪维度）。
    """
    spec = family.spec or {}
    threshold = float(spec.get("threshold", 0.9))
    value = family.passes / family.valid if family.valid > 0 else None
    achievement = value / threshold if value is not None else None
    return DimensionScore(
        key=family.key,
        display=family.display,
        block=str(spec.get("block", "other")),
        block_display=_block_display(summary, str(spec.get("block", "other"))),
        weight=float(spec.get("weight", 1.0)),
        threshold=threshold,
        redline=bool(spec.get("redline", False)),
        value=value,
        achievement=achievement,
        band=_band(achievement),
        tasks=family.tasks,
        valid=family.valid,
        passes=family.passes,
        failing_tasks=family.failing_tasks,
    )


def score_watermark(summary: RunSummary) -> Watermark | None:
    """按维度模型给 RunSummary 打达标分；没有族的套件返回 None。

    维度 = 族（值 = 族行通过率，红线族是真实行）。族是唯一的维度
    来源（v5）——不再有"类别维度 + 任务级红线伪维度"的第二条路。
    """
    if not summary.families or summary.family_rollups is None:
        return None
    dimensions = [
        _dimension_from_family(summary, family)
        for family in summary.family_rollups
    ]
    return _watermark_of(summary, dimensions)


def _watermark_of(
    summary: RunSummary, dimensions: list[DimensionScore]
) -> Watermark:
    # 大块：成员维度的加权均分（声明顺序稳定输出）。
    block_keys: list[str] = []
    for dim in dimensions:
        if dim.block not in block_keys:
            block_keys.append(dim.block)
    declared = list(summary.dimension_model.get("blocks", {}))
    block_keys = [b for b in declared if b in block_keys] + [
        b for b in block_keys if b not in declared
    ]
    blocks = tuple(
        BlockScore(
            key=block,
            display=_block_display(summary, block),
            score=_weighted_mean([d for d in dimensions if d.block == block]),
            members=tuple(
                sorted(d.key for d in dimensions if d.block == block)
            ),
        )
        for block in block_keys
    )

    offenders = tuple(
        sorted(
            d.display
            for d in dimensions
            if d.redline
            and (d.value is None or (d.achievement is not None
                                     and d.achievement < 1.0))
        )
    )
    bottom = tuple(
        sorted(
            (d for d in dimensions if d.achievement is not None),
            key=lambda d: d.achievement,
        )[:BOTTOM_K]
    )
    valid_total = sum(
        g.valid_trials for g in summary.task_groups.values()
    )
    return Watermark(
        composite=_weighted_mean(dimensions),
        overall_pass_rate=(
            summary.passes / valid_total if valid_total > 0 else None
        ),
        redline_tripped=bool(offenders),
        redline_offenders=offenders,
        dimensions=tuple(dimensions),
        blocks=blocks,
        bottom=bottom,
    )
