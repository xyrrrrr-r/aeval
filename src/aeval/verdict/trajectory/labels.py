"""指标中文展示名 —— 报告 / 面板的唯一解析入口。

展示名定义在指标类上（``name_zh``，与 ``name`` / ``category`` 同处），
本模块只做合并与解析：id 与名字不会各自漂移。

**它不进密封声明**。翻译是显示层的事——改一个中文名不该改变任何 run 的
身份（封存摘要、可比性分组、覆盖核对都只认 id），所以名字表不进
``verdict.metric_catalog``，也就不参与 overlay 摘要。套件自造、没登记过
的 id 返回空串，调用方照旧显示 id 本身（不编名字）。
"""

from __future__ import annotations

from aeval.verdict.trajectory.metrics import METRIC_ZH as _RUNTIME_ZH
from aeval.verdict.trajectory.quality import METRIC_ZH as _QUALITY_ZH

__all__ = ["METRIC_ZH", "metric_zh"]

#: 内置指标库的 id → 中文展示名（运行时层 + 会话质量/输出安全层）。
METRIC_ZH: dict[str, str] = {**_RUNTIME_ZH, **_QUALITY_ZH}


def metric_zh(metric_id: str) -> str:
    """指标 id 的中文展示名；未登记返回空串（调用方照旧显示 id）。"""
    return METRIC_ZH.get(metric_id, "")
